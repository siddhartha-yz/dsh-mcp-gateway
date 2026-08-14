import { randomUUID } from 'node:crypto'

export const name = 'dsh-chatgpt-bridge'
export const inject = ['webServer', 'tools', 'skills', 'llm']

const PREFIX = '/api/chatgpt-bridge'
const MAX_BODY_BYTES = 1_000_000
const EXTERNAL_PROVIDER = 'chatgpt-web-external'
const EXTERNAL_MODEL = 'chatgpt-web'

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
  const data = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(data),
    'cache-control': 'no-store',
  })
  res.end(data)
}

async function materializeToolContent(ctx, result) {
  if (!Array.isArray(result?.content)) return result
  const attachments = ctx.get('attachments')
  if (!attachments || !result.content.some(block => block?.type === 'image' && block.attachment)) return result

  const content = await Promise.all(result.content.map(async (block) => {
    if (block?.type !== 'image' || !block.attachment) return block
    const stored = await attachments.readImage(block.attachment)
    return {
      type: 'image',
      data: Buffer.from(stored.data).toString('base64'),
      mediaType: stored.ref.mediaType,
    }
  }))
  return { ...result, content }
}

async function readJson(req) {
  let size = 0
  const chunks = []
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += buffer.length
    if (size > MAX_BODY_BYTES) throw new Error('request body too large')
    chunks.push(buffer)
  }
  if (chunks.length === 0) return {}
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

export function apply(ctx) {
  let capabilityHandlePromise

  // Some native DSH tools gate their output modality through the calling
  // agent's exact model route. The bridge therefore registers a metadata-only
  // route describing ChatGPT Web's accepted modalities. Its stream method is a
  // hard failure: DSH can use the route only for capability checks, never for
  // inference. ChatGPT remains the sole reasoning/model agent.
  ctx.llm.registerAdapter([EXTERNAL_PROVIDER], new ExternalChatGPTCapabilityAdapter())

  async function capabilityAgent() {
    const agents = ctx.get('agents')
    const presets = ctx.get('agentPresets')
    if (!agents || !presets) return undefined
    if (capabilityHandlePromise) return (await capabilityHandlePromise).agent

    capabilityHandlePromise = agents.create({
      sessionId: `chatgpt-bridge-${randomUUID()}`,
      agentOptions: {
        provider: EXTERNAL_PROVIDER,
        model: EXTERNAL_MODEL,
      },
      meta: {
        cwd: process.cwd(),
        agentPreset: presets.defaultId,
      },
      // This Agent is only DSH's native scope/capability identity. The bridge
      // never submits a prompt to it. Its metadata-only route cannot perform
      // inference, while ToolRuntime sees the exact preset-scoped world DSH
      // intended for a session.
      setup: async agentCtx => {
        await presets.mount(agentCtx)
      },
    }).catch((error) => {
      capabilityHandlePromise = undefined
      throw error
    })
    return (await capabilityHandlePromise).agent
  }

  ctx.effect(() => async () => {
    const pending = capabilityHandlePromise
    capabilityHandlePromise = undefined
    if (!pending) return
    try {
      const handle = await pending
      await handle.dispose()
    } catch {
      // Creation failures already roll their unpublished scope back.
    }
  }, 'dsh-chatgpt-bridge.capability-agent')

  async function scopedLookup() {
    const agent = await capabilityAgent()
    return {
      agent,
      skillOptions: {
        cwd: agent?.session?.header?.meta?.cwd ?? process.cwd(),
        ...(agent ? { scope: agent } : {}),
      },
    }
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/tools`,
    handler: async (_req, res) => {
      try {
        const { agent } = await scopedLookup()
        json(res, 200, {
          tools: ctx.tools.schemas(agent),
          scope: agent ? 'dsh-agent-preset' : 'global',
        })
      } catch (error) {
        json(res, 500, {
          error: 'bridge_error',
          message: error instanceof Error ? error.message : String(error),
        })
      }
    },
  }))

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/skills`,
    handler: async (_req, res) => {
      try {
        const { skillOptions } = await scopedLookup()
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
        json(res, 500, {
          error: 'bridge_error',
          message: error instanceof Error ? error.message : String(error),
        })
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
        const { skillOptions } = await scopedLookup()
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
        json(res, 400, {
          error: 'bridge_error',
          message: error instanceof Error ? error.message : String(error),
        })
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
      try {
        const payload = await readJson(req)
        if (typeof payload !== 'object' || payload === null || Array.isArray(payload) || typeof payload.name !== 'string' || payload.name.length === 0) {
          json(res, 400, { error: 'invalid_request', message: 'name must be a non-empty string' })
          return
        }
        const { agent } = await scopedLookup()
        const result = await ctx.tools.execute({
          callId: `chatgpt-${randomUUID()}`,
          name: payload.name,
          arguments: payload.arguments ?? {},
          signal: AbortSignal.timeout(120_000),
          ...(agent ? { agent } : {}),
        })
        json(res, 200, await materializeToolContent(ctx, result))
      } catch (error) {
        json(res, 400, {
          error: 'bridge_error',
          message: error instanceof Error ? error.message : String(error),
        })
      }
    },
  }))
}
