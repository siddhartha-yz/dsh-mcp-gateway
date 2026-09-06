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

const clientPath = new URL('../dsh-chatgpt-web-bridge-plugin/lib/client.js', import.meta.url)
const packagePath = new URL('../dsh-chatgpt-web-bridge-plugin/package.json', import.meta.url)

test('bridge store leases, acknowledges, and reports messages', () => {
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
    () => store.ack({ clientId: 'companion-b', messageId: queued.id, outcome: 'sent' }),
    (error) => error instanceof BridgeError && error.code === 'claim_mismatch',
  )

  now += 100
  const settled = store.ack({ clientId: 'companion-a', messageId: queued.id, outcome: 'sent' })
  assert.equal(settled.message.status, 'sent')
  assert.equal(store.status().counts.sent, 1)
  assert.equal(store.status().counts.pending, 0)
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

test('bridge tool is mechanical and exposes only transport actions', async () => {
  const store = new ChatGPTWebBridgeStore()
  const tool = createBridgeTool(store)
  assert.equal(tool.name, 'chatgpt_web_bridge')
  assert.deepEqual(tool.parameters.properties.action.enum, ['status', 'poll', 'heartbeat', 'ack', 'publish'])
  assert.match(tool.description, /does not invoke a model or choose next actions/)
  const status = await tool.execute({ action: 'status' })
  assert.equal(status.version, BRIDGE_VERSION)
})

test('plugin is a DSH web dual-face plugin with a sidebar client surface', async () => {
  assert.equal(name, 'dsh-chatgpt-web-bridge-experiment')
  assert.deepEqual(inject, ['webServer', 'tools'])
  assert.equal(BRIDGE_BASE_PATH, '/plugins/chatgpt-web-bridge')

  const manifest = JSON.parse(await readFile(packagePath, 'utf8'))
  assert.equal(manifest.dsh.client.platform, 'web')
  assert.ok(manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-ui-sidebar'))

  const client = await readFile(clientPath, 'utf8')
  assert.match(client, /window\.__ModuleLoader__\.load/)
  assert.match(client, /sidebar\.footer\.action/)
  assert.match(client, /__DSH_CHATGPT_WEB_BRIDGE__/)
  assert.doesNotMatch(client, /Responses API|workspace_agents|api\.openai\.com/)
})
