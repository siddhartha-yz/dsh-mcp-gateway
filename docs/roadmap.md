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

### P2 — Define an external ChatGPT capability profile — COMPLETE

Do not expose every DSH ToolRuntime entry blindly through `dsh_tool_catalog`.

Separate tools that make sense for an external reasoning agent from tools that assume DSH's own AgentLoop lifecycle.

Typical externally useful tools:

- filesystem/read/write/edit/glob/grep
- shell and jobs
- deterministic utilities
- image reading
- skills, via the dedicated skill meta-tools rather than generic ToolRuntime invocation
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

Acceptance result: production now exposes the `chatgpt-external-v1` capability profile with 21 externally meaningful ToolRuntime entries. DSH AgentLoop/lifecycle tools are absent from discovery and direct guessed calls such as `workflow` and `create_goal` fail closed with `tool_unavailable` before execution. Approved filesystem, shell/jobs, deterministic utilities, web, plugin discovery, and `read_image` remain usable; native Skills continue through the separate SkillRegistry meta-tools. The Python gateway contains no duplicate allowlist. Live deployment commit `998999420463f5daf42634357d9195d7fc9e9a2f` passed service readiness, guarded `bash`, image materialization, and skill-catalog verification.

### P3 — Persistent Task State for ChatGPT — COMPLETE

Borrow only the useful task-state concepts from local-shell-mcp. P3 is a durable state container for work owned by ChatGPT, not a Logical Session runtime, Goal Mode, or second harness.

Hard architectural constraint:

> A task stores state only. It must never own an AgentLoop, invoke a model, choose the next action, automatically continue execution, spawn reasoning agents, or become a second reasoning agent.

Desired properties:

- durable task identity
- resumable state across ChatGPT conversations/reconnections
- compact checkpoints describing what happened, current progress, and candidate next steps
- optional goal/plan fields as passive data only, never control flow
- explicit create/get/list/update/checkpoint/pause/complete semantics
- workspace and related commit/reference metadata where useful
- state owned by DSH/plugin storage, not by a new gateway-side harness

Explicit non-goals:

- no autonomous DSH AgentLoop
- no model/API-provider invocation
- no `while goal incomplete -> decide -> execute` loop
- no automatic ChatGPT continuation or `app.sendMessage`
- no subagent orchestration
- no persistent shell state; that belongs to P4
- no browser-session state; that belongs to P5

Acceptance goal: after a ChatGPT conversation ends, a later ChatGPT conversation can load a compact task checkpoint and continue the work predictably, while all reasoning and next-action decisions remain in ChatGPT.

Acceptance result: `dsh-task-state-plugin` provides one reviewed `task_state` ToolRuntime capability backed by DSH `storageDomain`, with create/get/list/update/checkpoint/pause/resume/complete, bounded checkpoint history, and optimistic `if_revision` writes. Isolated-host tests proved durable JSON storage across full Host restart/reopen, passive pause/resume, list-based recovery, and stale-revision rejection without any model credential. Live production at commit `5a4c035343c8b19992b8b638dd7588621148bacc` exposed 22 external tools including `task_state`, and a completely fresh ChatGPT conversation recovered the repository, deployed commit, status, and remaining acceptance exclusively through `task_state` list/get. This validates cross-conversation resume while keeping all reasoning and next-action decisions in ChatGPT.

### P4 — Add persistent shell sessions — COMPLETE

Borrow LSM's persistent-shell ergonomics because DSH's current `bash` tool starts a fresh shell for each call.

Desired properties:

- named persistent shell/session identity
- preserved cwd/environment/process context where appropriate
- bounded output and cancellation
- explicit lifecycle/status tools
- DSH policy/sandbox remains authoritative

Acceptance goal: interactive CLI workflows no longer require reconstructing shell state manually between tool calls.

Acceptance result: P4 uses DSH's native `TerminalSessionService` and `terminal-bash` backend rather than copying local-shell-mcp's tmux/PTY layer. A thin reviewed `shell_session` ToolRuntime adapter exposes open/list/status/send/read/signal/close through an isolated ChatGPT-only terminal registry while DSH retains exact-Agent authorization, cwd/environment/process persistence, bounded scrollback/output, foreground signals, timeouts, process-tree cleanup, and shared sandbox policy. The existing one-shot `bash` remains available. Adapter/wiring tests and the full repository gate passed, including 194/194 Python tests. Live production at commit `5a94267e65eb9743f6a381ca48d4b1c8f59693a1` exposed 23 external tools including `shell_session`; a named native PTY preserved cwd and an exported environment variable across separate tool calls, bounded `read` returned retained scrollback, `wait=false` plus `SIGINT` interrupted a long foreground command and left the shell reusable, an attempted write under `/etc` remained sandbox-denied, and `close` removed the session cleanly. This validates persistent interactive shell state without introducing a second agent or bypassing DSH policy.

### P5 — Add a browser plugin — COMPLETE

Borrow the proven persistent browser-session model from LSM rather than embedding browser logic into the gateway.

Target capabilities:

- browser session create/list/close
- snapshot/inspect
- act
- script execution when explicitly appropriate
- persistent page/session state

Acceptance goal: browser automation is a normal DSH plugin capability reachable through the stable meta-tool path.

Acceptance result: P5 is implemented as a repository-local `browser_session` ToolRuntime plugin rather than adopting `dsh-browseruse` wholesale. The reviewed third-party plugin was rejected as the production surface because it carries its own LLM-driven `browser_task`, scheduler, and process-global browser singleton. The P5 plugin instead keeps ChatGPT as the only reasoning agent and exposes only open/list/status/snapshot/act/script/close. Each live browser session is owned by the exact DSH Agent. DSH remains the file-effect policy authority: the plugin resolves `sandboxPolicy` and asks the configured sandbox provider to validate the policy fail-closed. On this Ubuntu host the generic DSH Linux fallback is Landlock, but production testing proved that Landlock blocks `/proc/self/uid_map`, so Chromium cannot initialize its own userns sandbox inside that wrapper. For `workspace-write`, the plugin therefore uses the exact reviewed `@deepseek-ai/dsh-sandbox-local` 0.1.2-rc.1 bwrap mount profile derived from the same DSH policy: root read-only, private `/proc`, private `/tmp`, and only the DSH workspace bind-mounted writable. A narrow `dsh-browser-worker` systemd boundary has AppArmor attach the dedicated `userns` profile before the worker sets `NoNewPrivs=1`; the worker accepts spawn requests only from the live DSH Host MainPID using Unix `SO_PEERCRED`, validates the exact bwrap profile and pinned Chromium (or pinned Chromium directly for danger-full-access), rejects `--no-sandbox`, and owns process cleanup. Browser profile data lives inside bwrap's private `/tmp`. Root provisioning now verifies the complete AppArmor + effective DSH uid/gid + NNP + bwrap + nested-userns chain before reporting success. The runtime lock pins `playwright-core` 1.62.1 and Koffi 3.2.1. Local CI-equivalent gates pass 196 Python tests plus the JS/systemd/runtime-lock suites. Production at commit `e9c5f5b873e58d5303e326ff601350e6029dad3c` exposes 24 external tools including `browser_session`; live acceptance proved full-enforcement workspace-write Chromium startup, lossless snapshot refs, ref-driven fill/click, cross-call DOM/JavaScript persistence, two-page lifecycle, screenshot attachment rendering, clean close/not-found semantics, an empty owner session list after close, and no surviving Chromium process for the closed session. CI run `34031556400` passed 5/5. This completes P5 without adding another model loop, autonomous browser agent, scheduler, `--no-sandbox`, or a global weakening of Ubuntu userns policy.

### P6 — Experimental DSH GUI control surface for ChatGPT Web

P6 is now being explored on the isolated branch `experiment/dsh-gui-chat-resume` rather than directly on production `main`.

The preferred user-facing surface is the official DSH Web GUI, with only a small client-plugin/overlay modification where possible. ChatGPT Web chat remains the primary reasoning agent, but its real frontend may run hidden/background while DSH GUI becomes the panel the human actually uses.

The transport experiments are intentionally ordered from least to most invasive: official MCP App `ui/message` bridge, hybrid official-send plus injected read observer, full ChatGPT frontend injection, robust browser automation, and only then a disposable timer/click proof. Workspace Agents API and Responses API are explicitly out of scope because they replace the current ChatGPT-Web reasoning plane.

Current experiment evidence: the official DSH Web client-plugin seam is proven, and a real ChatGPT MCP App `ui/message` request has been proven to create the next turn in the same ChatGPT Web conversation. The original manual proof button is being replaced by an automatic outbound relay from a DSH-owned GUI mailbox through an app-only MCP transport tool. The remaining B1 question is receive-side lifecycle observation: automatic sending alone does not yet prove that the bridge can reliably detect completed turns and decide when a mechanical continuation is safe.

Detailed experiment plan and fallback ladder: [`experiments/dsh-gui-chatgpt-web.md`](experiments/dsh-gui-chatgpt-web.md).

The GUI/bridge must remain a control and transport surface over the existing ChatGPT + DSH architecture, not become another model loop or harness implementation.

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
