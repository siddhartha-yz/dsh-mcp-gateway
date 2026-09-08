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

function requireMaxContinuations(value) {
  if (value === null || value === undefined) return null
  if (!Number.isInteger(value) || value < 1 || value > 100) {
    throw new ContinuationControllerError('invalid_request', 'max_continuations must be an integer from 1 to 100 or null')
  }
  return value
}

function turnKeyOf(event) {
  const value = event?.payload?.turnKey
  return typeof value === 'string' && value.length > 0 && value.length <= 256 ? value : null
}

export function seedMessage(taskId) {
  return [
    `Begin or resume the current DSH task from its latest task_state. Task id: ${taskId}.`,
    'This is the explicit seed turn started by the DSH Bridge Arm & start action; do not count this seed as an automatic continuation.',
    'Before ending this turn, update task_state exactly once: mark it completed if the goal is achieved, pause it if genuine human input is required, otherwise checkpoint it while leaving it active.',
  ].join(' ')
}

export function continuationMessage(taskId, { index = null, maxContinuations = null } = {}) {
  const parts = [
    `${AUTO_CONTINUE_MESSAGE_PREFIX} Task id: ${taskId}.`,
    'Keep working toward the stored goal; do not stop merely to report progress.',
  ]
  if (Number.isInteger(index) && Number.isInteger(maxContinuations) && index === maxContinuations) {
    parts.push(
      `This is the final controller-capped continuation (${index} of ${maxContinuations}).`,
      'Before ending this turn, checkpoint task_state while leaving it active rather than marking it completed; the controller will stop mechanically after it verifies the checkpoint revision advanced. Pause only if genuine human input is required.',
    )
  } else {
    parts.push(
      'Before ending this turn, update task_state: mark it completed if the goal is achieved, pause it if genuine human input is required, otherwise checkpoint it while leaving it active for the next continuation.',
    )
  }
  return parts.join(' ')
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
      observerId: null,
      conversationId: null,
      hostId: null,
      armedAt: null,
      continuationCount: 0,
      maxContinuations: null,
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

  health(observerId = null, hostId = null) {
    const snapshot = this.store.status()
    const now = this.now()
    const companion = hostId === null ? snapshot.companion : this.store.companionForHost?.(hostId)
    const companionOnline = typeof companion?.lastSeenAt === 'number' && now - companion.lastSeenAt <= this.healthTtlMs
    const observer = observerId === null ? snapshot.observer : this.store.observerStatus?.(observerId)
    const observerOnline = typeof observer?.lastSeenAt === 'number' && now - observer.lastSeenAt <= this.healthTtlMs
    return { snapshot, companion, observer, now, companionOnline, observerOnline }
  }

  configure({ enabled, taskId = null, conversationId = null, start = false, maxContinuations = null } = {}) {
    if (enabled !== true && enabled !== false) {
      throw new ContinuationControllerError('invalid_request', 'enabled must be a boolean')
    }
    if (!enabled) {
      this.stop('disabled_by_user')
      return this.status()
    }

    const checkedTaskId = requireTaskId(taskId)
    const checkedMaxContinuations = requireMaxContinuations(maxContinuations)
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

    const checkedConversationId = requireConversationId(conversationId)
    const candidate = this.store.observerForConversation?.(checkedConversationId)
    if (!candidate) throw new ContinuationControllerError('not_ready', 'target ChatGPT conversation observer is unavailable')
    const hostId = typeof candidate.hostId === 'string' && candidate.hostId !== '' ? candidate.hostId : null
    const { snapshot, observer, now, companionOnline, observerOnline } = this.health(candidate.observerId, hostId)
    if (!companionOnline) throw new ContinuationControllerError('not_ready', 'ChatGPT companion is not healthy')
    if (!observerOnline) throw new ContinuationControllerError('not_ready', 'target ChatGPT read observer is not healthy')
    if (observer?.lastEventType === 'bridge_degraded' || observer?.lastEventType === 'error' || observer?.lastEventType === 'blocked') {
      throw new ContinuationControllerError('not_ready', 'target ChatGPT read observer is degraded')
    }
    const observerId = requireConversationId(observer?.observerId)
    const activeMessages = Array.isArray(snapshot.activeMessages) ? snapshot.activeMessages : []
    if (activeMessages.length > 0) {
      throw new ContinuationControllerError('queue_not_empty', 'bridge outbound queue is not empty')
    }

    this.state = {
      enabled: true,
      taskId: checkedTaskId,
      observerId,
      conversationId: checkedConversationId,
      hostId,
      armedAt: now,
      continuationCount: 0,
      maxContinuations: checkedMaxContinuations,
      lastTaskRevision: task.revision,
      lastObservedTurnKey: null,
      lastContinuedTurnKey: null,
      lastDecision: start ? 'seed_enqueued' : 'armed',
      lastDecisionAt: now,
      stopReason: null,
    }
    if (start) {
      try {
        this.store.enqueue(seedMessage(checkedTaskId), 'auto-seed', { targetHostId: hostId })
      } catch {
        this.state.enabled = false
        this.state.lastDecision = 'stopped'
        this.state.stopReason = 'seed_enqueue_failed'
        throw new ContinuationControllerError('enqueue_failed', 'failed to enqueue the explicit seed turn')
      }
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
      if (event.observerId === this.state.observerId) return this.stop('conversation_changed')
      return this.status()
    }

    if (event.observerId !== this.state.observerId) {
      const boundHealth = this.health(this.state.observerId, this.state.hostId)
      const candidateHostId = typeof event.payload?.hostId === 'string' && event.payload.hostId !== '' ? event.payload.hostId : null
      if (this.state.hostId !== null && candidateHostId !== this.state.hostId) return this.status()
      const candidateHealth = this.health(event.observerId, this.state.hostId)
      const candidate = candidateHealth.observer
      const candidateDegraded = candidate?.lastEventType === 'bridge_degraded'
        || candidate?.lastEventType === 'error'
        || candidate?.lastEventType === 'blocked'
      if (!candidateHealth.observerOnline || candidateDegraded) return this.status()

      const bound = boundHealth.observer
      const candidateAfterArm = typeof event.ts === 'number' && event.ts >= this.state.armedAt
      const boundHasPostArmLifecycle = typeof bound?.lastEventAt === 'number' && bound.lastEventAt > this.state.armedAt
      const startTakeover = event.type === 'turn_started'
        && candidateAfterArm
        && !boundHasPostArmLifecycle
      const completionTakeover = event.type === 'turn_completed'
        && bound?.lastEventType === 'turn_started'
        && typeof bound.lastEventAt === 'number'
        && candidateAfterArm
        && event.ts >= bound.lastEventAt

      if (boundHealth.observerOnline && !startTakeover && !completionTakeover) return this.status()

      this.state.observerId = event.observerId
      this.state.lastDecision = startTakeover
        ? 'observer_turn_rebound'
        : completionTakeover
          ? 'observer_completion_rebound'
          : 'observer_rebound'
      this.state.lastDecisionAt = this.now()
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

    const { snapshot, observer, companionOnline, observerOnline } = this.health(this.state.observerId, this.state.hostId)
    if (!companionOnline) return this.stop('companion_unhealthy')
    if (!observerOnline) return this.stop('observer_unhealthy')
    if (observer?.lastEventType === 'bridge_degraded' || observer?.lastEventType === 'error') {
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

    if (Number.isInteger(this.state.maxContinuations) && this.state.continuationCount >= this.state.maxContinuations) {
      return this.stop('max_continuations_reached')
    }

    const nextContinuation = this.state.continuationCount + 1
    try {
      this.store.enqueue(continuationMessage(this.state.taskId, {
        index: nextContinuation,
        maxContinuations: this.state.maxContinuations,
      }), 'auto-continue', { targetHostId: this.state.hostId })
    } catch {
      return this.stop('enqueue_failed')
    }
    this.state.lastContinuedTurnKey = turnKey
    this.state.continuationCount = nextContinuation
    this.state.lastDecision = 'continuation_enqueued'
    this.state.lastDecisionAt = this.now()
    this.state.stopReason = null
    return this.status()
  }
}
