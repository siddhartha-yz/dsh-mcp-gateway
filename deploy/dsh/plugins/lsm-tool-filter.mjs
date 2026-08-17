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
    // The standard Web preset is an ancestor scope, so an allow-list at the
    // agent scope would also hide DSH's preset tools. Use deny snapshots only
    // for this MCP namespace, and extend them when dsh-mcp-client re-syncs a
    // changed tools/list generation. DSH deny masks admit later unnamed globals,
    // so a one-time snapshot is insufficient for a long-lived agent.
    const denied = new Set()
    const refreshDeny = () => {
      const visibleNames = ctx.tools.schemas(agent).map(schema => schema.name)
      const deny = visibleNames.filter(toolName => (
        toolName.startsWith(prefix)
        && !configured.has(toolName)
        && !denied.has(toolName)
      ))
      if (deny.length === 0) return visibleNames

      // Mark before restrict(): ToolRuntime publishes tools/change when the new
      // restriction is installed, and the listener below may run synchronously.
      for (const toolName of deny) denied.add(toolName)
      try {
        agent.ctx.tools.restrict({ deny })
      } catch (error) {
        for (const toolName of deny) denied.delete(toolName)
        throw error
      }
      return visibleNames
    }

    const initialVisibleNames = refreshDeny()
    const known = new Set(initialVisibleNames.filter(toolName => toolName.startsWith(prefix)))
    const missing = [...configured].filter(toolName => !known.has(toolName))
    if (missing.length > 0) {
      ctx.logger.warn(
        `dsh-lsm-tool-filter: configured tools are unavailable: ${missing.join(', ')}`,
      )
    }

    agent.ctx.effect(
      () => ctx.on('tools/change', refreshDeny),
      'dsh-lsm-tool-filter.tools-change',
    )
  })
}
