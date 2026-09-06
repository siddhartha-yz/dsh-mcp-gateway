(() => {
  'use strict'

  const api = globalThis.browser ?? globalThis.chrome

  function isDshBridgePage() {
    for (const script of document.scripts) {
      if ((script.textContent || '').includes('__DSH_CHATGPT_WEB_BRIDGE__')) return true
    }
    return false
  }

  if (!isDshBridgePage()) return

  const port = api.runtime.connect({ name: 'dsh-gui-relay' })
  port.onMessage.addListener((value) => {
    if (value === null || typeof value !== 'object' || value.channel !== 'dsh-chatgpt-observer-relay') return
    const event = value.event
    if (event === null || typeof event !== 'object') return
    window.postMessage({
      source: 'dsh-chatgpt-web-observer-extension',
      type: 'observer-event',
      event,
    }, location.origin)
  })
})()
