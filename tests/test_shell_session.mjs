import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  createShellSessionTool,
  ShellSessionError,
  validateShellSessionArguments,
} from '../dsh-shell-session-plugin/index.js'

class FakeTerminals {
  constructor() {
    this.calls = []
    this.sessions = new Map()
    this.nextId = 1
    this.operations = []
  }

  async spawn(owner, request, signal) {
    this.calls.push(['spawn', owner, request, signal])
    const sessionId = `pty-${this.nextId++}`
    const snapshot = {
      sessionId,
      ...(request.name ? { name: request.name } : {}),
      type: request.type,
      pid: 4000 + this.nextId,
      status: { kind: 'running' },
      motd: 'ready',
    }
    this.sessions.set(sessionId, snapshot)
    return structuredClone(snapshot)
  }

  list(owner) {
    this.calls.push(['list', owner])
    return [...this.sessions.values()].map(value => {
      const { motd: _motd, ...snapshot } = value
      return structuredClone(snapshot)
    })
  }

  startSend(owner, id, request) {
    this.calls.push(['startSend', owner, id, request])
    const result = {
      viewport: `ran:${request.text}`,
      waitReason: 'stdin_read',
      sessionStatus: { kind: 'running' },
      truncated: false,
    }
    const operation = {
      done: Promise.resolve(result),
      readOutput: () => ({ delta: result.viewport, truncated: false }),
      cancel: () => true,
    }
    this.operations.push(operation)
    return operation
  }

  read(owner, id, request) {
    this.calls.push(['read', owner, id, request])
    return {
      text: 'line one\nline two',
      totalLines: 2,
      lineBegin: request.offset ?? 0,
      lineEnd: 2,
      truncated: false,
    }
  }

  async signal(owner, id, signal) {
    this.calls.push(['signal', owner, id, signal])
    return { delivered: true, targetPgid: 4321 }
  }

  async kill(owner, id, reason) {
    this.calls.push(['kill', owner, id, reason])
    return this.sessions.delete(id)
  }
}

const owner = { id: 'agent-test' }
const abortController = new AbortController()

{
  const terminals = new FakeTerminals()
  const tool = createShellSessionTool(terminals)
  const exec = { agent: owner, signal: abortController.signal }

  const opened = await tool.execute({ action: 'open', name: 'dev', cwd: '/workspace' }, exec)
  assert.equal(opened.sessionId, 'pty-1')
  assert.equal(opened.name, 'dev')
  assert.deepEqual(terminals.calls[0][2], { type: 'shell', name: 'dev', cwd: '/workspace' })
  assert.equal(terminals.calls[0][3], abortController.signal)

  const listed = await tool.execute({ action: 'list' }, exec)
  assert.equal(listed.sessions.length, 1)
  assert.equal(listed.sessions[0].sessionId, opened.sessionId)

  const status = await tool.execute({ action: 'status', id: opened.sessionId }, exec)
  assert.equal(status.name, 'dev')
  assert.deepEqual(status.status, { kind: 'running' })

  const sent = await tool.execute({ action: 'send', id: opened.sessionId, text: 'export DEMO=1' }, exec)
  assert.equal(sent.viewport, 'ran:export DEMO=1')
  const waitedRequest = terminals.calls.find(call => call[0] === 'startSend')[3]
  assert.equal(waitedRequest.submit, true)
  assert.equal(waitedRequest.signal, abortController.signal)

  const background = await tool.execute({ action: 'send', id: opened.sessionId, text: 'sleep 30', wait: false }, exec)
  assert.deepEqual(background, { sessionId: opened.sessionId, started: true })
  const backgroundRequest = terminals.calls.filter(call => call[0] === 'startSend').at(-1)[3]
  assert.equal(backgroundRequest.submit, true)
  assert.equal('signal' in backgroundRequest, false)

  const read = await tool.execute({ action: 'read', id: opened.sessionId, offset: 1, count: 20 }, exec)
  assert.equal(read.totalLines, 2)
  assert.deepEqual(terminals.calls.find(call => call[0] === 'read')[3], { offset: 1, count: 20 })

  const signalled = await tool.execute({ action: 'signal', id: opened.sessionId, signal: 'SIGINT' }, exec)
  assert.deepEqual(signalled, { delivered: true, targetPgid: 4321 })

  const closed = await tool.execute({ action: 'close', id: opened.sessionId }, exec)
  assert.deepEqual(closed, { sessionId: opened.sessionId, closed: true })
  assert.equal((await tool.execute({ action: 'list' }, exec)).sessions.length, 0)
}

{
  const terminals = new FakeTerminals()
  const tool = createShellSessionTool(terminals)
  await assert.rejects(
    tool.execute({ action: 'list' }, {}),
    error => error instanceof ShellSessionError && error.code === 'owner_required',
  )
  await assert.rejects(
    tool.execute({ action: 'status', id: 'missing' }, { agent: owner }),
    error => error instanceof ShellSessionError && error.code === 'not_found',
  )
}

{
  const invalid = [
    null,
    { action: 'unknown' },
    { action: 'list', id: 'x' },
    { action: 'status' },
    { action: 'open', name: ' dev ' },
    { action: 'send', id: 'x' },
    { action: 'send', id: 'x', text: 123 },
    { action: 'send', id: 'x', text: '', wait: 'no' },
    { action: 'read', id: 'x', offset: -1 },
    { action: 'read', id: 'x', count: 501 },
    { action: 'signal', id: 'x', signal: 'SIGUSR1' },
    { action: 'close' },
  ]
  for (const args of invalid) {
    assert.throws(
      () => validateShellSessionArguments(args),
      error => error instanceof ShellSessionError && error.code === 'invalid_request',
    )
  }

  assert.deepEqual(validateShellSessionArguments({ action: 'open' }), { action: 'open' })
  assert.deepEqual(validateShellSessionArguments({ action: 'send', id: 'x', text: '', submit: false }), { action: 'send', id: 'x', text: '', submit: false })
}

{
  const patch = await readFile(new URL('../deploy/dsh/chatgpt-bridge.cordis.yml', import.meta.url), 'utf8')
  assert.match(patch, /id: chatgpt-persistent-shell[\s\S]*isolate:\n\s+terminals: true/)
  assert.match(patch, /name: '@deepseek-ai\/dsh-terminal'/)
  assert.match(patch, /name: '@deepseek-ai\/dsh-terminal-bash'/)
  assert.match(patch, /name: \/srv\/dsh-mcp-gateway\/dsh-shell-session-plugin\/index\.js/)
  assert.match(patch, /allowExtraTools:[\s\S]*- task_state[\s\S]*- shell_session/)
}

console.log('chatgpt-shell-session-adapter-ok')
