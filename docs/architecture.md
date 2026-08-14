# Architecture

The canonical product contract is [`../AGENTS.md`](../AGENTS.md): **give ChatGPT Web a mature DSH Harness**.

`dsh-mcp-gateway` is an access/compatibility adapter between ChatGPT Web and DSH. ChatGPT is the primary reasoning/model agent; DSH is the harness/runtime authority. The adapter should generically project DSH-managed capabilities to ChatGPT over MCP instead of becoming a second harness itself.

It may own ChatGPT-facing OAuth/MCP transport, public access glue, capability projection, and compatibility code required to bridge ChatGPT Web to DSH. Harness concerns such as tools, skills, jobs, sessions, policy, MCP clients, lifecycle, and community extensions should preferentially remain DSH-owned.

local-shell-mcp is an implementation/reference source for proven public-tunnel, OAuth/remote-MCP, remote-worker, browser, and other differentiated execution capabilities. It is not the primary harness in this architecture.

The first implementation of this boundary is deliberately small. A DSH-resident Cordis plugin reads `ctx.tools.schemas()` and executes calls with `ctx.tools.execute()`. The public OAuth/MCP gateway talks only to that loopback bridge and dynamically projects those DSH schemas as first-class MCP tools. Consequently DSH tool plugins remain the capability owners and continue through DSH's own registration, policy, and execution pipeline.

```text
community DSH plugin -> ctx.tools -> loopback bridge -> OAuth/MCP adapter -> ChatGPT Web
```

Projection is catalog-driven rather than wrapper-driven: each MCP `tools/list` reads the current global DSH ToolRuntime view, so a newly loaded compatible DSH tool can appear without gateway code changes or restart. The generic catalog/call pair remains only as a compatibility/debugging escape hatch. Agent-scoped capabilities require an explicit ChatGPT scope/authority mapping and are not implicitly promoted to global calls.

The sections below document substantial prototype work that predates this clarified product boundary. They remain useful engineering evidence, but any gateway-owned session/goal/continuation design described below is **not automatically a current product requirement**. When a section conflicts with `AGENTS.md`, `AGENTS.md` is authoritative.

## Historical prototype: session routing invariant

Before sending a prompt, the gateway distinguishes three states:

```text
live      -> reuse the live Agent
persisted -> resume the persisted Agent
absent    -> create a fresh Agent
```

The persisted branch is fail-closed. If resume is unavailable or fails, the gateway reports that failure. It does not call create with the same id.

Within one gateway process, write admission for the same explicit session id is serialized. `SessionRouter.ensure()` makes the observe-and-create/resume decision atomic, while `GatewayService` extends the same re-entrant per-id admission lock through prompt submission and the mutating cancel/goal operations. This prevents duplicate creation, interleaved first prompts, and nondeterministic ordering between a prompt submission and another control mutation. The lock is released as soon as DSH accepts the control RPC; it does not wait for the resulting model turn or autonomous goal work to finish, so a later cancel or goal mutation can still act on a running Agent. Read-only status/history/search operations are not put behind this lock. Locks are per id rather than global, so unrelated sessions remain concurrent; the lock table uses weak values so completed session ids do not become a permanent in-memory registry. The public-SDK adapter also rolls back a failed first allocation only while the id is still allocated and not live, so a notification that proves the session became live cannot be followed by deleting its persistent catalog entry.

## Transport strategy

DeepSeek Harness is a developer preview, so DSH-specific request shapes should stay behind one adapter.

Preferred order:

1. A public DSH protocol supporting create/list/resume/prompt/status/events/cancel.
2. A small DSH protocol-driver plugin using the documented `ctx.agents` create/resume seam.
3. A temporary product API adapter only when clearly marked experimental.

Current ACP is not sufficient for this role because its documented surface is fresh-session-only. The current restart-capable implementation therefore uses option 3: `ExperimentalWebHostBackend`, isolated behind the stable backend contract and restricted to loopback by default. It has been exercised against the official rc6 Web Host, including a full Host stop/restart over the same persisted session.

The Web Host currently exposes turn activity (`session.list.running`) but no Host boot id and no reliable live-idle attachment bit. Therefore `running=false` is deliberately treated as an ambiguous durable/non-running state, not cached as proof of a live Agent. `dsh_status` preserves the routing-compatible `state=persisted` value but also reports `attachment_state=ambiguous-idle-or-cold` plus `write_attach_probe_required=true`; its Web Host implementation derives those fields from one `session.list` snapshot rather than racing two list reads. Before steering such a session, the adapter calls the turn-free `session.models` resolver: a live-idle Agent is reused, while a session after an independent Host restart is cold-resumed. Consequently an MCP receipt with `action=resumed` means the attach/resume path was taken; it is not telemetry proving that a process-level cold reconstruction occurred.

This matters even when the gateway process itself never restarts. A real rc6 test kept one `ExperimentalWebHostBackend` object alive, completed a first turn, stopped only the DSH Host, restarted it with the same `DSH_HOME`, and then sent a second turn through that same backend object. The second control path returned `action=resumed`; the shared history ended with 37 events and two `turn/end` records. No gateway-lifetime attachment cache is therefore treated as a Host-lifetime fact.

## Process model

An MCP request should enqueue or steer a DSH session and return a receipt/session id. It should not remain open for the autonomous goal's whole lifetime. DSH continues independently, while observation belongs to separate status/event calls or streams. A later MCP/chat session reconnects using the durable session id.

Reconnect observation has two deliberately different surfaces. Raw `history`/`history_page` preserves DSH events for diagnostics and exact paging. Compact `messages` is a deterministic projection over rc6 `session.history`: only append-origin human `user/message` and model `assistant/message` records are retained, visible text blocks are joined, and non-text block types are reported without exposing reasoning/tool-call bodies. Plugin-produced user context and replacement-origin model copies are excluded. This projection is cold-safe because Host history inspection does not resume/publish an Agent, and it performs no LLM summarization. A transport that lacks authoritative durable history must reject this capability rather than synthesize a partial transcript.

The current admission locks are process-local, so the supported deployment model is one active gateway process for a given DSH Host/public OAuth state.

Session existence is also constrained by the rc6 Host v1 API shape. `session.list` currently returns the entire persisted-session set and its reserved cursor is unimplemented; rc6 exposes no pure exact-id session-summary RPC. The gateway therefore performs an O(N) list scan for routing presence rather than abusing a side-effectful or unrelated endpoint. MCP list output is bounded separately: `dsh_list` slices the current backend snapshot with `limit<=100` plus `offset/next_offset`, preventing Host-scale session counts from becoming one unbounded model-context result. That gateway offset is intentionally best-effort rather than advertised as a stable Host cursor, because new sessions may shift a later snapshot between calls. The Host does publish incremental `host/session-added`, `host/session-removed`, and `host/session-status` frames, but the real network carrier is a WebSocket (`GET /api/events.host` returns `426 Upgrade Required`; SSE exists only in the in-process fetch carrier). A correct cache would need a new WebSocket client dependency plus generation-aware reconnect/rebaseline logic, because the stream sends no baseline and `session.list` is explicitly the reconnect authority. That optimization is intentionally deferred instead of turning a simple loopback HTTP adapter into a second connection-runtime implementation. Horizontal replicas would need a distributed admission/ownership protocol before they could preserve the same ordering guarantees. Likewise, the example deployment treats one DSH Host as the owner of a given `DSH_HOME`; it does not use multiple Host processes as concurrent writers to the same durable state.

## Session discovery is an optional derived capability

Durable identity does not require full-text indexing. DSH Web rc6 deliberately keeps its `ctx.sessionQuery` service mounted while shipping content search disabled with `openAt: never`; exact reads, lineage, and persistence remain available. The gateway therefore keeps session routing/list/history independent from search and maps the default disabled state to `SessionSearchUnavailable` rather than treating it as a Host failure.

Deployments that want remembered-text recovery may opt into the existing `@deepseek-ai/dsh-session-query-sqlite` row with `openAt: first-search` and a dedicated path under `DSH_HOME`. That SQLite FTS5 database is derived state: it must not be the canonical session-persistence database and must have one process owner. A real rc6 test searched the same persisted session after a complete Host restart and again after deleting the derived database; the latter search rebuilt the index from durable logs and returned the same hit. This makes search useful for rediscovery without making the index part of session correctness.

## Goal activation is separate from session persistence

DSH persists goal phase/revision/history but deliberately does not persist process-local continuation authority. After a session is resumed, a durable goal can remain `phase=active` while automatic goal rounds are disarmed. The gateway must not reinterpret session cold-resume as authorization to continue autonomous work.

Goal control therefore uses the Host goal domain directly. New autonomous work is armed deterministically with `goal.create(sessionId, objective, maxGoalRounds?)`; later lifecycle mutations are CAS-guarded from the durable projection:

```text
goal.create(objective, maxGoalRounds?)

session.history projections.values.goal
        |
        v
current {id, revision}
        |
        +-- goal.edit(ref, objective?, maxGoalRounds?)
        +-- goal.resume(ref)
        +-- goal.pause(ref)
        +-- goal.complete(ref)
        `-- goal.clear(ref)
```

The CAS ref is re-read for every mutation. A goal may change phase or revision between two control calls; such races are reported by DSH rather than hidden with an automatic retry. In particular, a resumed goal may become blocked or complete before a later pause request arrives. `goal.edit` is the structured recovery path when policy allows more work after a round-limit block: it can increase `maxGoalRounds` without changing phase, after which an explicit `goal.resume` re-arms continuation. `goal.complete` gives the operator a durable terminal transition, while `goal.clear` removes the current goal only after recording DSH's durable tombstone/history.

A real rc6 lifecycle test exercised exactly that recovery sequence: the goal blocked at 1/1 rounds, the gateway edited its objective/cap to 3 while it remained blocked, resumed it until 3/3 and another round-limit block, completed it, then cleared it. Every transition used the latest durable revision; the observed ref advanced from revision 2 through 6 before clear.

## Security boundary

The DSH Web Host is treated as an unauthenticated internal runtime endpoint and is loopback-only by default. Public clients terminate at the OAuth-protected MCP gateway; production deployment should place HTTPS in front of the gateway rather than exposing the raw DSH Host.

The gateway listener and the public MCP origin are separate concepts. The process can remain bound to `127.0.0.1` while a reverse proxy presents `https://gateway.example.com`. MCP's DNS-rebinding protection must remain enabled in that topology, with the declared public Host/Origin explicitly allowlisted alongside loopback values. Passing only the loopback bind host to the MCP SDK is insufficient: its secure localhost default accepts only localhost Host headers and will reject an authenticated reverse-proxied `/mcp` request. The CLI therefore derives transport-security allowlists from `--public-base-url` rather than disabling the protection.

OAuth bearer state is also fail-closed across deployment changes. Persisted access/refresh rows are bound to both issuer and resource, and each authorization grant has a private family id retained through refresh rotation. Revoking either an access or refresh token deletes the whole family. A database created by the earlier token schema cannot safely infer those families, so migration preserves registered OAuth clients but invalidates legacy authorization-code/token state and requires reauthorization.

OAuth discovery separates scopes clients may request from scopes the MCP resource requires. `offline_access` is advertised and may ride the authorization/refresh grant, while Bearer authorization for `/mcp` requires only `dsh:control`. The currently tested MCP Python SDK 2.0.0 accepts DCR public clients using `token_endpoint_auth_method=none` at the token endpoint but omits that implemented method from authorization-server metadata. The embedded-OAuth server therefore rebuilds only that metadata route from the SDK's own `build_metadata`, `MetadataHandler`, and CORS primitives, adding `none` to `token_endpoint_auth_methods_supported`; it does not replace the SDK's authorization, token, registration, revocation, or MCP handlers. Revocation advertising is deliberately untouched because the same SDK's revocation request model still requires a `client_secret` form field even for public clients. The shim can be removed when upstream metadata and public-client handling become self-consistent.

## Future composition

```text
ChatGPT / MCP client
        |
        v
dsh-mcp-gateway
        |
        v
DeepSeek Harness
        |
        +-- native DSH capabilities
        |
        `-- MCP -> optional external execution servers
                  such as local-shell-mcp
```

This keeps each repository independently useful and avoids a migration dependency between them.

The tested same-host LSM composition uses DSH's stdio MCP client rather than the public OAuth/MCP path. LSM 4.0.0 currently registers 43 MCP tools; a small host plugin narrows only the `mcp__lsm__*` inherited names at `agent/created` time by dynamically denying every provider tool except an explicit seven-tool differentiated subset. The filter is intentionally deny-only: an agent-level allow mask also filters the inherited standard preset and would remove DSH-native tools. In the rc6 smoke test the resulting surface was 25 DSH + 7 LSM tools before and after a Host restart/cold resume. This restriction is composition, not authority; LSM's own workspace containment, policy, and disabled HTTP-only features remain the execution security boundary.
