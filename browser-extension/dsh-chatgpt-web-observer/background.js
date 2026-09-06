(() => {
  'use strict'

  const api = globalThis.browser ?? globalThis.chrome
  const dshPorts = new Set()
  const CHATGPT_ORIGIN = 'https://chatgpt.com'
  const MAX_TEXT = 8_000
  const MAX_ID = 512
  const EVENT_TYPES = new Set([
    'observer_ready',
    'observer_heartbeat',
    'turn_started',
    'assistant_message',
    'turn_completed',
    'blocked',
    'bridge_degraded',
    'error',
  ])

  function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value)
  }

  function senderUrl(port) {
    return port?.sender?.url || port?.sender?.tab?.url || ''
  }

  function isChatGPTSender(port) {
    try {
      return new URL(senderUrl(port)).origin === CHATGPT_ORIGIN
    } catch {
      return false
    }
  }

  function isLocalDshSender(port) {
    try {
      const url = new URL(senderUrl(port))
      return url.protocol === 'http:' && url.port === '3080' && (url.hostname === '127.0.0.1' || url.hostname === 'localhost')
    } catch {
      return false
    }
  }

  function normalizeEvent(value) {
    if (!isPlainObject(value) || value.channel !== 'dsh-chatgpt-observer-event') return null
    if (typeof value.observerId !== 'string' || value.observerId.length < 1 || value.observerId.length > 128) return null
    if (!EVENT_TYPES.has(value.eventType)) return null
    if (value.text !== null && value.text !== undefined && (typeof value.text !== 'string' || value.text.length > MAX_TEXT)) return null
    if (!isPlainObject(value.payload)) return null
    if (typeof value.payload.conversationId !== 'string' || value.payload.conversationId.length > MAX_ID) return null
    return {
      observerId: value.observerId,
      eventType: value.eventType,
      text: value.text ?? null,
      payload: value.payload,
    }
  }

  function broadcast(event) {
    for (const port of [...dshPorts]) {
      try {
        port.postMessage({ channel: 'dsh-chatgpt-observer-relay', event })
      } catch {
        dshPorts.delete(port)
      }
    }
  }

  api.runtime.onConnect.addListener((port) => {
    if (port.name === 'dsh-gui-relay') {
      if (!isLocalDshSender(port)) {
        try { port.disconnect() } catch {}
        return
      }
      dshPorts.add(port)
      port.onDisconnect.addListener(() => dshPorts.delete(port))
      return
    }

    if (port.name !== 'chatgpt-observer' || !isChatGPTSender(port)) {
      try { port.disconnect() } catch {}
      return
    }

    port.onMessage.addListener((value) => {
      const event = normalizeEvent(value)
      if (event !== null) broadcast(event)
    })
  })
})()
