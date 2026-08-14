# dsh-mcp-gateway

**Give ChatGPT Web a mature DSH Harness.**

ChatGPT Web remains the only primary reasoning/model agent. This project connects a normal ChatGPT conversation to DSH through MCP, so DSH-managed tools, skills, jobs, sessions, policies, MCP clients, and compatible community extensions can become ChatGPT capabilities without rebuilding a bespoke wrapper for each extension.

The repository-level architecture contract is [`AGENTS.md`](AGENTS.md). When older prototype code or documentation conflicts with that contract, the contract wins unless the repository owner explicitly changes the product direction.

## Target architecture

```text
ChatGPT Web Chat
        |
     OAuth + MCP
        |
        v
 ChatGPT <-> DSH adapter
        |
        v
     DSH Harness
        |
        +-- DSH tools / skills / extensions
        +-- sessions / jobs / policy / MCP clients
        `-- optional execution providers
                 |
                 +-- local machine
                 `-- selected local-shell-mcp capabilities
```

The adapter should be as thin and generic as practical. DSH is the harness/runtime authority; local-shell-mcp is primarily a source of proven public-tunnel/OAuth/remote-worker patterns and selected differentiated execution capabilities, not the primary harness.

## Relationship to local-shell-mcp

local-shell-mcp is not the harness core here. Its useful contributions are the proven ChatGPT Remote MCP/OAuth and public-tunnel patterns plus differentiated execution capabilities such as remote workers and browser control. Those capabilities may be composed behind DSH when useful.

The recent LSM session/continuation work remains useful reference material, but this repository does not copy that runtime into the gateway. DSH owns harness lifecycle and capability composition.

## Current status

The first direct DSH Harness seam is implemented. A tiny DSH-resident Cordis plugin uses DSH's own `ctx.tools` registry:

```text
DSH tool/community plugin
        | ctx.tools.register(...)
        v
DSH ToolRuntime
        | schemas() / execute()
        v
loopback ChatGPT bridge
        |
        v
OAuth MCP gateway
        |
        v
ChatGPT Web
```

The gateway now projects DSH ToolRuntime schemas as first-class MCP tools in harness mode. On every MCP `tools/list`, it reads the current DSH catalog and exposes compatible DSH tool names/descriptions/JSON parameter schemas directly to ChatGPT; calls to those projected tools are forwarded back through `ctx.tools.execute(...)`, so DSH policy/guards remain authoritative.

Generic Harness operations cover both DSH tools and DSH community skills:

- `dsh_tool_catalog`: reads the current DSH `ToolRuntime` schema catalog.
- `dsh_tool_call`: executes a discovered tool through DSH's guarded `ToolRuntime` pipeline.
- `dsh_skill_catalog`: lists model-invocable entries from DSH's native `SkillRegistry` for the Harness workspace.
- `dsh_skill_load`: loads one compatible community skill's instructions from that registry.

The bridge now uses DSH's own AgentRegistry and agent-preset composition only as a **capability/scope identity**: it creates one idle DSH Agent, mounts the deployment's default preset through `agentPresets.mount(...)`, never submits a prompt to that Agent, and passes the resulting Agent to `ToolRuntime.schemas(agent)` / `ToolRuntime.execute({ ..., agent })` and SkillRegistry scope lookup. This preserves DSH's preset restrictions, guards, jobs/filesystem/session ownership, and community-extension scope instead of promoting scoped registrations to globals.

For native DSH tools whose execution is gated by the routed model's declared modalities, the idle capability Agent points at a local **metadata-only ChatGPT Web route**. That route declares `text` + `image` input so DSH's own `read_image` gate can validate the external ChatGPT consumer, but its `stream()` method always fails and the bridge never drives the Agent. It therefore performs no inference, requires no model API key, and cannot become a hidden second model. A real rc6 smoke verified preset-scoped `bash` execution and native `read_image` returning an image block through the bridge with no provider credential configured.

This means adding a compatible tool or skill to the normal DSH preset composition no longer requires another Python wrapper or gateway restart; it is available through the same generic bridge. Automatic MCP `tools/list_changed` notification is a later refinement rather than a prerequisite for direct projection.

The DSH-side bridge plugin is in [`dsh-bridge-plugin/`](dsh-bridge-plugin/) and the deployment overlay is [`deploy/dsh/chatgpt-bridge.cordis.yml`](deploy/dsh/chatgpt-bridge.cordis.yml).

## OAuth / MCP

The existing embedded OAuth implementation is retained. It provides persisted dynamic client registration, PKCE/public-client support, owner approval, resource/issuer-bound tokens, refresh rotation, revocation, bounded registration state, and DNS-rebinding protection for the public MCP endpoint.

The primary harness mode requires no model provider API key. Run the DSH Web Host with the bridge overlay, then point the OAuth gateway at its loopback bridge:

```sh
python -m pip install --constraint deploy/server-constraints.txt -e '.[server]'

export DSH_MCP_GATEWAY_ADMIN_PIN='choose-a-long-owner-pin'

dsh-mcp-gateway \
  --public-base-url https://gateway.example.com \
  --dsh-harness-url http://127.0.0.1:3080 \
  --state-dir .dsh-mcp-gateway
```

The public endpoint is:

```text
https://gateway.example.com/mcp
```

`GET /healthz` checks the gateway process. In harness mode, `GET /readyz` checks the loopback DSH capability bridge; it never probes an LLM provider.

## Optional legacy DSH adapter

Earlier development explored using DeepSeek Harness as a second autonomous Agent. That is no longer the default product direction. The experimental DSH Web Host adapter is retained only as an opt-in compatibility/research path:

```sh
dsh-mcp-gateway \
  --public-base-url https://gateway.example.com \
  --dsh-web-url http://127.0.0.1:3080 \
  --dsh-cwd /path/to/workspace
```

Only when this option is supplied are the legacy `dsh_*` MCP tools registered and `/readyz` made dependent on that Host. Historical evidence, goal-round experiments, deployment material, and rc6 restart notes are preserved in [`docs/legacy-dsh-prototype.md`](docs/legacy-dsh-prototype.md).

No DeepSeek API key is required for the primary ChatGPT-to-DSH harness path.

## Development

```sh
python -m pip install -e '.[dev]'
ruff check src tests scripts
python -m unittest discover -s tests -v
```

The server dependency graph used by CI is pinned in [`deploy/server-constraints.txt`](deploy/server-constraints.txt).

## Design invariants

- ChatGPT is the only primary reasoning Agent.
- DSH is the harness/runtime authority.
- DSH community capabilities should cross the adapter generically rather than acquire bespoke gateway wrappers.
- Tool execution stays inside DSH's own guarded `ToolRuntime` pipeline.
- The public OAuth/MCP boundary stays outside the loopback DSH Web Host.
- local-shell-mcp is an optional execution/access provider and reference, not the primary harness.
- Gateway-owned session/continuation experiments are legacy work, not the current architecture.

## License

No license has been selected yet.
