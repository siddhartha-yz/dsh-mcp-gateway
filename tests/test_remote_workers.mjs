import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import {
  RemoteWorkerController,
  RemoteWorkerError,
  parseStoredWorker,
} from '../dsh-remote-worker-plugin/controller.js'
import {
  createRemoteExecTool,
  createRemoteMachineTool,
  validateRemoteExecArguments,
  validateRemoteMachineArguments,
} from '../dsh-remote-worker-plugin/index.js'

class MemoryTable {
  #records = new Map()

  get(key) { return this.#records.get(key) }
  entries() { return new Map(this.#records).entries() }
  async put(key, value) { this.#records.set(key, structuredClone(value)) }
  async update(key, transform) {
    const current = this.#records.get(key)
    if (current === undefined) throw new Error(`missing key: ${key}`)
    const next = transform(structuredClone(current))
    this.#records.set(key, structuredClone(next))
    return structuredClone(next)
  }
  async delete(key) { return this.#records.delete(key) }
}

function makeController(options = {}) {
  let now = Date.parse('2026-09-07T03:00:00.000Z')
  const table = new MemoryTable()
  const controller = new RemoteWorkerController(table, {
    publicBaseUrl: 'https://dsh.example.com',
    now: () => now,
    pollTimeoutMs: 20,
    heartbeatIntervalMs: 10,
    offlineAfterMs: 1_000,
    maxPendingPerMachine: 2,
    ...options,
  })
  return {
    controller,
    table,
    advance(ms) { now += ms },
  }
}

async function enroll(controller, overrides = {}) {
  const invite = controller.createInvite({ name: overrides.inviteName, ttl_s: 60 })
  const registered = await controller.register({
    protocol_version: 1,
    invite: invite.code,
    name: overrides.name,
    workdir: overrides.workdir ?? '/home/test',
    capabilities: ['shell', 'files'],
    info: overrides.info ?? { user: 'tester', hostname: 'ubuntu.local', platform: 'linux' },
  })
  return { invite, ...registered }
}

{
  const { controller, table } = makeController()
  const invite = controller.createInvite({ name: 'desktop', workdir: '/home/test', ttl_s: 60 })
  assert.match(invite.code, /^dshrw_inv_/)
  assert.match(invite.command, /\/remote\/join\.sh/)
  assert.match(invite.command, /--invite/)
  assert.match(invite.persistent_command, /--persist$/)

  const registered = await controller.register({
    protocol_version: 1,
    invite: invite.code,
    workdir: '/home/test',
    capabilities: ['shell'],
    info: { user: 'tester', hostname: 'desktop' },
  })
  assert.equal(registered.name, 'desktop')
  assert.match(registered.token, /^dshrw_wk_/)
  const stored = table.get('desktop')
  assert.equal(stored.tokenHash, createHash('sha256').update(registered.token).digest('hex'))
  assert.equal(JSON.stringify(stored).includes(registered.token), false)
  assert.deepEqual(parseStoredWorker(structuredClone(stored)), stored)

  await assert.rejects(
    controller.register({ protocol_version: 1, invite: invite.code, workdir: '/home/test' }),
    error => error instanceof RemoteWorkerError && error.code === 'invalid_invite',
  )

  const listed = controller.listMachines()
  assert.equal(listed.counts.online, 1)
  assert.equal(listed.machines[0].name, 'desktop')
}

{
  const { controller, table } = makeController()
  const originalPut = table.put.bind(table)
  let putCalls = 0
  let releaseFirst
  let markFirstEntered
  const firstEntered = new Promise(resolve => { markFirstEntered = resolve })
  const firstGate = new Promise(resolve => { releaseFirst = resolve })
  table.put = async (key, value) => {
    putCalls += 1
    if (putCalls === 1) {
      markFirstEntered()
      await firstGate
    }
    return originalPut(key, value)
  }

  const invite = controller.createInvite({ name: 'concurrent', ttl_s: 60 })
  const payload = {
    protocol_version: 1,
    invite: invite.code,
    workdir: '/home/test',
    capabilities: ['shell'],
    info: { user: 'tester', hostname: 'desktop' },
  }
  const first = controller.register(payload)
  await firstEntered
  try {
    await assert.rejects(
      controller.register(payload),
      error => error instanceof RemoteWorkerError && error.code === 'invalid_invite',
    )
  } finally {
    releaseFirst()
    await first
  }
  assert.equal(putCalls, 1)
}

{
  const { controller, advance } = makeController()
  const invite = controller.createInvite({ ttl_s: 60 })
  advance(60_001)
  await assert.rejects(
    controller.register({ protocol_version: 1, invite: invite.code, workdir: '/tmp' }),
    error => error instanceof RemoteWorkerError && error.code === 'invalid_invite',
  )
}

{
  const { controller } = makeController()
  const worker = await enroll(controller, { info: { user: '___', hostname: '***ubuntu***' } })
  assert.equal(worker.name, 'user@ubuntu')

  const dispatch = controller.dispatch(worker.name, 'shell', { command: 'pwd' })
  const polled = await controller.poll(worker.token, { protocol_version: 1 })
  assert.equal(polled.job.action, 'shell')
  assert.deepEqual(polled.job.arguments, { command: 'pwd' })
  assert.match(polled.job.id, /^job_/)

  const accepted = controller.submitResult(worker.token, {
    job_id: polled.job.id,
    ok: true,
    value: { exit_code: 0, stdout: '/home/test\n', stderr: '' },
  })
  assert.equal(accepted.accepted, true)
  assert.equal((await dispatch).exit_code, 0)
}

{
  const { controller } = makeController()
  const first = await enroll(controller, { inviteName: 'first' })
  const second = await enroll(controller, { inviteName: 'second' })
  const pending = controller.dispatch('first', 'read', { file_path: '/tmp/x' })
  const polled = await controller.poll(first.token, { protocol_version: 1 })
  assert.throws(
    () => controller.submitResult(second.token, { job_id: polled.job.id, ok: true, value: 'wrong' }),
    error => error instanceof RemoteWorkerError && error.code === 'forbidden',
  )
  controller.submitResult(first.token, { job_id: polled.job.id, ok: true, value: 'right' })
  assert.equal(await pending, 'right')
}

{
  const { controller } = makeController({ maxPendingPerMachine: 1 })
  const worker = await enroll(controller, { inviteName: 'queue' })
  const abort = new AbortController()
  const first = controller.dispatch('queue', 'shell', { command: 'sleep 1' }, { signal: abort.signal })
  assert.throws(
    () => controller.dispatch('queue', 'shell', { command: 'second' }),
    error => error instanceof RemoteWorkerError && error.code === 'resource_limit',
  )
  abort.abort(new Error('test cancellation'))
  await assert.rejects(first, /test cancellation/)
  const empty = await controller.poll(worker.token, { protocol_version: 1 })
  assert.equal(empty.job, null)
}

{
  const { controller } = makeController()
  const worker = await enroll(controller, { inviteName: 'cancel' })
  const abort = new AbortController()
  const pending = controller.dispatch('cancel', 'shell', { command: 'sleep 30' }, { signal: abort.signal })
  const polled = await controller.poll(worker.token, { protocol_version: 1 })
  abort.abort(new Error('caller gone'))
  await assert.rejects(pending, /caller gone/)
  const heartbeat = controller.heartbeat(worker.token, { job_id: polled.job.id })
  assert.equal(heartbeat.cancelled, true)
  const late = controller.submitResult(worker.token, { job_id: polled.job.id, ok: true, value: 'late' })
  assert.deepEqual(late, { accepted: false, cancelled: true })
}

{
  const { controller } = makeController()
  const worker = await enroll(controller, { inviteName: 'old-name' })
  assert.deepEqual(await controller.rename('old-name', 'new-name'), { old_name: 'old-name', new_name: 'new-name' })
  const resumed = await controller.resume(worker.token, { protocol_version: 1, workdir: '/home/test' })
  assert.equal(resumed.name, 'new-name')
  await controller.revoke('new-name')
  await assert.rejects(
    controller.resume(worker.token, { protocol_version: 1 }),
    error => error instanceof RemoteWorkerError && error.code === 'unauthorized',
  )
}

{
  assert.deepEqual(validateRemoteMachineArguments({ action: 'list' }), { action: 'list' })
  assert.throws(() => validateRemoteMachineArguments({ action: 'list', machine: 'x' }), /not valid/)
  assert.throws(() => validateRemoteMachineArguments({ action: 'rename', machine: 'x' }), /required/)
  assert.deepEqual(
    validateRemoteExecArguments({ machine: 'desktop', action: 'shell', command: 'uname -a', timeout_ms: 5000 }),
    { machine: 'desktop', action: 'shell', command: 'uname -a', timeout_ms: 5000 },
  )
  assert.throws(() => validateRemoteExecArguments({ machine: 'desktop', action: 'shell' }), /command is required/)
  assert.throws(() => validateRemoteExecArguments({ machine: 'desktop', action: 'read', file_path: '/x', content: 'no' }), /not valid/)
}

{
  const calls = []
  const controller = {
    createInvite(args) { calls.push(['invite', args]); return { command: 'join' } },
    listMachines() { calls.push(['list']); return { machines: [] } },
    rename() { throw new Error('unused') },
    revoke() { throw new Error('unused') },
    async dispatch(machine, action, args) { calls.push(['dispatch', machine, action, args]); return { ok: true } },
  }
  const machineTool = createRemoteMachineTool(controller)
  const execTool = createRemoteExecTool(controller)
  await assert.rejects(machineTool.execute({ action: 'list' }, {}), /execution identity/)
  assert.deepEqual(await machineTool.execute({ action: 'list' }, { agent: {} }), { machines: [] })
  assert.deepEqual(
    await execTool.execute({ machine: 'desktop', action: 'shell', command: 'pwd' }, { agent: {}, signal: new AbortController().signal }),
    { ok: true },
  )
  assert.deepEqual(calls[1], ['dispatch', 'desktop', 'shell', { command: 'pwd' }])
}

console.log('remote worker tests passed')
