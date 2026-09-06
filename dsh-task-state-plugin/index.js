import { MAX_CHECKPOINTS, parseStoredTask, TaskStateError, TaskStore } from './task-store.js'

export const name = 'dsh-chatgpt-task-state'
export const inject = ['storageDomain', 'tools']
export const TASK_STATE_READ_SERVICE = 'chatgptTaskStateRead'

// storageDomain only requires a valueSchema.parse() contract at runtime. Keeping
// this tiny validator local avoids making a repository-local plugin depend on
// packages resolved from the DSH installation tree.
export const taskDomainSpec = Object.freeze({
  name: 'chatgpt_tasks',
  version: 1,
  layout: 'per-record',
  tables: Object.freeze({
    tasks: Object.freeze({
      valueSchema: Object.freeze({ parse: parseStoredTask }),
    }),
  }),
})

const ACTION_FIELDS = Object.freeze({
  create: Object.freeze(['action', 'title', 'goal', 'workspace', 'summary', 'plan', 'next_steps', 'references']),
  get: Object.freeze(['action', 'id', 'include_history']),
  list: Object.freeze(['action', 'query', 'status', 'limit']),
  update: Object.freeze(['action', 'id', 'if_revision', 'title', 'goal', 'workspace', 'summary', 'plan', 'next_steps', 'references']),
  checkpoint: Object.freeze(['action', 'id', 'if_revision', 'summary', 'plan', 'next_steps', 'references']),
  pause: Object.freeze(['action', 'id', 'if_revision']),
  resume: Object.freeze(['action', 'id', 'if_revision']),
  complete: Object.freeze(['action', 'id', 'if_revision']),
})

const ACTION_REQUIRED_FIELDS = Object.freeze({
  create: Object.freeze(['title']),
  get: Object.freeze(['id']),
  list: Object.freeze([]),
  update: Object.freeze(['id', 'if_revision']),
  checkpoint: Object.freeze(['id', 'if_revision', 'summary']),
  pause: Object.freeze(['id', 'if_revision']),
  resume: Object.freeze(['id', 'if_revision']),
  complete: Object.freeze(['id', 'if_revision']),
})

const TASK_STATE_PARAMETERS = Object.freeze({
  type: 'object',
  properties: {
    action: {
      type: 'string',
      enum: ['create', 'get', 'list', 'update', 'checkpoint', 'pause', 'resume', 'complete'],
      description: 'Task-state operation to perform.',
    },
    id: {
      type: 'string',
      description: 'Task id returned by create. Required for get/update/checkpoint/pause/resume/complete.',
    },
    if_revision: {
      type: 'integer',
      description: 'Latest task revision. Required for every mutation of an existing task.',
    },
    title: {
      type: 'string',
      description: 'Short task title. Required for create; optional for update.',
    },
    goal: {
      type: 'string',
      description: 'Passive goal text. Stored as data only; never drives an execution loop.',
    },
    workspace: {
      type: 'string',
      description: 'Workspace/repository path or other engineering scope identifier.',
    },
    summary: {
      type: 'string',
      description: 'Current compact engineering-state summary. Required for checkpoint.',
    },
    plan: {
      type: 'array',
      items: { type: 'string' },
      description: 'Passive plan items. They describe state; DSH never executes them.',
    },
    next_steps: {
      type: 'array',
      items: { type: 'string' },
      description: 'Candidate next steps for a later ChatGPT conversation.',
    },
    references: {
      type: 'array',
      items: { type: 'string' },
      description: 'Relevant commit ids, files, issue ids, URLs, or other compact references.',
    },
    query: {
      type: 'string',
      description: 'Case-insensitive substring filter for list across current task state.',
    },
    status: {
      type: 'string',
      enum: ['active', 'paused', 'completed'],
      description: 'Optional status filter for list.',
    },
    limit: {
      type: 'integer',
      minimum: 1,
      maximum: 100,
      description: 'Maximum list results. Defaults to 20.',
    },
    include_history: {
      type: 'boolean',
      description: `For get only: include up to ${MAX_CHECKPOINTS} prior checkpoints. Defaults to false for compact resume context.`,
    },
  },
  required: ['action'],
  additionalProperties: false,
})

function hasOwn(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key)
}

export function validateTaskStateArguments(args) {
  if (args === null || typeof args !== 'object' || Array.isArray(args)) {
    throw new TaskStateError('invalid_request', 'arguments must be an object')
  }
  const action = args.action
  if (typeof action !== 'string' || !hasOwn(ACTION_FIELDS, action)) {
    throw new TaskStateError('invalid_request', 'action must be one of: create, get, list, update, checkpoint, pause, resume, complete')
  }
  const allowed = new Set(ACTION_FIELDS[action])
  for (const field of Object.keys(args)) {
    if (!allowed.has(field)) {
      throw new TaskStateError('invalid_request', `${field} is not valid for action ${action}`)
    }
  }
  for (const field of ACTION_REQUIRED_FIELDS[action]) {
    if (!hasOwn(args, field)) {
      throw new TaskStateError('invalid_request', `${field} is required for action ${action}`)
    }
  }
  return args
}

function copyIfPresent(source, target, sourceKey, targetKey = sourceKey) {
  if (Object.prototype.hasOwnProperty.call(source, sourceKey)) target[targetKey] = source[sourceKey]
}

function normalizedInput(args) {
  const input = {}
  copyIfPresent(args, input, 'id')
  copyIfPresent(args, input, 'if_revision', 'ifRevision')
  copyIfPresent(args, input, 'title')
  copyIfPresent(args, input, 'goal')
  copyIfPresent(args, input, 'workspace')
  copyIfPresent(args, input, 'summary')
  copyIfPresent(args, input, 'plan')
  copyIfPresent(args, input, 'next_steps', 'nextSteps')
  copyIfPresent(args, input, 'references')
  copyIfPresent(args, input, 'query')
  copyIfPresent(args, input, 'status')
  copyIfPresent(args, input, 'limit')
  copyIfPresent(args, input, 'include_history', 'includeHistory')
  return input
}

export function createTaskStateTool(store) {
  return {
    name: 'task_state',
    description: [
      'Persist and restore passive engineering-task state across ChatGPT conversations.',
      'This tool stores checkpoints only; it never chooses actions, invokes a model, or continues work by itself.',
      'Actions: create, get, list, update, checkpoint, pause, resume, complete.',
      'Mutating an existing task requires if_revision from the latest get/checkpoint result to prevent stale overwrites.',
      'resume only marks a paused task active; ChatGPT still decides and performs every next action.',
    ].join(' '),
    parameters: TASK_STATE_PARAMETERS,
    output: {
      schema: {},
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const validated = validateTaskStateArguments(args)
      const input = normalizedInput(validated)
      switch (validated.action) {
        case 'create':
          return await store.create(input)
        case 'get':
          return store.get(input)
        case 'list':
          return store.list(input)
        case 'update':
          return await store.update(input)
        case 'checkpoint':
          return await store.checkpoint(input)
        case 'pause':
          return await store.transition(input, 'paused')
        case 'resume':
          return await store.transition(input, 'active')
        case 'complete':
          return await store.transition(input, 'completed')
        default:
          throw new Error(`unsupported task_state action: ${String(args.action)}`)
      }
    },
  }
}

export async function apply(ctx) {
  const domain = await ctx.storageDomain.open(taskDomainSpec)
  ctx.effect(() => () => domain.close(), 'chatgpt-task-state.domain-close')

  const store = new TaskStore(domain.table('tasks'))
  ctx.provide(TASK_STATE_READ_SERVICE, Object.freeze({
    get: (id) => store.get({ id, includeHistory: false }),
  }))
  ctx.effect(() => ctx.tools.register(createTaskStateTool(store)), 'chatgpt-task-state.tool')
}
