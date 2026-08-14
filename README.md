# dsh-mcp-gateway

Durable runtime/session control plane for ChatGPT over OAuth + MCP.

The product boundary is intentionally narrow: **ChatGPT remains the only reasoning agent.** This project does not require a DeepSeek API key, does not run an autonomous second model, and does not replace ChatGPT with DeepSeek Harness goal rounds.

## Target architecture

```text
ChatGPT web / MCP client
        |
     OAuth + MCP
        |
        v
 dsh-mcp-gateway
        |
 durable logical session
 checkpoint / run lease
 resume / takeover
 continuation coordination
        |
        v
 local execution MCP
 (for example local-shell-mcp)
        |
        v
 shell / files / browser / remotes
```

The runtime layer exists to outlive one ChatGPT agent run: it persists semantic task state, prevents stale runs from continuing to mutate the same task after takeover, and provides enough context for a later ChatGPT run to resume without relying on the previous conversation's transient context.

## Relationship to local-shell-mcp v4

The design follows the direction now implemented on `fwerkor/local-shell-mcp`'s `feat/logical-session-runtime-v4` branch:

- a durable logical Session is independent of one ChatGPT run;
- `resume(..., takeover=true)` creates a new run lease and supersedes the older run;
- meaningful progress is checkpointed semantically instead of copying every tool result;
- a Live Workspace/MCP App can use `@modelcontextprotocol/ext-apps` `app.updateModelContext(...)` plus `app.sendMessage(...)` to request another ChatGPT continuation after inactivity;
- continuation resumes the same durable Session rather than invoking a second model provider.

The goal here is to reuse or remain compatible with those runtime semantics rather than duplicate local-shell-mcp's execution tools.

## Current status

The model-provider-free logical-session core is implemented with SQLite persistence.

`session_manage` currently supports:

```text
start   -> create logical Session + active ChatGPT run lease
get     -> read full durable handoff state
list    -> rediscover recent Sessions with compact progress
report  -> persist semantic checkpoint/progress
resume  -> create a new run; takeover=true supersedes an old active run
finish  -> complete Session and release run lease
cancel  -> cancel Session and release run lease
```

A mutating call must carry the current `session_run_id`. Once another ChatGPT run takes over, the old run id can no longer mutate that Session.

Example flow:

```text
ChatGPT run A
  -> session_manage(action="start", objective="...")
  <- session_id + active_run.run_id
  -> do local-shell-mcp work
  -> session_manage(action="report", session_run_id=..., summary=..., next=...)

run A ends / is replaced

ChatGPT run B
  -> session_manage(action="resume", session_id=..., takeover=true)
  <- same durable task state + new active_run.run_id
  -> continue from checkpoint
```

The next core milestone is the MCP App continuation bridge: claim an eligible inactive plan/session, update ChatGPT model context with the checkpoint, then call `app.sendMessage(...)` so ChatGPT itself starts the next continuation run.

## OAuth / MCP

The existing embedded OAuth implementation is retained. It provides persisted dynamic client registration, PKCE/public-client support, owner approval, resource/issuer-bound tokens, refresh rotation, revocation, bounded registration state, and DNS-rebinding protection for the public MCP endpoint.

The default server mode requires no model provider and no DSH process:

```sh
python -m pip install --constraint deploy/server-constraints.txt -e '.[server]'

export DSH_MCP_GATEWAY_ADMIN_PIN='choose-a-long-owner-pin'

dsh-mcp-gateway \
  --public-base-url https://gateway.example.com \
  --state-dir .dsh-mcp-gateway
```

The public endpoint is:

```text
https://gateway.example.com/mcp
```

`GET /healthz` checks the gateway process. In the default ChatGPT runtime mode, `GET /readyz` checks that the local runtime state initialized; it does not probe any LLM provider.

## Optional legacy DSH adapter

Earlier development explored using DeepSeek Harness as a second autonomous Agent. That is no longer the default product direction. The experimental DSH Web Host adapter is retained only as an opt-in compatibility/research path:

```sh
dsh-mcp-gateway \
  --public-base-url https://gateway.example.com \
  --dsh-web-url http://127.0.0.1:3080 \
  --dsh-cwd /path/to/workspace
```

Only when this option is supplied are the legacy `dsh_*` MCP tools registered and `/readyz` made dependent on that Host. Historical evidence, goal-round experiments, deployment material, and rc6 restart notes are preserved in [`docs/legacy-dsh-prototype.md`](docs/legacy-dsh-prototype.md).

No DeepSeek API key is required for the default runtime.

## Development

```sh
python -m pip install -e '.[dev]'
ruff check src tests scripts
python -m unittest discover -s tests -v
```

The server dependency graph used by CI is pinned in [`deploy/server-constraints.txt`](deploy/server-constraints.txt).

## Design invariants

- ChatGPT is the only reasoning Agent in the primary architecture.
- Session state survives MCP reconnects and gateway process restarts.
- A stale ChatGPT run cannot mutate a Session after takeover.
- Handoff state is semantic and bounded; it is not an unbounded transcript dump.
- Continuation means triggering another ChatGPT run, not secretly switching to another model.
- Execution capabilities should come from local-shell-mcp or another execution MCP rather than being reimplemented here.
- Legacy DSH-specific behavior stays optional and isolated behind its adapter.

## License

No license has been selected yet.
