import assert from 'node:assert/strict'

import { apply } from '../deploy/dsh/plugins/lsm-tool-filter.mjs'

const listeners = new Map()
const visible = new Set([
  'dsh_tool_catalog',
  'mcp__lsm__browser_session',
  'mcp__lsm__run_shell_tool',
])
const denied = new Set()

function on(event, callback) {
  const callbacks = listeners.get(event) ?? new Set()
  callbacks.add(callback)
  listeners.set(event, callbacks)
  return () => callbacks.delete(callback)
}

function emit(event, payload) {
  for (const callback of [...(listeners.get(event) ?? [])]) callback(payload)
}

const ctx = {
  logger: { warn() {} },
  on,
  tools: {
    schemas() {
      return [...visible]
        .filter(name => !denied.has(name))
        .map(name => ({ name }))
    },
  },
}

const agent = {
  ctx: {
    effect(callback) {
      return callback()
    },
    tools: {
      restrict({ deny }) {
        for (const name of deny) {
          assert.ok(visible.has(name), `restriction named unknown tool ${name}`)
          denied.add(name)
        }
        // DSH ToolRuntime.restrict() itself publishes tools/change.
        emit('tools/change')
        return () => {}
      },
    },
  },
}

apply(ctx, {
  serverName: 'lsm',
  allowRawNames: ['browser_session'],
})
emit('agent/created', { agent })

assert.equal(denied.has('mcp__lsm__run_shell_tool'), true, 'initial disallowed LSM tool should be hidden')
assert.equal(denied.has('mcp__lsm__browser_session'), false, 'configured LSM tool should remain visible')
assert.equal(denied.has('dsh_tool_catalog'), false, 'non-LSM DSH tool should remain visible')

// dsh-mcp-client supports notifications/tools/list_changed and registers the new
// generation after the agent already exists. DSH deny restrictions are snapshots,
// so the filter must react to the registry change rather than assuming the old
// deny set covers a newly introduced name.
visible.add('mcp__lsm__write_file')
emit('tools/change')

assert.equal(
  denied.has('mcp__lsm__write_file'),
  true,
  'later disallowed LSM tool should be hidden after tools/list_changed resync',
)
assert.equal(denied.has('dsh_tool_catalog'), false, 'dynamic refresh must not mask DSH tools')

console.log('lsm-tool-filter-dynamic-resync-ok')
