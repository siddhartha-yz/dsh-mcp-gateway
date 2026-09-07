import {
  REMOTE_PROTOCOL_VERSION,
  REMOTE_TRANSPORT_PREFIX,
  RemoteWorkerController,
  RemoteWorkerError,
  publicError,
  remoteWorkerDomainSpec,
  validateMachineName,
} from './controller.js'

export const name = 'dsh-chatgpt-remote-workers'
export const inject = ['storageDomain', 'tools', 'webServer']

const MAX_BODY_BYTES = 64 * 1024
const MAX_FILE_CONTENT_CHARS = 1_000_000
const MAX_RESULT_CHARS = 48_000
const MAX_COMMAND_CHARS = 65_536

const REMOTE_MACHINE_PARAMETERS = Object.freeze({
  type: 'object',
  properties: {
    action: { type: 'string', enum: ['invite', 'list', 'rename', 'revoke'], description: 'Remote worker administration operation.' },
    name: { type: 'string', description: 'Optional machine name bound to a new invite.' },
    workdir: { type: 'string', description: 'Optional default working directory bound to a new invite.' },
    ttl_s: { type: 'integer', minimum: 60, maximum: 86400, description: 'Invite lifetime in seconds. Defaults to 600.' },
    machine: { type: 'string', description: 'Existing machine name for rename/revoke.' },
    new_name: { type: 'string', description: 'New machine name for rename.' },
  },
  required: ['action'],
  additionalProperties: false,
})

const REMOTE_EXEC_PARAMETERS = Object.freeze({
  type: 'object',
  properties: {
    machine: { type: 'string', description: 'Registered remote machine name.' },
    action: { type: 'string', enum: ['shell', 'read', 'write', 'edit', 'glob', 'grep'], description: 'Remote execution operation.' },
    command: { type: 'string', description: 'Bash command for action=shell.' },
    cwd: { type: 'string', description: 'Optional command working directory; defaults to the worker workdir.' },
    timeout_ms: { type: 'integer', minimum: 1000, maximum: 120000, description: 'Remote operation timeout. Defaults to 60000 ms.' },
    max_output_chars: { type: 'integer', minimum: 1, maximum: 48000, description: 'Maximum combined shell stdout/stderr characters. Defaults to 32000.' },
    file_path: { type: 'string', description: 'File path for read/write/edit.' },
    offset: { type: 'integer', minimum: 1, description: 'First 1-based line for read. Defaults to 1.' },
    limit: { type: 'integer', minimum: 1, maximum: 2000, description: 'Maximum lines/results for read/grep. Defaults to 200.' },
    content: { type: 'string', description: 'UTF-8 content for write.' },
    old_string: { type: 'string', description: 'Exact text to replace for edit.' },
    new_string: { type: 'string', description: 'Replacement text for edit.' },
    replace_all: { type: 'boolean', description: 'Replace all exact matches for edit. Defaults to false.' },
    pattern: { type: 'string', description: 'Glob or grep pattern.' },
    path: { type: 'string', description: 'Base path for glob/grep. Defaults to the worker workdir.' },
    include: { type: 'string', description: 'Optional glob filter for grep, such as *.js.' },
    literal: { type: 'boolean', description: 'Treat grep pattern literally. Defaults to false.' },
  },
  required: ['machine', 'action'],
  additionalProperties: false,
})

function own(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key)
}

function assertObject(args) {
  if (args === null || typeof args !== 'object' || Array.isArray(args)) throw new RemoteWorkerError('invalid_request', 'arguments must be an object')
  return args
}

function assertString(args, field, { required = false, max = 4096, allowEmpty = false } = {}) {
  if (!own(args, field)) {
    if (required) throw new RemoteWorkerError('invalid_request', `${field} is required for action ${args.action}`)
    return
  }
  const value = args[field]
  if (typeof value !== 'string' || value.length > max || (!allowEmpty && value.length === 0)) {
    throw new RemoteWorkerError('invalid_request', `${field} must be ${allowEmpty ? 'a' : 'a non-empty'} string of at most ${max} characters`)
  }
}

export function validateRemoteMachineArguments(raw) {
  const args = assertObject(raw)
  if (!['invite', 'list', 'rename', 'revoke'].includes(args.action)) throw new RemoteWorkerError('invalid_request', 'action must be invite, list, rename, or revoke')
  const allowed = {
    invite: ['action', 'name', 'workdir', 'ttl_s'],
    list: ['action'],
    rename: ['action', 'machine', 'new_name'],
    revoke: ['action', 'machine'],
  }[args.action]
  for (const field of Object.keys(args)) if (!allowed.includes(field)) throw new RemoteWorkerError('invalid_request', `${field} is not valid for action ${args.action}`)
  if (own(args, 'name')) validateMachineName(args.name, 'name')
  if (own(args, 'machine')) validateMachineName(args.machine)
  if (own(args, 'new_name')) validateMachineName(args.new_name, 'new_name')
  if (own(args, 'workdir')) assertString(args, 'workdir', { max: 4096 })
  if (own(args, 'ttl_s') && (!Number.isSafeInteger(args.ttl_s) || args.ttl_s < 60 || args.ttl_s > 86400)) throw new RemoteWorkerError('invalid_request', 'ttl_s must be an integer from 60 to 86400')
  if (args.action === 'rename') {
    if (!own(args, 'machine') || !own(args, 'new_name')) throw new RemoteWorkerError('invalid_request', 'machine and new_name are required for rename')
  }
  if (args.action === 'revoke' && !own(args, 'machine')) throw new RemoteWorkerError('invalid_request', 'machine is required for revoke')
  return args
}

export function validateRemoteExecArguments(raw) {
  const args = assertObject(raw)
  validateMachineName(args.machine)
  if (!['shell', 'read', 'write', 'edit', 'glob', 'grep'].includes(args.action)) throw new RemoteWorkerError('invalid_request', 'action must be shell, read, write, edit, glob, or grep')
  const allowed = {
    shell: ['machine', 'action', 'command', 'cwd', 'timeout_ms', 'max_output_chars'],
    read: ['machine', 'action', 'file_path', 'offset', 'limit', 'timeout_ms'],
    write: ['machine', 'action', 'file_path', 'content', 'timeout_ms'],
    edit: ['machine', 'action', 'file_path', 'old_string', 'new_string', 'replace_all', 'timeout_ms'],
    glob: ['machine', 'action', 'pattern', 'path', 'timeout_ms'],
    grep: ['machine', 'action', 'pattern', 'path', 'include', 'literal', 'limit', 'timeout_ms'],
  }[args.action]
  for (const field of Object.keys(args)) if (!allowed.includes(field)) throw new RemoteWorkerError('invalid_request', `${field} is not valid for action ${args.action}`)
  if (args.action === 'shell') {
    assertString(args, 'command', { required: true, max: MAX_COMMAND_CHARS })
    if (own(args, 'cwd')) assertString(args, 'cwd', { max: 4096 })
    if (own(args, 'max_output_chars') && (!Number.isSafeInteger(args.max_output_chars) || args.max_output_chars < 1 || args.max_output_chars > MAX_RESULT_CHARS)) throw new RemoteWorkerError('invalid_request', `max_output_chars must be an integer from 1 to ${MAX_RESULT_CHARS}`)
  }
  if (['read', 'write', 'edit'].includes(args.action)) assertString(args, 'file_path', { required: true, max: 4096 })
  if (args.action === 'read') {
    if (own(args, 'offset') && (!Number.isSafeInteger(args.offset) || args.offset < 1)) throw new RemoteWorkerError('invalid_request', 'offset must be a positive integer')
    if (own(args, 'limit') && (!Number.isSafeInteger(args.limit) || args.limit < 1 || args.limit > 2000)) throw new RemoteWorkerError('invalid_request', 'limit must be an integer from 1 to 2000')
  }
  if (args.action === 'write') assertString(args, 'content', { required: true, max: MAX_FILE_CONTENT_CHARS, allowEmpty: true })
  if (args.action === 'edit') {
    assertString(args, 'old_string', { required: true, max: MAX_FILE_CONTENT_CHARS })
    assertString(args, 'new_string', { required: true, max: MAX_FILE_CONTENT_CHARS, allowEmpty: true })
    if (own(args, 'replace_all') && typeof args.replace_all !== 'boolean') throw new RemoteWorkerError('invalid_request', 'replace_all must be a boolean')
  }
  if (['glob', 'grep'].includes(args.action)) assertString(args, 'pattern', { required: true, max: 16384 })
  if (own(args, 'path')) assertString(args, 'path', { max: 4096 })
  if (own(args, 'include')) assertString(args, 'include', { max: 4096 })
  if (own(args, 'literal') && typeof args.literal !== 'boolean') throw new RemoteWorkerError('invalid_request', 'literal must be a boolean')
  if (own(args, 'timeout_ms') && (!Number.isSafeInteger(args.timeout_ms) || args.timeout_ms < 1000 || args.timeout_ms > 120000)) throw new RemoteWorkerError('invalid_request', 'timeout_ms must be an integer from 1000 to 120000')
  return args
}

function requireOwner(exec) {
  if (!exec?.agent) throw new RemoteWorkerError('owner_required', 'remote worker tools require a DSH Agent execution identity')
}

function render(value) {
  return [{ type: 'text', text: JSON.stringify(value, null, 2) }]
}

export function createRemoteMachineTool(controller) {
  return {
    name: 'remote_machine',
    description: 'Manage DSH-native outbound-polling remote workers. Create one-time invites, list registered machines, rename them, or revoke their persistent identity. Remote machines run a small stdlib-only worker and do not install DSH.',
    parameters: REMOTE_MACHINE_PARAMETERS,
    output: { schema: {}, render: (_args, value) => render(value) },
    async execute(rawArgs, exec) {
      requireOwner(exec)
      const args = validateRemoteMachineArguments(rawArgs)
      switch (args.action) {
        case 'invite':
          return controller.createInvite(args)
        case 'list':
          return controller.listMachines()
        case 'rename':
          return await controller.rename(args.machine, args.new_name)
        case 'revoke':
          return await controller.revoke(args.machine)
        default:
          throw new Error(`unsupported remote_machine action ${String(args.action)}`)
      }
    },
  }
}

export function createRemoteExecTool(controller) {
  return {
    name: 'remote_exec',
    description: 'Execute bounded shell or UTF-8 filesystem operations on one explicitly enrolled DSH remote worker. Enrollment delegates that remote OS user authority; the worker workdir is a default cwd, not a filesystem sandbox.',
    parameters: REMOTE_EXEC_PARAMETERS,
    output: { schema: {}, render: (_args, value) => render(value) },
    async execute(rawArgs, exec) {
      requireOwner(exec)
      const args = validateRemoteExecArguments(rawArgs)
      const { machine, action, timeout_ms: timeoutMs, ...workerArguments } = args
      return await controller.dispatch(machine, action, workerArguments, { signal: exec?.signal, timeoutMs })
    },
  }
}

async function readJson(req) {
  let size = 0
  const chunks = []
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += buffer.length
    if (size > MAX_BODY_BYTES) throw new RemoteWorkerError('request_too_large', `request body exceeds ${MAX_BODY_BYTES} bytes`, { status: 413 })
    chunks.push(buffer)
  }
  if (chunks.length === 0) return {}
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    throw new RemoteWorkerError('invalid_request', 'request body must be valid JSON')
  }
}

function bearer(req) {
  const header = String(req.headers?.authorization ?? '')
  const match = /^Bearer\s+(.+)$/i.exec(header)
  return match ? match[1].trim() : ''
}

function sendJson(res, status, body) {
  const data = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(data),
    'cache-control': 'no-store',
  })
  res.end(data)
}

function registerRoute(ctx, path, operation) {
  return ctx.webServer.register({
    kind: 'exact',
    path: `${REMOTE_TRANSPORT_PREFIX}/${path}`,
    handler: async (req, res) => {
      if (req.method !== 'POST') {
        sendJson(res, 405, { ok: false, error: 'method_not_allowed' })
        return
      }
      try {
        const payload = await readJson(req)
        const value = await operation(payload, bearer(req))
        sendJson(res, 200, { ok: true, value })
      } catch (error) {
        const failure = publicError(error)
        if (failure.log) ctx.logger.warn(`dsh-chatgpt-remote-workers: ${failure.log}`)
        sendJson(res, failure.status, failure.body)
      }
    },
  })
}

export function registerTransportRoutes(ctx, controller) {
  return [
    () => registerRoute(ctx, 'register', payload => controller.register(payload)),
    () => registerRoute(ctx, 'resume', (payload, token) => controller.resume(token, payload)),
    () => registerRoute(ctx, 'poll', (payload, token) => controller.poll(token, payload)),
    () => registerRoute(ctx, 'heartbeat', (payload, token) => controller.heartbeat(token, payload)),
    () => registerRoute(ctx, 'result', (payload, token) => controller.submitResult(token, payload)),
  ]
}

function validatedPublicBaseUrl(value) {
  let parsed
  try {
    parsed = new URL(String(value ?? ''))
  } catch {
    throw new Error('dsh-chatgpt-remote-workers publicBaseUrl must be an absolute HTTPS origin')
  }
  if (
    parsed.protocol !== 'https:'
    || parsed.username
    || parsed.password
    || parsed.pathname !== '/'
    || parsed.search
    || parsed.hash
  ) {
    throw new Error('dsh-chatgpt-remote-workers publicBaseUrl must be an absolute HTTPS origin')
  }
  return parsed.origin
}

export async function apply(ctx, config = {}) {
  const publicBaseUrl = validatedPublicBaseUrl(config.publicBaseUrl)
  const domain = await ctx.storageDomain.open(remoteWorkerDomainSpec)
  ctx.effect(() => () => domain.close(), 'chatgpt-remote-workers.domain-close')
  const controller = new RemoteWorkerController(domain.table('workers'), {
    publicBaseUrl,
    inviteTtlMs: config.inviteTtlMs,
    pollTimeoutMs: config.pollTimeoutMs,
    heartbeatIntervalMs: config.heartbeatIntervalMs,
    offlineAfterMs: config.offlineAfterMs,
    maxPendingPerMachine: config.maxPendingPerMachine,
    defaultJobTimeoutMs: config.defaultJobTimeoutMs,
    maxJobTimeoutMs: config.maxJobTimeoutMs,
  })
  for (const [index, install] of registerTransportRoutes(ctx, controller).entries()) {
    ctx.effect(install, `chatgpt-remote-workers.transport-${index}`)
  }
  ctx.effect(() => ctx.tools.register(createRemoteMachineTool(controller)), 'chatgpt-remote-workers.machine-tool')
  ctx.effect(() => ctx.tools.register(createRemoteExecTool(controller)), 'chatgpt-remote-workers.exec-tool')
}

export { REMOTE_PROTOCOL_VERSION }
