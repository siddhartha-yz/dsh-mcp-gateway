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

The transport-independent control service and MCP v2 tool surface are implemented. `PublicSdkBackend` covers the live-session path for an injected public DSH SDK client and persists a small gateway-owned session catalog. Its live notification projection is intentionally bounded: each session keeps the newest 2000 events by default, while status reports the total observed event count, retained count, and whether the in-memory history was truncated. That catalog deliberately recognizes known ids after restart so this transport fails closed instead of recreating them; with the current public SDK, a catalogued cold session raises `ColdResumeUnavailable` before any new prompt is sent. The restart-capable Web Host path reads durable history directly and does not depend on this ring buffer.

For restart-capable operation, `ExperimentalWebHostBackend` targets the DSH developer-preview Web Host API behind loopback/private networking. It has been validated against the official `@deepseek-ai/dsh@0.1.0-rc.6` runtime: a persisted session can be reopened after the entire DSH Host process is stopped and restarted with the same `DSH_HOME`, and a later prompt continues the same durable history. The adapter also exposes structured goal creation plus explicit CAS-based goal status/resume/pause controls.

An embedded OAuth prototype is present for MCP deployments: persisted dynamic clients/tokens, an owner approval page, refresh-token rotation, resource/issuer-bound tokens, grant-family revocation, and MCP SDK authorization routes. The approval page shows the registered client name alongside the immutable client id, the exact redirect URI, token authentication method, requested scopes, and resource, so the owner does not have to trust a self-reported display name when deciding where an authorization code will be sent. Access and refresh tokens from one authorization grant share a private grant id; revoking either side invalidates the whole family, including access tokens minted before refresh rotation. The issuer is canonicalized once so metadata, RFC 9207 callback `iss`, persisted tokens, and token claims use the same URL. A legacy token-table schema is migrated fail-closed by invalidating old token/code state while preserving registered clients, because the old rows cannot be reconstructed into trustworthy grant families. Dynamic client registration is bounded (256 persisted clients by default, configurable with `--max-registered-clients`) and the SQLite count/insert decision is serialized so concurrent registrations cannot overrun the cap. Each normalized registered-client record is also limited to 32 KiB of UTF-8 JSON by default (`--max-client-metadata-bytes`), so one anonymous DCR request cannot consume unbounded persistent state even though the upstream MCP schema allows large optional metadata. Pending authorization requests are also bounded to 512 globally and 8 per client; each authorize write atomically prunes expired requests before enforcing those budgets, so an already registered client cannot grow the approval queue without bound. Approval and denial both atomically consume the same pending row under SQLite write serialization, so duplicate tabs or concurrent terminal submissions cannot both issue contradictory outcomes. Failed owner-PIN attempts are throttled per opaque pending request rather than by source IP or a global lockout; this avoids turning a loopback reverse proxy (where public clients can share one apparent source address) into a cross-request denial-of-service primitive. The limiter retains only failure buckets still inside its time window, and successful/denied requests are cleared immediately, so abandoned authorization ids do not become a permanent in-memory index. Authorization codes and refresh tokens are also single-use under concurrent exchange: the store serializes consume/rotation and the losing request receives `invalid_grant`. The authorization server advertises `offline_access` as an optional/allowed OAuth scope while the MCP resource itself requires only `dsh:control`, so long-lived clients can obtain refresh tokens without turning `offline_access` into a resource permission. A full HTTP regression also covers DCR public clients using `token_endpoint_auth_method=none`, PKCE, owner approval, authorization-code exchange without a client secret, rejection of an unauthenticated `/mcp` request, authenticated MCP initialization with the server-negotiated protocol version, `tools/list`, and a protected `dsh_list` tool call. The same regression then rebuilds the OAuth/MCP server around the persisted SQLite state: the old access token authenticates a fresh MCP initialization with a new transport session, `dsh_list` still works, and the pre-restart refresh token rotates exactly once while replay of the old value returns `invalid_grant`. This remains experimental infrastructure rather than a production security claim; the intended deployment keeps the DSH Host on loopback and terminates public HTTPS in front of the gateway.

The MCP tool catalog also carries conservative side-effect annotations. `dsh_status`, `dsh_history`, `dsh_history_page`, `dsh_messages`, `dsh_list`, `dsh_search`, and `dsh_goal_status` are marked read-only/idempotent; session/goal control tools are explicitly marked mutating and potentially consequential so clients can apply read-only filters or approval policies without guessing from tool names.

Current MCP tools:

```text
dsh_start
dsh_continue
dsh_status
dsh_history
dsh_history_page
dsh_messages
dsh_list          # bounded page: limit<=100, offset/next_offset
dsh_search
dsh_cancel
dsh_goal_status
dsh_goal_create
dsh_goal_edit
dsh_goal_resume
dsh_goal_pause
dsh_goal_complete
dsh_goal_clear
```

The MCP layer depends only on the stable gateway backend contract; it does not know whether DSH is reached through the Python SDK, ACP, a protocol-driver plugin, or a future official resumable API.

## Evidence behind the design

A local proof of concept using DeepSeek Harness `0.1.0rc6` verified that an initial controlling request can return while a DSH goal continues issuing autonomous goal rounds in the same live runtime. A later controller can send another prompt to the same live session and the model receives the retained history.

The current public Python SDK still cannot cold-resume an existing persisted session: a fresh SDK runtime follows the create path and hits a persisted-log id collision. The experimental Web Host adapter closes that transport gap without changing the MCP/service contract. In a real rc6 restart test, the first turn produced 22 durable events; after stopping and restarting the whole Host, the same session resumed, accepted a second turn, and retained both prompts in one history (37 events, two `turn/end` records).

Goal lifecycle has a separate safety boundary. DSH deliberately does not persist process-local goal activation: after session resume, a durable goal may still be `phase=active` while automatic continuation remains disarmed. A real rc6 test confirmed that cold-resuming the Agent did not create a new goal round; an explicit `goal.resume` CAS mutation re-armed continuation. The gateway therefore exposes explicit goal controls rather than silently auto-rearming goals after restart. The structured operator lifecycle now also covers `goal.edit`, `goal.complete`, and `goal.clear`. In a real rc6 round-limit test, a goal blocked after round 1, `goal.edit` raised its cap to 3 without changing the blocked phase, `goal.resume` ran rounds 2-3 until it blocked again, `goal.complete` moved it to `phase=complete`, and `goal.clear` removed the current goal while retaining DSH's durable history/tombstone semantics.

A public HTTPS development smoke test also exercised the complete client-facing chain through a temporary reverse proxy: dynamic client registration, PKCE authorization, owner PIN approval, authorization-code exchange, MCP initialization/tool discovery, `dsh_start`, structured `dsh_goal_create`, closing that MCP session, reconnecting from a second MCP session to the same DSH session, reading status/history/goal state, sending `dsh_continue`, and refresh-token rotation. The test exposed an important deployment requirement: MCP DNS-rebinding protection must explicitly allow the declared public reverse-proxy Host/Origin while the process itself remains bound to loopback.

Long histories have a separate exact pagination path. `dsh_history(limit)` remains a convenient newest-raw-events view, while `dsh_history_page(session_id, before_seq?, max_messages=50)` maps rc6 `session.history` page semantics and returns `has_more` plus `next_before_seq`. For chat/session handoff, `dsh_messages(session_id, before_seq?, limit=20)` uses the same cold-safe Host history call but projects only append-origin human `user/message` and model `assistant/message` records. It joins visible text blocks, identifies omitted image/reasoning/tool-call block types, skips plugin user context and model-only replacement copies, and never calls an LLM to summarize. Compact pagination walks across Host pages that contain only filtered plugin context and returns a cursor that leads to the next actual human/model message rather than exposing empty intermediate pages. The public SDK bridge fails this capability closed because its notification cache is not authoritative durable history. A real rc6 transcript smoke produced 22 raw events but only the expected human prompt and model answer in the compact view; `limit=1` paged assistant → human while skipping DSH's injected system-prompt `user/message`. After restarting the entire Host against the same `DSH_HOME`, the same transcript was read with `attachedSessions=0` both before and after the call, confirming that compact handoff reads do not resume or publish the cold Agent. A separate rc6 smoke test created three completed control turns, requested one complete append-origin message per page, and walked eight pages containing 49 raw events with strictly decreasing seq ranges and zero overlap before reaching `has_more=false`.

Browsing known sessions uses bounded MCP output even though the rc6 Host v1 list call itself is unpaged: `dsh_list(limit=50, offset=0)` returns `items`, `total`, `has_more`, and `next_offset`, with a hard output cap of 100 rows per call. The offset is applied to each current backend snapshot, so concurrent session creation can shift positions between pages; it is a context-size bound and browsing aid, not a stable Host-side cursor. Recovering a forgotten session id is also available through `dsh_search(query)`, which maps the rc6 Host's bounded content-search surface and returns at most 20 matching sessions/snippets. DSH Web deliberately ships full-text search disabled by default (`openAt: never`), so the gateway turns that state into `SessionSearchUnavailable` rather than exposing it as a generic Host `internal` failure. `deploy/dsh/session-search.cordis.yml` is an optional official-style overlay using a durable derived SQLite FTS5 index with `openAt: first-search`. A real rc6 test found a unique remembered phrase before and after a whole Host restart; after deleting the derived index completely, the next search rebuilt it from durable session logs and found the same session again.

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

For contributor development, install the shared lint/test extra so local checks match CI:

```sh
python -m pip install -e '.[dev]'
ruff check src tests scripts
python -m unittest discover -s tests -v
```

Production deployments do not need the development tools. The package keeps `mcp>=2,<3` as its compatibility range, while [`deploy/server-constraints.txt`](deploy/server-constraints.txt) records the exact server dependency graph currently exercised by the deployment CI. Use that constraints file when rebuilding a known-good OAuth/MCP gateway environment.

The server extra currently targets the stable MCP Python SDK v2 line (`mcp>=2,<3`). MCP Python SDK `2.0.0` accepts DCR public clients with `token_endpoint_auth_method=none` at the token endpoint but omits `none` from its generated authorization-server metadata. The embedded-OAuth server applies a narrow metadata-route compatibility shim built from the SDK's own metadata/CORS primitives and adds only that already-implemented token auth method; every authorization, token, registration, revocation, and MCP handler remains the SDK implementation. Revocation metadata is left untouched because the current SDK revocation request model still requires a `client_secret` form field even for a client registered as `none`. The shim is covered by HTTP regression tests, including metadata CORS, and can be removed once upstream metadata and public-client handling become self-consistent. See [`docs/architecture.md`](docs/architecture.md) for the boundary decisions and [`docs/deployment.md`](docs/deployment.md) for the tested process/security topology.

## Run the gateway

The current deployable path expects an external DSH Web Host on loopback and puts the OAuth/MCP gateway in front of it. The gateway itself also binds loopback by default; terminate public HTTPS with a reverse proxy or tunnel.

```sh
python -m pip install --constraint deploy/server-constraints.txt -e '.[server]'

export DSH_MCP_GATEWAY_ADMIN_PIN='choose-a-long-owner-pin'

dsh-mcp-gateway \
  --dsh-web-url http://127.0.0.1:3080 \
  --dsh-cwd /path/to/agent/workspace \
  --public-base-url https://gateway.example.com
```

The public MCP endpoint is `<public-base-url>/mcp`. Do not expose the raw DSH Web Host directly; the experimental adapter refuses non-loopback DSH targets by default. The CLI keeps MCP DNS-rebinding protection enabled and allowlists only the declared public origin plus loopback Host/Origin values, so reverse proxying does not require disabling transport security.

Deployment probes are intentionally small and unauthenticated: `GET /healthz` reports only that the gateway process is serving HTTP, while `GET /readyz` additionally probes the configured DSH Web Host and returns 503 when that dependency is unavailable. Readiness uses a dedicated 1-second Host diagnostic timeout rather than the normal 10-second control-RPC timeout, so a wedged dependency does not hold monitoring requests open for a full business-operation timeout. Gateway startup itself does not require the DSH Host to be reachable, so dependency readiness is not conflated with process liveness. Neither route returns the DSH descriptor, workspace path, provider, or transport error details; both are `Cache-Control: no-store`.

A deterministic long-task flow is:

```text
dsh_start(prompt, optional session_id)
        -> returns session_id

dsh_goal_create(session_id, objective, optional max_goal_rounds)
        -> creates + arms the durable goal

later chat / MCP session
        -> dsh_status / dsh_messages / dsh_goal_status
        -> dsh_history(_page) only when raw event detail is needed
        -> dsh_continue for steering

if the DSH Host itself restarted
        -> session cold-resumes when addressed again
        -> dsh_goal_resume explicitly re-arms autonomous continuation
```

## Relationship to local-shell-mcp

This repository is independent from `fwerkor/local-shell-mcp`.

The same-host stdio composition has now been exercised end-to-end. LSM 4.0.0 publishes 43 tools internally, but the optional deployment overlay uses a DSH agent-scoped filter to expose only seven differentiated browser/dynamic-MCP tools; the tested rc6 model surface is 32 tools total instead of the unfiltered 68. The filtered surface also remained identical after DSH Host restart and cold session resume. See [`docs/local-shell-mcp.md`](docs/local-shell-mcp.md) for the tested boundary and deployment overlay.

The composition is:

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

No migration of local-shell-mcp is assumed by this repository, and the base gateway deployment does not enable this backend automatically.

## License

No license has been selected yet.
