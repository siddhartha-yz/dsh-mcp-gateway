export const name = 'dsh-chatgpt-shell-session'
export const inject = ['tools', 'terminals']

const ACTION_FIELDS = Object.freeze({
  open: Object.freeze(['action', 'name', 'cwd']),
  list: Object.freeze(['action']),
  status: Object.freeze(['action', 'id']),
  send: Object.freeze(['action', 'id', 'text', 'submit', 'wait']),
  read: Object.freeze(['action', 'id', 'offset', 'count']),
  signal: Object.freeze(['action', 'id', 'signal']),
  close: Object.freeze(['action', 'id']),
})

const REQUIRED_FIELDS = Object.freeze({
  open: Object.freeze([]),
  list: Object.freeze([]),
  status: Object.freeze(['id']),
  send: Object.freeze(['id', 'text']),
  read: Object.freeze(['id']),
  signal: Object.freeze(['id', 'signal']),
  close: Object.freeze(['id']),
})

const SIGNALS = Object.freeze(['SIGINT', 'SIGTERM', 'SIGKILL', 'SIGTSTP', 'SIGHUP'])
const SIGNAL_SET = new Set(SIGNALS)

export class ShellSessionError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'ShellSessionError'
    this.code = code
  }
}

export const SHELL_SESSION_PARAMETERS = Object.freeze({
  type: 'object',
  properties: {
    action: {
      type: 'string',
      enum: Object.keys(ACTION_FIELDS),
      description: 'Operation to perform.',
    },
    id: {
      type: 'string',
      description: 'Terminal session id returned by open. Required for status/send/read/signal/close.',
    },
    name: {
      type: 'string',
      description: 'Optional owner-local display name for open.',
    },
    cwd: {
      type: 'string',
      description: 'Optional initial working directory for open. DSH sandbox policy remains authoritative.',
    },
    text: {
      type: 'string',
      description: 'UTF-8 terminal input for send. May contain shell commands or interactive input.',
    },
    submit: {
      type: 'boolean',
      description: 'For send, append Enter after text. Defaults to true.',
    },
    wait: {
      type: 'boolean',
      description: 'For send, wait for readiness/completion. Defaults to true. Set false for long-running commands, then use read/signal.',
    },
    offset: {
      type: 'integer',
      minimum: 0,
      description: 'For read, newest-relative retained-line offset.',
    },
    count: {
      type: 'integer',
      minimum: 1,
      maximum: 500,
      description: 'For read, requested retained-line count. DSH backend bounds still apply.',
    },
    signal: {
      type: 'string',
      enum: SIGNALS,
      description: 'Foreground process-group signal for signal.',
    },
  },
  required: ['action'],
  additionalProperties: false,
})

function own(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key)
}

function assertString(value, field, { allowEmpty = false, maxLength = 65536 } = {}) {
  if (typeof value !== 'string' || value.length > maxLength || (!allowEmpty && value.trim().length === 0)) {
    throw new ShellSessionError('invalid_request', `${field} must be ${allowEmpty ? 'a' : 'a non-empty'} string of at most ${maxLength} characters`)
  }
}

export function validateShellSessionArguments(args) {
  if (typeof args !== 'object' || args === null || Array.isArray(args)) {
    throw new ShellSessionError('invalid_request', 'arguments must be an object')
  }
  const action = args.action
  if (typeof action !== 'string' || !own(ACTION_FIELDS, action)) {
    throw new ShellSessionError('invalid_request', 'action must be one of: open, list, status, send, read, signal, close')
  }
  const allowed = new Set(ACTION_FIELDS[action])
  for (const field of Object.keys(args)) {
    if (!allowed.has(field)) throw new ShellSessionError('invalid_request', `${field} is not valid for action ${action}`)
  }
  for (const field of REQUIRED_FIELDS[action]) {
    if (!own(args, field)) throw new ShellSessionError('invalid_request', `${field} is required for action ${action}`)
  }

  if (own(args, 'id')) assertString(args.id, 'id', { maxLength: 256 })
  if (own(args, 'name')) {
    assertString(args.name, 'name', { maxLength: 128 })
    if (args.name.trim() !== args.name) throw new ShellSessionError('invalid_request', 'name must not have leading or trailing whitespace')
  }
  if (own(args, 'cwd')) assertString(args.cwd, 'cwd', { maxLength: 4096 })
  if (own(args, 'text')) assertString(args.text, 'text', { allowEmpty: true, maxLength: 65536 })
  if (own(args, 'submit') && typeof args.submit !== 'boolean') throw new ShellSessionError('invalid_request', 'submit must be a boolean')
  if (own(args, 'wait') && typeof args.wait !== 'boolean') throw new ShellSessionError('invalid_request', 'wait must be a boolean')
  if (own(args, 'offset') && (!Number.isInteger(args.offset) || args.offset < 0)) throw new ShellSessionError('invalid_request', 'offset must be a non-negative integer')
  if (own(args, 'count') && (!Number.isInteger(args.count) || args.count < 1 || args.count > 500)) throw new ShellSessionError('invalid_request', 'count must be an integer from 1 to 500')
  if (own(args, 'signal') && (typeof args.signal !== 'string' || !SIGNAL_SET.has(args.signal))) throw new ShellSessionError('invalid_request', `signal must be one of: ${SIGNALS.join(', ')}`)
  return args
}

function requireOwner(exec) {
  if (!exec?.agent) throw new ShellSessionError('owner_required', 'shell_session requires a DSH Agent execution identity')
  return exec.agent
}

function statusOf(terminals, owner, id) {
  const session = terminals.list(owner).find(entry => entry.sessionId === id)
  if (!session) throw new ShellSessionError('not_found', `shell session ${JSON.stringify(id)} not found`)
  return session
}

export function createShellSessionTool(terminals) {
  return {
    name: 'shell_session',
    description: [
      'Manage named persistent bash sessions through DSH native PTY and sandbox services.',
      'Actions: open, list, status, send, read, signal, close.',
      'Shell cwd and exported environment persist until close or owner disposal.',
      'Use send with wait=false for long-running foreground commands, then read output or deliver a signal.',
      'DSH owns session authorization, bounded output, process cleanup, and sandbox policy; this tool does not bypass them.',
    ].join(' '),
    parameters: SHELL_SESSION_PARAMETERS,
    output: {
      schema: {},
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(rawArgs, exec) {
      const args = validateShellSessionArguments(rawArgs)
      const owner = requireOwner(exec)
      switch (args.action) {
        case 'open': {
          const request = { type: 'shell' }
          if (own(args, 'name')) request.name = args.name
          if (own(args, 'cwd')) request.cwd = args.cwd
          return await terminals.spawn(owner, request, exec?.signal)
        }
        case 'list':
          return { sessions: terminals.list(owner) }
        case 'status':
          return statusOf(terminals, owner, args.id)
        case 'send': {
          const wait = args.wait ?? true
          const request = {
            text: args.text,
            submit: args.submit ?? true,
            ...(wait && exec?.signal ? { signal: exec.signal } : {}),
          }
          const operation = terminals.startSend(owner, args.id, request)
          if (!wait) {
            // The native terminal session owns the foreground process and its
            // timeout/cleanup. Detach only this tool call's await so a short
            // MCP request lifecycle cannot cancel the long-running command.
            void operation.done.catch(() => {})
            return { sessionId: args.id, started: true }
          }
          return await operation.done
        }
        case 'read': {
          const request = {}
          if (own(args, 'offset')) request.offset = args.offset
          if (own(args, 'count')) request.count = args.count
          return terminals.read(owner, args.id, request)
        }
        case 'signal':
          return await terminals.signal(owner, args.id, args.signal)
        case 'close':
          return { sessionId: args.id, closed: await terminals.kill(owner, args.id, 'closed by ChatGPT shell_session') }
        default:
          throw new Error(`unsupported shell_session action: ${String(args.action)}`)
      }
    },
  }
}

export function apply(ctx) {
  ctx.effect(() => ctx.tools.register(createShellSessionTool(ctx.terminals)), 'chatgpt-shell-session.tool')
}
