# Architecture

The canonical product contract is [`../AGENTS.md`](../AGENTS.md): **give ChatGPT Web a mature DSH Harness**.

`dsh-mcp-gateway` is an access/compatibility adapter between ChatGPT Web and DSH. ChatGPT is the primary reasoning/model agent; DSH is the harness/runtime authority. The gateway owns only the public OAuth/MCP boundary and compatibility required to expose DSH capabilities to ChatGPT.

## Current data path

```text
ChatGPT Web
    |
    | OAuth + MCP
    v
dsh-mcp-gateway
    |
    | loopback capability bridge
    v
DSH Harness
    |
    +-- ToolRuntime
    +-- SkillRegistry
    +-- jobs / filesystem / policy / MCP clients
    `-- optional execution providers
```

The gateway does not own a second session runtime or autonomous AgentLoop. Removed prototype session routing, public-SDK agent control, and gateway-owned continuation state remain available through git history rather than the production tree.

## Stable ChatGPT surface

The default ChatGPT-facing surface is deliberately fixed to four meta-tools:

- `dsh_tool_catalog`
- `dsh_tool_call`
- `dsh_skill_catalog`
- `dsh_skill_load`

`dsh_skill_catalog` reads the live preset-scoped SkillRegistry. `dsh_tool_catalog` reads the preset-scoped ToolRuntime only after the DSH-side `chatgpt-external-v1` profile has projected capabilities with clear external-agent semantics. `dsh_tool_call` enforces the same profile again before executing through DSH `ToolRuntime.execute(...)`, so a hidden tool cannot be invoked merely by guessing its name. DSH remains responsible for guards, execution policy, and the external capability policy; the Python OAuth gateway does not carry a second allowlist.

The default profile admits deterministic utilities, filesystem operations, shell/jobs, web, image reading, and plugin discovery. AgentLoop lifecycle/orchestration tools remain reserved for architecture review, while reviewed community ToolRuntime entries can be explicitly admitted with the bridge's `allowExtraTools` configuration. Skills remain on their dedicated meta-tool surface.

The optional `projected` surface is only a UX mode. It projects the same approved DSH tools as first-class MCP tools and publishes tool-list changes, but correctness must never depend on it.

## DSH bridge identity

Discovery uses the default preset standing scope and does not start a model turn. DSH currently requires an Agent identity for some execution-time policy and modality checks, so the bridge lazily creates or resumes a metadata-only capability Agent for execution. Its route declares the external ChatGPT modalities but cannot perform inference. No model-provider credential is required.

The capability identity is bound to the workspace and preset generation. Preset changes therefore do not silently reuse an execution identity composed against stale configuration. Tool results preserve visible DSH text/image content and `additionalContexts` across the external-agent boundary.

## Security boundary

The DSH Web Host and bridge are internal, loopback-facing runtime endpoints. Public clients terminate at the OAuth-protected gateway. Production deployment keeps DNS-rebinding protection enabled and derives allowed public Host/Origin values from the configured HTTPS origin.

The gateway persists OAuth state only. Harness state belongs to DSH or DSH plugins.

## local-shell-mcp relationship

local-shell-mcp is an optional execution provider and implementation reference, not a second harness. DSH-native tools, jobs, skills, policy, and MCP composition should be preferred when they already cover a need. Differentiated LSM capabilities such as persistent browser sessions, persistent shell ergonomics, or remote workers may be adapted as small DSH plugins when useful.

The decision rule is: integrate or copy the smallest useful mechanism into the DSH plugin layer rather than growing gateway runtime state.
