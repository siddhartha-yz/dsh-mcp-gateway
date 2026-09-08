import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  BRIDGE_BASE_PATH,
  BRIDGE_VERSION,
  BridgeError,
  ChatGPTWebBridgeStore,
  createBridgeTool,
  inject,
  name,
} from '../dsh-chatgpt-web-bridge-plugin/index.js'
import {
  ContinuationController,
  ContinuationControllerError,
  continuationMessage,
  seedMessage,
} from '../dsh-chatgpt-web-bridge-plugin/continuation-controller.js'

const clientPath = new URL('../dsh-chatgpt-web-bridge-plugin/lib/client.js', import.meta.url)
const packagePath = new URL('../dsh-chatgpt-web-bridge-plugin/package.json', import.meta.url)
const extensionRoot = new URL('../browser-extension/dsh-chatgpt-web-observer/', import.meta.url)

test('bridge store leases, begins fail-closed dispatch, acknowledges, and reports messages', () => {
  let now = 1_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const queued = store.enqueue('continue current task')
  assert.equal(queued.status, 'pending')

  const leased = store.poll('companion-a')
  assert.equal(leased.message.id, queued.id)
  assert.equal(leased.message.status, 'claimed')
  assert.equal(leased.message.claimedBy, 'companion-a')
  assert.equal(leased.message.attempts, 1)

  assert.throws(
    () => store.beginSend({ clientId: 'companion-b', messageId: queued.id }),
    (error) => error instanceof BridgeError && error.code === 'claim_mismatch',
  )
  assert.throws(
    () => store.ack({ clientId: 'companion-a', messageId: queued.id, outcome: 'sent' }),
    (error) => error instanceof BridgeError && error.code === 'dispatch_mismatch',
  )

  now += 50
  const dispatching = store.beginSend({ clientId: 'companion-a', messageId: queued.id })
  assert.equal(dispatching.message.status, 'dispatching')
  assert.equal(dispatching.message.claimExpiresAt, null)
  assert.equal(store.status().counts.dispatching, 1)

  assert.throws(
    () => store.ack({ clientId: 'companion-b', messageId: queued.id, outcome: 'sent' }),
    (error) => error instanceof BridgeError && error.code === 'dispatch_mismatch',
  )

  now += 50
  const settled = store.ack({ clientId: 'companion-a', messageId: queued.id, outcome: 'sent' })
  assert.equal(settled.message.status, 'sent')
  assert.equal(store.status().counts.sent, 1)
  assert.equal(store.status().counts.dispatching, 0)
  assert.equal(store.status().counts.pending, 0)
})

test('host-targeted bridge messages can only be claimed by a companion from that browser host', () => {
  const store = new ChatGPTWebBridgeStore()
  store.heartbeat('companion-a', 'host-a')
  store.heartbeat('companion-b', 'host-b')
  const queued = store.enqueue('isolated only', 'auto-seed', { targetHostId: 'host-b' })

  assert.equal(store.poll('companion-a', 'host-a').message, null)
  const leased = store.poll('companion-b', 'host-b')
  assert.equal(leased.message.id, queued.id)
  assert.equal(leased.message.targetHostId, 'host-b')
})

test('Arm and start seeds the observer browser host and keeps continuations on that host', async () => {
  let now = 8_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_isolated_host', status: 'active', revision: 1 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })

  store.publish({ clientId: 'companion-normal', hostId: 'host-normal', eventType: 'companion_ready', payload: { relay: 'automatic' } })
  store.publish({ clientId: 'companion-isolated', hostId: 'host-isolated', eventType: 'companion_ready', payload: { relay: 'automatic' } })
  store.publishObserver({
    observerId: 'observer-isolated', eventType: 'observer_ready',
    payload: { conversationId: '/c/isolated', hostId: 'host-isolated' },
  })

  const armed = controller.configure({ enabled: true, start: true, taskId: task.id, conversationId: '/c/isolated' })
  assert.equal(armed.enabled, true)
  assert.equal(armed.hostId, 'host-isolated')
  assert.equal(armed.continuationCount, 0)
  assert.equal(armed.lastDecision, 'seed_enqueued')
  assert.equal(store.poll('companion-normal', 'host-normal').message, null)

  const seed = store.poll('companion-isolated', 'host-isolated').message
  assert.equal(seed.source, 'auto-seed')
  assert.equal(seed.text, seedMessage(task.id))
  assert.equal(seed.targetHostId, 'host-isolated')
  store.beginSend({ clientId: 'companion-isolated', hostId: 'host-isolated', messageId: seed.id })
  store.ack({ clientId: 'companion-isolated', hostId: 'host-isolated', messageId: seed.id, outcome: 'sent' })

  now += 1_000
  task.revision += 1
  const completed = store.publishObserver({
    observerId: 'observer-isolated', eventType: 'turn_completed',
    payload: { conversationId: '/c/isolated', hostId: 'host-isolated', turnKey: 'seed-turn' },
  })
  const continued = await controller.onObserverEvent(completed.event)
  assert.equal(continued.continuationCount, 1)
  const queued = store.status().activeMessages[0]
  assert.equal(queued.source, 'auto-continue')
  assert.equal(queued.targetHostId, 'host-isolated')
  assert.equal(store.poll('companion-normal', 'host-normal').message, null)
  assert.equal(store.poll('companion-isolated', 'host-isolated').message.id, queued.id)
})

test('expired claims return to the queue and can be reclaimed', () => {
  let now = 10_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const queued = store.enqueue('continue')
  assert.equal(store.poll('companion-a').message.id, queued.id)

  now += 31_000
  const reclaimed = store.poll('companion-b')
  assert.equal(reclaimed.message.id, queued.id)
  assert.equal(reclaimed.message.claimedBy, 'companion-b')
  assert.equal(reclaimed.message.attempts, 2)
})

test('dispatching messages never auto-requeue after the claim TTL', () => {
  let now = 50_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const queued = store.enqueue('one turn only')
  store.poll('companion-a')
  const dispatching = store.beginSend({ clientId: 'companion-a', messageId: queued.id })
  assert.equal(dispatching.message.status, 'dispatching')

  now += 31_000
  const otherPoll = store.poll('companion-b')
  assert.equal(otherPoll.message, null)
  const status = store.status()
  assert.equal(status.counts.dispatching, 1)
  assert.equal(status.counts.pending, 0)
  assert.equal(status.activeMessages[0].id, queued.id)
  assert.equal(status.activeMessages[0].attempts, 1)
})

test('companion lifecycle events are bounded and visible in status', () => {
  let now = 20_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const event = store.publish({
    clientId: 'companion-a',
    eventType: 'turn_completed',
    text: 'done',
    payload: { turn: 1 },
  })
  assert.equal(event.event.seq, 1)
  assert.equal(store.status().companion.lastEventType, 'turn_completed')
  assert.equal(store.status().recentEvents.at(-1).payload.turn, 1)
})

test('observer events stay separate from companion lifecycle and heartbeat is history-free', () => {
  let now = 30_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  store.publish({ clientId: 'companion-a', eventType: 'companion_ready', payload: { relay: 'automatic' } })

  now += 10
  const ready = store.publishObserver({
    observerId: 'observer-a',
    eventType: 'observer_ready',
    payload: { conversationId: '/c/example', transport: 'read-only-dom-observer' },
  })
  assert.equal(ready.event.source, 'observer')
  assert.equal(store.status().observer.observerId, 'observer-a')
  assert.equal(store.status().observer.conversationId, '/c/example')
  assert.equal(store.status().companion.clientId, 'companion-a')
  const readyAt = store.status().observer.lastEventAt
  assert.equal(readyAt, now)
  assert.equal(store.status().recentEvents.length, 2)

  now += 5_000
  store.observerHeartbeat({ observerId: 'observer-a', payload: { conversationId: '/c/example' } })
  const status = store.status()
  assert.equal(status.observer.lastSeenAt, now)
  assert.equal(status.observer.lastEventAt, readyAt)
  assert.equal(status.observer.lastEventType, 'observer_ready')
  assert.equal(status.recentEvents.length, 2)
})

test('background heartbeats from another ChatGPT tab do not steal the selected observer', () => {
  let now = 40_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  store.publishObserver({ observerId: 'observer-a', eventType: 'turn_started', payload: { conversationId: '/c/a' } })
  now += 1_000
  const other = store.observerHeartbeat({ observerId: 'observer-b', payload: { conversationId: '/c/b' } })
  assert.equal(other.observer.observerId, 'observer-b')
  assert.equal(other.observer.conversationId, '/c/b')
  const selected = store.status().observer
  assert.equal(selected.observerId, 'observer-a')
  assert.equal(selected.conversationId, '/c/a')
  assert.equal(selected.lastEventType, 'turn_started')
})

test('controller binds an observer identity and ignores lifecycle events from other tabs', async () => {
  let now = 60_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_multitab', status: 'active', revision: 1 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })
  store.heartbeat('companion-a')
  store.publishObserver({ observerId: 'observer-a', eventType: 'turn_started', payload: { conversationId: '/c/a' } })
  const armed = controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/a' })
  assert.equal(armed.observerId, 'observer-a')
  assert.equal(armed.conversationId, '/c/a')

  now += 1_000
  const unrelated = store.publishObserver({
    observerId: 'observer-b', eventType: 'turn_completed', payload: { conversationId: '/c/b', turnKey: 'other-turn' },
  })
  const ignored = await controller.onObserverEvent(unrelated.event)
  assert.equal(ignored.enabled, true)
  assert.equal(ignored.continuationCount, 0)
  assert.equal(store.status().activeMessages.length, 0)

  now += 1_000
  task.revision += 1
  store.observerHeartbeat({ observerId: 'observer-a', payload: { conversationId: '/c/a' } })
  const target = store.publishObserver({
    observerId: 'observer-a', eventType: 'turn_completed', payload: { conversationId: '/c/a', turnKey: 'target-turn' },
  })
  const continued = await controller.onObserverEvent(target.event)
  assert.equal(continued.enabled, true)
  assert.equal(continued.continuationCount, 1)
  assert.equal(store.status().activeMessages.length, 1)
})

test('controller rebinds to a healthy observer of the same conversation only after the bound observer is stale', async () => {
  let now = 70_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_same_conversation_rebind', status: 'active', revision: 1 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })
  store.heartbeat('companion-a')
  store.publishObserver({ observerId: 'observer-a', eventType: 'observer_ready', payload: { conversationId: '/c/shared' } })
  const armed = controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/shared' })
  assert.equal(armed.observerId, 'observer-a')

  now += 500
  const boundStarted = store.publishObserver({
    observerId: 'observer-a', eventType: 'turn_started', payload: { conversationId: '/c/shared' },
  })
  await controller.onObserverEvent(boundStarted.event)
  now += 500
  store.publishObserver({ observerId: 'observer-b', eventType: 'turn_started', payload: { conversationId: '/c/shared' } })
  const whileBoundHealthy = await controller.onObserverEvent(store.status().recentEvents.at(-1))
  assert.equal(whileBoundHealthy.observerId, 'observer-a')
  assert.equal(whileBoundHealthy.lastDecision, 'armed')

  now += 16_000
  store.heartbeat('companion-a')
  task.revision += 1
  const replacement = store.publishObserver({
    observerId: 'observer-b', eventType: 'turn_completed', payload: { conversationId: '/c/shared', turnKey: 'shared-turn-1' },
  })
  const rebound = await controller.onObserverEvent(replacement.event)
  assert.equal(rebound.enabled, true)
  assert.equal(rebound.observerId, 'observer-b')
  assert.equal(rebound.conversationId, '/c/shared')
  assert.equal(rebound.continuationCount, 1)
  assert.equal(rebound.lastContinuedTurnKey, 'shared-turn-1')
  assert.equal(store.status().activeMessages.length, 1)
})

test('controller gives post-arm turn ownership to a same-conversation observer when the armed observer has no post-arm lifecycle', async () => {
  let now = 72_500
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_post_arm_turn_takeover', status: 'active', revision: 2 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })
  store.heartbeat('companion-a')
  store.publishObserver({
    observerId: 'observer-a', eventType: 'turn_completed',
    payload: { conversationId: '/c/shared', turnKey: 'assistant-old' },
  })
  const armed = controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/shared' })
  assert.equal(armed.observerId, 'observer-a')

  now += 1_000
  const started = store.publishObserver({
    observerId: 'observer-b', eventType: 'turn_started',
    payload: { conversationId: '/c/shared', baselineTurnKey: 'assistant-old' },
  })
  const rebound = await controller.onObserverEvent(started.event)
  assert.equal(rebound.enabled, true)
  assert.equal(rebound.observerId, 'observer-b')
  assert.equal(rebound.lastDecision, 'observer_turn_rebound')

  task.revision += 1
  now += 1_000
  const completed = store.publishObserver({
    observerId: 'observer-b', eventType: 'turn_completed',
    payload: { conversationId: '/c/shared', turnKey: 'assistant-new' },
  })
  const continued = await controller.onObserverEvent(completed.event)
  assert.equal(continued.enabled, true)
  assert.equal(continued.observerId, 'observer-b')
  assert.equal(continued.continuationCount, 1)
  assert.equal(continued.lastContinuedTurnKey, 'assistant-new')
  assert.equal(store.status().activeMessages.length, 1)
})

test('controller accepts same-conversation completion takeover when bound observer is lifecycle-stuck', async () => {
  let now = 75_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_completion_takeover', status: 'active', revision: 1 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })
  store.heartbeat('companion-a')
  store.publishObserver({ observerId: 'observer-a', eventType: 'observer_ready', payload: { conversationId: '/c/shared' } })
  controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/shared' })

  now += 1_000
  const started = store.publishObserver({
    observerId: 'observer-a', eventType: 'turn_started', payload: { conversationId: '/c/shared', baselineTurnKey: 'assistant-4' },
  })
  await controller.onObserverEvent(started.event)
  const boundEventAt = store.observerStatus('observer-a').lastEventAt

  now += 2_000
  store.observerHeartbeat({ observerId: 'observer-a', payload: { conversationId: '/c/shared' } })
  assert.equal(store.observerStatus('observer-a').lastSeenAt, now)
  assert.equal(store.observerStatus('observer-a').lastEventAt, boundEventAt)
  task.revision += 1

  now += 1_000
  store.publishObserver({
    observerId: 'observer-b', eventType: 'assistant_message', text: 'seed done',
    payload: { conversationId: '/c/shared', turnKey: 'assistant-5' },
  })
  const completed = store.publishObserver({
    observerId: 'observer-b', eventType: 'turn_completed',
    payload: { conversationId: '/c/shared', turnKey: 'assistant-5' },
  })
  const takeover = await controller.onObserverEvent(completed.event)

  assert.equal(takeover.enabled, true)
  assert.equal(takeover.observerId, 'observer-b')
  assert.equal(takeover.conversationId, '/c/shared')
  assert.equal(takeover.continuationCount, 1)
  assert.equal(takeover.lastContinuedTurnKey, 'assistant-5')
  assert.equal(takeover.lastDecision, 'continuation_enqueued')
  assert.equal(store.status().activeMessages.length, 1)
})

test('controller arms the explicit target conversation even when another tab emitted the latest durable event', () => {
  let now = 80_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_arm_target', status: 'active', revision: 3 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })
  store.heartbeat('companion-a')
  store.publishObserver({ observerId: 'observer-target', eventType: 'observer_ready', payload: { conversationId: '/c/target' } })
  now += 1_000
  store.publishObserver({ observerId: 'observer-other', eventType: 'bridge_degraded', payload: { conversationId: '/c/other' } })
  assert.equal(store.status().observer.observerId, 'observer-other')

  const armed = controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/target' })
  assert.equal(armed.enabled, true)
  assert.equal(armed.observerId, 'observer-target')
  assert.equal(armed.conversationId, '/c/target')
})

test('controller refuses an unavailable or degraded explicit target conversation', () => {
  let now = 90_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_arm_fail_closed', status: 'active', revision: 1 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })
  store.heartbeat('companion-a')
  assert.throws(
    () => controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/missing' }),
    (error) => error instanceof ContinuationControllerError && error.code === 'not_ready',
  )
  store.publishObserver({ observerId: 'observer-bad', eventType: 'bridge_degraded', payload: { conversationId: '/c/bad' } })
  assert.throws(
    () => controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/bad' }),
    (error) => error instanceof ContinuationControllerError && error.code === 'not_ready',
  )
})

test('observer accepts completion and fail-closed degradation events', () => {
  const store = new ChatGPTWebBridgeStore()
  store.publishObserver({
    observerId: 'observer-a',
    eventType: 'assistant_message',
    text: 'finished answer',
    payload: { conversationId: '/c/example', turnKey: 'conversation-turn-4' },
  })
  store.publishObserver({
    observerId: 'observer-a',
    eventType: 'turn_completed',
    payload: { conversationId: '/c/example', turnKey: 'conversation-turn-4' },
  })
  store.publishObserver({
    observerId: 'observer-a',
    eventType: 'bridge_degraded',
    text: 'selector drift',
    payload: { conversationId: '/c/example', reason: 'assistant_message_missing_after_run' },
  })
  const status = store.status()
  assert.deepEqual(status.recentEvents.slice(-3).map((event) => event.type), ['assistant_message', 'turn_completed', 'bridge_degraded'])
  assert.equal(status.observer.lastEventType, 'bridge_degraded')
})

test('observer rejects malformed identity and payloads', () => {
  const store = new ChatGPTWebBridgeStore()
  assert.throws(
    () => store.publishObserver({ observerId: '', eventType: 'observer_ready', payload: { conversationId: '/c/example' } }),
    (error) => error instanceof BridgeError && error.code === 'invalid_request',
  )
  assert.throws(
    () => store.publishObserver({ observerId: 'observer-a', eventType: 'unknown', payload: { conversationId: '/c/example' } }),
    (error) => error instanceof BridgeError && error.code === 'invalid_request',
  )
  assert.throws(
    () => store.publishObserver({ observerId: 'observer-a', eventType: 'observer_ready', payload: [] }),
    (error) => error instanceof BridgeError && error.code === 'invalid_request',
  )
})

test('controller can chain three completed turns and stops when task_state becomes completed', async () => {
  let now = 100_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_loop_test', status: 'active', revision: 1 }
  const controller = new ContinuationController({
    store,
    taskReader: { get: (id) => ({ ...task, id }) },
    now: () => now,
  })

  store.publish({ clientId: 'companion-a', eventType: 'companion_ready', payload: { relay: 'automatic' } })
  store.publishObserver({
    observerId: 'observer-a',
    eventType: 'observer_ready',
    payload: { conversationId: '/c/loop-test' },
  })
  controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/loop-test' })

  for (let turn = 1; turn <= 3; turn += 1) {
    now += 1_000
    task.revision += 1
    const observed = store.publishObserver({
      observerId: 'observer-a',
      eventType: 'turn_completed',
      payload: { conversationId: '/c/loop-test', turnKey: `conversation-turn-${turn}` },
    })
    const decision = await controller.onObserverEvent(observed.event)
    assert.equal(decision.enabled, true)
    assert.equal(decision.continuationCount, turn)
    assert.equal(decision.lastContinuedTurnKey, `conversation-turn-${turn}`)

    const leased = store.poll('companion-a')
    assert.equal(leased.message.source, 'auto-continue')
    assert.equal(leased.message.text, continuationMessage(task.id))
    store.beginSend({ clientId: 'companion-a', messageId: leased.message.id })
    store.ack({ clientId: 'companion-a', messageId: leased.message.id, outcome: 'sent' })
    store.observerHeartbeat({ observerId: 'observer-a', payload: { conversationId: '/c/loop-test' } })
    assert.equal(store.status().activeMessages.length, 0)
  }

  task.status = 'completed'
  now += 1_000
  const finalObserved = store.publishObserver({
    observerId: 'observer-a',
    eventType: 'turn_completed',
    payload: { conversationId: '/c/loop-test', turnKey: 'conversation-turn-4' },
  })
  const stopped = await controller.onObserverEvent(finalObserved.event)
  assert.equal(stopped.enabled, false)
  assert.equal(stopped.stopReason, 'task_completed')
  assert.equal(stopped.continuationCount, 3)
  assert.equal(store.status().activeMessages.length, 0)
})

test('controller ignores duplicate turn completion and fails closed on blockers', async () => {
  let now = 200_000
  const store = new ChatGPTWebBridgeStore({ now: () => now })
  const task = { id: 'task_guard_test', status: 'active', revision: 1 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) }, now: () => now })
  store.heartbeat('companion-a')
  store.publishObserver({ observerId: 'observer-a', eventType: 'observer_ready', payload: { conversationId: '/c/guard' } })
  controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/guard' })

  task.revision += 1
  const first = store.publishObserver({
    observerId: 'observer-a', eventType: 'turn_completed', payload: { conversationId: '/c/guard', turnKey: 'turn-1' },
  })
  await controller.onObserverEvent(first.event)
  const duplicate = store.publishObserver({
    observerId: 'observer-a', eventType: 'turn_completed', payload: { conversationId: '/c/guard', turnKey: 'turn-1' },
  })
  const duplicateDecision = await controller.onObserverEvent(duplicate.event)
  assert.equal(duplicateDecision.enabled, true)
  assert.equal(duplicateDecision.continuationCount, 1)
  assert.equal(duplicateDecision.lastDecision, 'duplicate_turn_ignored')
  assert.equal(store.status().activeMessages.length, 1)

  const leased = store.poll('companion-a')
  store.beginSend({ clientId: 'companion-a', messageId: leased.message.id })
  store.ack({ clientId: 'companion-a', messageId: leased.message.id, outcome: 'sent' })
  const blocked = store.publishObserver({
    observerId: 'observer-a', eventType: 'blocked', payload: { conversationId: '/c/guard' },
  })
  const stopped = await controller.onObserverEvent(blocked.event)
  assert.equal(stopped.enabled, false)
  assert.equal(stopped.stopReason, 'observer_blocked')
})

test('controller can only arm against healthy transport and an active task', () => {
  const store = new ChatGPTWebBridgeStore()
  const controller = new ContinuationController({
    store,
    taskReader: { get: () => ({ id: 'task_arm_test', status: 'active', revision: 1 }) },
  })
  assert.throws(
    () => controller.configure({ enabled: true, taskId: 'task_arm_test', conversationId: '/c/arm' }),
    (error) => error instanceof ContinuationControllerError && error.code === 'not_ready',
  )

  store.heartbeat('companion-a')
  store.publishObserver({ observerId: 'observer-a', eventType: 'observer_ready', payload: { conversationId: '/c/arm' } })
  const armed = controller.configure({ enabled: true, taskId: 'task_arm_test', conversationId: '/c/arm' })
  assert.equal(armed.enabled, true)
  assert.equal(armed.continuationCount, 0)
  assert.equal(armed.conversationId, '/c/arm')
  assert.equal(controller.configure({ enabled: false }).stopReason, 'disabled_by_user')
})

test('controller stops instead of looping when ChatGPT did not advance task_state', async () => {
  const store = new ChatGPTWebBridgeStore()
  const task = { id: 'task_revision_guard', status: 'active', revision: 5 }
  const controller = new ContinuationController({ store, taskReader: { get: () => ({ ...task }) } })
  store.heartbeat('companion-a')
  store.publishObserver({ observerId: 'observer-a', eventType: 'observer_ready', payload: { conversationId: '/c/revision' } })
  controller.configure({ enabled: true, taskId: task.id, conversationId: '/c/revision' })
  const completed = store.publishObserver({
    observerId: 'observer-a', eventType: 'turn_completed', payload: { conversationId: '/c/revision', turnKey: 'turn-1' },
  })
  const stopped = await controller.onObserverEvent(completed.event)
  assert.equal(stopped.enabled, false)
  assert.equal(stopped.stopReason, 'task_state_not_advanced')
  assert.equal(stopped.continuationCount, 0)
  assert.equal(store.status().activeMessages.length, 0)
})

test('bridge tool is mechanical and exposes only transport actions', async () => {
  const store = new ChatGPTWebBridgeStore()
  const tool = createBridgeTool(store)
  assert.equal(tool.name, 'chatgpt_web_bridge')
  assert.deepEqual(tool.parameters.properties.action.enum, ['status', 'poll', 'heartbeat', 'begin_send', 'ack', 'publish'])
  assert.match(tool.description, /does not invoke a model or choose next actions/)
  assert.match(tool.description, /fail-closed against duplicate turns/)
  const status = await tool.execute({ action: 'status' })
  assert.equal(status.version, BRIDGE_VERSION)
})

test('plugin is a DSH web dual-face plugin with a sidebar client surface', async () => {
  assert.equal(name, 'dsh-chatgpt-web-bridge-experiment')
  assert.deepEqual(inject, ['webServer', 'tools', 'chatgptTaskStateRead'])
  assert.equal(BRIDGE_BASE_PATH, '/plugins/chatgpt-web-bridge')

  const manifest = JSON.parse(await readFile(packagePath, 'utf8'))
  assert.equal(manifest.dsh.client.platform, 'web')
  assert.ok(manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-ui-sidebar'))

  const client = await readFile(clientPath, 'utf8')
  assert.match(client, /window\.__ModuleLoader__\.load/)
  assert.match(client, /sidebar\.footer\.action/)
  assert.match(client, /__DSH_CHATGPT_WEB_BRIDGE__/)
  assert.match(client, /counts\?\.dispatching/)
  assert.match(client, /dsh-chatgpt-web-observer-extension/)
  assert.match(client, /bridgeFetch\('\/observer'/)
  assert.match(client, /Read observer/)
  assert.match(client, /Auto continue/)
  assert.match(client, /bridgeFetch\('\/controller'/)
  assert.match(client, /state\?\.observers/)
  assert.match(client, /conversation_id: targetConversation/)
  assert.match(client, /Target conversation/)
  assert.doesNotMatch(client, /Responses API|workspace_agents|api\.openai\.com/)
})

test('B2 extension is narrow read-only relay with cross-browser MV3 background declarations', async () => {
  const manifest = JSON.parse(await readFile(new URL('manifest.json', extensionRoot), 'utf8'))
  assert.equal(manifest.manifest_version, 3)
  assert.deepEqual(manifest.permissions, [])
  assert.deepEqual(manifest.host_permissions, [
    'https://chatgpt.com/*',
    'http://127.0.0.1:3080/*',
    'http://localhost:3080/*',
  ])
  assert.equal(manifest.background.service_worker, 'background.js')
  assert.deepEqual(manifest.background.scripts, ['background.js'])
  assert.deepEqual(manifest.content_scripts[0].matches, ['https://chatgpt.com/*'])
  assert.deepEqual(manifest.content_scripts[1].matches, ['http://127.0.0.1/*', 'http://localhost/*'])

  const background = await readFile(new URL('background.js', extensionRoot), 'utf8')
  const observer = await readFile(new URL('chatgpt-observer.js', extensionRoot), 'utf8')
  const relay = await readFile(new URL('dsh-gui-relay.js', extensionRoot), 'utf8')

  assert.match(observer, /MutationObserver/)
  assert.match(observer, /data-message-author-role="assistant"/)
  assert.match(observer, /stop-button/)
  assert.match(observer, /turn_completed/)
  assert.match(observer, /bridge_degraded/)
  assert.match(observer, /observer_heartbeat/)
  assert.match(observer, /typeof cloneInto === 'function'/)
  assert.match(observer, /cloneInto\(message, targetWindow\)/)
  assert.match(observer, /postHostToFrameTree/)
  assert.match(observer, /targetWindow\[index\]/)
  assert.match(observer, /document\.querySelectorAll\('iframe'\)/)
  assert.doesNotMatch(observer, /\.wrappedJSObject/)
  assert.doesNotMatch(observer, /\.click\(|\.submit\(|fetch\(|XMLHttpRequest|api\.openai\.com/)

  assert.match(background, /port\.name === 'dsh-gui-relay'/)
  assert.match(background, /port\.name !== 'chatgpt-observer'/)
  assert.doesNotMatch(background, /senderUrl|sender\?\.url|sender\?\.tab\?\.url/)
  assert.doesNotMatch(background, /fetch\(|XMLHttpRequest|tabs\.update|scripting\.executeScript/)

  assert.match(relay, /location\.protocol !== 'http:'/)
  assert.match(relay, /location\.port !== '3080'/)
  assert.match(relay, /location\.hostname === '127\.0\.0\.1'/)
  assert.match(relay, /location\.hostname === 'localhost'/)
  assert.match(relay, /runtime\.connect\(\{ name: 'dsh-gui-relay' \}\)/)
  assert.match(relay, /window\.postMessage/)
  assert.doesNotMatch(relay, /document\.scripts|__DSH_CHATGPT_WEB_BRIDGE__/)
  assert.doesNotMatch(relay, /fetch\(|XMLHttpRequest|x-dsh-chatgpt-bridge-token/)
})
