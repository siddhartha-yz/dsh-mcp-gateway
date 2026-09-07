(() => {
  'use strict'

  const api = globalThis.browser ?? globalThis.chrome

  // Firefox does not match WebExtension content-script patterns that include
  // an explicit port. The manifest therefore covers localhost broadly and this
  // runtime guard restores the intended fail-closed :3080 boundary.
  const localHost = location.hostname === '127.0.0.1' || location.hostname === 'localhost'
  if (location.protocol !== 'http:' || !localHost || location.port !== '3080') return

  // Do not depend on DSH's HTML containing an inline marker: the production Web
  // client loads bridge globals/modules dynamically.
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
