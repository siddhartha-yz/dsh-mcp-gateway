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

The transport-independent control service and MCP v2 tool surface are now implemented. The real DSH transport, OAuth, event streaming, and restart supervisor remain intentionally separate because DeepSeek Harness is currently a developer preview and its public control protocols are still settling.

Current MCP tools:

```text
dsh_start
dsh_continue
dsh_status
dsh_history
dsh_list
dsh_cancel
```

The MCP layer depends only on the stable gateway backend contract; it does not know whether DSH is reached through the Python SDK, ACP, a protocol-driver plugin, or a future official resumable API.

## Evidence behind the design

A local proof of concept using DeepSeek Harness `0.1.0rc6` verified that an initial controlling request can return while a DSH goal continues issuing autonomous goal rounds in the same live runtime. A later controller can send another prompt to the same live session and the model receives the retained history.

The same experiment also verified the current public Python SDK limitation: after a fresh runtime starts over an existing persisted session, sending the same session id follows the create path and fails with a persisted-log id collision rather than cold-resuming. That is the first transport gap this project intends to isolate cleanly rather than work around in application code.

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
