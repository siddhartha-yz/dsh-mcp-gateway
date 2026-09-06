import { randomUUID } from 'node:crypto'

export const TASK_STATUS = Object.freeze(['active', 'paused', 'completed'])
export const MAX_CHECKPOINTS = 20

const LIMITS = Object.freeze({
  title: 200,
  goal: 4_000,
  workspace: 4_096,
  summary: 12_000,
  planItems: 100,
  planItem: 1_000,
  nextSteps: 50,
  nextStep: 1_000,
  references: 50,
  reference: 1_000,
  query: 1_000,
  listLimit: 100,
})

export class TaskStateError extends Error {
  constructor(code, message) {
    super(`task_state ${code}: ${message}`)
    this.name = 'TaskStateError'
    this.code = code
  }
}

function record(value, field) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TaskStateError('invalid_record', `${field} must be an object`)
  }
  return value
}

function exactKeys(value, field, expected) {
  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new TaskStateError('invalid_record', `${field} has an unexpected shape`)
  }
}

function storedText(value, field, { max, nonblank = false } = {}) {
  if (typeof value !== 'string') throw new TaskStateError('invalid_record', `${field} must be a string`)
  if (nonblank && value.trim().length === 0) throw new TaskStateError('invalid_record', `${field} must not be blank`)
  if (max !== undefined && value.length > max) throw new TaskStateError('invalid_record', `${field} exceeds ${max} characters`)
  return value
}

function storedTextArray(value, field, { maxItems, maxItem } = {}) {
  if (!Array.isArray(value)) throw new TaskStateError('invalid_record', `${field} must be an array`)
  if (value.length > maxItems) throw new TaskStateError('invalid_record', `${field} exceeds ${maxItems} items`)
  return value.map((item, index) => storedText(item, `${field}[${index}]`, { max: maxItem }))
}

function isoTimestamp(value, field) {
  const textValue = storedText(value, field, { max: 64, nonblank: true })
  if (!Number.isFinite(Date.parse(textValue))) throw new TaskStateError('invalid_record', `${field} must be an ISO timestamp`)
  return textValue
}

export function parseStoredTask(value) {
  const task = record(value, 'task')
  exactKeys(task, 'task', [
    'id', 'title', 'status', 'goal', 'workspace', 'summary', 'plan', 'nextSteps', 'references',
    'revision', 'createdAt', 'updatedAt', 'completedAt', 'checkpoints',
  ])

  const id = storedText(task.id, 'task.id', { max: 128, nonblank: true })
  if (!/^task_[A-Za-z0-9_-]+$/.test(id)) throw new TaskStateError('invalid_record', 'task.id is not storage-safe')
  const status = storedText(task.status, 'task.status', { max: 16, nonblank: true })
  if (!TASK_STATUS.includes(status)) throw new TaskStateError('invalid_record', `task.status must be one of: ${TASK_STATUS.join(', ')}`)
  if (!Number.isSafeInteger(task.revision) || task.revision < 1) {
    throw new TaskStateError('invalid_record', 'task.revision must be a positive integer')
  }
  const createdAt = isoTimestamp(task.createdAt, 'task.createdAt')
  const updatedAt = isoTimestamp(task.updatedAt, 'task.updatedAt')
  const completedAt = task.completedAt === null ? null : isoTimestamp(task.completedAt, 'task.completedAt')
  if ((status === 'completed') !== (completedAt !== null)) {
    throw new TaskStateError('invalid_record', 'task.completedAt must be present exactly when status is completed')
  }
  if (!Array.isArray(task.checkpoints) || task.checkpoints.length > MAX_CHECKPOINTS) {
    throw new TaskStateError('invalid_record', `task.checkpoints must contain at most ${MAX_CHECKPOINTS} entries`)
  }
  const checkpoints = task.checkpoints.map((item, index) => {
    const checkpoint = record(item, `task.checkpoints[${index}]`)
    exactKeys(checkpoint, `task.checkpoints[${index}]`, ['revision', 'at', 'summary', 'nextSteps', 'references'])
    if (!Number.isSafeInteger(checkpoint.revision) || checkpoint.revision < 2 || checkpoint.revision > task.revision) {
      throw new TaskStateError('invalid_record', `task.checkpoints[${index}].revision is invalid`)
    }
    return {
      revision: checkpoint.revision,
      at: isoTimestamp(checkpoint.at, `task.checkpoints[${index}].at`),
      summary: storedText(checkpoint.summary, `task.checkpoints[${index}].summary`, { max: LIMITS.summary, nonblank: true }),
      nextSteps: storedTextArray(checkpoint.nextSteps, `task.checkpoints[${index}].nextSteps`, { maxItems: LIMITS.nextSteps, maxItem: LIMITS.nextStep }),
      references: storedTextArray(checkpoint.references, `task.checkpoints[${index}].references`, { maxItems: LIMITS.references, maxItem: LIMITS.reference }),
    }
  })

  return {
    id,
    title: storedText(task.title, 'task.title', { max: LIMITS.title, nonblank: true }),
    status,
    goal: storedText(task.goal, 'task.goal', { max: LIMITS.goal }),
    workspace: storedText(task.workspace, 'task.workspace', { max: LIMITS.workspace }),
    summary: storedText(task.summary, 'task.summary', { max: LIMITS.summary }),
    plan: storedTextArray(task.plan, 'task.plan', { maxItems: LIMITS.planItems, maxItem: LIMITS.planItem }),
    nextSteps: storedTextArray(task.nextSteps, 'task.nextSteps', { maxItems: LIMITS.nextSteps, maxItem: LIMITS.nextStep }),
    references: storedTextArray(task.references, 'task.references', { maxItems: LIMITS.references, maxItem: LIMITS.reference }),
    revision: task.revision,
    createdAt,
    updatedAt,
    completedAt,
    checkpoints,
  }
}

function own(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key)
}

function text(value, field, { required = false, max } = {}) {
  if (value === undefined) {
    if (required) throw new TaskStateError('invalid_request', `${field} is required`)
    return undefined
  }
  if (typeof value !== 'string') throw new TaskStateError('invalid_request', `${field} must be a string`)
  if (required && value.trim().length === 0) {
    throw new TaskStateError('invalid_request', `${field} must not be blank`)
  }
  if (max !== undefined && value.length > max) {
    throw new TaskStateError('invalid_request', `${field} exceeds ${max} characters`)
  }
  return value
}

function textArray(value, field, { maxItems, maxItem } = {}) {
  if (value === undefined) return undefined
  if (!Array.isArray(value)) throw new TaskStateError('invalid_request', `${field} must be an array`)
  if (maxItems !== undefined && value.length > maxItems) {
    throw new TaskStateError('invalid_request', `${field} exceeds ${maxItems} items`)
  }
  return value.map((item, index) => {
    if (typeof item !== 'string') {
      throw new TaskStateError('invalid_request', `${field}[${index}] must be a string`)
    }
    if (maxItem !== undefined && item.length > maxItem) {
      throw new TaskStateError('invalid_request', `${field}[${index}] exceeds ${maxItem} characters`)
    }
    return item
  })
}

function taskId(value) {
  const id = text(value, 'id', { required: true, max: 128 })
  if (!/^task_[A-Za-z0-9_-]+$/.test(id)) {
    throw new TaskStateError('invalid_request', 'id must be a task_state id returned by create')
  }
  return id
}

function revision(value) {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TaskStateError('invalid_request', 'if_revision must be a positive integer')
  }
  return value
}

function listLimit(value) {
  if (value === undefined) return 20
  if (!Number.isSafeInteger(value) || value < 1 || value > LIMITS.listLimit) {
    throw new TaskStateError('invalid_request', `limit must be an integer from 1 to ${LIMITS.listLimit}`)
  }
  return value
}

function cloneTask(task, { includeHistory = false } = {}) {
  const snapshot = {
    id: task.id,
    title: task.title,
    status: task.status,
    goal: task.goal,
    workspace: task.workspace,
    summary: task.summary,
    plan: [...task.plan],
    nextSteps: [...task.nextSteps],
    references: [...task.references],
    revision: task.revision,
    createdAt: task.createdAt,
    updatedAt: task.updatedAt,
    completedAt: task.completedAt,
    checkpointCount: task.checkpoints.length,
  }
  if (includeHistory) {
    snapshot.checkpoints = task.checkpoints.map(item => ({
      revision: item.revision,
      at: item.at,
      summary: item.summary,
      nextSteps: [...item.nextSteps],
      references: [...item.references],
    }))
  }
  return snapshot
}

function listProjection(task) {
  const summaryPreview = task.summary.length <= 240 ? task.summary : `${task.summary.slice(0, 237)}...`
  return {
    id: task.id,
    title: task.title,
    status: task.status,
    workspace: task.workspace,
    summaryPreview,
    nextSteps: task.nextSteps.slice(0, 3),
    revision: task.revision,
    updatedAt: task.updatedAt,
    checkpointCount: task.checkpoints.length,
  }
}

export class TaskStore {
  constructor(table, { now = () => Date.now(), idFactory = () => `task_${randomUUID()}` } = {}) {
    if (!table || typeof table.get !== 'function' || typeof table.put !== 'function' || typeof table.update !== 'function') {
      throw new TypeError('TaskStore requires a DSH storage-domain table handle')
    }
    this.table = table
    this.now = now
    this.idFactory = idFactory
  }

  timestamp(previous) {
    const floor = previous === undefined ? 0 : Date.parse(previous) + 1
    return new Date(Math.max(this.now(), Number.isFinite(floor) ? floor : 0)).toISOString()
  }

  requireTask(id) {
    const checkedId = taskId(id)
    const task = this.table.get(checkedId)
    if (task === undefined) throw new TaskStateError('not_found', `task ${JSON.stringify(id)} does not exist`)
    return task
  }

  assertRevision(task, ifRevision) {
    const expected = revision(ifRevision)
    if (task.revision !== expected) {
      throw new TaskStateError(
        'revision_conflict',
        `task ${JSON.stringify(task.id)} is at revision ${task.revision}; reload it before writing`,
      )
    }
  }

  async create(input) {
    const title = text(input.title, 'title', { required: true, max: LIMITS.title })
    const goal = text(input.goal, 'goal', { max: LIMITS.goal }) ?? ''
    const workspace = text(input.workspace, 'workspace', { max: LIMITS.workspace }) ?? ''
    const summary = text(input.summary, 'summary', { max: LIMITS.summary }) ?? ''
    const plan = textArray(input.plan, 'plan', { maxItems: LIMITS.planItems, maxItem: LIMITS.planItem }) ?? []
    const nextSteps = textArray(input.nextSteps, 'next_steps', { maxItems: LIMITS.nextSteps, maxItem: LIMITS.nextStep }) ?? []
    const references = textArray(input.references, 'references', { maxItems: LIMITS.references, maxItem: LIMITS.reference }) ?? []

    let id
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const candidate = this.idFactory()
      if (typeof candidate !== 'string' || !/^task_[A-Za-z0-9_-]+$/.test(candidate)) {
        throw new TaskStateError('invalid_id_factory', 'generated task id is not storage-safe')
      }
      if (this.table.get(candidate) === undefined) {
        id = candidate
        break
      }
    }
    if (id === undefined) throw new TaskStateError('id_collision', 'could not allocate a unique task id')

    const createdAt = this.timestamp()
    const task = {
      id,
      title,
      status: 'active',
      goal,
      workspace,
      summary,
      plan,
      nextSteps,
      references,
      revision: 1,
      createdAt,
      updatedAt: createdAt,
      completedAt: null,
      checkpoints: [],
    }
    await this.table.put(id, task)
    return cloneTask(task)
  }

  get(input) {
    const task = this.requireTask(input.id)
    if (input.includeHistory !== undefined && typeof input.includeHistory !== 'boolean') {
      throw new TaskStateError('invalid_request', 'include_history must be a boolean')
    }
    return cloneTask(task, { includeHistory: input.includeHistory === true })
  }

  list(input) {
    const status = input.status
    if (status !== undefined && !TASK_STATUS.includes(status)) {
      throw new TaskStateError('invalid_request', `status must be one of: ${TASK_STATUS.join(', ')}`)
    }
    const query = text(input.query, 'query', { max: LIMITS.query })?.trim().toLowerCase() ?? ''
    const limit = listLimit(input.limit)
    const matches = []
    for (const [, task] of this.table.entries()) {
      if (status !== undefined && task.status !== status) continue
      if (query) {
        const haystack = [
          task.id,
          task.title,
          task.goal,
          task.workspace,
          task.summary,
          ...task.plan,
          ...task.nextSteps,
          ...task.references,
        ].join('\n').toLowerCase()
        if (!haystack.includes(query)) continue
      }
      matches.push(task)
    }
    matches.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt) || a.id.localeCompare(b.id))
    return {
      tasks: matches.slice(0, limit).map(listProjection),
      count: Math.min(matches.length, limit),
      totalMatches: matches.length,
      truncated: matches.length > limit,
    }
  }

  async update(input) {
    const id = taskId(input.id)
    const expected = revision(input.ifRevision)
    const patch = {}
    if (own(input, 'title')) patch.title = text(input.title, 'title', { required: true, max: LIMITS.title })
    if (own(input, 'goal')) patch.goal = text(input.goal, 'goal', { max: LIMITS.goal })
    if (own(input, 'workspace')) patch.workspace = text(input.workspace, 'workspace', { max: LIMITS.workspace })
    if (own(input, 'summary')) patch.summary = text(input.summary, 'summary', { max: LIMITS.summary })
    if (own(input, 'plan')) patch.plan = textArray(input.plan, 'plan', { maxItems: LIMITS.planItems, maxItem: LIMITS.planItem })
    if (own(input, 'nextSteps')) patch.nextSteps = textArray(input.nextSteps, 'next_steps', { maxItems: LIMITS.nextSteps, maxItem: LIMITS.nextStep })
    if (own(input, 'references')) patch.references = textArray(input.references, 'references', { maxItems: LIMITS.references, maxItem: LIMITS.reference })
    if (Object.keys(patch).length === 0) throw new TaskStateError('invalid_request', 'update requires at least one mutable field')

    const current = this.requireTask(id)
    this.assertRevision(current, expected)
    const next = await this.table.update(id, task => {
      this.assertRevision(task, expected)
      return {
        ...task,
        ...patch,
        revision: task.revision + 1,
        updatedAt: this.timestamp(task.updatedAt),
      }
    })
    return cloneTask(next)
  }

  async checkpoint(input) {
    const id = taskId(input.id)
    const expected = revision(input.ifRevision)
    const summary = text(input.summary, 'summary', { required: true, max: LIMITS.summary })
    const nextSteps = textArray(input.nextSteps, 'next_steps', { maxItems: LIMITS.nextSteps, maxItem: LIMITS.nextStep }) ?? []
    const references = textArray(input.references, 'references', { maxItems: LIMITS.references, maxItem: LIMITS.reference }) ?? []
    const plan = own(input, 'plan')
      ? textArray(input.plan, 'plan', { maxItems: LIMITS.planItems, maxItem: LIMITS.planItem })
      : undefined

    const current = this.requireTask(id)
    this.assertRevision(current, expected)
    const next = await this.table.update(id, task => {
      this.assertRevision(task, expected)
      const nextRevision = task.revision + 1
      const at = this.timestamp(task.updatedAt)
      const checkpoint = { revision: nextRevision, at, summary, nextSteps, references }
      return {
        ...task,
        summary,
        nextSteps,
        references,
        ...(plan === undefined ? {} : { plan }),
        checkpoints: [...task.checkpoints, checkpoint].slice(-MAX_CHECKPOINTS),
        revision: nextRevision,
        updatedAt: at,
      }
    })
    return cloneTask(next)
  }

  async transition(input, target) {
    const id = taskId(input.id)
    const expected = revision(input.ifRevision)
    const current = this.requireTask(id)
    this.assertRevision(current, expected)

    if (target === 'active' && current.status === 'completed') {
      throw new TaskStateError('invalid_transition', 'a completed task cannot be resumed')
    }
    if (target === 'paused' && current.status === 'completed') {
      throw new TaskStateError('invalid_transition', 'a completed task cannot be paused')
    }
    if (current.status === target) return cloneTask(current)

    const next = await this.table.update(id, task => {
      this.assertRevision(task, expected)
      if (target === 'active' && task.status === 'completed') {
        throw new TaskStateError('invalid_transition', 'a completed task cannot be resumed')
      }
      if (target === 'paused' && task.status === 'completed') {
        throw new TaskStateError('invalid_transition', 'a completed task cannot be paused')
      }
      const updatedAt = this.timestamp(task.updatedAt)
      return {
        ...task,
        status: target,
        revision: task.revision + 1,
        updatedAt,
        completedAt: target === 'completed' ? updatedAt : null,
      }
    })
    return cloneTask(next)
  }
}
