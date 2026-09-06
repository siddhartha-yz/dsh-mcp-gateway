import assert from 'node:assert/strict'

import { createTaskStateTool } from '../dsh-task-state-plugin/index.js'
import { MAX_CHECKPOINTS, parseStoredTask, TaskStateError, TaskStore } from '../dsh-task-state-plugin/task-store.js'

class MemoryTable {
  #records = new Map()

  get(key) {
    return this.#records.get(key)
  }

  entries() {
    return new Map(this.#records).entries()
  }

  async put(key, value) {
    this.#records.set(key, value)
  }

  async update(key, transform) {
    const current = this.#records.get(key)
    if (current === undefined) throw new Error(`missing key: ${key}`)
    const next = transform(current)
    this.#records.set(key, next)
    return next
  }
}

function makeStore(id = 'task_test') {
  let tick = Date.parse('2026-09-06T01:00:00.000Z')
  return new TaskStore(new MemoryTable(), {
    now: () => tick++,
    idFactory: () => id,
  })
}

function storedFixture() {
  return {
    id: 'task_persisted',
    title: 'Persisted task',
    status: 'active',
    goal: '',
    workspace: '/workspace/demo',
    summary: 'checkpoint',
    plan: ['continue later'],
    nextSteps: ['reload'],
    references: ['abc123'],
    revision: 2,
    createdAt: '2026-09-06T01:00:00.000Z',
    updatedAt: '2026-09-06T01:00:01.000Z',
    completedAt: null,
    checkpoints: [{
      revision: 2,
      at: '2026-09-06T01:00:01.000Z',
      summary: 'checkpoint',
      nextSteps: ['reload'],
      references: ['abc123'],
    }],
  }
}

{
  const store = makeStore()
  const created = await store.create({
    title: 'DSH P3',
    goal: 'Persist engineering task state without an AgentLoop',
    workspace: '/workspace/dsh-mcp-gateway',
    summary: 'Initial design',
    plan: ['define storage', 'register tool'],
    nextSteps: ['write tests'],
    references: ['127877b'],
  })

  assert.equal(created.id, 'task_test')
  assert.equal(created.status, 'active')
  assert.equal(created.revision, 1)
  assert.equal(created.checkpointCount, 0)
  assert.equal('checkpoints' in created, false)

  const listed = store.list({ query: 'agentloop' })
  assert.equal(listed.count, 1)
  assert.equal(listed.tasks[0].id, 'task_test')
  assert.equal(listed.tasks[0].summaryPreview, 'Initial design')

  const updated = await store.update({
    id: created.id,
    ifRevision: created.revision,
    summary: 'Core store implemented',
    plan: ['define storage ✓', 'register tool'],
  })
  assert.equal(updated.revision, 2)
  assert.equal(updated.summary, 'Core store implemented')

  await assert.rejects(
    store.update({ id: created.id, ifRevision: 1, summary: 'stale overwrite' }),
    error => error instanceof TaskStateError && error.code === 'revision_conflict',
  )

  const checkpointed = await store.checkpoint({
    id: created.id,
    ifRevision: updated.revision,
    summary: 'Core and tool adapter implemented',
    nextSteps: ['run isolated host smoke'],
    references: ['127877b', 'working-tree'],
  })
  assert.equal(checkpointed.revision, 3)
  assert.equal(checkpointed.checkpointCount, 1)
  assert.deepEqual(checkpointed.nextSteps, ['run isolated host smoke'])

  const withHistory = store.get({ id: created.id, includeHistory: true })
  assert.equal(withHistory.checkpoints.length, 1)
  assert.equal(withHistory.checkpoints[0].revision, 3)
  assert.equal(withHistory.checkpoints[0].summary, 'Core and tool adapter implemented')

  const paused = await store.transition({ id: created.id, ifRevision: 3 }, 'paused')
  assert.equal(paused.status, 'paused')
  assert.equal(paused.revision, 4)

  const resumed = await store.transition({ id: created.id, ifRevision: 4 }, 'active')
  assert.equal(resumed.status, 'active')
  assert.equal(resumed.revision, 5)

  const completed = await store.transition({ id: created.id, ifRevision: 5 }, 'completed')
  assert.equal(completed.status, 'completed')
  assert.equal(completed.revision, 6)
  assert.match(completed.completedAt, /^2026-09-06T/)

  await assert.rejects(
    store.transition({ id: created.id, ifRevision: 6 }, 'active'),
    error => error instanceof TaskStateError && error.code === 'invalid_transition',
  )
}

{
  const store = makeStore('task_history')
  let current = await store.create({ title: 'Checkpoint bound' })
  for (let index = 0; index < MAX_CHECKPOINTS + 3; index += 1) {
    current = await store.checkpoint({
      id: current.id,
      ifRevision: current.revision,
      summary: `checkpoint-${index}`,
      nextSteps: [`next-${index}`],
    })
  }
  assert.equal(current.checkpointCount, MAX_CHECKPOINTS)
  const history = store.get({ id: current.id, includeHistory: true }).checkpoints
  assert.equal(history.length, MAX_CHECKPOINTS)
  assert.equal(history[0].summary, 'checkpoint-3')
  assert.equal(history.at(-1).summary, `checkpoint-${MAX_CHECKPOINTS + 2}`)
}

{
  const store = makeStore('task_validation')
  await assert.rejects(
    store.create({ title: '   ' }),
    error => error instanceof TaskStateError && error.code === 'invalid_request',
  )
  const task = await store.create({ title: 'Valid task' })
  await assert.rejects(
    store.update({ id: task.id, ifRevision: task.revision }),
    error => error instanceof TaskStateError && error.code === 'invalid_request',
  )
  assert.throws(
    () => store.get({ id: 'task_missing' }),
    error => error instanceof TaskStateError && error.code === 'not_found',
  )
  assert.throws(
    () => store.list({ limit: 101 }),
    error => error instanceof TaskStateError && error.code === 'invalid_request',
  )
}

{
  const fixture = storedFixture()
  assert.deepEqual(parseStoredTask(structuredClone(fixture)), fixture)

  const extra = structuredClone(fixture)
  extra.unexpected = true
  assert.throws(
    () => parseStoredTask(extra),
    error => error instanceof TaskStateError && error.code === 'invalid_record',
  )

  const badCompletion = structuredClone(fixture)
  badCompletion.status = 'completed'
  assert.throws(
    () => parseStoredTask(badCompletion),
    error => error instanceof TaskStateError && error.code === 'invalid_record',
  )

  const badCheckpoint = structuredClone(fixture)
  badCheckpoint.checkpoints[0].revision = 99
  assert.throws(
    () => parseStoredTask(badCheckpoint),
    error => error instanceof TaskStateError && error.code === 'invalid_record',
  )
}

{
  const store = makeStore('task_adapter')
  const tool = createTaskStateTool(store)
  const created = await tool.execute({ action: 'create', title: 'Adapter task', next_steps: ['reload later'] })
  assert.equal(created.id, 'task_adapter')
  assert.deepEqual(created.nextSteps, ['reload later'])

  const loaded = await tool.execute({ action: 'get', id: created.id, include_history: true })
  assert.equal(loaded.id, created.id)
  assert.deepEqual(loaded.checkpoints, [])

  for (const invalid of [
    null,
    { action: 'unknown' },
    { action: 'get' },
    { action: 'list', id: created.id },
    { action: 'pause', id: created.id },
    { action: 'create', title: 'x', if_revision: 1 },
  ]) {
    await assert.rejects(
      Promise.resolve().then(() => tool.execute(invalid)),
      error => error instanceof TaskStateError && error.code === 'invalid_request',
    )
  }

  await assert.rejects(
    tool.execute({ action: 'get', id: '../escape' }),
    error => error instanceof TaskStateError && error.code === 'invalid_request',
  )
}

console.log('chatgpt-task-state-core-ok')
