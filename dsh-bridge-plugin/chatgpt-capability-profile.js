export const CHATGPT_CAPABILITY_PROFILE_ID = 'chatgpt-external-v1'

// These ToolRuntime entries have clear semantics when ChatGPT Web is the sole
// reasoning agent and DSH is only the guarded execution harness.
export const DEFAULT_CHATGPT_TOOL_NAMES = Object.freeze([
  'find_dsh_plugin',
  'calculator',
  'json',
  'regex',
  'stat',
  'csv',
  'encoding',
  'schema',
  'time',
  'bash',
  'read',
  'write',
  'edit',
  'glob',
  'grep',
  'job_output',
  'job_list',
  'job_kill',
  'web_search',
  'web_fetch',
  'read_image',
])

// These tools assume DSH's own AgentLoop/session/orchestration lifecycle. They
// stay unavailable to the external ChatGPT capability profile until a future
// architecture phase explicitly defines their external semantics. A generic
// plugin opt-in must not silently bypass that review.
export const REVIEW_REQUIRED_DSH_AGENT_TOOL_NAMES = Object.freeze([
  'ask_user_question',
  'todo_write',
  'get_goal',
  'create_goal',
  'update_goal',
  'send_message',
  'interrupt_agent',
  'list_agents',
  'workflow',
  'ralph',
  'exit_plan_mode',
  'subagent',
  'subagent_fork',
])

// Skills already have a dedicated external surface through dsh_skill_catalog
// and dsh_skill_load. Keeping the ToolRuntime `skill` helper out of the generic
// catalog avoids two overlapping ways to perform the same operation.
export const DEDICATED_CHATGPT_SURFACE_TOOL_NAMES = Object.freeze(['skill'])

const reservedToolNames = new Set([
  ...REVIEW_REQUIRED_DSH_AGENT_TOOL_NAMES,
  ...DEDICATED_CHATGPT_SURFACE_TOOL_NAMES,
])

function explicitExtraTools(config) {
  const value = config?.allowExtraTools
  if (value === undefined) return []
  if (!Array.isArray(value)) {
    throw new TypeError('dsh-chatgpt-bridge allowExtraTools must be an array of tool names')
  }

  const extras = []
  const seen = new Set()
  for (const rawName of value) {
    if (typeof rawName !== 'string' || rawName.trim() !== rawName || rawName.length === 0) {
      throw new TypeError('dsh-chatgpt-bridge allowExtraTools entries must be non-empty trimmed strings')
    }
    if (seen.has(rawName)) {
      throw new TypeError(`dsh-chatgpt-bridge allowExtraTools contains duplicate tool ${JSON.stringify(rawName)}`)
    }
    if (reservedToolNames.has(rawName)) {
      throw new TypeError(
        `dsh-chatgpt-bridge allowExtraTools cannot expose reserved tool ${JSON.stringify(rawName)} without architecture review`,
      )
    }
    seen.add(rawName)
    extras.push(rawName)
  }
  return extras
}

export function buildChatGPTCapabilityProfile(config = {}) {
  const extraToolNames = Object.freeze(explicitExtraTools(config))
  const allowedToolNames = new Set([...DEFAULT_CHATGPT_TOOL_NAMES, ...extraToolNames])

  return Object.freeze({
    id: CHATGPT_CAPABILITY_PROFILE_ID,
    defaultToolNames: DEFAULT_CHATGPT_TOOL_NAMES,
    extraToolNames,
    allows(toolName) {
      return allowedToolNames.has(toolName)
    },
    project(schemas) {
      if (!Array.isArray(schemas)) {
        throw new TypeError('DSH ToolRuntime schemas must be an array')
      }
      return schemas.filter(schema => schema && allowedToolNames.has(schema.name))
    },
  })
}
