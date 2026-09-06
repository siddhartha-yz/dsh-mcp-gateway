window.__ModuleLoader__.load({
  id: '@siddhartha-yz/dsh-chatgpt-web-bridge',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')

    const inject = ['slots']
    const DEFAULT_MESSAGE = 'Continue the current task from the latest DSH task state. Do not stop merely to report progress; stop only when the task is complete or human input is genuinely required.'

    function bridgeConfig() {
      const config = globalThis.__DSH_CHATGPT_WEB_BRIDGE__
      if (config === null || typeof config !== 'object') throw new Error('ChatGPT bridge boot config is missing')
      if (typeof config.token !== 'string' || typeof config.basePath !== 'string') throw new Error('ChatGPT bridge boot config is invalid')
      return config
    }

    async function bridgeFetch(path, options = {}) {
      const config = bridgeConfig()
      const headers = new Headers(options.headers || {})
      headers.set('x-dsh-chatgpt-bridge-token', config.token)
      if (options.body !== undefined) headers.set('content-type', 'application/json')
      const response = await fetch(`${config.basePath}${path}`, { ...options, headers, cache: 'no-store' })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.message || data.error || `bridge request failed (${response.status})`)
      return data
    }

    function ageLabel(timestamp, now) {
      if (typeof timestamp !== 'number') return 'not connected'
      const seconds = Math.max(0, Math.floor((now - timestamp) / 1000))
      if (seconds < 2) return 'now'
      if (seconds < 60) return `${seconds}s ago`
      return `${Math.floor(seconds / 60)}m ago`
    }

    function eventLabel(event) {
      if (!event || typeof event !== 'object') return 'event'
      const source = event.source === 'observer' ? 'observer · ' : event.source === 'companion' ? 'companion · ' : ''
      return `${source}${String(event.type || 'event').replaceAll('_', ' ')}`
    }

    const OBSERVER_EVENT_TYPES = new Set(['observer_ready', 'observer_heartbeat', 'turn_started', 'assistant_message', 'turn_completed', 'blocked', 'bridge_degraded', 'error'])

    function normalizeObserverEvent(value) {
      if (value === null || typeof value !== 'object') return null
      if (typeof value.observerId !== 'string' || value.observerId.length < 1 || value.observerId.length > 128) return null
      if (!OBSERVER_EVENT_TYPES.has(value.eventType)) return null
      if (value.text !== null && value.text !== undefined && (typeof value.text !== 'string' || value.text.length > 8000)) return null
      if (value.payload === null || typeof value.payload !== 'object' || Array.isArray(value.payload)) return null
      if (typeof value.payload.conversationId !== 'string' || value.payload.conversationId.length > 512) return null
      return {
        observer_id: value.observerId,
        event_type: value.eventType,
        text: value.text ?? null,
        payload: value.payload,
      }
    }

    function BridgeFooterAction({ wide }) {
      const [open, setOpen] = React.useState(false)
      const [state, setState] = React.useState(null)
      const [error, setError] = React.useState(null)
      const [text, setText] = React.useState(DEFAULT_MESSAGE)
      const [sending, setSending] = React.useState(false)
      const [taskId, setTaskId] = React.useState('')
      const [controllerBusy, setControllerBusy] = React.useState(false)
      const [now, setNow] = React.useState(Date.now())

      const refresh = React.useCallback(async () => {
        try {
          const next = await bridgeFetch('/state')
          setState(next)
          setError(null)
          setNow(Date.now())
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      }, [])

      React.useEffect(() => {
        let live = true
        const run = async () => {
          if (!live) return
          await refresh()
        }
        run()
        const timer = window.setInterval(run, open ? 1500 : 5000)
        return () => {
          live = false
          window.clearInterval(timer)
        }
      }, [open, refresh])

      const companionAge = state?.companion?.lastSeenAt
      const companionOnline = typeof companionAge === 'number' && now - companionAge < 15_000
      const observerAge = state?.observer?.lastSeenAt
      const observerOnline = typeof observerAge === 'number' && now - observerAge < 15_000
      const observerDegraded = state?.observer?.lastEventType === 'bridge_degraded' || state?.observer?.lastEventType === 'error'
      const pending = Number(state?.counts?.pending || 0) + Number(state?.counts?.claimed || 0) + Number(state?.counts?.dispatching || 0)
      const controller = state?.controller
      const controllerEnabled = controller?.enabled === true

      const send = async () => {
        if (sending || text.trim() === '') return
        setSending(true)
        try {
          await bridgeFetch('/enqueue', { method: 'POST', body: JSON.stringify({ text }) })
          setError(null)
          await refresh()
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : String(reason))
        } finally {
          setSending(false)
        }
      }

      const configureController = async (enabled) => {
        if (controllerBusy) return
        setControllerBusy(true)
        try {
          await bridgeFetch('/controller', {
            method: 'POST',
            body: JSON.stringify(enabled ? { enabled: true, task_id: taskId.trim() } : { enabled: false }),
          })
          setError(null)
          await refresh()
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : String(reason))
        } finally {
          setControllerBusy(false)
        }
      }

      const button = React.createElement('button', {
        type: 'button',
        title: 'ChatGPT Web bridge',
        'aria-label': 'ChatGPT Web bridge',
        onClick: () => setOpen((value) => !value),
        style: {
          minWidth: wide ? 92 : 36,
          height: 36,
          border: '0.5px solid var(--dsw-alias-border-l3)',
          borderRadius: 10,
          background: open ? 'var(--dsw-alias-button-ghost-active-fill)' : 'transparent',
          color: 'var(--dsw-alias-label-primary)',
          cursor: 'pointer',
          padding: wide ? '0 10px' : 0,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 7,
          fontSize: 12,
          whiteSpace: 'nowrap',
        },
      },
      React.createElement('span', {
        'aria-hidden': true,
        style: {
          width: 8,
          height: 8,
          borderRadius: 99,
          background: companionOnline ? 'var(--dsw-alias-state-success-primary)' : pending > 0 ? 'var(--dsw-alias-state-warn-label)' : 'var(--dsw-alias-label-tertiary)',
          boxShadow: companionOnline ? '0 0 0 3px color-mix(in srgb, var(--dsw-alias-state-success-primary) 18%, transparent)' : 'none',
          flex: 'none',
        },
      }),
      wide ? React.createElement('span', null, pending > 0 ? `Bridge · ${pending}` : 'Bridge') : null)

      if (!open) return button

      const recentEvents = Array.isArray(state?.recentEvents) ? state.recentEvents.slice(-6).reverse() : []
      const panel = React.createElement('section', {
        role: 'dialog',
        'aria-label': 'ChatGPT Web bridge experiment',
        style: {
          position: 'fixed',
          zIndex: 10000,
          left: wide ? 16 : 68,
          bottom: 16,
          width: 'min(430px, calc(100vw - 32px))',
          maxHeight: 'min(620px, calc(100vh - 32px))',
          overflow: 'auto',
          boxSizing: 'border-box',
          padding: 16,
          border: '0.5px solid var(--dsw-alias-border-l3)',
          borderRadius: 16,
          background: 'var(--dsw-alias-bg-base)',
          color: 'var(--dsw-alias-label-primary)',
          boxShadow: '0 14px 50px rgba(0,0,0,.22)',
          fontSize: 13,
        },
      },
      React.createElement('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, marginBottom: 14 } },
        React.createElement('div', null,
          React.createElement('strong', { style: { display: 'block', fontSize: 15, marginBottom: 3 } }, 'ChatGPT Web Bridge'),
          React.createElement('span', { style: { color: 'var(--dsw-alias-label-secondary)', fontSize: 12 } }, 'B1 official send + B2 read observer + mechanical multi-turn controller')
        ),
        React.createElement('button', {
          type: 'button',
          onClick: () => setOpen(false),
          style: { border: 0, background: 'transparent', color: 'var(--dsw-alias-label-secondary)', cursor: 'pointer', fontSize: 18, lineHeight: 1 },
        }, '×')
      ),
      React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14 } },
        React.createElement('div', { style: { padding: 10, borderRadius: 10, background: 'var(--dsw-alias-button-ghost-active-fill)' } },
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-secondary)', fontSize: 11, marginBottom: 4 } }, 'Companion'),
          React.createElement('div', { style: { fontWeight: 600 } }, companionOnline ? 'Connected' : 'Waiting'),
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-tertiary)', fontSize: 11, marginTop: 3 } }, ageLabel(state?.companion?.lastSeenAt, now))
        ),
        React.createElement('div', { style: { padding: 10, borderRadius: 10, background: 'var(--dsw-alias-button-ghost-active-fill)' } },
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-secondary)', fontSize: 11, marginBottom: 4 } }, 'Outbound queue'),
          React.createElement('div', { style: { fontWeight: 600 } }, `${pending} active`),
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-tertiary)', fontSize: 11, marginTop: 3 } }, `sent ${Number(state?.counts?.sent || 0)} · failed ${Number(state?.counts?.failed || 0)}`)
        ),
        React.createElement('div', { style: { gridColumn: '1 / -1', padding: 10, borderRadius: 10, background: 'var(--dsw-alias-button-ghost-active-fill)' } },
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-secondary)', fontSize: 11, marginBottom: 4 } }, 'Read observer'),
          React.createElement('div', { style: { fontWeight: 600 } }, observerDegraded ? 'Degraded' : observerOnline ? 'Connected' : 'Waiting'),
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-tertiary)', fontSize: 11, marginTop: 3 } }, `${ageLabel(state?.observer?.lastSeenAt, now)}${state?.observer?.lastEventType ? ` · ${String(state.observer.lastEventType).replaceAll('_', ' ')}` : ''}`)
        ),
        React.createElement('div', { style: { gridColumn: '1 / -1', padding: 10, borderRadius: 10, background: 'var(--dsw-alias-button-ghost-active-fill)' } },
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-secondary)', fontSize: 11, marginBottom: 4 } }, 'Auto continue'),
          React.createElement('div', { style: { fontWeight: 600 } }, controllerEnabled ? 'Armed' : 'Stopped'),
          React.createElement('div', { style: { color: 'var(--dsw-alias-label-tertiary)', fontSize: 11, marginTop: 3 } }, controllerEnabled
            ? `${controller?.taskId || ''} · ${Number(controller?.continuationCount || 0)} continuations`
            : `${controller?.stopReason || controller?.lastDecision || 'disabled'}${controller?.continuationCount ? ` · ${controller.continuationCount} continuations` : ''}`)
        )
      ),
      React.createElement('label', { style: { display: 'block', color: 'var(--dsw-alias-label-secondary)', fontSize: 11, marginBottom: 5 } }, 'Task state ID for mechanical auto-continue'),
      React.createElement('input', {
        value: controllerEnabled ? (controller?.taskId || taskId) : taskId,
        disabled: controllerEnabled,
        onChange: (event) => setTaskId(event.target.value),
        placeholder: 'task_...',
        style: {
          width: '100%', boxSizing: 'border-box', padding: 9, marginBottom: 8,
          border: '0.5px solid var(--dsw-alias-border-l3)', borderRadius: 9,
          background: 'var(--dsw-alias-bg-elevated, var(--dsw-alias-bg-base))', color: 'var(--dsw-alias-label-primary)', font: 'inherit', outline: 'none',
        },
      }),
      React.createElement('button', {
        type: 'button',
        disabled: controllerBusy || (!controllerEnabled && taskId.trim() === ''),
        onClick: () => configureController(!controllerEnabled),
        style: {
          border: controllerEnabled ? '0.5px solid var(--dsw-alias-border-l3)' : 0,
          borderRadius: 9,
          background: controllerEnabled ? 'transparent' : 'var(--dsw-alias-button-primary-fill, var(--dsw-alias-label-primary))',
          color: controllerEnabled ? 'var(--dsw-alias-label-primary)' : 'var(--dsw-alias-button-primary-label, var(--dsw-alias-label-primary-inverted))',
          padding: '8px 12px', cursor: controllerBusy ? 'wait' : 'pointer', fontWeight: 600, marginBottom: 14,
        },
      }, controllerBusy ? 'Updating…' : controllerEnabled ? 'Stop auto-continue' : 'Arm auto-continue'),
      React.createElement('label', { style: { display: 'block', color: 'var(--dsw-alias-label-secondary)', fontSize: 11, marginBottom: 5 } }, 'Message to the hidden ChatGPT conversation'),
      React.createElement('textarea', {
        value: text,
        onChange: (event) => setText(event.target.value),
        rows: 5,
        style: {
          width: '100%',
          resize: 'vertical',
          boxSizing: 'border-box',
          padding: 10,
          border: '0.5px solid var(--dsw-alias-border-l3)',
          borderRadius: 10,
          background: 'var(--dsw-alias-bg-elevated, var(--dsw-alias-bg-base))',
          color: 'var(--dsw-alias-label-primary)',
          font: 'inherit',
          lineHeight: 1.45,
          outline: 'none',
        },
      }),
      React.createElement('div', { style: { display: 'flex', gap: 8, marginTop: 9 } },
        React.createElement('button', {
          type: 'button',
          disabled: sending || text.trim() === '',
          onClick: send,
          style: {
            border: 0,
            borderRadius: 9,
            background: 'var(--dsw-alias-button-primary-fill, var(--dsw-alias-label-primary))',
            color: 'var(--dsw-alias-button-primary-label, var(--dsw-alias-label-primary-inverted))',
            padding: '8px 12px',
            cursor: sending ? 'wait' : 'pointer',
            fontWeight: 600,
          },
        }, sending ? 'Queueing…' : 'Queue message'),
        React.createElement('button', {
          type: 'button',
          onClick: refresh,
          style: { border: '0.5px solid var(--dsw-alias-border-l3)', borderRadius: 9, background: 'transparent', color: 'var(--dsw-alias-label-primary)', padding: '8px 12px', cursor: 'pointer' },
        }, 'Refresh')
      ),
      error ? React.createElement('div', { style: { marginTop: 10, color: 'var(--dsw-alias-state-error-primary)', fontSize: 12 } }, error) : null,
      React.createElement('div', { style: { marginTop: 16, paddingTop: 12, borderTop: '0.5px solid var(--dsw-alias-border-l4)' } },
        React.createElement('div', { style: { color: 'var(--dsw-alias-label-secondary)', fontSize: 11, marginBottom: 7 } }, 'Recent companion events'),
        recentEvents.length === 0
          ? React.createElement('div', { style: { color: 'var(--dsw-alias-label-tertiary)', fontSize: 12 } }, 'No ChatGPT-side lifecycle events yet.')
          : recentEvents.map((event) => React.createElement('div', { key: event.seq, style: { padding: '5px 0', display: 'flex', justifyContent: 'space-between', gap: 12, fontSize: 12 } },
              React.createElement('span', null, eventLabel(event)),
              React.createElement('span', { style: { color: 'var(--dsw-alias-label-tertiary)' } }, ageLabel(event.ts, now))
            ))
      ))

      return React.createElement(React.Fragment, null, button, panel)
    }

    function apply(ctx) {
      ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
        name: 'sidebar.footer.action',
        id: 'chatgpt-web-bridge',
        order: 20,
      }, BridgeFooterAction))

      ctx.effect(() => {
        const onObserverMessage = (event) => {
          if (event.source !== window || event.origin !== location.origin) return
          const message = event.data
          if (message === null || typeof message !== 'object') return
          if (message.source !== 'dsh-chatgpt-web-observer-extension' || message.type !== 'observer-event') return
          const body = normalizeObserverEvent(message.event)
          if (body === null) return
          void bridgeFetch('/observer', { method: 'POST', body: JSON.stringify(body) }).catch((reason) => {
            console.warn('chatgpt-web-bridge: observer relay failed', reason)
          })
        }
        window.addEventListener('message', onObserverMessage)
        return () => window.removeEventListener('message', onObserverMessage)
      }, 'chatgpt-web-bridge.observer-relay')
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
