# dsh-mcp-gateway

Experimental MCP gateway for controlling durable [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) agent sessions from ChatGPT and other MCP clients.

The project is intentionally small at the start. It does **not** fork or mirror DeepSeek Harness or local-shell-mcp. Its job is to bridge a client-facing MCP/OAuth control plane to a persistent DSH agent runtime.

## Why

MCP clients and chat conversations are comparatively short-lived, while autonomous coding and research tasks may need to continue for hours. DeepSeek Harness already provides durable session logs, goal rounds, subagents, workflows, tools, and persistence. The missing boundary is a stable way for an external MCP client to create, observe, steer, and later resume those sessions.

The target architecture is:

```text
ChatGPT / MCP client
        |
     OAuth + MCP
        |
        v
 dsh-mcp-gateway
        |
  stable control API
        |
        v
 DeepSeek Harness
        |
 durable agent session
```

A separate execution MCP such as local-shell-mcp can remain available *to the DSH agent* when its remote-machine, browser, or other execution capabilities are useful.

## Current status

Early control-plane prototype.

The first invariant implemented here is session routing:

```text
requested session
    |
    +-- live ------> reuse
    |
    +-- persisted -> resume
    |
    `-- absent ----> create
```

A persisted session must never silently fall back to `create` if resume fails; doing so turns a recoverable transport/runtime problem into a session-id collision or split-brain state.

The transport-independent control service and MCP v2 tool surface are implemented. `PublicSdkBackend` covers the live-session path for an injected public DSH SDK client and persists a small gateway-owned session catalog. That catalog deliberately recognizes known ids after restart so this transport fails closed instead of recreating them; with the current public SDK, a catalogued cold session raises `ColdResumeUnavailable` before any new prompt is sent.

For restart-capable operation, `ExperimentalWebHostBackend` targets the DSH developer-preview Web Host API behind loopback/private networking. It has been validated against the official `@deepseek-ai/dsh@0.1.0-rc.6` runtime: a persisted session can be reopened after the entire DSH Host process is stopped and restarted with the same `DSH_HOME`, and a later prompt continues the same durable history. The adapter also exposes structured goal creation plus explicit CAS-based goal status/resume/pause controls.

An embedded OAuth prototype is present for MCP deployments: persisted dynamic clients/tokens, an owner approval page, refresh-token rotation, resource-bound access tokens, and MCP SDK authorization routes. The issuer is canonicalized once so metadata, RFC 9207 callback `iss`, and token claims use the same URL. This remains experimental infrastructure rather than a production security claim; the intended deployment keeps the DSH Host on loopback and terminates public HTTPS in front of the gateway.

Current MCP tools:

```text
dsh_start
dsh_continue
dsh_status
dsh_history
dsh_list
dsh_cancel
dsh_goal_status
dsh_goal_create
dsh_goal_resume
dsh_goal_pause
```

The MCP layer depends only on the stable gateway backend contract; it does not know whether DSH is reached through the Python SDK, ACP, a protocol-driver plugin, or a future official resumable API.

## Evidence behind the design

A local proof of concept using DeepSeek Harness `0.1.0rc6` verified that an initial controlling request can return while a DSH goal continues issuing autonomous goal rounds in the same live runtime. A later controller can send another prompt to the same live session and the model receives the retained history.

The current public Python SDK still cannot cold-resume an existing persisted session: a fresh SDK runtime follows the create path and hits a persisted-log id collision. The experimental Web Host adapter closes that transport gap without changing the MCP/service contract. In a real rc6 restart test, the first turn produced 22 durable events; after stopping and restarting the whole Host, the same session resumed, accepted a second turn, and retained both prompts in one history (37 events, two `turn/end` records).

Goal lifecycle has a separate safety boundary. DSH deliberately does not persist process-local goal activation: after session resume, a durable goal may still be `phase=active` while automatic continuation remains disarmed. A real rc6 test confirmed that cold-resuming the Agent did not create a new goal round; an explicit `goal.resume` CAS mutation re-armed continuation. The gateway therefore exposes explicit goal controls rather than silently auto-rearming goals after restart.

## Milestone 1: persistent DSH session over MCP

- Start a DSH session from an MCP client.
- Return a session id without holding the MCP call open for the full autonomous task.
- Observe status/events while the DSH goal continues in the background.
- Send steering prompts from a later MCP/chat session.
- Cold-resume a persisted DSH session after the gateway/runtime process restarts.
- Keep DSH-specific protocol churn behind one adapter.

## Development

The routing/control core has no third-party runtime dependencies:

```sh
python3 -m unittest discover -s tests -v
```

To exercise the real MCP v2 schemas as well:

```sh
python -m pip install -e '.[server]'
python -m unittest discover -s tests -v
```

The server extra currently targets the stable MCP Python SDK v2 line (`mcp>=2,<3`). See [`docs/architecture.md`](docs/architecture.md) for the boundary decisions.

## Run the gateway

The current deployable path expects an external DSH Web Host on loopback and puts the OAuth/MCP gateway in front of it. The gateway itself also binds loopback by default; terminate public HTTPS with a reverse proxy or tunnel.

```sh
python -m pip install -e '.[server]'

export DSH_MCP_GATEWAY_ADMIN_PIN='choose-a-long-owner-pin'

dsh-mcp-gateway \
  --dsh-web-url http://127.0.0.1:3080 \
  --dsh-cwd /path/to/agent/workspace \
  --public-base-url https://gateway.example.com
```

The public MCP endpoint is `<public-base-url>/mcp`. Do not expose the raw DSH Web Host directly; the experimental adapter refuses non-loopback DSH targets by default.

A deterministic long-task flow is:

```text
dsh_start(prompt, optional session_id)
        -> returns session_id

dsh_goal_create(session_id, objective, optional max_goal_rounds)
        -> creates + arms the durable goal

later chat / MCP session
        -> dsh_status / dsh_history / dsh_goal_status
        -> dsh_continue for steering

if the DSH Host itself restarted
        -> session cold-resumes when addressed again
        -> dsh_goal_resume explicitly re-arms autonomous continuation
```

## Relationship to local-shell-mcp

This repository is independent from `fwerkor/local-shell-mcp`.

A possible long-term composition is:

```text
ChatGPT
   |
   v
 dsh-mcp-gateway
   |
   v
 DSH agent
   |
   +-- native DSH tools
   |
   `-- MCP --> local-shell-mcp --> browser / remote workers / execution
```

No migration of local-shell-mcp is assumed by this repository.

## License

No license has been selected yet.
