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

The primary compatibility surface is deliberately small and stable. ChatGPT can keep the same approved MCP tools while DSH gains new community extensions: `dsh_tool_catalog` reads the live DSH ToolRuntime catalog and `dsh_tool_call` executes any discovered capability through `ctx.tools.execute(...)`, so DSH policy/guards remain authoritative. This path does not require ChatGPT to refresh or re-approve its MCP tool snapshot.

Generic Harness operations cover both DSH tools and DSH community skills:

- `dsh_tool_catalog`: reads the current DSH `ToolRuntime` schema catalog.
- `dsh_tool_call`: executes a discovered tool through DSH's guarded `ToolRuntime` pipeline.
- `dsh_skill_catalog`: lists model-invocable entries from DSH's native `SkillRegistry` for the Harness workspace.
- `dsh_skill_load`: loads one compatible community skill's instructions from that registry.

The default gateway mode is deliberately **meta-only**: `tools/list` exposes only those four stable DSH meta-tools, individual DSH ToolRuntime schemas are not projected into ChatGPT, and the gateway does not advertise the modern tool-list change subscription. This makes the frozen-snapshot property an enforced protocol boundary rather than an assumption about client behavior. Operators may explicitly enable `--tool-surface projected` as a separate UX mode when they want compatible DSH tools to appear as first-class MCP tools.

The bridge now uses DSH's own AgentRegistry and agent-preset composition only as a **capability/scope identity**: it creates one idle DSH Agent, mounts the deployment's default preset through `agentPresets.mount(...)`, never submits a prompt to that Agent, and passes the resulting Agent to `ToolRuntime.schemas(agent)` / `ToolRuntime.execute({ ..., agent })` and SkillRegistry scope lookup. This preserves DSH's preset restrictions, guards, jobs/filesystem/session ownership, and community-extension scope instead of promoting scoped registrations to globals.

For native DSH tools whose execution is gated by the routed model's declared modalities, the idle capability Agent points at a local **metadata-only ChatGPT Web route**. That route declares `text` + `image` input so DSH's own `read_image` gate can validate the external ChatGPT consumer, but its `stream()` method always fails and the bridge never drives the Agent. It therefore performs no inference, requires no model API key, and cannot become a hidden second model. A real rc6 smoke verified preset-scoped `bash` execution and native `read_image` returning an image block through the bridge with no provider credential configured.

DSH `additionalContexts` are also preserved across the external-agent boundary. DSH uses these follow-up user contexts for guard reminders and for nested Code Mode results such as an image returned by `run_code`. The bridge materializes attachment-backed images inside those contexts and the MCP adapter appends their visible text/image blocks to the tool result, so replacing DSH's own model loop with ChatGPT Web does not silently discard policy or nested multimodal context.

This means adding a compatible tool or skill to the normal DSH preset composition no longer requires another Python wrapper, gateway restart, or ChatGPT app re-publication. The fixed meta-tools discover the live catalog on demand. In the optional `projected` tool-surface mode only, the DSH-side bridge tracks native `tools/change` invalidations and the gateway publishes MCP `tools/list_changed`, allowing clients that support dynamic refresh to gain first-class tool entries.

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
- Stable meta-tools are the correctness path for extension discovery/invocation; dynamic first-class tool projection is optional UX.
- Tool execution stays inside DSH's own guarded `ToolRuntime` pipeline.
- The public OAuth/MCP boundary stays outside the loopback DSH Web Host.
- local-shell-mcp is an optional execution/access provider and reference, not the primary harness.
- Gateway-owned session/continuation experiments are legacy work, not the current architecture.

## License

MIT. See [`LICENSE`](LICENSE). Security reporting guidance is in [`SECURITY.md`](SECURITY.md).
