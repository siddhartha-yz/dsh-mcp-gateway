import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  BrowserSessionError,
  BrowserSessionManager,
  browserBwrapArgv,
  createBrowserSessionTool,
  validateBrowserSessionArguments,
} from '../dsh-browser-session-plugin/index.js'
import { assertAuthorizedPeer, validateSpawnRequest } from '../dsh-browser-worker/index.js'

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
  constructor() { this._pages = [new FakePage()]; this.listeners = new Map() }
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

const testRoot = await mkdtemp(join(tmpdir(), 'dsh-browser-session-test-'))
let harnessCounter = 0

function makeHarness({ mode = 'workspace-write' } = {}) {
  const launched = []
  const confined = []
  const handles = []
  const contexts = []
  const browsers = []
  const profileRoot = join(testRoot, `profiles-${++harnessCounter}`)
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
  }
  const launchBrowser = async spec => {
    launched.push(spec)
    const handle = new FakeProcessHandle()
    handles.push(handle)
    return { handle, endpoint: 'ws://127.0.0.1:9222/devtools/browser/fake' }
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
  return { ctx, playwright: { chromium }, launchBrowser, launched, confined, handles, contexts, browsers, profileRoot }
}

function managerFor(h) {
  return new BrowserSessionManager(h.ctx, h.playwright, {
    maxSessionsGlobal: 3,
    maxSessionsPerOwner: 2,
    workerSocket: '/run/dsh-browser-worker/browser.sock',
    profileRoot: h.profileRoot,
    launchBrowser: h.launchBrowser,
  })
}

try {
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
    const manager = managerFor(h)
    const ownerA = makeOwner('owner-a')
    const ownerB = makeOwner('owner-b')
    const controller = new AbortController()

    const opened = await manager.open(ownerA, { name: 'primary', url: 'https://example.com/' }, controller.signal)
    assert.equal(opened.name, 'primary')
    assert.equal(opened.status, 'running')
    assert.equal(opened.sandbox_mode, 'workspace-write')
    assert.equal(opened.sandbox_enforcement, 'full')
    assert.equal(opened.pages.length, 1)
    assert.equal(opened.pages[0].url, 'https://example.com/')
    assert.equal(h.confined.length, 1)
    assert.equal(h.confined[0].policy.mode, 'workspace-write')
    assert.equal(h.confined[0].policy.workspaceRoot, '/workspace')
    assert.equal(h.launched.length, 1)
    assert.equal(h.launched[0].cwd, '/workspace')
    assert.equal(h.launched[0].socketPath, '/run/dsh-browser-worker/browser.sock')
    assert.equal(h.launched[0].signal, controller.signal)
    assert.equal(h.launched[0].argv[0], '/usr/bin/bwrap')
    assert.deepEqual(h.launched[0].argv.slice(0, 16), [
      '/usr/bin/bwrap', '--ro-bind', '/', '/', '--dev', '/dev', '--unshare-pid', '--proc', '/proc',
      '--die-with-parent', '--tmpfs', '/tmp', '--bind', '/workspace', '/workspace', '--',
    ])
    assert.ok(h.launched[0].argv.includes('--remote-debugging-port=0'))
    assert.ok(h.launched[0].argv.some(arg => arg.startsWith(`--user-data-dir=${h.profileRoot}/`)))

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
    const manager = managerFor(h)
    await assert.rejects(
      manager.open(makeOwner('read-only'), {}),
      error => error instanceof BrowserSessionError && error.code === 'sandbox_denied',
    )
    assert.equal(h.launched.length, 0)
  }

  {
    const expectedExecutable = '/runtime/browsers/chromium/chrome'
    const profileRoot = '/tmp/dsh-browser-worker'
    const expectedBwrap = '/usr/bin/bwrap'
    const browserArgv = [
      expectedExecutable,
      '--headless=new',
      '--remote-debugging-port=0',
      '--user-data-dir=/tmp/dsh-browser-worker/session-abc',
      'about:blank',
    ]
    const wrapped = browserBwrapArgv(browserArgv, { mode: 'workspace-write', workspaceRoot: '/workspace' })
    assert.deepEqual(wrapped.slice(0, 16), [
      expectedBwrap, '--ro-bind', '/', '/', '--dev', '/dev', '--unshare-pid', '--proc', '/proc',
      '--die-with-parent', '--tmpfs', '/tmp', '--bind', '/workspace', '/workspace', '--',
    ])
    const valid = {
      op: 'spawn',
      id: 'browser-12345678-1234-1234-1234-123456789abc',
      cwd: '/workspace',
      startupTimeoutMs: 15000,
      argv: wrapped,
    }
    const checked = validateSpawnRequest(valid, { expectedExecutable, profileRoot, expectedBwrap })
    assert.equal(checked.profileDir, '/tmp/dsh-browser-worker/session-abc')
    for (const request of [
      { ...valid, auth: 'obsolete-token' },
      { ...valid, extra: true },
      { ...valid, id: 'wrong' },
      { ...valid, argv: valid.argv.filter(arg => arg !== '--remote-debugging-port=0') },
      { ...valid, argv: [...valid.argv.slice(0, -1), '--no-sandbox', 'about:blank'] },
      { ...valid, argv: valid.argv.map(arg => arg.startsWith('--user-data-dir=') ? '--user-data-dir=/tmp/escape' : arg) },
      { ...valid, argv: ['/bin/sh', ...valid.argv.slice(1)] },
      { ...valid, argv: ['/workspace/bwrap', ...valid.argv.slice(1)] },
      { ...valid, argv: valid.argv.map((arg, index) => index === 13 ? '/tmp' : arg) },
      { ...valid, cwd: '/other-workspace' },
    ]) {
      assert.throws(() => validateSpawnRequest(request, { expectedExecutable, profileRoot, expectedBwrap }))
    }

    const uid = process.getuid?.() ?? 1000
    const authorized = assertAuthorizedPeer({}, {
      peerCredentials: () => ({ pid: 4242, uid, gid: 4242 }),
      hostMainPid: () => 4242,
    })
    assert.deepEqual(authorized, { pid: 4242, uid, gid: 4242 })
    assert.throws(() => assertAuthorizedPeer({}, {
      peerCredentials: () => ({ pid: 4243, uid, gid: 4242 }),
      hostMainPid: () => 4242,
    }), /rejects peer pid/)
    assert.throws(() => assertAuthorizedPeer({}, {
      peerCredentials: () => ({ pid: 4242, uid: uid + 1, gid: 4242 }),
      hostMainPid: () => 4242,
    }), /rejects peer uid/)
  }

  {
    const source = await readFile(new URL('../dsh-browser-session-plugin/index.js', import.meta.url), 'utf8')
    const workerClient = await readFile(new URL('../dsh-browser-session-plugin/worker-client.js', import.meta.url), 'utf8')
    const worker = await readFile(new URL('../dsh-browser-worker/index.js', import.meta.url), 'utf8')
    assert.doesNotMatch(source, /ctx\.llm|browser_task|browser_schedule/)
    assert.doesNotMatch(source, /subprocess\.spawn/)
    assert.doesNotMatch(source, /--no-sandbox/)
    assert.match(source, /sandboxPolicy/)
    assert.match(source, /sandbox\.confine/)
    assert.match(source, /browserBwrapArgv/)
    assert.match(source, /Mirrors the reviewed @deepseek-ai\/dsh-sandbox-local 0\.1\.2-rc\.1 bwrap/)
    assert.match(source, /launchBrowser/)
    assert.doesNotMatch(workerClient, /CREDENTIALS_DIRECTORY|browser-worker\.key/)
    assert.match(worker, /SO_PEERCRED/)
    assert.match(worker, /getsockopt/)
    assert.match(worker, /dsh-web-host\.service/)
    assert.match(worker, /MainPID/)
    assert.match(worker, /koffi/)
    assert.match(worker, /--no-sandbox is forbidden/)
    assert.match(worker, /pinned DSH bwrap profile/)
    assert.match(worker, /NoNewPrivs/)
    assert.match(worker, /await mkdir\(profileDir/)
    assert.doesNotMatch(source, /mkdtemp|mkdir\(this\.config\.profileRoot|rm\(state\.profileDir/)
    assert.doesNotMatch(worker, /ctx\.llm|browser_task|browser_schedule/)

    const runtime = JSON.parse(await readFile(new URL('../deploy/dsh-runtime/package.json', import.meta.url), 'utf8'))
    assert.equal(runtime.dependencies.koffi, '3.2.1')
    assert.equal(runtime.dependencies['playwright-core'], '1.62.1')

    const patch = await readFile(new URL('../deploy/dsh/chatgpt-bridge.cordis.yml', import.meta.url), 'utf8')
    assert.match(patch, /name: \/srv\/dsh-mcp-gateway\/dsh-browser-session-plugin\/index\.js/)
    assert.match(patch, /workerSocket: \/run\/dsh-browser-worker\/browser\.sock/)
    assert.match(patch, /allowExtraTools:[\s\S]*- task_state[\s\S]*- shell_session[\s\S]*- browser_session/)

    const hostService = await readFile(new URL('../deploy/systemd/dsh-web-host.service', import.meta.url), 'utf8')
    const workerService = await readFile(new URL('../deploy/systemd/dsh-browser-worker.service', import.meta.url), 'utf8')
    assert.match(hostService, /NoNewPrivileges=true/)
    assert.doesNotMatch(hostService, /LoadCredential=/)
    assert.match(hostService, /Wants=.*dsh-browser-worker\.service/)
    assert.match(workerService, /AppArmorProfile=dsh-browser-worker/)
    assert.match(workerService, /ExecStart=\/usr\/bin\/setpriv --no-new-privs/)
    assert.match(workerService, /NoNewPrivileges=false/)
    assert.doesNotMatch(workerService, /LoadCredential=/)
    assert.match(workerService, /RuntimeDirectory=dsh-browser-worker/)
    assert.match(workerService, /PrivateTmp=true/)
    assert.match(workerService, /DSH_BROWSER_PROFILE_ROOT=\/tmp\/dsh-browser-worker/)

    const apparmor = await readFile(new URL('../deploy/apparmor/dsh-browser-worker', import.meta.url), 'utf8')
    assert.match(apparmor, /profile dsh-browser-worker flags=\(unconfined\)/)
    assert.match(apparmor, /userns,/)
    assert.doesNotMatch(apparmor, /apparmor_restrict_unprivileged_userns=0/)

    const upgrade = await readFile(new URL('../scripts/upgrade-live-host.sh', import.meta.url), 'utf8')
    assert.match(upgrade, /BROWSER_SERVICE=dsh-browser-worker\.service/)
    assert.match(upgrade, /dsh-browser-worker\/index\.js/)
    assert.match(upgrade, /browser-worker-security-ok/)
    assert.match(upgrade, /APPARMOR_PROFILE_TARGET=\/etc\/apparmor\.d\/dsh-browser-worker/)
    assert.match(upgrade, /cmp -s "\$APPARMOR_PROFILE_TARGET" "\$APPARMOR_PROFILE_SOURCE"/)

    const provision = await readFile(new URL('../scripts/provision-browser-deps.sh', import.meta.url), 'utf8')
    assert.match(provision, /timeout --foreground --signal=TERM --kill-after=30s 1200s/)
    assert.match(provision, /apparmor_parser -r -K "\$APPARMOR_PROFILE_TARGET"/)
    assert.match(provision, /aa-exec -p dsh-browser-worker --/)
    assert.match(provision, /setpriv --reuid "\$DSH_UID" --regid "\$DSH_GID" --clear-groups --no-new-privs/)
    assert.match(provision, /bwrap[\s\S]*--ro-bind \/ \/[\s\S]*--proc \/proc[\s\S]*--tmpfs \/tmp[\s\S]*--bind "\$DSH_WORKSPACE" "\$DSH_WORKSPACE"/)
    assert.match(provision, /unshare --user --map-root-user \/usr\/bin\/true/)
    assert.match(provision, /Browser worker AppArmor\+bwrap nested-userns probe passed under NoNewPrivileges/)
    assert.match(provision, /LEGACY_BROWSER_CREDENTIAL=.*browser-worker\.key/)
    assert.match(provision, /authorization now uses Unix SO_PEERCRED/)
    assert.match(provision, /identity\.conf/)
    assert.match(provision, /LEGACY_APPARMOR_PROFILE=\/etc\/apparmor\.d\/dsh-chromium/)
  }
} finally {
  await rm(testRoot, { recursive: true, force: true })
}

console.log('chatgpt-browser-session-adapter-ok')
