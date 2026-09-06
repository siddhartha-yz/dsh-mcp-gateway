import { randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { launchBrowserViaWorker } from './worker-client.js'

export const name = 'dsh-chatgpt-browser-session'
export const inject = ['tools', 'sandboxPolicy', 'sessionProjections']

const REF_ATTRIBUTE = 'data-dsh-browser-ref'
const MAX_SESSIONS_GLOBAL = 3
const MAX_SESSIONS_PER_OWNER = 2
const MAX_ACTIONS = 24
const MAX_ELEMENTS = 200
const MAX_TEXT_CHARS = 100_000
const MAX_SCRIPT_CHARS = 16_384
const MAX_SCRIPT_RESULT_CHARS = 65_536
const MAX_METADATA_CHARS = 2_000
const STARTUP_TIMEOUT_MS = 15_000
const ACTION_TIMEOUT_MS = 30_000
const MAX_ACTION_TIMEOUT_MS = 60_000
const BWRAP_PATH = '/usr/bin/bwrap'

const ACTION_FIELDS = Object.freeze({
  open: Object.freeze(['action', 'name', 'url']),
  list: Object.freeze(['action']),
  status: Object.freeze(['action', 'id']),
  snapshot: Object.freeze(['action', 'id', 'page_id', 'include_text', 'screenshot', 'full_page', 'max_text_chars', 'max_elements']),
  act: Object.freeze(['action', 'id', 'page_id', 'actions', 'timeout_ms']),
  script: Object.freeze(['action', 'id', 'page_id', 'script']),
  close: Object.freeze(['action', 'id']),
})

const REQUIRED_FIELDS = Object.freeze({
  open: Object.freeze([]),
  list: Object.freeze([]),
  status: Object.freeze(['id']),
  snapshot: Object.freeze(['id']),
  act: Object.freeze(['id', 'actions']),
  script: Object.freeze(['id', 'script']),
  close: Object.freeze(['id']),
})

export function browserBwrapArgv(argv, policy) {
  if (!policy || policy.mode !== 'workspace-write') throw new BrowserSessionError('sandbox_unavailable', 'browser bwrap confinement requires workspace-write policy')
  const workspaceRoot = resolve(policy.workspaceRoot)
  if (!workspaceRoot.startsWith('/')) throw new BrowserSessionError('sandbox_unavailable', 'browser workspace root must be absolute')
  // Mirrors the reviewed @deepseek-ai/dsh-sandbox-local 0.1.2-rc.1 bwrap
  // profile. DSH remains the policy authority; this browser-specific execution
  // rung avoids Landlock denying Chromium userns writes to its private /proc.
  return [
    BWRAP_PATH,
    '--ro-bind', '/', '/',
    '--dev', '/dev',
    '--unshare-pid',
    '--proc', '/proc',
    '--die-with-parent',
    '--tmpfs', '/tmp',
    '--bind', workspaceRoot, workspaceRoot,
    '--',
    ...argv,
  ]
}

export class BrowserSessionError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'BrowserSessionError'
    this.code = code
  }
}

export const BROWSER_SESSION_PARAMETERS = Object.freeze({
  type: 'object',
  properties: {
    action: {
      type: 'string',
      enum: Object.keys(ACTION_FIELDS),
      description: 'Browser session operation.',
    },
    id: {
      type: 'string',
      description: 'Browser session id returned by open.',
    },
    name: {
      type: 'string',
      description: 'Optional owner-local display name for open.',
    },
    url: {
      type: 'string',
      description: 'Optional initial HTTP(S) URL for open.',
    },
    page_id: {
      type: 'string',
      description: 'Optional page id. Defaults to the most recently registered live page.',
    },
    include_text: {
      type: 'boolean',
      description: 'For snapshot, include bounded body text. Defaults to true.',
    },
    screenshot: {
      type: 'boolean',
      description: 'For snapshot, attach a viewport screenshot when the DSH attachment service is available. Defaults to true.',
    },
    full_page: {
      type: 'boolean',
      description: 'For snapshot screenshots, capture the full page. Defaults to false.',
    },
    max_text_chars: {
      type: 'integer',
      minimum: 0,
      maximum: MAX_TEXT_CHARS,
      description: 'Maximum page-text characters returned by snapshot.',
    },
    max_elements: {
      type: 'integer',
      minimum: 1,
      maximum: MAX_ELEMENTS,
      description: 'Maximum visible interactive elements returned by snapshot.',
    },
    actions: {
      type: 'array',
      minItems: 1,
      maxItems: MAX_ACTIONS,
      description: 'Ordered browser actions for act.',
      items: {
        type: 'object',
        properties: {
          action: {
            type: 'string',
            enum: ['navigate', 'new_page', 'close_page', 'back', 'forward', 'reload', 'click', 'fill', 'type', 'select', 'press', 'check', 'uncheck', 'hover', 'wait', 'wait_for_text', 'wait_for_url'],
          },
          url: { type: 'string' },
          target: { type: 'string' },
          value: {},
          key: { type: 'string' },
          text: { type: 'string' },
          ms: { type: 'integer' },
          wait_until: { type: 'string', enum: ['load', 'domcontentloaded', 'networkidle', 'commit'] },
        },
        required: ['action'],
        additionalProperties: false,
      },
    },
    timeout_ms: {
      type: 'integer',
      minimum: 1,
      maximum: MAX_ACTION_TIMEOUT_MS,
      description: 'Per-action timeout for act. Defaults to 30000 ms.',
    },
    script: {
      type: 'string',
      description: 'For script, a bounded JavaScript expression evaluated only inside the selected page context.',
    },
  },
  required: ['action'],
  additionalProperties: false,
})

function own(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key)
}

function assertString(value, field, { allowEmpty = false, maxLength = 4096 } = {}) {
  if (typeof value !== 'string' || value.length > maxLength || (!allowEmpty && value.trim().length === 0)) {
    throw new BrowserSessionError('invalid_request', `${field} must be ${allowEmpty ? 'a' : 'a non-empty'} string of at most ${maxLength} characters`)
  }
}

function assertHttpUrl(value, field = 'url') {
  assertString(value, field, { maxLength: 16_384 })
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new BrowserSessionError('invalid_request', `${field} must be a valid absolute HTTP(S) URL`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new BrowserSessionError('invalid_request', `${field} must use http:// or https://`)
  }
  return parsed.toString()
}

export function validateBrowserSessionArguments(args) {
  if (typeof args !== 'object' || args === null || Array.isArray(args)) {
    throw new BrowserSessionError('invalid_request', 'arguments must be an object')
  }
  const action = args.action
  if (typeof action !== 'string' || !own(ACTION_FIELDS, action)) {
    throw new BrowserSessionError('invalid_request', 'action must be one of: open, list, status, snapshot, act, script, close')
  }
  const allowed = new Set(ACTION_FIELDS[action])
  for (const field of Object.keys(args)) {
    if (!allowed.has(field)) throw new BrowserSessionError('invalid_request', `${field} is not valid for action ${action}`)
  }
  for (const field of REQUIRED_FIELDS[action]) {
    if (!own(args, field)) throw new BrowserSessionError('invalid_request', `${field} is required for action ${action}`)
  }
  if (own(args, 'id')) assertString(args.id, 'id', { maxLength: 256 })
  if (own(args, 'name')) {
    assertString(args.name, 'name', { maxLength: 128 })
    if (args.name.trim() !== args.name) throw new BrowserSessionError('invalid_request', 'name must not have leading or trailing whitespace')
  }
  if (own(args, 'url')) assertHttpUrl(args.url)
  if (own(args, 'page_id')) assertString(args.page_id, 'page_id', { maxLength: 256 })
  for (const field of ['include_text', 'screenshot', 'full_page']) {
    if (own(args, field) && typeof args[field] !== 'boolean') throw new BrowserSessionError('invalid_request', `${field} must be a boolean`)
  }
  if (own(args, 'max_text_chars') && (!Number.isInteger(args.max_text_chars) || args.max_text_chars < 0 || args.max_text_chars > MAX_TEXT_CHARS)) {
    throw new BrowserSessionError('invalid_request', `max_text_chars must be an integer from 0 to ${MAX_TEXT_CHARS}`)
  }
  if (own(args, 'max_elements') && (!Number.isInteger(args.max_elements) || args.max_elements < 1 || args.max_elements > MAX_ELEMENTS)) {
    throw new BrowserSessionError('invalid_request', `max_elements must be an integer from 1 to ${MAX_ELEMENTS}`)
  }
  if (own(args, 'timeout_ms') && (!Number.isInteger(args.timeout_ms) || args.timeout_ms < 1 || args.timeout_ms > MAX_ACTION_TIMEOUT_MS)) {
    throw new BrowserSessionError('invalid_request', `timeout_ms must be an integer from 1 to ${MAX_ACTION_TIMEOUT_MS}`)
  }
  if (own(args, 'actions')) {
    if (!Array.isArray(args.actions) || args.actions.length < 1 || args.actions.length > MAX_ACTIONS) {
      throw new BrowserSessionError('invalid_request', `actions must contain from 1 to ${MAX_ACTIONS} entries`)
    }
  }
  if (own(args, 'script')) assertString(args.script, 'script', { maxLength: MAX_SCRIPT_CHARS })
  return args
}

function requireOwner(exec) {
  if (!exec?.agent) throw new BrowserSessionError('owner_required', 'browser_session requires a DSH Agent execution identity')
  return exec.agent
}

function runtimeRootFromProcess() {
  if (process.env.DSH_RUNTIME_ROOT) return resolve(process.env.DSH_RUNTIME_ROOT)
  return resolve(dirname(process.execPath), '..', '..')
}

export function loadPlaywrightCore(runtimeRoot = runtimeRootFromProcess()) {
  const require = createRequire(join(runtimeRoot, 'package.json'))
  return require('playwright-core')
}

async function settleWithin(promise, timeoutMs) {
  let timer
  try {
    return await Promise.race([
      Promise.resolve(promise),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`operation exceeded ${timeoutMs} ms`)), timeoutMs)
      }),
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

function pageKey(page) {
  return page
}

export class BrowserSessionManager {
  constructor(ctx, playwright, config = {}) {
    if (!playwright?.chromium) throw new Error('BrowserSessionManager requires playwright.chromium')
    this.ctx = ctx
    this.chromium = playwright.chromium
    this.config = {
      startupTimeoutMs: config.startupTimeoutMs ?? STARTUP_TIMEOUT_MS,
      maxSessionsGlobal: config.maxSessionsGlobal ?? MAX_SESSIONS_GLOBAL,
      maxSessionsPerOwner: config.maxSessionsPerOwner ?? MAX_SESSIONS_PER_OWNER,
      workerSocket: config.workerSocket ?? '/run/dsh-browser-worker/browser.sock',
      profileRoot: config.profileRoot ?? '/tmp/dsh-browser-worker',
      launchBrowser: config.launchBrowser ?? launchBrowserViaWorker,
    }
    this.sessions = new Map()
    this.ownerSessions = new WeakMap()
    this.ownerNames = new WeakMap()
    this.fencedOwners = new WeakSet()
  }

  _idsFor(owner, create = false) {
    let ids = this.ownerSessions.get(owner)
    if (!ids && create) {
      ids = new Set()
      this.ownerSessions.set(owner, ids)
    }
    return ids
  }

  _namesFor(owner, create = false) {
    let names = this.ownerNames.get(owner)
    if (!names && create) {
      names = new Map()
      this.ownerNames.set(owner, names)
    }
    return names
  }

  hasOwnerActivity(owner) {
    return (this._idsFor(owner)?.size ?? 0) > 0
  }

  _ensureModeFence(owner) {
    if (this.fencedOwners.has(owner)) return
    this.fencedOwners.add(owner)
    owner.ctx?.on?.('internal/dispatch', (_mode, eventName, args) => {
      if (eventName !== 'session/event') return
      const [session, event] = args
      if (session !== owner.session || event?.type !== 'sandbox/mode') return
      const currentMode = this.ctx.sessionProjections.stateOf(session, 'sandboxMode') ?? this.ctx.sandboxPolicy.defaultMode
      if (event.data?.mode === currentMode || !this.hasOwnerActivity(owner)) return
      throw new Error(`cannot change sandbox mode from "${currentMode}" to "${event.data?.mode}" while persistent browser sessions are open; close them first`)
    }, { global: true })
  }

  _policy(owner) {
    return this.ctx.sandboxPolicy.resolve({ session: owner.session })
  }

  _assertPolicyStable(session) {
    const current = this._policy(session.owner)
    if (current.mode !== session.policy.mode || current.workspaceRoot !== session.policy.workspaceRoot) {
      throw new BrowserSessionError('sandbox_changed', 'browser session sandbox policy changed; close and reopen the browser session')
    }
  }

  async open(owner, { name, url } = {}, signal) {
    signal?.throwIfAborted?.()
    const ownerIds = this._idsFor(owner, true)
    if (this.sessions.size >= this.config.maxSessionsGlobal) {
      throw new BrowserSessionError('resource_limit', `at most ${this.config.maxSessionsGlobal} browser sessions may be active globally`)
    }
    if (ownerIds.size >= this.config.maxSessionsPerOwner) {
      throw new BrowserSessionError('resource_limit', `at most ${this.config.maxSessionsPerOwner} browser sessions may be active for one owner`)
    }
    const names = this._namesFor(owner, true)
    if (name && names.has(name)) throw new BrowserSessionError('name_in_use', `browser session name ${JSON.stringify(name)} is already in use`)

    const policy = this._policy(owner)
    if (policy.mode === 'read-only') {
      throw new BrowserSessionError('sandbox_denied', 'browser_session open requires workspace-write or danger-full-access because Chromium needs a writable temporary profile')
    }
    const executable = this.chromium.executablePath()
    const id = `browser-${randomUUID()}`
    const profileDir = join(this.config.profileRoot, id)
    let handle
    let browser
    try {
      const baseArgv = [
        executable,
        '--headless=new',
        '--remote-debugging-port=0',
        `--user-data-dir=${profileDir}`,
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-background-networking',
        '--disable-component-update',
        '--disable-dev-shm-usage',
        '--disable-notifications',
        '--disable-sync',
        '--metrics-recording-only',
        '--window-size=1440,1000',
        'about:blank',
      ]
      let argv = baseArgv
      let enforcement = 'none'
      if (policy.mode !== 'danger-full-access') {
        const sandbox = this.ctx.get?.('sandbox')
        if (!sandbox) throw new BrowserSessionError('sandbox_unavailable', `browser_session requires a DSH sandbox provider under mode ${policy.mode}`)
        // Ask the DSH sandbox provider to validate the resolved policy fail-closed.
        // Its Linux fallback is Landlock on this host, but Landlock intentionally
        // blocks /proc uid_map writes required by Chromium's own userns sandbox.
        sandbox.confine(baseArgv, { ...policy, mode: policy.mode })
        argv = browserBwrapArgv(baseArgv, policy)
        enforcement = 'full'
      }
      const launched = await this.config.launchBrowser({
        socketPath: this.config.workerSocket,
        argv,
        cwd: policy.workspaceRoot,
        id,
        startupTimeoutMs: this.config.startupTimeoutMs,
        signal,
      })
      handle = launched.handle
      const endpoint = launched.endpoint
      browser = await this.chromium.connectOverCDP(endpoint, { timeout: this.config.startupTimeoutMs })
      const context = browser.contexts()[0]
      if (!context) throw new BrowserSessionError('browser_start_failed', 'Chromium CDP connection exposed no default browser context')
      let pages = context.pages()
      let page = pages[pages.length - 1]
      if (!page) page = await context.newPage()
      const state = {
        id,
        name: name ?? null,
        owner,
        policy,
        enforcement,
        profileDir,
        process: handle,
        browser,
        context,
        pages: new Map(),
        refs: new Map(),
        errors: [],
        network: [],
        createdAt: Date.now(),
        lastUsedAt: Date.now(),
        exited: false,
        disconnected: false,
      }
      context.on('page', opened => this._registerPage(state, opened))
      for (const existing of context.pages()) this._registerPage(state, existing)
      if (url) {
        await page.goto(assertHttpUrl(url), { waitUntil: 'domcontentloaded', timeout: ACTION_TIMEOUT_MS })
      }
      ownerIds.add(id)
      if (name) names.set(name, id)
      this.sessions.set(id, state)
      this._ensureModeFence(owner)
      void handle.done.then(() => { state.exited = true }, () => { state.exited = true })
      browser.on('disconnected', () => { state.disconnected = true })
      return await this._summary(state)
    } catch (error) {
      try { await settleWithin(browser?.close?.(), 2000) } catch {}
      try { handle?.terminate?.() } catch {}
      try { await handle?.waitForExit?.(AbortSignal.timeout(4000)) } catch {}
      throw error
    }
  }

  async list(owner) {
    const rows = []
    for (const id of this._idsFor(owner) ?? []) {
      const state = this.sessions.get(id)
      if (state) rows.push(await this._summary(state))
    }
    return { sessions: rows }
  }

  _owned(owner, id) {
    const state = this.sessions.get(id)
    if (!state || state.owner !== owner) throw new BrowserSessionError('not_found', `browser session ${JSON.stringify(id)} not found`)
    return state
  }

  async status(owner, id) {
    const state = this._owned(owner, id)
    this._assertPolicyStable(state)
    return await this._summary(state)
  }

  async snapshot(owner, id, options = {}) {
    const state = this._owned(owner, id)
    this._assertPolicyStable(state)
    this._assertAlive(state)
    const page = await this._selectPage(state, options.page_id)
    state.lastUsedAt = Date.now()
    const maxTextChars = options.max_text_chars ?? MAX_TEXT_CHARS
    const maxElements = options.max_elements ?? 100
    const includeText = options.include_text ?? true
    const elements = await this._captureInteractiveElements(state, page, maxElements)
    let text = null
    let textTruncated = false
    if (includeText) {
      const bounded = await page.evaluate(limit => {
        const value = document.body?.innerText ?? ''
        return { text: value.slice(0, limit), truncated: value.length > limit }
      }, maxTextChars)
      text = bounded.text
      textTruncated = bounded.truncated
    }
    let screenshot
    if (options.screenshot ?? true) screenshot = await this._saveScreenshot(page, options.full_page ?? false)
    await this._syncPages(state)
    const pageId = this._pageId(state, page)
    return {
      session_id: id,
      page_id: pageId,
      title: await page.title(),
      url: page.url(),
      pages: await this._pageSummaries(state),
      text,
      text_truncated: textTruncated,
      interactive_elements: elements,
      errors: state.errors.slice(-20),
      network: state.network.slice(-30),
      screenshot,
    }
  }

  async act(owner, id, actions, options = {}) {
    const state = this._owned(owner, id)
    this._assertPolicyStable(state)
    this._assertAlive(state)
    const timeout = Math.min(Math.max(options.timeout_ms ?? ACTION_TIMEOUT_MS, 1), MAX_ACTION_TIMEOUT_MS)
    let page = await this._selectPage(state, options.page_id)
    const results = []
    for (let index = 0; index < actions.length; index += 1) {
      const raw = actions[index]
      if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) throw new BrowserSessionError('invalid_request', `actions[${index}] must be an object`)
      const action = String(raw.action ?? '').trim()
      if (!action) throw new BrowserSessionError('invalid_request', `actions[${index}].action is required`)
      const result = await this._runAction(state, page, action, raw, timeout)
      page = result.page
      results.push({ index, action, ...result.value })
      await this._syncPages(state)
    }
    state.lastUsedAt = Date.now()
    return {
      session_id: id,
      page_id: this._pageId(state, page),
      title: await page.title(),
      url: page.url(),
      pages: await this._pageSummaries(state),
      results,
    }
  }

  async script(owner, id, script, options = {}) {
    const state = this._owned(owner, id)
    this._assertPolicyStable(state)
    this._assertAlive(state)
    const page = await this._selectPage(state, options.page_id)
    state.lastUsedAt = Date.now()
    const value = await page.evaluate(script)
    let serialized
    try {
      serialized = JSON.stringify(value)
    } catch {
      serialized = JSON.stringify(String(value))
    }
    if (serialized === undefined) return { session_id: id, page_id: this._pageId(state, page), value: null, truncated: false }
    if (serialized.length > MAX_SCRIPT_RESULT_CHARS) {
      return {
        session_id: id,
        page_id: this._pageId(state, page),
        value_json: serialized.slice(0, MAX_SCRIPT_RESULT_CHARS),
        truncated: true,
      }
    }
    return { session_id: id, page_id: this._pageId(state, page), value, truncated: false }
  }

  async close(owner, id) {
    const state = this._owned(owner, id)
    this._removeState(state)
    await this._disposeState(state)
    return { session_id: id, closed: true }
  }

  async closeOwner(owner) {
    const ids = [...(this._idsFor(owner) ?? [])]
    await Promise.allSettled(ids.map(async id => {
      const state = this.sessions.get(id)
      if (!state) return
      this._removeState(state)
      await this._disposeState(state)
    }))
  }

  async closeAll() {
    const states = [...this.sessions.values()]
    for (const state of states) this._removeState(state)
    await Promise.allSettled(states.map(state => this._disposeState(state)))
  }

  _removeState(state) {
    this.sessions.delete(state.id)
    const ids = this._idsFor(state.owner)
    ids?.delete(state.id)
    if (state.name) this._namesFor(state.owner)?.delete(state.name)
  }

  async _disposeState(state) {
    try { await settleWithin(state.browser?.close?.(), 2500) } catch {}
    try { state.process?.terminate?.() } catch {}
    try { await state.process?.waitForExit?.(AbortSignal.timeout(5000)) } catch {}
  }

  _assertAlive(state) {
    if (state.exited || state.disconnected || !state.browser?.isConnected?.()) {
      throw new BrowserSessionError('session_exited', `browser session ${JSON.stringify(state.id)} is no longer running`)
    }
  }

  _registerPage(state, page) {
    for (const [id, existing] of state.pages) {
      if (pageKey(existing) === pageKey(page)) return id
    }
    const id = `page-${randomUUID().slice(0, 12)}`
    state.pages.set(id, page)
    state.refs.set(id, new Map())
    page.on('pageerror', error => {
      state.errors.push({ page_id: id, kind: 'pageerror', message: String(error).slice(0, 4000) })
      if (state.errors.length > 100) state.errors.shift()
    })
    page.on('console', message => {
      if (message.type() !== 'error') return
      state.errors.push({ page_id: id, kind: 'console', message: message.text().slice(0, 4000) })
      if (state.errors.length > 100) state.errors.shift()
    })
    page.on('requestfailed', request => {
      state.errors.push({ page_id: id, kind: 'requestfailed', method: request.method(), url: request.url().slice(0, 4000), failure: request.failure()?.errorText ?? null })
      if (state.errors.length > 100) state.errors.shift()
    })
    page.on('response', response => {
      state.network.push({ page_id: id, method: response.request().method(), status: response.status(), url: response.url().slice(0, 4000) })
      if (state.network.length > 100) state.network.shift()
    })
    return id
  }

  _pageId(state, page) {
    for (const [id, existing] of state.pages) if (pageKey(existing) === pageKey(page)) return id
    return this._registerPage(state, page)
  }

  async _syncPages(state) {
    const live = state.context.pages().filter(page => !page.isClosed())
    const liveSet = new Set(live)
    for (const [id, page] of state.pages) {
      if (!liveSet.has(page)) {
        state.pages.delete(id)
        state.refs.delete(id)
      }
    }
    for (const page of live) this._registerPage(state, page)
  }

  async _selectPage(state, pageId) {
    await this._syncPages(state)
    if (pageId) {
      const page = state.pages.get(pageId)
      if (!page) throw new BrowserSessionError('not_found', `browser page ${JSON.stringify(pageId)} not found`)
      return page
    }
    const pages = [...state.pages.values()]
    if (pages.length) return pages[pages.length - 1]
    const page = await state.context.newPage()
    this._registerPage(state, page)
    return page
  }

  async _summary(state) {
    await this._syncPages(state)
    return {
      session_id: state.id,
      name: state.name,
      status: state.exited || state.disconnected || !state.browser?.isConnected?.() ? 'exited' : 'running',
      sandbox_mode: state.policy.mode,
      sandbox_enforcement: state.enforcement,
      pages: await this._pageSummaries(state),
      created_at: state.createdAt,
      last_used_at: state.lastUsedAt,
    }
  }

  async _pageSummaries(state) {
    const rows = []
    for (const [id, page] of state.pages) {
      if (page.isClosed()) continue
      rows.push({ page_id: id, title: await page.title().catch(() => ''), url: page.url() })
    }
    return rows
  }

  async _captureInteractiveElements(state, page, maxElements) {
    const token = randomUUID().slice(0, 12)
    const raw = await page.locator("a,button,input,textarea,select,[role='button'],[role='link'],[role='checkbox'],[role='tab'],[contenteditable='true']").evaluateAll((elements, payload) => {
      const [attribute, markerToken, limit, metadataLimit] = payload
      const clip = value => typeof value === 'string' ? value.slice(0, metadataLimit) : null
      const visible = elements.filter(element => {
        const style = window.getComputedStyle(element)
        const rect = element.getBoundingClientRect()
        return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0
      }).slice(0, limit)
      return visible.map((element, index) => {
        const ref = `e${index + 1}`
        const marker = `${markerToken}-${ref}`
        element.setAttribute(attribute, marker)
        const text = (element.innerText || element.value || element.getAttribute('aria-label') || element.getAttribute('title') || '').trim().slice(0, 500)
        return {
          ref,
          marker,
          tag: element.tagName.toLowerCase(),
          role: clip(element.getAttribute('role')),
          type: clip(element.getAttribute('type')),
          text,
          name: clip(element.getAttribute('name')),
          placeholder: clip(element.getAttribute('placeholder')),
          href: clip(element.href || null),
          disabled: Boolean(element.disabled),
        }
      })
    }, [REF_ATTRIBUTE, token, maxElements, MAX_METADATA_CHARS])
    const pageId = this._pageId(state, page)
    const refs = new Map()
    for (const item of raw) {
      refs.set(item.ref, `[${REF_ATTRIBUTE}="${item.marker}"]`)
      delete item.marker
    }
    state.refs.set(pageId, refs)
    return raw
  }

  _locator(state, page, target) {
    assertString(target, 'target', { maxLength: 4096 })
    const pageId = this._pageId(state, page)
    const selector = state.refs.get(pageId)?.get(target) ?? target
    return page.locator(selector).first()
  }

  async _runAction(state, page, action, data, timeout) {
    if (action === 'navigate') {
      const url = assertHttpUrl(data.url)
      const waitUntil = data.wait_until ?? 'domcontentloaded'
      const response = await page.goto(url, { waitUntil, timeout })
      return { page, value: { status: response?.status() ?? null, url: page.url() } }
    }
    if (action === 'new_page') {
      const opened = await state.context.newPage()
      this._registerPage(state, opened)
      if (data.url) await opened.goto(assertHttpUrl(data.url), { waitUntil: data.wait_until ?? 'domcontentloaded', timeout })
      return { page: opened, value: { page_id: this._pageId(state, opened), url: opened.url() } }
    }
    if (action === 'close_page') {
      await page.close()
      await this._syncPages(state)
      const replacement = await this._selectPage(state)
      return { page: replacement, value: { closed: true, page_id: this._pageId(state, replacement) } }
    }
    if (action === 'back') {
      await page.goBack({ waitUntil: data.wait_until ?? 'domcontentloaded', timeout })
      return { page, value: { url: page.url() } }
    }
    if (action === 'forward') {
      await page.goForward({ waitUntil: data.wait_until ?? 'domcontentloaded', timeout })
      return { page, value: { url: page.url() } }
    }
    if (action === 'reload') {
      await page.reload({ waitUntil: data.wait_until ?? 'domcontentloaded', timeout })
      return { page, value: { url: page.url() } }
    }
    if (action === 'wait') {
      const ms = Math.min(Math.max(Number(data.ms ?? 1000), 0), 30_000)
      await page.waitForTimeout(ms)
      return { page, value: { waited_ms: ms } }
    }
    if (action === 'wait_for_text') {
      assertString(data.text, 'text', { maxLength: 4096 })
      await page.getByText(data.text).first().waitFor({ timeout })
      return { page, value: { matched: data.text } }
    }
    if (action === 'wait_for_url') {
      assertString(data.url, 'url', { maxLength: 16_384 })
      await page.waitForURL(data.url, { timeout })
      return { page, value: { url: page.url() } }
    }

    const locator = this._locator(state, page, data.target)
    if (action === 'click') await locator.click({ timeout })
    else if (action === 'fill') await locator.fill(String(data.value ?? ''), { timeout })
    else if (action === 'type') await locator.pressSequentially(String(data.value ?? ''), { timeout })
    else if (action === 'select') {
      const value = Array.isArray(data.value) ? data.value.map(item => String(item)) : String(data.value ?? '')
      await locator.selectOption(value, { timeout })
    } else if (action === 'press') {
      assertString(data.key, 'key', { maxLength: 128 })
      await locator.press(data.key, { timeout })
    } else if (action === 'check') await locator.check({ timeout })
    else if (action === 'uncheck') await locator.uncheck({ timeout })
    else if (action === 'hover') await locator.hover({ timeout })
    else throw new BrowserSessionError('invalid_request', `unsupported browser action ${JSON.stringify(action)}`)
    return { page, value: { target: data.target } }
  }

  async _saveScreenshot(page, fullPage) {
    const attachments = this.ctx.get?.('attachments')
    if (!attachments) return undefined
    const limits = attachments.imageLimits ?? {}
    const maxBytes = Math.min(limits.maxImageBytes ?? 4_000_000, limits.maxMessageImageBytes ?? 4_000_000)
    let data = await page.screenshot({ type: 'png', fullPage: Boolean(fullPage) })
    let mediaType = 'image/png'
    if (data.length > maxBytes) {
      data = await page.screenshot({ type: 'jpeg', quality: 60, fullPage: Boolean(fullPage) })
      mediaType = 'image/jpeg'
    }
    if (data.length > maxBytes) throw new BrowserSessionError('output_limit', `browser screenshot exceeds DSH attachment limit of ${maxBytes} bytes`)
    return await attachments.saveImage({
      data: new Uint8Array(data),
      mediaType,
      name: `browser-${Date.now()}.${mediaType === 'image/png' ? 'png' : 'jpg'}`,
    })
  }
}

export function createBrowserSessionTool(manager) {
  return {
    name: 'browser_session',
    description: [
      'Manage owner-scoped persistent Chromium sessions through a DSH runtime plugin.',
      'Actions: open, list, status, snapshot, act, script, close.',
      'Browser page/cookie/DOM state persists across tool calls until close or Agent disposal.',
      'The plugin contains no model loop, autonomous task runner, or scheduler.',
      'DSH owns the resolved sandbox policy; workspace-write browser sessions use the reviewed DSH 0.1.2-rc.1 bwrap file profile so Chromium can retain its own userns sandbox, and a narrow local worker launches only that validated argv under AppArmor plus NoNewPrivileges.',
    ].join(' '),
    parameters: BROWSER_SESSION_PARAMETERS,
    output: {
      schema: {},
      render: (args, value) => {
        const blocks = [{ type: 'text', text: JSON.stringify(value, null, 2) }]
        if (args?.action === 'snapshot' && value?.screenshot) blocks.push({ type: 'image', attachment: value.screenshot })
        return blocks
      },
    },
    async execute(rawArgs, exec) {
      const args = validateBrowserSessionArguments(rawArgs)
      const owner = requireOwner(exec)
      switch (args.action) {
        case 'open': return await manager.open(owner, args, exec?.signal)
        case 'list': return await manager.list(owner)
        case 'status': return await manager.status(owner, args.id)
        case 'snapshot': return await manager.snapshot(owner, args.id, args)
        case 'act': return await manager.act(owner, args.id, args.actions, args)
        case 'script': return await manager.script(owner, args.id, args.script, args)
        case 'close': return await manager.close(owner, args.id)
        default: throw new Error(`unsupported browser_session action: ${String(args.action)}`)
      }
    },
  }
}

function positiveInteger(value, fallback, name) {
  const resolved = value ?? fallback
  if (!Number.isInteger(resolved) || resolved < 1) throw new Error(`${name} must be a positive integer`)
  return resolved
}

export function apply(ctx, config = {}) {
  const playwright = loadPlaywrightCore(config.runtimeRoot)
  const manager = new BrowserSessionManager(ctx, playwright, {
    startupTimeoutMs: positiveInteger(config.startupTimeoutMs, STARTUP_TIMEOUT_MS, 'startupTimeoutMs'),
    maxSessionsGlobal: positiveInteger(config.maxSessionsGlobal, MAX_SESSIONS_GLOBAL, 'maxSessionsGlobal'),
    maxSessionsPerOwner: positiveInteger(config.maxSessionsPerOwner, MAX_SESSIONS_PER_OWNER, 'maxSessionsPerOwner'),
    workerSocket: config.workerSocket ?? '/run/dsh-browser-worker/browser.sock',
    profileRoot: config.profileRoot ?? '/tmp/dsh-browser-worker',
  })
  ctx.on('agent/disposed', ({ agent }) => {
    void manager.closeOwner(agent)
  })
  ctx.effect(() => ctx.tools.register(createBrowserSessionTool(manager)), 'chatgpt-browser-session.tool')
  ctx.effect(() => async () => {
    await manager.closeAll()
  }, 'chatgpt-browser-session.cleanup')
}
