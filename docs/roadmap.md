# DSH personal roadmap

This roadmap records the current product direction for the next large update so that future maintenance does not drift back into legacy experiments.

## Core invariant

Keep the architecture simple:

```text
ChatGPT Web
    -> stable dsh_* meta tools
    -> DSH Harness
    -> DSH-native tools / skills / plugins / optional execution providers
```

ChatGPT remains the primary reasoning agent. DSH remains the harness/runtime authority. `dsh-mcp-gateway` should stay a thin OAuth/MCP/compatibility adapter rather than grow into a second harness.

## Priority order

### P0 — Remove legacy harness baggage — COMPLETE

Completed before adding major features: the repository is simplified around the current architecture.

Removed from the production tree:

- `--dsh-web-url`
- `--legacy-session-runtime`
- `ExperimentalWebHostBackend`
- `PublicSdkBackend`
- `GatewayService`
- `SessionRouter`
- `DurableSessionRuntime`
- tests and documentation that exist only for those legacy paths

Historical design evidence may be retained in an archive document if useful, but it should not remain production complexity.

Acceptance result: the gateway production path now consists of OAuth/MCP transport plus the DSH Harness bridge; the old gateway-owned harness runtime is retained only in git history.

### P1 — Upgrade the DSH runtime baseline — COMPLETE

Moved the deployment from the old `0.1.0-rc.6` baseline to `0.1.2-rc.1` and revalidated every bridge seam used by the project.

Current bridge-sensitive seams include:

- `agentPresets.standingKeyFor(...)`
- preset resolution/mounting
- `ctx.tools.schemas(...)`
- `ctx.tools.execute(...)`
- `ctx.skills`
- attachment/image materialization

Upgrade conservatively: prefer a current release-candidate baseline over an alpha unless an alpha-only capability is required.

Acceptance result: live runtime, deployment lockfile, bridge peer dependencies, tests, and the pinned local reference source all describe DSH `0.1.2-rc.1`. Post-upgrade verification from ChatGPT confirmed the four stable `dsh_*` meta tools, a 34-tool DSH ToolRuntime catalog, one native skill, live bridge/gateway readiness, and successful guarded tool execution.

### P2 — Define an external ChatGPT capability profile

Do not expose every DSH ToolRuntime entry blindly through `dsh_tool_catalog`.

Separate tools that make sense for an external reasoning agent from tools that assume DSH's own AgentLoop lifecycle.

Typical externally useful tools:

- filesystem/read/write/edit/glob/grep
- shell and jobs
- deterministic utilities
- image reading
- skills
- explicitly approved plugins

Tools requiring special review before exposure include DSH-agent-oriented lifecycle/orchestration tools such as:

- `create_goal` / `update_goal`
- `subagent` / `subagent_fork`
- `workflow`
- `ralph`
- `send_message`
- `ask_user_question`
- `exit_plan_mode`

Acceptance goal: the catalog exposed to ChatGPT has clear semantics under the "ChatGPT is the agent" architecture.

### P3 — Add a ChatGPT-oriented Logical Session / Plan capability

Borrow the useful concepts from local-shell-mcp without importing its whole harness.

Desired properties:

- durable logical task identity
- resumable state across ChatGPT conversations/reconnections
- compact checkpoints rather than full model ownership
- optional Goal/Plan state attached to the logical task
- explicit status, resume, pause, completion, and recovery semantics
- state owned by DSH/plugin storage, not by a new gateway-side harness

Acceptance goal: a long-running ChatGPT task can be resumed predictably without requiring DSH to become a second reasoning agent.

### P4 — Add persistent shell sessions

Borrow LSM's persistent-shell ergonomics because DSH's current `bash` tool starts a fresh shell for each call.

Desired properties:

- named persistent shell/session identity
- preserved cwd/environment/process context where appropriate
- bounded output and cancellation
- explicit lifecycle/status tools
- DSH policy/sandbox remains authoritative

Acceptance goal: interactive CLI workflows no longer require reconstructing shell state manually between tool calls.

### P5 — Add a browser plugin

Borrow the proven persistent browser-session model from LSM rather than embedding browser logic into the gateway.

Target capabilities:

- browser session create/list/close
- snapshot/inspect
- act
- script execution when explicitly appropriate
- persistent page/session state

Acceptance goal: browser automation is a normal DSH plugin capability reachable through the stable meta-tool path.

### P6 — Consider a Live Workspace experience

Only after sessions, persistent shell, and browser are stable, evaluate a lightweight ChatGPT-facing workspace/status UI inspired by LSM Live Workspace.

The UI must remain an observer/control surface over DSH state, not become another harness implementation.

### P7 — Remote workers only when a real need appears

Remote-worker execution is useful but should not be copied pre-emptively. Add it only when local-host execution becomes an actual limitation.

## Explicitly do not duplicate for now

DSH already has suitable native abstractions for these areas, so avoid copying the LSM equivalents:

- generic file tools
- jobs
- Skill system / SkillRegistry
- generic MCP/plugin discovery and invocation
- DSH Web UI/TUI equivalents
- another OAuth implementation
- another full harness/AgentLoop
- broad packaging/integration surface merely for feature parity

## Rule for copying from local-shell-mcp

For every LSM feature considered, decide in this order:

1. Does DSH already provide a good native abstraction?
2. Can the feature be implemented as a small DSH plugin?
3. Can LSM be used only as an optional execution provider instead of copied?
4. Only then copy the smallest useful mechanism.

The goal is not feature parity with LSM. The goal is a smaller DSH that is easier for its owner to understand, maintain, and use.
