import { createHash, randomUUID } from 'node:crypto'
import { readFile, stat } from 'node:fs/promises'

export const name = 'dsh-chatgpt-bridge'
export const inject = ['webServer', 'tools', 'skills', 'llm', 'agents', 'agentPresets']

const PREFIX = '/api/chatgpt-bridge'
const MAX_BODY_BYTES = 1_000_000
const MAX_RESPONSE_BYTES = 16 * 1024 * 1024
const TOOL_CALL_TIMEOUT_MS = 120_000
const EXTERNAL_PROVIDER = 'chatgpt-web-external'
const EXTERNAL_MODEL = 'chatgpt-web'
const CAPABILITY_SESSION_PREFIX = 'dsh-mcp-gateway-chatgpt-capability'

async function presetStamp(path) {
  const metadata = await stat(path)
  const digest = createHash('sha256').update(await readFile(path)).digest('hex')
  return { mtimeMs: metadata.mtimeMs, size: metadata.size, digest }
}

function samePresetStamp(left, right) {
  return left.mtimeMs === right.mtimeMs && left.size === right.size && left.digest === right.digest
}

function capabilitySessionId(cwd, presetId, presetPath, stamp) {
  const suffix = createHash('sha256')
    .update(JSON.stringify([cwd, presetId, presetPath, stamp.mtimeMs, stamp.size, stamp.digest]))
    .digest('hex')
    .slice(0, 24)
  return `${CAPABILITY_SESSION_PREFIX}-${suffix}`
}

function assertPersistedHelperHeader(header, cwd, presetId) {
  if (header.cwd !== cwd || header.agentPreset !== presetId) {
    throw new Error('persisted DSH bridge capability session metadata does not match the current workspace/preset')
  }
}

class ExternalChatGPTCapabilityAdapter {
  providerInfo(provider) {
    return { id: provider, name: 'ChatGPT Web (external capability identity)' }
  }

  listModels(provider) {
    return Promise.resolve([{
      provider,
      id: EXTERNAL_MODEL,
      name: 'ChatGPT Web',
      inputModalities: ['text', 'image'],
    }])
  }

  resolveModel(provider, model) {
    return Promise.resolve({
      provider,
      id: model,
      name: 'ChatGPT Web',
      inputModalities: ['text', 'image'],
    })
  }

  providerRetryPolicy() {
    return undefined
  }

  async *stream() {
    throw new Error('ChatGPT Web is external to DSH; the capability identity cannot perform model inference')
  }
}

function json(res, status, body) {
  let data = JSON.stringify(body)
  if (Buffer.byteLength(data) > MAX_RESPONSE_BYTES) {
    status = 500
    data = JSON.stringify({ error: 'bridge_error', message: 'DSH bridge response exceeds the configured size limit' })
  }
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(data),
    'cache-control': 'no-store',
  })
  res.end(data)
}

class BridgeRequestError extends Error {
  constructor(status, code, message) {
    super(message)
    this.name = 'BridgeRequestError'
    this.status = status
    this.code = code
  }
}

function replyBridgeFailure(ctx, res, operation, error) {
  if (error instanceof BridgeRequestError) {
    json(res, error.status, { error: error.code, message: error.message })
    return
  }
  ctx.logger.warn(`dsh-chatgpt-bridge: ${operation} failed`)
  ctx.logger.warn(error instanceof Error ? error : new Error(String(error)))
  json(res, 500, { error: 'bridge_error', message: 'internal DSH bridge operation failed' })
}

function toolCallLifetime(res) {
  const disconnected = new AbortController()
  const onClose = () => {
    if (!res.writableEnded && !disconnected.signal.aborted) {
      disconnected.abort(new Error('bridge client disconnected before tool call completed'))
    }
  }
  res.once('close', onClose)
  if (res.destroyed && !res.writableEnded) onClose()
  return {
    disconnected: disconnected.signal,
    signal: AbortSignal.any([
      AbortSignal.timeout(TOOL_CALL_TIMEOUT_MS),
      disconnected.signal,
    ]),
    dispose() {
      res.off('close', onClose)
    },
  }
}

function awaitWithinToolCallLifetime(promise, lifetime, onLateResolve) {
  const disposeLateValue = value => {
    void Promise.resolve(onLateResolve(value)).catch(() => {})
  }
  if (lifetime.signal.aborted) {
    promise.then(disposeLateValue, () => {})
    return Promise.reject(lifetime.signal.reason)
  }
  let removeAbortListener
  const aborted = new Promise((_, reject) => {
    const onAbort = () => reject(lifetime.signal.reason)
    lifetime.signal.addEventListener('abort', onAbort, { once: true })
    removeAbortListener = () => lifetime.signal.removeEventListener('abort', onAbort)
  })
  return Promise.race([promise, aborted]).then(
    value => {
      removeAbortListener?.()
      return value
    },
    error => {
      removeAbortListener?.()
      if (lifetime.signal.aborted) promise.then(disposeLateValue, () => {})
      throw error
    },
  )
}

async function materializeContentBlocks(ctx, blocks) {
  if (!Array.isArray(blocks)) return blocks
  const hasAttachmentImage = blocks.some(block => block?.type === 'image' && block.attachment)
  if (!hasAttachmentImage) return blocks
  const attachments = ctx.get('attachments')
  if (!attachments) throw new Error('DSH attachments service is unavailable for image materialization')

  return Promise.all(blocks.map(async (block) => {
    if (block?.type !== 'image' || !block.attachment) return block
    const stored = await attachments.readImage(block.attachment)
    const mediaType = stored?.ref?.mediaType
    if (typeof mediaType !== 'string' || !mediaType) {
      throw new Error('DSH attachments service returned an image without a media type')
    }
    return {
      type: 'image',
      data: Buffer.from(stored.data).toString('base64'),
      mediaType,
    }
  }))
}

async function materializeToolContent(ctx, result) {
  if (!result || typeof result !== 'object') return result
  const content = await materializeContentBlocks(ctx, result.content)
  let additionalContexts = result.additionalContexts
  if (Array.isArray(additionalContexts)) {
    additionalContexts = await Promise.all(additionalContexts.map(async (message) => {
      if (!message || typeof message !== 'object') return message
      return {
        ...message,
        content: await materializeContentBlocks(ctx, message.content),
      }
    }))
  }
  return { ...result, content, additionalContexts }
}

async function readJson(req) {
  let size = 0
  const chunks = []
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += buffer.length
    if (size > MAX_BODY_BYTES) {
      throw new BridgeRequestError(413, 'request_too_large', 'request body too large')
    }
    chunks.push(buffer)
  }
  if (chunks.length === 0) return {}
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new BridgeRequestError(400, 'invalid_request', 'request body must be valid JSON')
    }
    throw error
  }
}

export function apply(ctx) {
  const capabilityHandles = new Map()
  const instanceId = randomUUID()
  let toolRevision = 1
  let skillRevision = 1

  ctx.effect(() => ctx.on('tools/change', () => {
    toolRevision += 1
  }), 'dsh-chatgpt-bridge.tools-change')

  ctx.effect(() => ctx.on('skills/change', () => {
    skillRevision += 1
  }), 'dsh-chatgpt-bridge.skills-change')

  // Some native DSH tools gate their output modality through the calling
  // agent's exact model route. The bridge therefore registers a metadata-only
  // route describing ChatGPT Web's accepted modalities. Its stream method is a
  // hard failure: DSH can use the route only for capability checks, never for
  // inference. ChatGPT remains the sole reasoning/model agent.
  ctx.llm.registerAdapter([EXTERNAL_PROVIDER], new ExternalChatGPTCapabilityAdapter())

  function forceDisposeCapabilityEntry(helperSessionId, entry) {
    if (entry.disposePromise) return entry.disposePromise
    entry.disposePromise = (async () => {
      try {
        const handle = await entry.promise
        await handle.dispose()
      } catch {
        // Creation/resume failures already roll their unpublished scope back.
      } finally {
        if (capabilityHandles.get(helperSessionId) === entry) {
          capabilityHandles.delete(helperSessionId)
        }
      }
    })()
    return entry.disposePromise
  }

  function maybeDisposeCapabilityEntry(helperSessionId, entry) {
    if (!entry.stale || entry.leases !== 0) return undefined
    return forceDisposeCapabilityEntry(helperSessionId, entry)
  }

  function retireOtherCapabilityEntries(currentHelperSessionId) {
    for (const [helperSessionId, entry] of capabilityHandles) {
      if (helperSessionId === currentHelperSessionId) continue
      entry.stale = true
      void maybeDisposeCapabilityEntry(helperSessionId, entry)
    }
  }

  async function acquireCapabilityAgent(signal) {
    const assertActive = () => {
      if (signal?.aborted) throw signal.reason
    }
    const agents = ctx.get('agents')
    const presets = ctx.get('agentPresets')
    if (!agents || !presets) {
      throw new Error('DSH AgentRegistry and agentPresets services are required for bridge tool execution')
    }

    const presetId = presets.defaultId
    const preset = await presets.resolve(presetId)
    assertActive()
    const stamp = await presetStamp(preset.path)
    assertActive()
    if (presets.defaultId !== presetId) return acquireCapabilityAgent(signal)
    const helperSessionId = capabilitySessionId(process.cwd(), presetId, preset.path, stamp)

    let entry = capabilityHandles.get(helperSessionId)
    if (entry?.disposePromise) {
      await entry.disposePromise
      assertActive()
      entry = capabilityHandles.get(helperSessionId)
    }
    if (!entry) {
      const agentOptions = {
        provider: EXTERNAL_PROVIDER,
        model: EXTERNAL_MODEL,
      }
      const setup = async agentCtx => {
        const mountedPreset = await presets.mount(agentCtx, presetId)
        const mountedStamp = await presetStamp(mountedPreset.path)
        if (mountedPreset.path !== preset.path || !samePresetStamp(stamp, mountedStamp)) {
          throw new Error('DSH preset composition changed while creating the bridge capability agent')
        }
      }
      const promise = (async () => {
        const persistence = ctx.get('sessionPersistence')
        if (persistence) {
          const persisted = await persistence.list()
          assertActive()
          const persistedHelper = persisted.find(header => header.id === helperSessionId)
          if (persistedHelper) {
            assertPersistedHelperHeader(persistedHelper, process.cwd(), presetId)
            return agents.resume({
              resumeSessionId: helperSessionId,
              agentOptions,
              setup,
            })
          }
        }
        assertActive()
        return agents.create({
          sessionId: helperSessionId,
          agentOptions,
          meta: {
            cwd: process.cwd(),
            agentPreset: presetId,
          },
          // This Agent is only DSH's native execution identity. The bridge never
          // submits a prompt to it. Its metadata-only route cannot perform
          // inference; a workspace+preset+composition-generation id lets
          // persistence reuse the same helper across DSH restarts without
          // colliding with another workspace or pinning execution to stale scope.
          setup,
        })
      })()
      entry = {
        promise,
        leases: 0,
        stale: false,
        disposePromise: undefined,
      }
      capabilityHandles.set(helperSessionId, entry)
      promise.catch(() => {
        if (capabilityHandles.get(helperSessionId) === entry) {
          capabilityHandles.delete(helperSessionId)
        }
      })
    }

    entry.leases += 1
    let verified = false
    try {
      const handle = signal
        ? await awaitWithinToolCallLifetime(entry.promise, { signal }, () => {})
        : await entry.promise
      if (presets.defaultId !== presetId) {
        entry.stale = true
        entry.leases -= 1
        await maybeDisposeCapabilityEntry(helperSessionId, entry)
        return acquireCapabilityAgent(signal)
      }
      const currentPreset = await presets.resolve(presetId)
      const currentStamp = await presetStamp(currentPreset.path)
      if (
        presets.defaultId !== presetId
        || currentPreset.path !== preset.path
        || !samePresetStamp(stamp, currentStamp)
      ) {
        entry.stale = true
        entry.leases -= 1
        await maybeDisposeCapabilityEntry(helperSessionId, entry)
        return acquireCapabilityAgent(signal)
      }
      entry.stale = false
      retireOtherCapabilityEntries(helperSessionId)
      verified = true
      let released = false
      return {
        agent: handle.agent,
        async release() {
          if (released) return
          released = true
          entry.leases -= 1
          // A stale helper is no longer on the request's correctness path.
          // Its DSH dispose hook may itself stall, so do not keep a completed
          // bridge request alive waiting for best-effort retirement.
          void Promise.resolve(maybeDisposeCapabilityEntry(helperSessionId, entry)).catch(() => {})
        },
      }
    } catch (error) {
      if (!verified) entry.stale = true
      entry.leases -= 1
      if (signal?.aborted && entry.leases === 0 && capabilityHandles.get(helperSessionId) === entry) {
        capabilityHandles.delete(helperSessionId)
        void entry.promise.then(handle => handle.dispose(), () => {})
      } else {
        await maybeDisposeCapabilityEntry(helperSessionId, entry)
      }
      throw error
    }
  }

  ctx.effect(() => async () => {
    for (const [helperSessionId, entry] of capabilityHandles) {
      if (capabilityHandles.get(helperSessionId) === entry) {
        capabilityHandles.delete(helperSessionId)
      }
      // DSH creation/resume and handle disposal do not expose cancellation.
      // Teardown must therefore only trigger best-effort disposal, not wait on
      // an operation that can stall indefinitely. Pending handles are disposed
      // as soon as they arrive; settled handles follow the same path.
      void entry.promise.then(handle => handle.dispose(), () => {})
    }
  }, 'dsh-chatgpt-bridge.capability-agent')

  async function standingLookup() {
    const presets = ctx.get('agentPresets')
    if (!presets) {
      throw new Error('DSH agentPresets service is required for bridge discovery')
    }
    const presetId = presets.defaultId
    const scope = await presets.standingKeyFor(presetId)
    return {
      scope,
      skillOptions: {
        cwd: process.cwd(),
        scope,
      },
    }
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/revision`,
    handler: async (_req, res) => {
      json(res, 200, { instanceId, toolRevision, skillRevision })
    },
  }))

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/tools`,
    handler: async (_req, res) => {
      try {
        const { scope } = await standingLookup()
        json(res, 200, {
          tools: ctx.tools.schemas(scope),
          scope: 'dsh-preset-standing',
        })
      } catch (error) {
        replyBridgeFailure(ctx, res, 'tool catalog', error)
      }
    },
  }))

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/skills`,
    handler: async (_req, res) => {
      try {
        const { skillOptions } = await standingLookup()
        const skills = await ctx.skills.list(skillOptions)
        json(res, 200, {
          skills: skills
            .filter(skill => skill.invocation?.modelInvocable === true)
            .map(skill => ({
              name: skill.name,
              description: skill.description,
              ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
              source: skill.source,
              provider: skill.provider,
              ...(skill.resourceBase ? { resourceBase: skill.resourceBase } : {}),
            })),
        })
      } catch (error) {
        replyBridgeFailure(ctx, res, 'skill catalog', error)
      }
    },
  }))

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/skill`,
    handler: async (req, res) => {
      if (req.method !== 'POST') {
        json(res, 405, { error: 'method_not_allowed' })
        return
      }
      try {
        const payload = await readJson(req)
        const skillName = typeof payload?.name === 'string' ? payload.name.trim() : ''
        if (!skillName) {
          json(res, 400, { error: 'invalid_request', message: 'name must be a non-empty string' })
          return
        }
        const { skillOptions } = await standingLookup()
        const summary = (await ctx.skills.list(skillOptions)).find(skill => skill.name === skillName)
        if (!summary || summary.invocation?.modelInvocable !== true) {
          json(res, 404, { error: 'skill_unavailable', message: `skill "${skillName}" is unavailable for model invocation` })
          return
        }
        const skill = await ctx.skills.get(skillName, skillOptions)
        if (!skill || skill.invocation?.modelInvocable !== true) {
          json(res, 404, { error: 'skill_unavailable', message: `skill "${skillName}" is unavailable for model invocation` })
          return
        }
        json(res, 200, {
          skill: {
            name: skill.name,
            description: skill.description,
            ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
            source: skill.source,
            provider: skill.provider,
            ...(skill.resourceBase ? { resourceBase: skill.resourceBase } : {}),
            content: skill.content,
          },
        })
      } catch (error) {
        replyBridgeFailure(ctx, res, 'skill load', error)
      }
    },
  }))

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/call`,
    handler: async (req, res) => {
      if (req.method !== 'POST') {
        json(res, 405, { error: 'method_not_allowed' })
        return
      }
      let lifetime
      let capability
      try {
        const payload = await readJson(req)
        const toolName = typeof payload?.name === 'string' ? payload.name.trim() : ''
        if (!toolName) {
          json(res, 400, { error: 'invalid_request', message: 'name must be a non-empty string' })
          return
        }
        lifetime = toolCallLifetime(res)
        const toolArguments = payload.arguments ?? {}
        if (typeof toolArguments !== 'object' || toolArguments === null || Array.isArray(toolArguments)) {
          throw new BridgeRequestError(400, 'invalid_request', 'arguments must be an object when supplied')
        }
        capability = await awaitWithinToolCallLifetime(
          acquireCapabilityAgent(lifetime.signal),
          lifetime,
          lateCapability => lateCapability.release(),
        )
        if (lifetime.disconnected.aborted) return
        const result = await ctx.tools.execute({
          callId: `chatgpt-${randomUUID()}`,
          name: toolName,
          arguments: toolArguments,
          signal: lifetime.signal,
          agent: capability.agent,
        })
        if (lifetime.disconnected.aborted) return
        const materialized = await awaitWithinToolCallLifetime(
          materializeToolContent(ctx, result),
          lifetime,
          () => {},
        )
        if (!lifetime.disconnected.aborted) json(res, 200, materialized)
      } catch (error) {
        if (!lifetime?.disconnected.aborted) {
          replyBridgeFailure(ctx, res, 'tool call', error)
        }
      } finally {
        await capability?.release()
        lifetime?.dispose()
      }
    },
  }))
}
