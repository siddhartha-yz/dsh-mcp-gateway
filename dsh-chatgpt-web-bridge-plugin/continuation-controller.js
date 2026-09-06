export const AUTO_CONTINUE_MESSAGE_PREFIX = 'Continue the current DSH task from its latest task_state.'

const HEALTH_TTL_MS = 15_000

export class ContinuationControllerError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'ContinuationControllerError'
    this.code = code
  }
}

function requireTaskId(value) {
  if (typeof value !== 'string' || !/^task_[A-Za-z0-9_-]+$/.test(value)) {
    throw new ContinuationControllerError('invalid_request', 'task_id must be a valid task_state id')
  }
  return value
}

function requireConversationId(value) {
  if (typeof value !== 'string' || value.length < 1 || value.length > 512) {
    throw new ContinuationControllerError('not_ready', 'observer conversation id is unavailable')
  }
  return value
}

function turnKeyOf(event) {
  const value = event?.payload?.turnKey
  return typeof value === 'string' && value.length > 0 && value.length <= 256 ? value : null
}

export function continuationMessage(taskId) {
  return [
    `${AUTO_CONTINUE_MESSAGE_PREFIX} Task id: ${taskId}.`,
    'Keep working toward the stored goal; do not stop merely to report progress.',
    'Before ending this turn, update task_state: mark it completed if the goal is achieved, pause it if genuine human input is required, otherwise checkpoint it while leaving it active for the next continuation.',
  ].join(' ')
}

export class ContinuationController {
  constructor({ store, taskReader, now = () => Date.now(), healthTtlMs = HEALTH_TTL_MS } = {}) {
    if (!store || typeof store.status !== 'function' || typeof store.enqueue !== 'function') {
      throw new TypeError('ContinuationController requires a bridge store')
    }
    if (!taskReader || typeof taskReader.get !== 'function') {
      throw new TypeError('ContinuationController requires a read-only task_state service')
    }
    this.store = store
    this.taskReader = taskReader
    this.now = now
    this.healthTtlMs = healthTtlMs
    this.state = {
      enabled: false,
      taskId: null,
      conversationId: null,
      armedAt: null,
      continuationCount: 0,
      lastTaskRevision: null,
      lastObservedTurnKey: null,
      lastContinuedTurnKey: null,
      lastDecision: 'disabled',
      lastDecisionAt: null,
      stopReason: null,
    }
  }

  status() {
    return { ...this.state }
  }

  health() {
    const snapshot = this.store.status()
    const now = this.now()
    const companionOnline = typeof snapshot.companion?.lastSeenAt === 'number' && now - snapshot.companion.lastSeenAt <= this.healthTtlMs
    const observerOnline = typeof snapshot.observer?.lastSeenAt === 'number' && now - snapshot.observer.lastSeenAt <= this.healthTtlMs
    return { snapshot, now, companionOnline, observerOnline }
  }

  configure({ enabled, taskId = null } = {}) {
    if (enabled !== true && enabled !== false) {
      throw new ContinuationControllerError('invalid_request', 'enabled must be a boolean')
    }
    if (!enabled) {
      this.stop('disabled_by_user')
      return this.status()
    }

    const checkedTaskId = requireTaskId(taskId)
    let task
    try {
      task = this.taskReader.get(checkedTaskId)
    } catch {
      throw new ContinuationControllerError('task_unavailable', `task ${checkedTaskId} is unavailable`)
    }
    if (task?.status !== 'active') {
      throw new ContinuationControllerError(
        'task_not_active',
        `task ${checkedTaskId} is ${String(task?.status ?? 'unavailable')}; only active tasks can be armed`,
      )
    }

    if (!Number.isInteger(task.revision) || task.revision < 0) {
      throw new ContinuationControllerError('task_invalid', 'task_state revision is unavailable')
    }

    const { snapshot, now, companionOnline, observerOnline } = this.health()
    if (!companionOnline) throw new ContinuationControllerError('not_ready', 'ChatGPT companion is not healthy')
    if (!observerOnline) throw new ContinuationControllerError('not_ready', 'ChatGPT read observer is not healthy')
    if (snapshot.observer?.lastEventType === 'bridge_degraded' || snapshot.observer?.lastEventType === 'error') {
      throw new ContinuationControllerError('not_ready', 'ChatGPT read observer is degraded')
    }
    const conversationId = requireConversationId(snapshot.observer?.conversationId)
    const activeMessages = Array.isArray(snapshot.activeMessages) ? snapshot.activeMessages : []
    if (activeMessages.length > 0) {
      throw new ContinuationControllerError('queue_not_empty', 'bridge outbound queue is not empty')
    }

    this.state = {
      enabled: true,
      taskId: checkedTaskId,
      conversationId,
      armedAt: now,
      continuationCount: 0,
      lastTaskRevision: task.revision,
      lastObservedTurnKey: null,
      lastContinuedTurnKey: null,
      lastDecision: 'armed',
      lastDecisionAt: now,
      stopReason: null,
    }
    return this.status()
  }

  stop(reason) {
    const now = this.now()
    this.state.enabled = false
    this.state.lastDecision = 'stopped'
    this.state.lastDecisionAt = now
    this.state.stopReason = reason
    return this.status()
  }

  async onObserverEvent(event) {
    if (!this.state.enabled) return this.status()
    if (!event || event.source !== 'observer') return this.status()

    const eventConversation = event.payload?.conversationId
    if (eventConversation !== this.state.conversationId) {
      return this.stop('conversation_changed')
    }

    if (event.type === 'bridge_degraded' || event.type === 'error' || event.type === 'blocked') {
      return this.stop(`observer_${event.type}`)
    }
    if (event.type !== 'turn_completed') return this.status()

    const turnKey = turnKeyOf(event)
    if (turnKey === null) return this.stop('turn_identity_missing')
    this.state.lastObservedTurnKey = turnKey
    if (turnKey === this.state.lastContinuedTurnKey) {
      this.state.lastDecision = 'duplicate_turn_ignored'
      this.state.lastDecisionAt = this.now()
      return this.status()
    }

    const { snapshot, companionOnline, observerOnline } = this.health()
    if (!companionOnline) return this.stop('companion_unhealthy')
    if (!observerOnline) return this.stop('observer_unhealthy')
    if (snapshot.observer?.lastEventType === 'bridge_degraded' || snapshot.observer?.lastEventType === 'error') {
      return this.stop('observer_degraded')
    }
    const activeMessages = Array.isArray(snapshot.activeMessages) ? snapshot.activeMessages : []
    if (activeMessages.length > 0) return this.stop('outbound_queue_not_empty')

    let task
    try {
      task = this.taskReader.get(this.state.taskId)
    } catch {
      return this.stop('task_unavailable')
    }
    if (task?.status === 'completed') return this.stop('task_completed')
    if (task?.status === 'paused') return this.stop('task_paused')
    if (task?.status !== 'active') return this.stop('task_not_active')
    if (!Number.isInteger(task.revision) || task.revision <= this.state.lastTaskRevision) {
      return this.stop('task_state_not_advanced')
    }
    this.state.lastTaskRevision = task.revision

    try {
      this.store.enqueue(continuationMessage(this.state.taskId), 'auto-continue')
    } catch {
      return this.stop('enqueue_failed')
    }
    this.state.lastContinuedTurnKey = turnKey
    this.state.continuationCount += 1
    this.state.lastDecision = 'continuation_enqueued'
    this.state.lastDecisionAt = this.now()
    this.state.stopReason = null
    return this.status()
  }
}
