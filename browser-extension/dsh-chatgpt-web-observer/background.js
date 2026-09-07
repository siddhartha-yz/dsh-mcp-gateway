(() => {
  'use strict'

  const api = globalThis.browser ?? globalThis.chrome
  const dshPorts = new Set()
  const hostId = crypto.randomUUID()
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

  function normalizeEvent(value) {
    if (!isPlainObject(value) || value.channel !== 'dsh-chatgpt-observer-event') return null
    if (typeof value.observerId !== 'string' || value.observerId.length < 1 || value.observerId.length > 128) return null
    if (!EVENT_TYPES.has(value.eventType)) return null
    if (value.text !== null && value.text !== undefined && (typeof value.text !== 'string' || value.text.length > MAX_TEXT)) return null
    if (!isPlainObject(value.payload)) return null
    if (typeof value.payload.conversationId !== 'string' || value.payload.conversationId.length > MAX_ID) return null
    if (value.payload.hostId !== undefined && (typeof value.payload.hostId !== 'string' || value.payload.hostId.length < 1 || value.payload.hostId.length > 128)) return null
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
    // Only this extension's own content scripts can open runtime ports here;
    // page scripts cannot call runtime.connect without externally_connectable.
    // The manifest already limits those scripts to chatgpt.com and local DSH :3080.
    if (port.name === 'dsh-gui-relay') {
      dshPorts.add(port)
      port.onDisconnect.addListener(() => dshPorts.delete(port))
      return
    }

    if (port.name !== 'chatgpt-observer') {
      try { port.disconnect() } catch {}
      return
    }

    try { port.postMessage({ channel: 'dsh-chatgpt-observer-host', hostId }) } catch {}
    port.onMessage.addListener((value) => {
      const event = normalizeEvent(value)
      if (event !== null) broadcast(event)
    })
  })
})()
