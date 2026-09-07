import { createHash, randomBytes, randomUUID } from 'node:crypto'

export const REMOTE_PROTOCOL_VERSION = 1
export const REMOTE_TRANSPORT_PREFIX = '/api/chatgpt-remote-workers/v1'

const MACHINE_RE = /^[A-Za-z0-9][A-Za-z0-9._@-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$/
const TOKEN_HASH_RE = /^[a-f0-9]{64}$/
const MAX_INFO_KEYS = 32
const MAX_CAPABILITIES = 32
const MAX_WORKDIR = 4096
const MAX_NAME = 128
const MAX_INVITES = 32

export class RemoteWorkerError extends Error {
  constructor(code, message, { status = 400, details } = {}) {
    super(message)
    this.name = 'RemoteWorkerError'
    this.code = code
    this.status = status
    this.details = details
  }
}

function own(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key)
}

function object(value, field) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new RemoteWorkerError('invalid_request', `${field} must be an object`)
  }
  return value
}

function text(value, field, { required = false, max = 4096, trim = false, allowEmpty = false } = {}) {
  if (value === undefined) {
    if (required) throw new RemoteWorkerError('invalid_request', `${field} is required`)
    return undefined
  }
  if (typeof value !== 'string' || value.length > max) {
    throw new RemoteWorkerError('invalid_request', `${field} must be a string of at most ${max} characters`)
  }
  const result = trim ? value.trim() : value
  if (!allowEmpty && result.length === 0) {
    throw new RemoteWorkerError('invalid_request', `${field} must not be empty`)
  }
  return result
}

function finiteInteger(value, field, { min, max, defaultValue } = {}) {
  if (value === undefined) return defaultValue
  if (!Number.isSafeInteger(value) || (min !== undefined && value < min) || (max !== undefined && value > max)) {
    throw new RemoteWorkerError('invalid_request', `${field} must be an integer${min !== undefined ? ` >= ${min}` : ''}${max !== undefined ? ` and <= ${max}` : ''}`)
  }
  return value
}

export function validateMachineName(value, field = 'machine') {
  const result = text(value, field, { required: true, max: MAX_NAME, trim: true })
  if (!MACHINE_RE.test(result)) {
    throw new RemoteWorkerError('invalid_request', `${field} must use only letters, digits, '.', '_', '@', '-' and start/end with a letter or digit`)
  }
  return result
}

function machineNamePart(value, fallback) {
  const normalized = String(value ?? '')
    .replace(/[^A-Za-z0-9._@-]+/g, '_')
    .replace(/^[^A-Za-z0-9]+|[^A-Za-z0-9]+$/g, '')
  return normalized || fallback
}

function defaultMachineName(info) {
  const user = machineNamePart(info.user, 'user')
  const host = machineNamePart(info.hostname, 'remote')
  let combined = `${user}@${host}`.slice(0, MAX_NAME)
  combined = combined.replace(/[^A-Za-z0-9]+$/g, '') || 'remote'
  return validateMachineName(combined, 'generated machine name')
}

function boundedStringArray(value, field) {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.length > MAX_CAPABILITIES) {
    throw new RemoteWorkerError('invalid_request', `${field} must be an array with at most ${MAX_CAPABILITIES} entries`)
  }
  return value.map((item, index) => text(item, `${field}[${index}]`, { required: true, max: 128, trim: true }))
}

function boundedInfo(value) {
  if (value === undefined) return {}
  object(value, 'info')
  const entries = Object.entries(value)
  if (entries.length > MAX_INFO_KEYS) throw new RemoteWorkerError('invalid_request', `info may contain at most ${MAX_INFO_KEYS} keys`)
  const result = {}
  for (const [key, raw] of entries) {
    if (key.length === 0 || key.length > 128) throw new RemoteWorkerError('invalid_request', 'info keys must contain 1..128 characters')
    if (!['string', 'number', 'boolean'].includes(typeof raw) && raw !== null) {
      throw new RemoteWorkerError('invalid_request', `info.${key} must be a JSON scalar`)
    }
    if (typeof raw === 'string' && raw.length > 2048) throw new RemoteWorkerError('invalid_request', `info.${key} exceeds 2048 characters`)
    result[key] = raw
  }
  return result
}

function iso(value, field) {
  const result = text(value, field, { required: true, max: 64, trim: true })
  if (!Number.isFinite(Date.parse(result))) throw new RemoteWorkerError('invalid_record', `${field} must be an ISO timestamp`)
  return result
}

export function parseStoredWorker(value) {
  const worker = object(value, 'worker')
  const expected = ['name', 'tokenHash', 'workdir', 'createdAt', 'capabilities', 'info'].sort()
  const actual = Object.keys(worker).sort()
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new RemoteWorkerError('invalid_record', 'worker has an unexpected shape')
  }
  const name = validateMachineName(worker.name, 'worker.name')
  const tokenHash = text(worker.tokenHash, 'worker.tokenHash', { required: true, max: 64, trim: true })
  if (!TOKEN_HASH_RE.test(tokenHash)) throw new RemoteWorkerError('invalid_record', 'worker.tokenHash must be a SHA-256 hex digest')
  const workdir = text(worker.workdir, 'worker.workdir', { required: true, max: MAX_WORKDIR, allowEmpty: true })
  return {
    name,
    tokenHash,
    workdir,
    createdAt: iso(worker.createdAt, 'worker.createdAt'),
    capabilities: boundedStringArray(worker.capabilities, 'worker.capabilities'),
    info: boundedInfo(worker.info),
  }
}

export const remoteWorkerDomainSpec = Object.freeze({
  name: 'chatgpt_remote_workers',
  version: 1,
  layout: 'per-record',
  tables: Object.freeze({
    workers: Object.freeze({ valueSchema: Object.freeze({ parse: parseStoredWorker }) }),
  }),
})

function digestToken(token) {
  return createHash('sha256').update(token).digest('hex')
}

function randomSecret(prefix, bytes = 32) {
  return `${prefix}${randomBytes(bytes).toString('base64url')}`
}

function abortError(message) {
  const error = new Error(message)
  error.name = 'AbortError'
  return error
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

export class RemoteWorkerController {
  constructor(table, config = {}) {
    if (!table || typeof table.get !== 'function' || typeof table.entries !== 'function' || typeof table.put !== 'function' || typeof table.update !== 'function' || typeof table.delete !== 'function') {
      throw new TypeError('RemoteWorkerController requires a DSH storage-domain table handle')
    }
    this.table = table
    this.now = config.now ?? (() => Date.now())
    this.publicBaseUrl = String(config.publicBaseUrl ?? '').replace(/\/$/, '')
    this.inviteTtlMs = config.inviteTtlMs ?? 10 * 60_000
    this.pollTimeoutMs = config.pollTimeoutMs ?? 25_000
    this.heartbeatIntervalMs = config.heartbeatIntervalMs ?? 10_000
    this.offlineAfterMs = config.offlineAfterMs ?? Math.max(60_000, this.pollTimeoutMs * 2 + 10_000)
    this.maxPendingPerMachine = config.maxPendingPerMachine ?? 8
    this.defaultJobTimeoutMs = config.defaultJobTimeoutMs ?? 60_000
    this.maxJobTimeoutMs = config.maxJobTimeoutMs ?? 120_000
    this.invites = new Map()
    this.runtime = new Map()
    this.tokens = new Map()
    this.pending = new Map()
    this._loadWorkers()
  }

  _loadWorkers() {
    for (const [key, worker] of this.table.entries()) {
      if (key !== worker.name) throw new RemoteWorkerError('invalid_record', `worker key ${JSON.stringify(key)} does not match stored name`)
      if (this.tokens.has(worker.tokenHash)) throw new RemoteWorkerError('invalid_record', 'duplicate remote worker identity digest')
      this.tokens.set(worker.tokenHash, worker.name)
      this.runtime.set(worker.name, {
        lastSeenMs: 0,
        queue: [],
        waiters: new Set(),
      })
    }
  }

  _runtimeFor(name) {
    let runtime = this.runtime.get(name)
    if (!runtime) {
      runtime = { lastSeenMs: 0, queue: [], waiters: new Set() }
      this.runtime.set(name, runtime)
    }
    return runtime
  }

  _worker(name) {
    const worker = this.table.get(name)
    if (worker === undefined) throw new RemoteWorkerError('not_found', `remote machine ${JSON.stringify(name)} does not exist`, { status: 404 })
    return worker
  }

  _workerForToken(token) {
    const raw = text(token, 'worker token', { required: true, max: 512, trim: true })
    const name = this.tokens.get(digestToken(raw))
    if (!name) throw new RemoteWorkerError('unauthorized', 'invalid or revoked remote worker token', { status: 401 })
    return this._worker(name)
  }

  _markSeen(name) {
    this._runtimeFor(name).lastSeenMs = this.now()
  }

  _status(name) {
    const lastSeenMs = this._runtimeFor(name).lastSeenMs
    const ageMs = lastSeenMs === 0 ? null : Math.max(0, this.now() - lastSeenMs)
    return {
      status: ageMs !== null && ageMs <= this.offlineAfterMs ? 'online' : 'offline',
      lastSeenAt: lastSeenMs === 0 ? null : new Date(lastSeenMs).toISOString(),
      lastSeenAgeMs: ageMs,
    }
  }

  _pruneInvites() {
    const now = this.now()
    for (const [code, invite] of this.invites) {
      if (invite.expiresAtMs <= now) this.invites.delete(code)
    }
  }

  createInvite({ name, workdir, ttl_s: ttlSeconds } = {}) {
    if (!this.publicBaseUrl) throw new RemoteWorkerError('configuration_error', 'remote worker publicBaseUrl is not configured', { status: 500 })
    this._pruneInvites()
    if (this.invites.size >= MAX_INVITES) throw new RemoteWorkerError('resource_limit', 'too many pending remote worker invites', { status: 429 })
    const checkedName = name === undefined ? undefined : validateMachineName(name, 'name')
    const checkedWorkdir = workdir === undefined ? undefined : text(workdir, 'workdir', { required: true, max: MAX_WORKDIR })
    const ttlS = finiteInteger(ttlSeconds, 'ttl_s', { min: 60, max: 24 * 3600, defaultValue: Math.round(this.inviteTtlMs / 1000) })
    if (checkedName && this.table.get(checkedName) !== undefined) throw new RemoteWorkerError('conflict', `remote machine ${JSON.stringify(checkedName)} already exists`, { status: 409 })
    const code = randomSecret('dshrw_inv_', 24)
    const expiresAtMs = this.now() + ttlS * 1000
    this.invites.set(code, { code, name: checkedName, workdir: checkedWorkdir, expiresAtMs })
    const args = [`--invite`, shellQuote(code)]
    if (checkedName) args.push('--name', shellQuote(checkedName))
    if (checkedWorkdir) args.push('--workdir', shellQuote(checkedWorkdir))
    const joinUrl = `${this.publicBaseUrl}/remote/join.sh`
    const command = `curl -fsSL ${shellQuote(joinUrl)} | bash -s -- ${args.join(' ')}`
    return {
      code,
      name: checkedName ?? null,
      workdir: checkedWorkdir ?? null,
      expires_at: new Date(expiresAtMs).toISOString(),
      ttl_s: ttlS,
      join_url: joinUrl,
      command,
      persistent_command: `${command} --persist`,
    }
  }

  async register(payload) {
    object(payload, 'payload')
    const protocolVersion = finiteInteger(payload.protocol_version, 'protocol_version', { min: 1, max: REMOTE_PROTOCOL_VERSION, defaultValue: REMOTE_PROTOCOL_VERSION })
    if (protocolVersion !== REMOTE_PROTOCOL_VERSION) throw new RemoteWorkerError('protocol_mismatch', `remote worker protocol ${protocolVersion} is unsupported`, { status: 409 })
    const code = text(payload.invite, 'invite', { required: true, max: 256, trim: true })
    this._pruneInvites()
    const invite = this.invites.get(code)
    if (!invite) throw new RemoteWorkerError('invalid_invite', 'remote worker invite is invalid or expired', { status: 400 })
    const requestedName = payload.name === undefined || payload.name === null || payload.name === '' ? undefined : validateMachineName(payload.name, 'name')
    if (invite.name && requestedName && invite.name !== requestedName) throw new RemoteWorkerError('invalid_invite', `invite is bound to machine ${JSON.stringify(invite.name)}`, { status: 400 })
    const info = boundedInfo(payload.info)
    const defaultName = defaultMachineName(info)
    let name = requestedName ?? invite.name ?? defaultName
    if (this.table.get(name) !== undefined) {
      if (requestedName || invite.name) throw new RemoteWorkerError('conflict', `remote machine ${JSON.stringify(name)} already exists`, { status: 409 })
      let suffix = 2
      while (this.table.get(`${name.slice(0, Math.max(1, MAX_NAME - String(suffix).length - 1))}-${suffix}`) !== undefined) suffix += 1
      name = `${name.slice(0, Math.max(1, MAX_NAME - String(suffix).length - 1))}-${suffix}`
    }
    const workdir = text(payload.workdir ?? invite.workdir ?? '', 'workdir', { required: true, max: MAX_WORKDIR })
    const capabilities = boundedStringArray(payload.capabilities, 'capabilities')
    const token = randomSecret('dshrw_wk_', 32)
    const tokenHash = digestToken(token)
    const worker = {
      name,
      tokenHash,
      workdir,
      createdAt: new Date(this.now()).toISOString(),
      capabilities,
      info,
    }
    // Consume before the first async boundary so concurrent register requests
    // cannot redeem the same one-time invite. Storage failure is fail-closed:
    // the caller must create a fresh invite rather than replay this credential.
    this.invites.delete(code)
    await this.table.put(name, worker)
    this.tokens.set(tokenHash, name)
    this.runtime.set(name, { lastSeenMs: this.now(), queue: [], waiters: new Set() })
    return this._connectionResponse(worker, token)
  }

  async resume(token, payload) {
    object(payload, 'payload')
    const worker = this._workerForToken(token)
    const protocolVersion = finiteInteger(payload.protocol_version, 'protocol_version', { min: 1, max: REMOTE_PROTOCOL_VERSION, defaultValue: REMOTE_PROTOCOL_VERSION })
    if (protocolVersion !== REMOTE_PROTOCOL_VERSION) throw new RemoteWorkerError('protocol_mismatch', `remote worker protocol ${protocolVersion} is unsupported`, { status: 409 })
    const workdir = payload.workdir === undefined ? worker.workdir : text(payload.workdir, 'workdir', { required: true, max: MAX_WORKDIR })
    const capabilities = payload.capabilities === undefined ? worker.capabilities : boundedStringArray(payload.capabilities, 'capabilities')
    const info = payload.info === undefined ? worker.info : boundedInfo(payload.info)
    const next = await this.table.update(worker.name, current => ({ ...current, workdir, capabilities, info }))
    this._markSeen(worker.name)
    return this._connectionResponse(next)
  }

  _connectionResponse(worker, token) {
    return {
      name: worker.name,
      ...(token ? { token } : {}),
      protocol_version: REMOTE_PROTOCOL_VERSION,
      poll_timeout_s: this.pollTimeoutMs / 1000,
      heartbeat_interval_s: this.heartbeatIntervalMs / 1000,
    }
  }

  listMachines() {
    const machines = []
    const counts = { online: 0, offline: 0, total: 0 }
    for (const [, worker] of this.table.entries()) {
      const runtime = this._runtimeFor(worker.name)
      const status = this._status(worker.name)
      counts[status.status] += 1
      counts.total += 1
      machines.push({
        name: worker.name,
        status: status.status,
        workdir: worker.workdir,
        created_at: worker.createdAt,
        last_seen_at: status.lastSeenAt,
        last_seen_age_ms: status.lastSeenAgeMs,
        queue_depth: runtime.queue.length,
        pending_jobs: [...this.pending.values()].filter(item => item.machine === worker.name).length,
        capabilities: [...worker.capabilities],
        info: { ...worker.info },
      })
    }
    machines.sort((left, right) => (left.status === right.status ? left.name.localeCompare(right.name) : left.status === 'online' ? -1 : 1))
    return { machines, counts }
  }

  async rename(machine, newName) {
    const oldName = validateMachineName(machine)
    const checkedNew = validateMachineName(newName, 'new_name')
    if (oldName === checkedNew) return { old_name: oldName, new_name: checkedNew }
    if (this.table.get(checkedNew) !== undefined) throw new RemoteWorkerError('conflict', `remote machine ${JSON.stringify(checkedNew)} already exists`, { status: 409 })
    const worker = this._worker(oldName)
    const runtime = this._runtimeFor(oldName)
    const next = { ...worker, name: checkedNew }
    await this.table.put(checkedNew, next)
    try {
      await this.table.delete(oldName)
    } catch (error) {
      await this.table.delete(checkedNew).catch(() => {})
      throw error
    }
    this.tokens.set(worker.tokenHash, checkedNew)
    this.runtime.delete(oldName)
    this.runtime.set(checkedNew, runtime)
    for (const pending of this.pending.values()) {
      if (pending.machine === oldName) pending.machine = checkedNew
    }
    return { old_name: oldName, new_name: checkedNew }
  }

  async revoke(machine) {
    const name = validateMachineName(machine)
    const worker = this._worker(name)
    await this.table.delete(name)
    this.tokens.delete(worker.tokenHash)
    const runtime = this.runtime.get(name)
    this.runtime.delete(name)
    if (runtime) {
      for (const waiter of runtime.waiters) waiter.resolve({ revoked: true })
      runtime.waiters.clear()
    }
    for (const [jobId, pending] of this.pending) {
      if (pending.machine !== name) continue
      this.pending.delete(jobId)
      clearTimeout(pending.expiryTimer)
      if (!pending.callerSettled) {
        pending.callerSettled = true
        pending.reject(new RemoteWorkerError('revoked', `remote machine ${JSON.stringify(name)} was revoked`, { status: 410 }))
      }
    }
    return { machine: name, revoked: true }
  }

  async poll(token, payload = {}) {
    object(payload, 'payload')
    const worker = this._workerForToken(token)
    const protocolVersion = finiteInteger(payload.protocol_version, 'protocol_version', { min: 1, max: REMOTE_PROTOCOL_VERSION, defaultValue: REMOTE_PROTOCOL_VERSION })
    if (protocolVersion !== REMOTE_PROTOCOL_VERSION) throw new RemoteWorkerError('protocol_mismatch', `remote worker protocol ${protocolVersion} is unsupported`, { status: 409 })
    this._markSeen(worker.name)
    const immediate = this._takeQueued(worker.name)
    if (immediate) return { name: worker.name, job: immediate, protocol_version: REMOTE_PROTOCOL_VERSION, poll_timeout_s: this.pollTimeoutMs / 1000 }

    const runtime = this._runtimeFor(worker.name)
    let timer
    const signal = new Promise(resolve => {
      const waiter = { resolve }
      runtime.waiters.add(waiter)
      timer = setTimeout(() => {
        runtime.waiters.delete(waiter)
        resolve({ timeout: true })
      }, this.pollTimeoutMs)
    })
    const wake = await signal
    if (timer !== undefined) clearTimeout(timer)
    this._markSeen(worker.name)
    if (wake.revoked) throw new RemoteWorkerError('unauthorized', 'remote worker identity was revoked', { status: 401 })
    const job = this._takeQueued(worker.name)
    return {
      name: worker.name,
      job: job ?? null,
      heartbeat: job ? undefined : true,
      protocol_version: REMOTE_PROTOCOL_VERSION,
      poll_timeout_s: this.pollTimeoutMs / 1000,
    }
  }

  _wakeWorker(name) {
    const runtime = this._runtimeFor(name)
    const waiter = runtime.waiters.values().next().value
    if (!waiter) return
    runtime.waiters.delete(waiter)
    waiter.resolve({ job: true })
  }

  _takeQueued(name) {
    const runtime = this._runtimeFor(name)
    while (runtime.queue.length > 0) {
      const job = runtime.queue.shift()
      const pending = this.pending.get(job.id)
      if (!pending || pending.cancelled) continue
      pending.claimed = true
      return job
    }
    return null
  }

  heartbeat(token, payload = {}) {
    object(payload, 'payload')
    const worker = this._workerForToken(token)
    this._markSeen(worker.name)
    const jobId = payload.job_id === undefined ? '' : text(payload.job_id, 'job_id', { required: true, max: 128, trim: true })
    const pending = jobId ? this.pending.get(jobId) : undefined
    const cancelled = Boolean(pending && pending.machine === worker.name && pending.cancelled)
    return { name: worker.name, accepted: !cancelled, cancelled }
  }

  submitResult(token, payload) {
    object(payload, 'payload')
    const worker = this._workerForToken(token)
    this._markSeen(worker.name)
    const jobId = text(payload.job_id, 'job_id', { required: true, max: 128, trim: true })
    const pending = this.pending.get(jobId)
    if (!pending) return { accepted: false }
    if (pending.machine !== worker.name) throw new RemoteWorkerError('forbidden', `job ${JSON.stringify(jobId)} belongs to another remote machine`, { status: 403 })
    this.pending.delete(jobId)
    clearTimeout(pending.expiryTimer)
    if (pending.cancelled || pending.callerSettled) return { accepted: false, cancelled: pending.cancelled }
    pending.callerSettled = true
    if (payload.ok === true) {
      pending.resolve(payload.value)
    } else {
      const error = object(payload.error ?? {}, 'error')
      const code = typeof error.code === 'string' && error.code ? error.code.slice(0, 128) : 'remote_error'
      const message = typeof error.message === 'string' && error.message ? error.message.slice(0, 4096) : 'remote worker operation failed'
      pending.reject(new RemoteWorkerError(code, message, { status: 500, details: own(error, 'details') ? error.details : undefined }))
    }
    return { accepted: true }
  }

  dispatch(machine, action, arguments_, { signal, timeoutMs } = {}) {
    const name = validateMachineName(machine)
    const worker = this._worker(name)
    const status = this._status(name)
    if (status.status !== 'online') throw new RemoteWorkerError('offline', `remote machine ${JSON.stringify(name)} is offline`, { status: 503 })
    const runtime = this._runtimeFor(name)
    const machinePending = [...this.pending.values()].filter(item => item.machine === name && !item.cancelled).length
    if (runtime.queue.length >= this.maxPendingPerMachine || machinePending >= this.maxPendingPerMachine) {
      throw new RemoteWorkerError('resource_limit', `remote machine ${JSON.stringify(name)} queue is full`, { status: 429 })
    }
    const effectiveTimeoutMs = finiteInteger(timeoutMs, 'timeout_ms', { min: 1000, max: this.maxJobTimeoutMs, defaultValue: this.defaultJobTimeoutMs })
    const jobId = `job_${randomUUID()}`
    const expiresAtMs = this.now() + effectiveTimeoutMs
    const job = {
      id: jobId,
      action,
      arguments: arguments_ ?? {},
      expires_at: new Date(expiresAtMs).toISOString(),
    }

    return new Promise((resolve, reject) => {
      const pending = {
        machine: worker.name,
        claimed: false,
        cancelled: false,
        callerSettled: false,
        resolve,
        reject,
        expiryTimer: undefined,
      }
      const cancel = reason => {
        if (pending.callerSettled) return
        pending.cancelled = true
        pending.callerSettled = true
        if (!pending.claimed) {
          const index = runtime.queue.findIndex(item => item.id === jobId)
          if (index !== -1) runtime.queue.splice(index, 1)
          this.pending.delete(jobId)
          clearTimeout(pending.expiryTimer)
        }
        reject(reason)
      }
      pending.expiryTimer = setTimeout(() => {
        cancel(new RemoteWorkerError('timeout', `remote ${action} timed out on ${JSON.stringify(name)}`, { status: 504 }))
        if (pending.claimed) {
          setTimeout(() => {
            const current = this.pending.get(jobId)
            if (current === pending) this.pending.delete(jobId)
          }, Math.max(this.heartbeatIntervalMs * 2, 30_000)).unref?.()
        }
      }, effectiveTimeoutMs)
      pending.expiryTimer.unref?.()
      this.pending.set(jobId, pending)
      runtime.queue.push(job)
      this._wakeWorker(name)

      if (signal) {
        const onAbort = () => cancel(signal.reason instanceof Error ? signal.reason : abortError(`remote ${action} cancelled`))
        if (signal.aborted) onAbort()
        else signal.addEventListener('abort', onAbort, { once: true })
      }
    })
  }
}

export function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'"'"'`)}'`
}

export function publicError(error) {
  if (error instanceof RemoteWorkerError) {
    return {
      status: error.status,
      body: {
        ok: false,
        error: error.code,
        message: error.message,
        ...(error.details === undefined ? {} : { details: error.details }),
      },
    }
  }
  return {
    status: 500,
    body: { ok: false, error: 'internal_error', message: 'internal remote worker error' },
    log: errorMessage(error),
  }
}
