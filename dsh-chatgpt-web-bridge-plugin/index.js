import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'

export const name = 'dsh-chatgpt-web-bridge-experiment'
export const inject = ['webServer', 'tools']

export const BRIDGE_BASE_PATH = '/plugins/chatgpt-web-bridge'
export const BRIDGE_VERSION = 2

const MAX_BODY_BYTES = 16 * 1024
const MAX_MESSAGE_CHARS = 8_000
const MAX_ERROR_CHARS = 2_000
const MAX_QUEUE = 64
const MAX_EVENTS = 128
const CLAIM_TTL_MS = 30_000
const RECENT_EVENT_LIMIT = 20

const EVENT_TYPES = Object.freeze([
  'companion_ready',
  'turn_started',
  'assistant_message',
  'turn_completed',
  'blocked',
  'error',
])

export class BridgeError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'BridgeError'
    this.code = code
  }
}

function requireNonEmptyString(value, field, maxChars = 256) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new BridgeError('invalid_request', `${field} must be a non-empty string`)
  }
  const normalized = value.trim()
  if (normalized.length > maxChars) {
    throw new BridgeError('invalid_request', `${field} exceeds ${maxChars} characters`)
  }
  return normalized
}

function optionalString(value, field, maxChars) {
  if (value === undefined || value === null || value === '') return null
  if (typeof value !== 'string') throw new BridgeError('invalid_request', `${field} must be a string`)
  if (value.length > maxChars) throw new BridgeError('invalid_request', `${field} exceeds ${maxChars} characters`)
  return value
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export class ChatGPTWebBridgeStore {
  constructor({ now = () => Date.now() } = {}) {
    this.now = now
    this.messages = []
    this.events = []
    this.nextEventSeq = 1
    this.companion = {
      clientId: null,
      lastSeenAt: null,
      lastEventType: null,
    }
  }

  enqueue(text, source = 'dsh-gui') {
    const normalizedText = requireNonEmptyString(text, 'text', MAX_MESSAGE_CHARS)
    const active = this.messages.filter((message) => ['pending', 'claimed', 'dispatching'].includes(message.status))
    if (active.length >= MAX_QUEUE) throw new BridgeError('queue_full', `bridge queue is limited to ${MAX_QUEUE} active messages`)
    const now = this.now()
    const message = {
      id: randomUUID(),
      text: normalizedText,
      source,
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      claimedBy: null,
      claimExpiresAt: null,
      attempts: 0,
      outcome: null,
      error: null,
    }
    this.messages.push(message)
    this.trimSettledMessages()
    return this.publicMessage(message)
  }

  poll(clientId) {
    const normalizedClientId = requireNonEmptyString(clientId, 'client_id', 128)
    const now = this.now()
    this.touchCompanion(normalizedClientId, null, now)
    this.releaseExpiredClaims(now)

    let message = this.messages.find((candidate) => candidate.status === 'claimed' && candidate.claimedBy === normalizedClientId)
    if (message === undefined) message = this.messages.find((candidate) => candidate.status === 'pending')
    if (message === undefined) return { message: null, companion: this.publicCompanion() }

    if (message.status === 'pending') {
      message.status = 'claimed'
      message.claimedBy = normalizedClientId
      message.attempts += 1
    }
    message.updatedAt = now
    message.claimExpiresAt = now + CLAIM_TTL_MS
    return { message: this.publicMessage(message), companion: this.publicCompanion() }
  }

  heartbeat(clientId) {
    const normalizedClientId = requireNonEmptyString(clientId, 'client_id', 128)
    this.touchCompanion(normalizedClientId, null, this.now())
    return { companion: this.publicCompanion() }
  }

  beginSend({ clientId, messageId }) {
    const normalizedClientId = requireNonEmptyString(clientId, 'client_id', 128)
    const normalizedMessageId = requireNonEmptyString(messageId, 'message_id', 128)
    const message = this.messages.find((candidate) => candidate.id === normalizedMessageId)
    if (message === undefined) throw new BridgeError('message_not_found', 'bridge message not found')
    if (message.status !== 'claimed' || message.claimedBy !== normalizedClientId) {
      throw new BridgeError('claim_mismatch', 'bridge message is not claimed by this companion')
    }
    const now = this.now()
    message.status = 'dispatching'
    message.updatedAt = now
    message.claimExpiresAt = null
    this.touchCompanion(normalizedClientId, null, now)
    return { message: this.publicMessage(message), companion: this.publicCompanion() }
  }

  ack({ clientId, messageId, outcome, error = null }) {
    const normalizedClientId = requireNonEmptyString(clientId, 'client_id', 128)
    const normalizedMessageId = requireNonEmptyString(messageId, 'message_id', 128)
    if (outcome !== 'sent' && outcome !== 'failed') {
      throw new BridgeError('invalid_request', 'outcome must be sent or failed')
    }
    const message = this.messages.find((candidate) => candidate.id === normalizedMessageId)
    if (message === undefined) throw new BridgeError('message_not_found', 'bridge message not found')
    if (message.status !== 'dispatching' || message.claimedBy !== normalizedClientId) {
      throw new BridgeError('dispatch_mismatch', 'bridge message is not dispatching through this companion')
    }
    const now = this.now()
    message.status = outcome
    message.outcome = outcome
    message.error = outcome === 'failed' ? optionalString(error, 'error', MAX_ERROR_CHARS) : null
    message.updatedAt = now
    message.claimExpiresAt = null
    this.touchCompanion(normalizedClientId, null, now)
    return { message: this.publicMessage(message), companion: this.publicCompanion() }
  }

  publish({ clientId, eventType, text = null, payload = null }) {
    const normalizedClientId = requireNonEmptyString(clientId, 'client_id', 128)
    if (!EVENT_TYPES.includes(eventType)) {
      throw new BridgeError('invalid_request', `event_type must be one of: ${EVENT_TYPES.join(', ')}`)
    }
    const normalizedText = optionalString(text, 'text', MAX_MESSAGE_CHARS)
    if (payload !== null && !isPlainObject(payload)) {
      throw new BridgeError('invalid_request', 'payload must be an object or null')
    }
    const now = this.now()
    const event = {
      seq: this.nextEventSeq++,
      ts: now,
      clientId: normalizedClientId,
      type: eventType,
      text: normalizedText,
      payload,
    }
    this.events.push(event)
    if (this.events.length > MAX_EVENTS) this.events.splice(0, this.events.length - MAX_EVENTS)
    this.touchCompanion(normalizedClientId, eventType, now)
    return { event: { ...event }, companion: this.publicCompanion() }
  }

  status() {
    const now = this.now()
    this.releaseExpiredClaims(now)
    const counts = { pending: 0, claimed: 0, dispatching: 0, sent: 0, failed: 0 }
    for (const message of this.messages) counts[message.status] += 1
    return {
      version: BRIDGE_VERSION,
      now,
      counts,
      companion: this.publicCompanion(),
      activeMessages: this.messages
        .filter((message) => ['pending', 'claimed', 'dispatching'].includes(message.status))
        .map((message) => this.publicMessage(message)),
      recentEvents: this.events.slice(-RECENT_EVENT_LIMIT).map((event) => ({ ...event })),
    }
  }

  publicCompanion() {
    return { ...this.companion }
  }

  publicMessage(message) {
    return {
      id: message.id,
      text: message.text,
      source: message.source,
      status: message.status,
      createdAt: message.createdAt,
      updatedAt: message.updatedAt,
      claimedBy: message.claimedBy,
      claimExpiresAt: message.claimExpiresAt,
      attempts: message.attempts,
      outcome: message.outcome,
      error: message.error,
    }
  }

  touchCompanion(clientId, eventType, now) {
    this.companion.clientId = clientId
    this.companion.lastSeenAt = now
    if (eventType !== null) this.companion.lastEventType = eventType
  }

  releaseExpiredClaims(now) {
    for (const message of this.messages) {
      if (message.status !== 'claimed' || message.claimExpiresAt === null || message.claimExpiresAt > now) continue
      message.status = 'pending'
      message.claimedBy = null
      message.claimExpiresAt = null
      message.updatedAt = now
    }
  }

  trimSettledMessages() {
    if (this.messages.length <= MAX_QUEUE * 2) return
    const active = this.messages.filter((message) => ['pending', 'claimed', 'dispatching'].includes(message.status))
    const settled = this.messages.filter((message) => message.status === 'sent' || message.status === 'failed').slice(-MAX_QUEUE)
    this.messages = [...settled, ...active]
  }
}

const TOOL_PARAMETERS = Object.freeze({
  type: 'object',
  properties: {
    action: { type: 'string', enum: ['status', 'poll', 'heartbeat', 'begin_send', 'ack', 'publish'] },
    client_id: { type: 'string' },
    message_id: { type: 'string' },
    outcome: { type: 'string', enum: ['sent', 'failed'] },
    error: { type: 'string' },
    event_type: { type: 'string', enum: EVENT_TYPES },
    text: { type: 'string' },
    payload: { type: 'object', additionalProperties: true },
  },
  required: ['action'],
  additionalProperties: false,
})

export function createBridgeTool(store) {
  return {
    name: 'chatgpt_web_bridge',
    description: [
      'Experimental mechanical bridge between a DSH Web GUI and a companion running inside the real ChatGPT Web conversation.',
      'It does not invoke a model or choose next actions.',
      'Actions: status, poll, heartbeat, begin_send, ack, publish.',
      'poll leases one GUI-originated message; begin_send makes dispatch fail-closed against duplicate turns; ack settles the dispatch; publish reports ChatGPT-side lifecycle observations back to DSH.',
    ].join(' '),
    parameters: TOOL_PARAMETERS,
    output: {
      schema: {},
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      if (!isPlainObject(args)) throw new BridgeError('invalid_request', 'arguments must be an object')
      switch (args.action) {
        case 'status':
          return store.status()
        case 'poll':
          return store.poll(args.client_id)
        case 'heartbeat':
          return store.heartbeat(args.client_id)
        case 'begin_send':
          return store.beginSend({ clientId: args.client_id, messageId: args.message_id })
        case 'ack':
          return store.ack({ clientId: args.client_id, messageId: args.message_id, outcome: args.outcome, error: args.error })
        case 'publish':
          return store.publish({ clientId: args.client_id, eventType: args.event_type, text: args.text, payload: args.payload ?? null })
        default:
          throw new BridgeError('invalid_request', 'unsupported action')
      }
    },
  }
}

function tokenMatches(expected, received) {
  if (typeof received !== 'string') return false
  const left = Buffer.from(expected)
  const right = Buffer.from(received)
  return left.length === right.length && timingSafeEqual(left, right)
}

function writeJson(res, status, value) {
  const body = JSON.stringify(value)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  })
  res.end(body)
}

async function readJsonBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) throw new BridgeError('body_too_large', `request body exceeds ${MAX_BODY_BYTES} bytes`)
    chunks.push(chunk)
  }
  if (size === 0) return {}
  let parsed
  try {
    parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    throw new BridgeError('invalid_json', 'request body must be valid JSON')
  }
  if (!isPlainObject(parsed)) throw new BridgeError('invalid_json', 'request JSON must be an object')
  return parsed
}

function routeHandler({ token, store, mode }) {
  return async (req, res) => {
    if (!tokenMatches(token, req.headers['x-dsh-chatgpt-bridge-token'])) {
      writeJson(res, 403, { error: 'forbidden' })
      return
    }
    try {
      if (mode === 'state') {
        if (req.method !== 'GET' && req.method !== 'HEAD') {
          writeJson(res, 405, { error: 'method_not_allowed' })
          return
        }
        if (req.method === 'HEAD') {
          res.writeHead(200, { 'cache-control': 'no-store' })
          res.end()
          return
        }
        writeJson(res, 200, store.status())
        return
      }
      if (mode === 'enqueue') {
        if (req.method !== 'POST') {
          writeJson(res, 405, { error: 'method_not_allowed' })
          return
        }
        const body = await readJsonBody(req)
        writeJson(res, 201, { message: store.enqueue(body.text, 'dsh-gui') })
        return
      }
      writeJson(res, 404, { error: 'not_found' })
    } catch (error) {
      if (error instanceof BridgeError) {
        writeJson(res, error.code === 'queue_full' ? 409 : 400, { error: error.code, message: error.message })
        return
      }
      writeJson(res, 500, { error: 'internal_error' })
    }
  }
}

export function apply(ctx) {
  const store = new ChatGPTWebBridgeStore()
  const token = randomBytes(24).toString('base64url')

  ctx.on('webserver/index-inject', (table) => {
    table.push({
      kind: 'global',
      name: '__DSH_CHATGPT_WEB_BRIDGE__',
      value: {
        version: BRIDGE_VERSION,
        token,
        basePath: BRIDGE_BASE_PATH,
      },
    })
  })

  ctx.effect(() => {
    const disposeState = ctx.webServer.register({
      kind: 'exact',
      path: `${BRIDGE_BASE_PATH}/state`,
      handler: routeHandler({ token, store, mode: 'state' }),
    })
    const disposeEnqueue = ctx.webServer.register({
      kind: 'exact',
      path: `${BRIDGE_BASE_PATH}/enqueue`,
      handler: routeHandler({ token, store, mode: 'enqueue' }),
    })
    return () => {
      disposeEnqueue()
      disposeState()
    }
  }, 'chatgpt-web-bridge.http')

  ctx.effect(() => ctx.tools.register(createBridgeTool(store)), 'chatgpt-web-bridge.tool')
}
