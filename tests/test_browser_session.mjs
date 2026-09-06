import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  BrowserSessionError,
  BrowserSessionManager,
  createBrowserSessionTool,
  validateBrowserSessionArguments,
} from '../dsh-browser-session-plugin/index.js'

class FakePage {
  constructor(url = 'about:blank', title = 'Blank') {
    this._url = url
    this._title = title
    this._closed = false
    this.listeners = new Map()
  }
  on(name, callback) { this.listeners.set(name, callback) }
  isClosed() { return this._closed }
  url() { return this._url }
  async title() { return this._title }
  async close() { this._closed = true }
  async goto(url) { this._url = url; this._title = 'Opened'; return { status: () => 200 } }
}

class FakeContext {
  constructor() { this._pages = [new FakePage()] ; this.listeners = new Map() }
  pages() { return this._pages.filter(page => !page.isClosed()) }
  on(name, callback) { this.listeners.set(name, callback) }
  async newPage() {
    const page = new FakePage()
    this._pages.push(page)
    this.listeners.get('page')?.(page)
    return page
  }
}

class FakeBrowser {
  constructor(context) { this.context = context; this.connected = true; this.listeners = new Map(); this.closed = false }
  contexts() { return [this.context] }
  isConnected() { return this.connected }
  on(name, callback) { this.listeners.set(name, callback) }
  async close() { this.closed = true; this.connected = false; this.listeners.get('disconnected')?.() }
}

class FakeProcessHandle {
  constructor() {
    this.pid = 4242
    this.terminated = false
    this.waited = false
    this.collected = {
      stdout: { readFrom: () => ({ text: '', nextOffset: 0, lossy: false }) },
      stderr: { readFrom: () => ({ text: 'DevTools listening on ws://127.0.0.1:9222/devtools/browser/fake\n', nextOffset: 70, lossy: false }) },
    }
    this.done = new Promise(() => {})
  }
  terminate() { this.terminated = true }
  async waitForExit() { this.waited = true; return true }
}

function makeOwner(id) {
  const listeners = []
  return {
    id,
    session: { id: `session-${id}` },
    ctx: {
      on(name, callback, options) { listeners.push({ name, callback, options }); return () => {} },
    },
    _listeners: listeners,
  }
}

function makeHarness({ mode = 'workspace-write' } = {}) {
  const spawned = []
  const confined = []
  const handles = []
  const contexts = []
  const browsers = []
  const sandbox = {
    confine(argv, policy) {
      confined.push({ argv, policy })
      return { argv: ['/usr/bin/landlock-run', '--', ...argv], enforcement: 'partial' }
    },
  }
  const ctx = {
    sandboxPolicy: {
      defaultMode: 'workspace-write',
      resolve: ({ session }) => ({ mode, workspaceRoot: '/workspace', sessionId: session.id }),
    },
    sessionProjections: { stateOf: () => mode },
    get(name) { return name === 'sandbox' ? sandbox : undefined },
    subprocess: {
      spawn(spec) {
        spawned.push(spec)
        const handle = new FakeProcessHandle()
        handles.push(handle)
        return handle
      },
    },
  }
  const chromium = {
    executablePath: () => '/runtime/browsers/chromium/chrome',
    async connectOverCDP(endpoint) {
      assert.match(endpoint, /^ws:\/\/127\.0\.0\.1:/)
      const context = new FakeContext()
      const browser = new FakeBrowser(context)
      contexts.push(context)
      browsers.push(browser)
      return browser
    },
  }
  return { ctx, playwright: { chromium }, spawned, confined, handles, contexts, browsers }
}

{
  const invalid = [
    null,
    { action: 'wat' },
    { action: 'list', id: 'x' },
    { action: 'status' },
    { action: 'open', name: ' dev ' },
    { action: 'open', url: 'file:///etc/passwd' },
    { action: 'snapshot', id: 'x', max_elements: 201 },
    { action: 'snapshot', id: 'x', max_text_chars: -1 },
    { action: 'act', id: 'x', actions: [] },
    { action: 'act', id: 'x', actions: new Array(25).fill({ action: 'wait' }) },
    { action: 'act', id: 'x', actions: [{ action: 'wait' }], timeout_ms: 60001 },
    { action: 'script', id: 'x', script: '' },
    { action: 'close' },
  ]
  for (const args of invalid) {
    assert.throws(
      () => validateBrowserSessionArguments(args),
      error => error instanceof BrowserSessionError && error.code === 'invalid_request',
    )
  }
  assert.deepEqual(validateBrowserSessionArguments({ action: 'open', url: 'https://example.com/' }), { action: 'open', url: 'https://example.com/' })
}

{
  const managerCalls = []
  const manager = {}
  for (const name of ['open', 'list', 'status', 'snapshot', 'act', 'script', 'close']) {
    manager[name] = async (...args) => { managerCalls.push([name, ...args]); return { routed: name } }
  }
  const tool = createBrowserSessionTool(manager)
  const owner = makeOwner('tool-owner')
  const controller = new AbortController()
  const exec = { agent: owner, signal: controller.signal }

  assert.deepEqual(await tool.execute({ action: 'open', name: 'web' }, exec), { routed: 'open' })
  assert.equal(managerCalls[0][1], owner)
  assert.equal(managerCalls[0][3], controller.signal)
  await tool.execute({ action: 'snapshot', id: 'b1', screenshot: false }, exec)
  await tool.execute({ action: 'act', id: 'b1', actions: [{ action: 'wait', ms: 1 }] }, exec)
  await tool.execute({ action: 'script', id: 'b1', script: 'document.title' }, exec)
  await tool.execute({ action: 'close', id: 'b1' }, exec)
  await assert.rejects(
    tool.execute({ action: 'list' }, {}),
    error => error instanceof BrowserSessionError && error.code === 'owner_required',
  )
}

{
  const h = makeHarness()
  const manager = new BrowserSessionManager(h.ctx, h.playwright, { maxSessionsGlobal: 3, maxSessionsPerOwner: 2 })
  const ownerA = makeOwner('owner-a')
  const ownerB = makeOwner('owner-b')
  const controller = new AbortController()

  const opened = await manager.open(ownerA, { name: 'primary', url: 'https://example.com/' }, controller.signal)
  assert.equal(opened.name, 'primary')
  assert.equal(opened.status, 'running')
  assert.equal(opened.sandbox_mode, 'workspace-write')
  assert.equal(opened.sandbox_enforcement, 'partial')
  assert.equal(opened.pages.length, 1)
  assert.equal(opened.pages[0].url, 'https://example.com/')
  assert.equal(h.confined.length, 1)
  assert.equal(h.confined[0].policy.mode, 'workspace-write')
  assert.equal(h.confined[0].policy.workspaceRoot, '/workspace')
  assert.equal(h.spawned.length, 1)
  assert.equal(h.spawned[0].cwd, '/workspace')
  assert.equal('signal' in h.spawned[0], false)
  assert.equal(h.spawned[0].argv[0], '/usr/bin/landlock-run')
  assert.ok(h.spawned[0].argv.includes('--remote-debugging-port=0'))
  assert.ok(h.spawned[0].argv.some(arg => arg.startsWith('--user-data-dir=')))

  assert.equal((await manager.list(ownerA)).sessions.length, 1)
  assert.equal((await manager.list(ownerB)).sessions.length, 0)
  await assert.rejects(
    manager.status(ownerB, opened.session_id),
    error => error instanceof BrowserSessionError && error.code === 'not_found',
  )
  await assert.rejects(
    manager.open(ownerA, { name: 'primary' }),
    error => error instanceof BrowserSessionError && error.code === 'name_in_use',
  )

  assert.equal(ownerA._listeners.some(listener => listener.name === 'internal/dispatch'), true)
  const modeFence = ownerA._listeners.find(listener => listener.name === 'internal/dispatch')
  assert.throws(
    () => modeFence.callback('emit', 'session/event', [ownerA.session, { type: 'sandbox/mode', data: { mode: 'read-only' } }]),
    /cannot change sandbox mode/,
  )

  const closed = await manager.close(ownerA, opened.session_id)
  assert.deepEqual(closed, { session_id: opened.session_id, closed: true })
  assert.equal(h.handles[0].terminated, true)
  assert.equal(h.handles[0].waited, true)
  assert.equal(h.browsers[0].closed, true)
  assert.equal((await manager.list(ownerA)).sessions.length, 0)
}

{
  const h = makeHarness({ mode: 'read-only' })
  const manager = new BrowserSessionManager(h.ctx, h.playwright)
  await assert.rejects(
    manager.open(makeOwner('read-only'), {}),
    error => error instanceof BrowserSessionError && error.code === 'sandbox_denied',
  )
  assert.equal(h.spawned.length, 0)
}

{
  const source = await readFile(new URL('../dsh-browser-session-plugin/index.js', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /ctx\.llm|browser_task|browser_schedule/)
  assert.doesNotMatch(source, /--no-sandbox/)
  assert.match(source, /sandboxPolicy/)
  assert.match(source, /sandbox\.confine/)
  assert.match(source, /subprocess\.spawn/)

  const runtime = JSON.parse(await readFile(new URL('../deploy/dsh-runtime/package.json', import.meta.url), 'utf8'))
  assert.equal(runtime.dependencies['playwright-core'], '1.62.1')

  const patch = await readFile(new URL('../deploy/dsh/chatgpt-bridge.cordis.yml', import.meta.url), 'utf8')
  assert.match(patch, /name: \/srv\/dsh-mcp-gateway\/dsh-browser-session-plugin\/index\.js/)
  assert.match(patch, /allowExtraTools:[\s\S]*- task_state[\s\S]*- shell_session[\s\S]*- browser_session/)

  const service = await readFile(new URL('../deploy/systemd/dsh-web-host.service', import.meta.url), 'utf8')
  assert.match(service, /PLAYWRIGHT_BROWSERS_PATH=\/opt\/dsh-runtime\/browsers/)
  assert.match(service, /DSH_RUNTIME_ROOT=\/opt\/dsh-runtime/)

  const apparmor = await readFile(new URL('../deploy/apparmor/dsh-chromium', import.meta.url), 'utf8')
  assert.match(apparmor, /\/opt\/dsh-runtime\/browsers\/chromium-\*\/chrome-linux64\/chrome/)
  assert.match(apparmor, /userns,/)
  assert.doesNotMatch(apparmor, /apparmor_restrict_unprivileged_userns=0/)

  const upgrade = await readFile(new URL('../scripts/upgrade-live-host.sh', import.meta.url), 'utf8')
  assert.match(upgrade, /playwright-core/)
  assert.match(upgrade, /install --no-shell chromium/)
  assert.match(upgrade, /provision-browser-deps\.sh/)
  assert.match(upgrade, /APPARMOR_PROFILE_TARGET=\/etc\/apparmor\.d\/dsh-chromium/)
  assert.match(upgrade, /cmp -s "\$APPARMOR_PROFILE_TARGET" "\$APPARMOR_PROFILE_SOURCE"/)

  const provision = await readFile(new URL('../scripts/provision-browser-deps.sh', import.meta.url), 'utf8')
  assert.match(provision, /timeout --foreground --signal=TERM --kill-after=30s 1200s/)
  assert.match(provision, /apparmor_parser -Q -K "\$APPARMOR_PROFILE_SOURCE"/)
  assert.match(provision, /apparmor_parser -r -K "\$APPARMOR_PROFILE_TARGET"/)
}

console.log('chatgpt-browser-session-adapter-ok')
