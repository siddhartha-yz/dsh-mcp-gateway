(() => {
  'use strict'

  const api = globalThis.browser ?? globalThis.chrome

  // The manifest restricts this content script to localhost/127.0.0.1:3080,
  // and background.js independently validates the sender URL before accepting
  // the relay port. Do not depend on DSH's HTML containing an inline marker:
  // the production Web client loads bridge globals/modules dynamically.
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
