/**
 * Agent-scoped visibility filter for tools registered by dsh-mcp-client.
 *
 * This is a model-facing composition filter, not a security boundary. The
 * local-shell-mcp workspace/policy remains the authority boundary.
 */

export const name = 'dsh-lsm-tool-filter'
export const inject = ['tools']

export function apply(ctx, config = {}) {
  const serverName = typeof config.serverName === 'string' && config.serverName.length > 0
    ? config.serverName
    : 'lsm'
  const rawNames = Array.isArray(config.allowRawNames)
    ? config.allowRawNames.filter(value => typeof value === 'string' && value.length > 0)
    : []
  const prefix = `mcp__${serverName}__`
  const configured = new Set(rawNames.map(rawName => `${prefix}${rawName}`))

  ctx.on('agent/created', ({ agent }) => {
    // Read the agent's pre-filter inherited surface. The standard Web preset is
    // an ancestor scope, so an allow-list at the agent scope would also hide
    // DSH's preset tools. Instead dynamically deny only this MCP provider's
    // currently registered names that were not selected. New LSM tools are
    // therefore hidden by default without naming or filtering DSH tools.
    const visibleNames = ctx.tools.schemas(agent).map(schema => schema.name)
    const lsmNames = visibleNames.filter(toolName => toolName.startsWith(prefix))
    const known = new Set(lsmNames)
    const missing = [...configured].filter(toolName => !known.has(toolName))
    if (missing.length > 0) {
      ctx.logger.warn(
        `dsh-lsm-tool-filter: configured tools are unavailable: ${missing.join(', ')}`,
      )
    }

    const deny = lsmNames.filter(toolName => !configured.has(toolName))
    if (deny.length > 0) agent.ctx.tools.restrict({ deny })
  })
}
