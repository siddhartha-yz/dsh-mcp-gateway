import { randomUUID } from 'node:crypto'

export const name = 'dsh-chatgpt-bridge'
export const inject = ['webServer', 'tools']

const PREFIX = '/api/chatgpt-bridge'
const MAX_BODY_BYTES = 1_000_000

function json(res, status, body) {
  const data = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(data),
    'cache-control': 'no-store',
  })
  res.end(data)
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
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: `${PREFIX}/tools`,
    handler: (_req, res) => {
      json(res, 200, { tools: ctx.tools.schemas() })
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
        const result = await ctx.tools.execute({
          callId: `chatgpt-${randomUUID()}`,
          name: payload.name,
          arguments: payload.arguments ?? {},
          signal: AbortSignal.timeout(120_000),
        })
        json(res, 200, result)
      } catch (error) {
        json(res, 400, {
          error: 'bridge_error',
          message: error instanceof Error ? error.message : String(error),
        })
      }
    },
  }))
}
