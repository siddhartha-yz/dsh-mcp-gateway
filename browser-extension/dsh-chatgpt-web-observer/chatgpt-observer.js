(() => {
  'use strict'

  const api = globalThis.browser ?? globalThis.chrome
  const observerId = crypto.randomUUID()
  const MAX_TEXT = 8_000
  const ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
  const STOP_SELECTORS = [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop"]',
    'button[aria-label="停止生成"]',
    'button[aria-label="停止"]',
  ]
  const RETRY_SELECTORS = [
    'button[data-testid="regenerate-button"]',
    'button[aria-label="Retry"]',
    'button[aria-label="Try again"]',
    'button[aria-label="重试"]',
  ]

  let port = null
  let hostId = null
  let reconnectTimer = null
  let domObserver = null
  let watchdog = null
  let lastPath = location.pathname
  let running = false
  let runningConversation = null
  let completionScheduled = false
  let lastCompletedTurnKey = null
  let initialLastTurnKey = null
  let lastHeartbeatAt = 0

  function conversationId() {
    return location.pathname.slice(0, 512)
  }

  function boundedText(value) {
    const normalized = String(value ?? '').trim()
    return normalized.length <= MAX_TEXT ? normalized : normalized.slice(-MAX_TEXT)
  }

  function emit(eventType, text = null, payload = {}) {
    if (port === null) return
    try {
      port.postMessage({
        channel: 'dsh-chatgpt-observer-event',
        observerId,
        eventType,
        text: text === null ? null : boundedText(text),
        payload: {
          ...payload,
          conversationId: conversationId(),
          ...(hostId === null ? {} : { hostId }),
          observedAt: Date.now(),
        },
      })
    } catch {
      // The disconnect handler reconnects. A dropped observation fails closed;
      // it never causes a message to be sent.
    }
  }


  function postHostMessage(targetWindow, message) {
    if (!targetWindow) return
    try {
      if (typeof cloneInto === 'function') {
        const pageMessage = cloneInto(message, targetWindow)
        targetWindow.postMessage(pageMessage, '*')
        return
      }
    } catch {
      // Firefox can expose cross-origin WindowProxy children while denying
      // direct realm wrappers. Fall through to the standard postMessage path.
    }
    try { targetWindow.postMessage(message, '*') } catch {}
  }

  function postHostToFrameTree(targetWindow, message, depth = 0) {
    if (!targetWindow || depth > 4) return
    postHostMessage(targetWindow, message)

    let childCount = 0
    try { childCount = Math.min(Number(targetWindow.length) || 0, 16) } catch {}
    for (let index = 0; index < childCount; index += 1) {
      try { postHostToFrameTree(targetWindow[index], message, depth + 1) } catch {}
    }
  }

  function announceHostToFrames() {
    if (hostId === null) return
    const message = { source: 'dsh-chatgpt-web-observer-extension', type: 'host-instance', hostId }
    for (const frame of document.querySelectorAll('iframe')) {
      postHostToFrameTree(frame.contentWindow, message)
    }
  }

  function connect() {
    if (port !== null) return
    try {
      port = api.runtime.connect({ name: 'chatgpt-observer' })
      port.onMessage.addListener((message) => {
        if (message?.channel !== 'dsh-chatgpt-observer-host') return
        if (typeof message.hostId !== 'string' || message.hostId.length < 1 || message.hostId.length > 128) return
        hostId = message.hostId
        announceHostToFrames()
        emit('observer_ready', null, { version: 2, transport: 'read-only-dom-observer', hostBound: true })
      })
      port.onDisconnect.addListener(() => {
        port = null
        if (reconnectTimer !== null) clearTimeout(reconnectTimer)
        reconnectTimer = setTimeout(connect, 1_000)
      })
      emit('observer_ready', null, {
        version: 1,
        transport: 'read-only-dom-observer',
        assistantSelector: ASSISTANT_SELECTOR,
      })
    } catch {
      port = null
      reconnectTimer = setTimeout(connect, 1_000)
    }
  }

  function assistantMessages() {
    return [...document.querySelectorAll(ASSISTANT_SELECTOR)]
  }

  function turnKey(node, index) {
    const article = node.closest('article[data-testid^="conversation-turn-"]')
    const testId = article?.getAttribute('data-testid')
    return testId || `assistant-${index}`
  }

  function latestAssistant() {
    const messages = assistantMessages()
    if (messages.length === 0) return null
    const node = messages[messages.length - 1]
    return {
      key: turnKey(node, messages.length - 1),
      text: boundedText(node.innerText || node.textContent || ''),
    }
  }

  function isGenerating() {
    return STOP_SELECTORS.some((selector) => document.querySelector(selector) !== null)
  }

  function hasRetrySignal() {
    return RETRY_SELECTORS.some((selector) => document.querySelector(selector) !== null)
  }

  function finishTurn() {
    completionScheduled = false
    if (!running || isGenerating()) return

    const latest = latestAssistant()
    if (latest === null || latest.text === '') {
      emit('bridge_degraded', 'Generation ended but no assistant message could be identified with the semantic selector.', {
        reason: 'assistant_message_missing_after_run',
        retryVisible: hasRetrySignal(),
      })
      running = false
      runningConversation = null
      return
    }

    if (latest.key === initialLastTurnKey && latest.key === lastCompletedTurnKey) {
      emit('bridge_degraded', 'Generation ended without a distinguishable new assistant turn.', {
        reason: 'turn_identity_not_advanced',
      })
      running = false
      runningConversation = null
      return
    }

    if (latest.key !== lastCompletedTurnKey) {
      emit('assistant_message', latest.text, { turnKey: latest.key })
      emit('turn_completed', null, {
        turnKey: latest.key,
        retryVisible: hasRetrySignal(),
      })
      lastCompletedTurnKey = latest.key
    }
    running = false
    runningConversation = null
  }

  function scheduleFinish() {
    if (completionScheduled) return
    completionScheduled = true
    setTimeout(() => {
      if (isGenerating()) {
        completionScheduled = false
        return
      }
      finishTurn()
    }, 700)
  }

  function inspect() {
    const now = Date.now()
    announceHostToFrames()
    if (now - lastHeartbeatAt >= 5_000) {
      lastHeartbeatAt = now
      emit('observer_heartbeat')
    }

    const currentPath = location.pathname
    if (currentPath !== lastPath) {
      lastPath = currentPath
      running = false
      runningConversation = null
      completionScheduled = false
      const latest = latestAssistant()
      initialLastTurnKey = latest?.key ?? null
      lastCompletedTurnKey = initialLastTurnKey
      emit('observer_ready', null, {
        version: 1,
        transport: 'read-only-dom-observer',
        navigation: true,
      })
    }

    const generating = isGenerating()
    if (generating && !running) {
      running = true
      runningConversation = conversationId()
      completionScheduled = false
      emit('turn_started', null, { baselineTurnKey: latestAssistant()?.key ?? null })
      return
    }

    if (!generating && running) {
      if (runningConversation !== conversationId()) {
        emit('bridge_degraded', 'Conversation identity changed while a turn was running.', {
          reason: 'conversation_changed_during_turn',
          previousConversationId: runningConversation,
        })
        running = false
        runningConversation = null
        completionScheduled = false
        return
      }
      scheduleFinish()
    }
  }

  function start() {
    const latest = latestAssistant()
    initialLastTurnKey = latest?.key ?? null
    lastCompletedTurnKey = initialLastTurnKey
    connect()

    domObserver = new MutationObserver(inspect)
    domObserver.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['aria-label', 'data-testid', 'data-message-author-role'],
    })
    watchdog = setInterval(inspect, 1_000)
    inspect()
  }

  start()

  addEventListener('pagehide', () => {
    if (domObserver !== null) domObserver.disconnect()
    if (watchdog !== null) clearInterval(watchdog)
    if (reconnectTimer !== null) clearTimeout(reconnectTimer)
    try { port?.disconnect() } catch {}
  }, { once: true })
})()
