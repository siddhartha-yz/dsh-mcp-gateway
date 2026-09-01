import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Readable } from 'node:stream'

import { apply } from '../dsh-bridge-plugin/index.js'

const PREFIX = '/api/chatgpt-bridge'
const CAPABILITY_SESSION_PREFIX = 'dsh-mcp-gateway-chatgpt-capability'
const DEFAULT_PRESET_PATH = join(process.cwd(), 'dsh-bridge-plugin', 'index.js')

function expectedCapabilitySessionId(presetId = 'default', presetPath = DEFAULT_PRESET_PATH) {
  const { mtimeMs, size } = statSync(presetPath)
  const digest = createHash('sha256').update(readFileSync(presetPath)).digest('hex')
  const suffix = createHash('sha256')
    .update(JSON.stringify([process.cwd(), presetId, presetPath, mtimeMs, size, digest]))
    .digest('hex')
    .slice(0, 24)
  return `${CAPABILITY_SESSION_PREFIX}-${suffix}`
}

function responseCapture() {
  const state = { status: undefined, headers: undefined, body: undefined }
  const listeners = new Map()
  return {
    state,
    writableEnded: false,
    destroyed: false,
    once(name, callback) {
      listeners.set(name, callback)
      return this
    },
    off(name, callback) {
      if (listeners.get(name) === callback) listeners.delete(name)
      return this
    },
    emitClose() {
      this.destroyed = true
      const callback = listeners.get('close')
      if (callback) {
        listeners.delete('close')
        callback()
      }
    },
    writeHead(status, headers) {
      state.status = status
      state.headers = headers
    },
    end(data) {
      this.writableEnded = true
      state.body = JSON.parse(String(data))
    },
  }
}

function postJson(payload) {
  const req = Readable.from([Buffer.from(JSON.stringify(payload))])
  req.method = 'POST'
  return req
}

function postRaw(body) {
  const req = Readable.from([Buffer.isBuffer(body) ? body : Buffer.from(body)])
  req.method = 'POST'
  return req
}

function makeContext({
  persisted = false,
  persistedHeader,
  defaultId = 'default',
  presetPath = DEFAULT_PRESET_PATH,
  listError,
  resumeError,
  executeError,
  executeHook,
  executeResult,
  executeWaitForAbort = false,
  skillListError,
  mountHook,
  resolveHook,
  createHook,
  disposeHook,
  omitAgents = false,
  omitPresets = false,
  attachments,
} = {}) {
  const routes = new Map()
  const cleanups = new Map()
  let currentDefaultId = defaultId
  const scope = { agentPreset: defaultId }
  const helperSessionId = expectedCapabilitySessionId(defaultId, presetPath)
  const agent = { id: helperSessionId, session: { header: { meta: { cwd: process.cwd() } } } }
  const agentFor = id => id === helperSessionId
    ? agent
    : { id, session: { header: { meta: { cwd: process.cwd() } } } }
  const calls = {
    standing: 0,
    standingIds: [],
    mountIds: [],
    create: [],
    resume: [],
    schemas: [],
    execute: [],
    skillList: [],
    warnings: [],
    disposed: [],
  }

  const ctx = {
    logger: {
      warn(value) {
        calls.warnings.push(value)
      },
    },
    llm: {
      registerAdapter() {},
    },
    effect(factory, label) {
      const cleanup = factory()
      if (typeof cleanup === 'function' && label) cleanups.set(label, cleanup)
      return cleanup
    },
    on() {
      return () => {}
    },
    webServer: {
      register(route) {
        routes.set(route.path, route.handler)
        return () => routes.delete(route.path)
      },
    },
    tools: {
      schemas(viewScope) {
        calls.schemas.push(viewScope)
        return [{ name: 'bash', description: 'bash', parameters: { type: 'object' } }]
      },
      async execute(input) {
        calls.execute.push(input)
        if (executeError) throw executeError
        if (executeHook) return executeHook(input, calls)
        if (executeWaitForAbort) {
          await new Promise((resolve, reject) => {
            if (input.signal.aborted) {
              reject(input.signal.reason)
              return
            }
            input.signal.addEventListener('abort', () => reject(input.signal.reason), { once: true })
          })
        }
        return executeResult ?? { isError: false, value: null, content: [{ type: 'text', text: 'ok' }] }
      },
    },
    skills: {
      async list(options) {
        calls.skillList.push(options)
        if (skillListError) throw skillListError
        return []
      },
      async get() {
        return undefined
      },
    },
    get(name) {
      if (name === 'agentPresets') {
        if (omitPresets) return undefined
        return {
          get defaultId() {
            return currentDefaultId
          },
          async resolve(id) {
            if (resolveHook) await resolveHook({ id, presetPath, calls })
            return { id, path: presetPath }
          },
          async standingKeyFor(id) {
            calls.standing += 1
            calls.standingIds.push(id)
            return { agentPreset: id }
          },
          async mount(_agentCtx, id) {
            calls.mountIds.push(id)
            if (mountHook) await mountHook({ id, presetPath, calls })
            return { id, path: presetPath }
          },
        }
      }
      if (name === 'agents') {
        if (omitAgents) return undefined
        return {
          async create(options) {
            calls.create.push(options)
            if (createHook) return createHook(options, calls)
            const createdAgent = agentFor(options.sessionId)
            if (options.setup) await options.setup({ agent: createdAgent })
            return {
              agent: createdAgent,
              async dispose() {
                calls.disposed.push(createdAgent.id)
                if (disposeHook) await disposeHook(createdAgent.id, calls)
              },
            }
          },
          async resume(options) {
            calls.resume.push(options)
            if (resumeError) throw resumeError
            const resumedAgent = agentFor(options.resumeSessionId)
            if (options.setup) await options.setup({ agent: resumedAgent })
            return {
              agent: resumedAgent,
              async dispose() {
                calls.disposed.push(resumedAgent.id)
                if (disposeHook) await disposeHook(resumedAgent.id, calls)
              },
            }
          },
        }
      }
      if (name === 'sessionPersistence') {
        return {
          async list() {
            if (listError) throw listError
            if (!persisted) return []
            return [{
              id: helperSessionId,
              cwd: process.cwd(),
              agentPreset: defaultId,
              ...persistedHeader,
            }]
          },
        }
      }
      if (name === 'attachments') return attachments
      return undefined
    },
  }

  apply(ctx)
  return {
    routes,
    cleanups,
    scope,
    agent,
    calls,
    helperSessionId,
    setDefaultId(value) {
      currentDefaultId = value
    },
  }
}

{
  const { routes, scope, calls } = makeContext()
  const toolsRes = responseCapture()
  await routes.get(`${PREFIX}/tools`)({ method: 'GET' }, toolsRes)
  assert.equal(toolsRes.state.status, 200)
  assert.equal(toolsRes.state.body.scope, 'dsh-preset-standing')
  assert.deepEqual(calls.schemas, [scope])
  assert.equal(calls.create.length, 0)
  assert.equal(calls.resume.length, 0)

  const skillsRes = responseCapture()
  await routes.get(`${PREFIX}/skills`)({ method: 'GET' }, skillsRes)
  assert.equal(skillsRes.state.status, 200)
  assert.equal(calls.skillList.length, 1)
  assert.deepEqual(calls.skillList[0].scope, scope)
  assert.equal(calls.create.length, 0)
  assert.equal(calls.resume.length, 0)
}

{
  const { routes, agent, calls, helperSessionId } = makeContext({ persisted: false })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: { command: 'true' } }), res)
  assert.equal(res.state.status, 200)
  assert.equal(calls.create.length, 1)
  assert.equal(calls.create[0].sessionId, helperSessionId)
  assert.match(helperSessionId, /^dsh-mcp-gateway-chatgpt-capability-[0-9a-f]{24}$/)
  assert.equal(helperSessionId.includes(process.cwd()), false)
  assert.equal(calls.resume.length, 0)
  assert.equal(calls.execute.length, 1)
  assert.equal(calls.execute[0].agent, agent)
}

{
  const { routes, calls } = makeContext({
    persisted: true,
    persistedHeader: { cwd: '/wrong/workspace' },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(calls.resume.length, 0)
  assert.equal(calls.create.length, 0)
  assert.equal(calls.execute.length, 0)
}

{
  const { routes, calls } = makeContext({
    persisted: true,
    persistedHeader: { agentPreset: 'wrong-preset' },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(calls.resume.length, 0)
  assert.equal(calls.create.length, 0)
  assert.equal(calls.execute.length, 0)
}

{
  const { routes, agent, calls, helperSessionId } = makeContext({ persisted: true })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: { command: 'true' } }), res)
  assert.equal(res.state.status, 200)
  assert.equal(calls.create.length, 0)
  assert.equal(calls.resume.length, 1)
  assert.equal(calls.resume[0].resumeSessionId, helperSessionId)
  assert.equal(calls.execute.length, 1)
  assert.equal(calls.execute[0].agent, agent)
}

{
  const first = makeContext({ defaultId: 'default' }).helperSessionId
  const second = makeContext({ defaultId: 'alternate' }).helperSessionId
  assert.notEqual(first, second)
}

{
  const { routes, calls, setDefaultId } = makeContext()
  const first = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), first)
  const firstHelper = calls.create[0].sessionId
  assert.equal(calls.execute[0].agent.id, firstHelper)

  setDefaultId('alternate')
  const catalog = responseCapture()
  await routes.get(`${PREFIX}/tools`)({ method: 'GET' }, catalog)
  assert.equal(calls.schemas.at(-1).agentPreset, 'alternate')

  const second = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), second)
  const secondHelper = calls.create[1].sessionId
  assert.notEqual(secondHelper, firstHelper)
  assert.equal(secondHelper, expectedCapabilitySessionId('alternate'))
  assert.equal(calls.execute[1].agent.id, secondHelper)
  assert.equal(calls.mountIds[1], 'alternate')
  assert.deepEqual(calls.disposed, [firstHelper])

  const third = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), third)
  assert.equal(calls.create.length, 2)
  assert.equal(calls.execute[2].agent.id, secondHelper)
}

{
  const dir = mkdtempSync(join(tmpdir(), 'dsh-bridge-preset-generation-'))
  const presetPath = join(dir, 'agent.cordis.yml')
  try {
    writeFileSync(presetPath, 'first')
    const { routes, calls } = makeContext({ presetPath })
    const first = responseCapture()
    await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), first)
    const firstHelper = calls.create[0].sessionId

    writeFileSync(presetPath, 'second-generation-is-longer')
    const second = responseCapture()
    await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), second)
    const secondHelper = calls.create[1].sessionId

    assert.notEqual(secondHelper, firstHelper)
    assert.equal(calls.create.length, 2)
    assert.equal(calls.execute[1].agent.id, secondHelper)
    assert.equal(calls.mountIds.length, 2)
    assert.deepEqual(calls.disposed, [firstHelper])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

{
  const dir = mkdtempSync(join(tmpdir(), 'dsh-bridge-preset-size-limit-'))
  const presetPath = join(dir, 'agent.cordis.yml')
  try {
    writeFileSync(presetPath, Buffer.alloc(4 * 1024 * 1024 + 1, 0x61))
    const { routes, calls } = makeContext({ presetPath })
    const response = responseCapture()
    await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), response)

    assert.equal(response.state.status, 500)
    assert.equal(response.state.body.error, 'bridge_error')
    assert.equal(calls.create.length, 0)
    assert.equal(calls.execute.length, 0)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

{
  const dir = mkdtempSync(join(tmpdir(), 'dsh-bridge-preset-content-generation-'))
  const presetPath = join(dir, 'agent.cordis.yml')
  try {
    writeFileSync(presetPath, 'allow-A\n')
    const fixedTime = new Date(Math.trunc(Date.now() / 1000) * 1000)
    utimesSync(presetPath, fixedTime, fixedTime)
    const original = statSync(presetPath)
    const { routes, calls } = makeContext({ presetPath })
    const first = responseCapture()
    await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), first)
    const firstHelper = calls.create[0].sessionId

    writeFileSync(presetPath, 'allow-B\n')
    utimesSync(presetPath, fixedTime, fixedTime)
    const replaced = statSync(presetPath)
    assert.equal(replaced.size, original.size)
    assert.equal(replaced.mtimeMs, original.mtimeMs)

    const second = responseCapture()
    await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), second)
    const secondHelper = calls.create[1].sessionId

    assert.notEqual(secondHelper, firstHelper)
    assert.equal(calls.create.length, 2)
    assert.equal(calls.execute[1].agent.id, secondHelper)
    assert.deepEqual(calls.disposed, [firstHelper])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

{
  let releaseResolve
  const resolveGate = new Promise(resolve => {
    releaseResolve = resolve
  })
  let blockOldResolve = true
  const expectedNewHelper = expectedCapabilitySessionId('alternate')
  const { routes, calls, setDefaultId } = makeContext({
    async resolveHook({ id }) {
      if (id === 'default' && blockOldResolve) {
        blockOldResolve = false
        await resolveGate
      }
    },
  })

  const oldRes = responseCapture()
  const oldCall = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), oldRes)
  await new Promise(resolve => setImmediate(resolve))

  setDefaultId('alternate')
  const newRes = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), newRes)
  assert.equal(newRes.state.status, 200)
  assert.equal(calls.execute.at(-1).agent.id, expectedNewHelper)

  releaseResolve()
  await oldCall
  assert.equal(oldRes.state.status, 200)
  assert.equal(calls.execute.at(-1).agent.id, expectedNewHelper)
  assert.equal(calls.create.filter(call => call.sessionId === expectedNewHelper).length, 1)
}

{
  let releaseFirst
  const firstGate = new Promise(resolve => {
    releaseFirst = resolve
  })
  const firstHelper = expectedCapabilitySessionId('default')
  const { routes, calls, setDefaultId } = makeContext({
    async executeHook(input) {
      if (input.agent.id === firstHelper) {
        await firstGate
      }
      return { isError: false, value: null, content: [{ type: 'text', text: 'ok' }] }
    },
  })

  const firstRes = responseCapture()
  const firstCall = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), firstRes)
  for (let attempt = 0; attempt < 20 && calls.execute.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(calls.execute.length, 1)

  setDefaultId('alternate')
  const secondRes = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), secondRes)
  assert.equal(secondRes.state.status, 200)
  assert.equal(calls.disposed.includes(firstHelper), false)

  releaseFirst()
  await firstCall
  assert.equal(firstRes.state.status, 200)
  assert.equal(calls.disposed.includes(firstHelper), true)
}

{
  let releaseFirst
  const firstGate = new Promise(resolve => {
    releaseFirst = resolve
  })
  const neverDispose = new Promise(() => {})
  const firstHelper = expectedCapabilitySessionId('default')
  const { routes, calls, setDefaultId } = makeContext({
    async executeHook(input) {
      if (input.agent.id === firstHelper) await firstGate
      return { isError: false, value: null, content: [{ type: 'text', text: 'ok' }] }
    },
    async disposeHook(id) {
      if (id === firstHelper) await neverDispose
    },
  })

  const firstRes = responseCapture()
  const firstCall = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), firstRes)
  for (let attempt = 0; attempt < 20 && calls.execute.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(calls.execute.length, 1)

  setDefaultId('alternate')
  const secondRes = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), secondRes)
  assert.equal(secondRes.state.status, 200)

  releaseFirst()
  const firstCompleted = await Promise.race([
    firstCall.then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(firstCompleted, true)
  assert.equal(firstRes.state.status, 200)
  assert.equal(calls.disposed.includes(firstHelper), true)
}

{
  const dir = mkdtempSync(join(tmpdir(), 'dsh-bridge-preset-race-'))
  const presetPath = join(dir, 'agent.cordis.yml')
  let mutateOnce = true
  try {
    writeFileSync(presetPath, 'first')
    const { routes, calls } = makeContext({
      presetPath,
      mountHook() {
        if (!mutateOnce) return
        mutateOnce = false
        writeFileSync(presetPath, 'second-generation-is-longer')
      },
    })

    const raced = responseCapture()
    await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), raced)
    assert.equal(raced.state.status, 500)
    assert.equal(raced.state.body.error, 'bridge_error')
    assert.equal(calls.execute.length, 0)

    const retried = responseCapture()
    await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), retried)
    assert.equal(retried.state.status, 200)
    assert.equal(calls.create.length, 2)
    assert.equal(calls.execute.length, 1)
    assert.equal(calls.mountIds.length, 2)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

{
  const { routes, calls } = makeContext({ listError: new Error('persistence unavailable') })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(res.state.body.message, 'internal DSH bridge operation failed')
  assert.equal(JSON.stringify(res.state.body).includes('persistence unavailable'), false)
  assert.equal(calls.create.length, 0)
  assert.equal(calls.resume.length, 0)
  assert.equal(calls.warnings.length, 2)
}

{
  const { routes, calls } = makeContext({ persisted: true, resumeError: new Error('resume failed') })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(JSON.stringify(res.state.body).includes('resume failed'), false)
  assert.equal(calls.resume.length, 1)
  assert.equal(calls.create.length, 0)
}

{
  const { routes, calls } = makeContext({ omitPresets: true })
  const res = responseCapture()
  await routes.get(`${PREFIX}/tools`)({ method: 'GET' }, res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(calls.schemas.length, 0)
}

{
  const { routes, calls } = makeContext({ omitAgents: true })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(calls.execute.length, 0)
}

{
  const { routes } = makeContext({
    executeResult: {
      isError: false,
      value: { image: true },
      content: [{ type: 'image', attachment: { id: 'image-1' } }],
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'read_image', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
}

{
  const { routes } = makeContext({
    attachments: {
      async readImage() {
        return {
          data: Buffer.from('image-bytes'),
          ref: {},
        }
      },
    },
    executeResult: {
      isError: false,
      value: { image: true },
      content: [{ type: 'image', attachment: { id: 'image-1' } }],
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'read_image', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
}

{
  const { routes } = makeContext({
    attachments: {
      async readImage() {
        return {
          data: Buffer.from('image-bytes'),
          ref: { mediaType: 'image/png' },
        }
      },
    },
    executeResult: {
      isError: false,
      value: { image: true },
      content: [{ type: 'image', attachment: { id: 'image-1' } }],
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'read_image', arguments: {} }), res)
  assert.equal(res.state.status, 200)
  assert.equal(res.state.body.content[0].type, 'image')
  assert.equal(res.state.body.content[0].mediaType, 'image/png')
  assert.equal(res.state.body.content[0].data, Buffer.from('image-bytes').toString('base64'))
}

{
  let activeReads = 0
  let maxActiveReads = 0
  const { routes } = makeContext({
    attachments: {
      async readImage(attachment) {
        activeReads += 1
        maxActiveReads = Math.max(maxActiveReads, activeReads)
        await new Promise(resolve => setTimeout(resolve, 1))
        activeReads -= 1
        return { data: Buffer.from(attachment.id), ref: { mediaType: 'image/png' } }
      },
    },
    executeResult: {
      isError: false,
      value: { images: true },
      content: [
        { type: 'image', attachment: { id: 'one' } },
        { type: 'image', attachment: { id: 'two' } },
      ],
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'read_images', arguments: {} }), res)
  assert.equal(res.state.status, 200)
  assert.equal(maxActiveReads, 1)
}

{
  let activeReads = 0
  let maxActiveReads = 0
  const { routes } = makeContext({
    attachments: {
      async readImage(attachment) {
        activeReads += 1
        maxActiveReads = Math.max(maxActiveReads, activeReads)
        await new Promise(resolve => setTimeout(resolve, 1))
        activeReads -= 1
        return { data: Buffer.from(attachment.id), ref: { mediaType: 'image/png' } }
      },
    },
    executeResult: {
      isError: false,
      value: { images: true },
      additionalContexts: [
        { role: 'user', content: [{ type: 'image', attachment: { id: 'one' } }] },
        { role: 'user', content: [{ type: 'image', attachment: { id: 'two' } }] },
      ],
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'read_images', arguments: {} }), res)
  assert.equal(res.state.status, 200)
  assert.equal(maxActiveReads, 1)
}

{
  const { routes } = makeContext({
    attachments: {
      async readImage() {
        return { data: Buffer.alloc(12 * 1024 * 1024 + 1), ref: { mediaType: 'image/png' } }
      },
    },
    executeResult: {
      isError: false,
      value: { image: true },
      content: [{ type: 'image', attachment: { id: 'oversize' } }],
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'read_image', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
}

{
  const { routes, calls } = makeContext({ executeError: new Error('/private/workspace/tool failed') })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: ' bash ', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(JSON.stringify(res.state.body).includes('/private/workspace'), false)
  assert.equal(calls.execute[0].name, 'bash')
  assert.equal(calls.warnings.length, 2)
}

{
  const { routes, calls } = makeContext({ skillListError: new Error('/private/skills unavailable') })
  const res = responseCapture()
  await routes.get(`${PREFIX}/skills`)({ method: 'GET' }, res)
  assert.equal(res.state.status, 500)
  assert.equal(res.state.body.error, 'bridge_error')
  assert.equal(JSON.stringify(res.state.body).includes('/private/skills'), false)
  assert.equal(calls.warnings.length, 2)
}

{
  let releaseSetup
  const setupGate = new Promise(resolve => {
    releaseSetup = resolve
  })
  const { routes, calls } = makeContext({ mountHook: () => setupGate })
  const res = responseCapture()
  const pending = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  for (let attempt = 0; attempt < 20 && calls.mountIds.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(calls.mountIds.length, 1)
  res.emitClose()
  releaseSetup()
  await pending
  assert.equal(calls.execute.length, 0)
  assert.equal(res.state.status, undefined)
  assert.equal(calls.warnings.length, 0)
}

{
  let releaseSetup
  const setupGate = new Promise(resolve => {
    releaseSetup = resolve
  })
  const { routes, calls, setDefaultId } = makeContext({ mountHook: () => setupGate })
  const res = responseCapture()
  const pending = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  for (let attempt = 0; attempt < 20 && calls.mountIds.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(calls.mountIds.length, 1)
  const firstHelper = calls.create[0].sessionId
  res.emitClose()
  const completedAfterDisconnect = await Promise.race([
    pending.then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(completedAfterDisconnect, true)
  assert.equal(calls.execute.length, 0)
  releaseSetup()
  for (let attempt = 0; attempt < 200 && calls.create[0]?.handle === undefined; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  setDefaultId('alternate')
  const second = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), second)
  assert.equal(second.state.status, 200)
  assert.equal(calls.disposed.includes(firstHelper), true)
  assert.equal(res.state.status, undefined)
  assert.equal(calls.warnings.length, 0)
}

{
  const never = new Promise(() => {})
  const { routes, calls } = makeContext({
    createHook(options) {
      if (calls.create.length === 1) return never
      return Promise.resolve({
        agent: { id: options.sessionId },
        async dispose() {
          calls.disposed.push(options.sessionId)
        },
      })
    },
  })
  const first = responseCapture()
  const firstPending = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), first)
  for (let attempt = 0; attempt < 20 && calls.create.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(calls.create.length, 1)
  first.emitClose()
  const completedAfterDisconnect = await Promise.race([
    firstPending.then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(completedAfterDisconnect, true)

  const second = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), second)
  assert.equal(calls.create.length, 2)
  assert.equal(second.state.status, 200)
  assert.equal(calls.execute.length, 1)
}

{
  const never = new Promise(() => {})
  let setDefaultId
  const state = makeContext({
    resolveHook({ id }) {
      if (id === 'default') setDefaultId('alternate')
    },
    createHook(options) {
      if (state.calls.create.length === 1) return never
      return Promise.resolve({
        agent: { id: options.sessionId },
        async dispose() {
          state.calls.disposed.push(options.sessionId)
        },
      })
    },
  })
  setDefaultId = state.setDefaultId

  const first = responseCapture()
  const firstPending = state.routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), first)
  for (let attempt = 0; attempt < 20 && state.calls.create.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(state.calls.create.length, 1)
  assert.equal(state.calls.create[0].sessionId, expectedCapabilitySessionId('alternate'))
  first.emitClose()
  const completedAfterDisconnect = await Promise.race([
    firstPending.then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(completedAfterDisconnect, true)

  const second = responseCapture()
  const secondPending = state.routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), second)
  const secondCompleted = await Promise.race([
    secondPending.then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  if (!secondCompleted) {
    second.emitClose()
    await secondPending
  }
  assert.equal(secondCompleted, true)
  assert.equal(state.calls.create.length, 2)
  assert.equal(second.state.status, 200)
  assert.equal(state.calls.execute.length, 1)
}


{
  let releaseResolve
  const resolveGate = new Promise(resolve => {
    releaseResolve = resolve
  })
  const { routes, calls } = makeContext({
    async resolveHook() {
      await resolveGate
    },
  })
  const res = responseCapture()
  const pending = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  await new Promise(resolve => setImmediate(resolve))
  res.emitClose()
  const completedAfterDisconnect = await Promise.race([
    pending.then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(completedAfterDisconnect, true)
  assert.equal(calls.create.length, 0)

  releaseResolve()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls.create.length, 0)
}

{
  const never = new Promise(() => {})
  const { routes, cleanups, calls } = makeContext({
    async disposeHook() {
      await never
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  assert.equal(res.state.status, 200)
  assert.equal(calls.create.length, 1)

  const cleanup = cleanups.get('dsh-chatgpt-bridge.capability-agent')
  const teardownCompleted = await Promise.race([
    cleanup().then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(teardownCompleted, true)
  assert.equal(calls.disposed.length, 1)
}

{
  const never = new Promise(() => {})
  const { routes, cleanups, calls } = makeContext({
    createHook() {
      return never
    },
  })
  const res = responseCapture()
  const pending = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  for (let attempt = 0; attempt < 20 && calls.create.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(calls.create.length, 1)
  const cleanup = cleanups.get('dsh-chatgpt-bridge.capability-agent')
  const teardownCompleted = await Promise.race([
    cleanup().then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(teardownCompleted, true)
  res.emitClose()
  await pending
}

{
  const { routes, calls } = makeContext({ executeWaitForAbort: true })
  const res = responseCapture()
  const pending = routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: {} }), res)
  for (let attempt = 0; attempt < 20 && calls.execute.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(calls.execute.length, 1)
  assert.equal(calls.execute[0].signal.aborted, false)
  res.emitClose()
  await pending
  assert.equal(calls.execute[0].signal.aborted, true)
  assert.equal(res.state.status, undefined)
  assert.equal(calls.warnings.length, 0)
}

{
  let readStarted = false
  const never = new Promise(() => {})
  const { routes, calls } = makeContext({
    attachments: {
      async readImage() {
        readStarted = true
        return never
      },
    },
    executeResult: {
      isError: false,
      value: { image: true },
      content: [{ type: 'image', attachment: { id: 'image-1' } }],
    },
  })
  const res = responseCapture()
  const pending = routes.get(`${PREFIX}/call`)(postJson({ name: 'read_image', arguments: {} }), res)
  for (let attempt = 0; attempt < 20 && !readStarted; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(readStarted, true)
  res.emitClose()
  const completedAfterDisconnect = await Promise.race([
    pending.then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 50)),
  ])
  assert.equal(completedAfterDisconnect, true)
  assert.equal(res.state.status, undefined)
  assert.equal(calls.warnings.length, 0)
}

{
  const { routes, calls } = makeContext()
  const badArguments = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'bash', arguments: [] }), badArguments)
  assert.equal(badArguments.state.status, 400)
  assert.deepEqual(badArguments.state.body, {
    error: 'invalid_request',
    message: 'arguments must be an object when supplied',
  })
  assert.equal(calls.create.length, 0)
  assert.equal(calls.execute.length, 0)
}

{
  const { routes } = makeContext()
  const invalid = responseCapture()
  await routes.get(`${PREFIX}/call`)(postRaw('{not-json'), invalid)
  assert.equal(invalid.state.status, 400)
  assert.deepEqual(invalid.state.body, {
    error: 'invalid_request',
    message: 'request body must be valid JSON',
  })

  const tooLarge = responseCapture()
  await routes.get(`${PREFIX}/call`)(postRaw(Buffer.alloc(1_000_001, 0x20)), tooLarge)
  assert.equal(tooLarge.state.status, 413)
  assert.deepEqual(tooLarge.state.body, {
    error: 'request_too_large',
    message: 'request body too large',
  })

  const whitespace = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: '   ', arguments: {} }), whitespace)
  assert.equal(whitespace.state.status, 400)
  assert.equal(whitespace.state.body.error, 'invalid_request')
}

{
  const { routes } = makeContext({
    executeResult: {
      isError: false,
      value: null,
      content: [{ type: 'text', text: 'x'.repeat(16 * 1024 * 1024) }],
    },
  })
  const res = responseCapture()
  await routes.get(`${PREFIX}/call`)(postJson({ name: 'large_result', arguments: {} }), res)
  assert.equal(res.state.status, 500)
  assert.deepEqual(res.state.body, {
    error: 'bridge_error',
    message: 'DSH bridge response exceeds the configured size limit',
  })
}

console.log('chatgpt-bridge-standing-scope-and-capability-session-ok')
