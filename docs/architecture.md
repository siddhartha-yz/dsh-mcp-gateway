# Architecture

`dsh-mcp-gateway` is a control-plane adapter, not an agent harness and not an execution sandbox.

It should own MCP-facing session and goal controls, OAuth/scopes when needed, stable session identifiers, status/event projection, restart policy around a DSH runtime, and one replaceable adapter that absorbs DSH protocol churn.

It should not duplicate DSH agent loop, goals, workflows, subagents, persistence, shell, filesystem, jobs, skills, or MCP client functionality. It also should not duplicate differentiated execution backends from projects such as local-shell-mcp.

## Session routing invariant

Before sending a prompt, the gateway distinguishes three states:

```text
live      -> reuse the live Agent
persisted -> resume the persisted Agent
absent    -> create a fresh Agent
```

The persisted branch is fail-closed. If resume is unavailable or fails, the gateway reports that failure. It does not call create with the same id.

## Transport strategy

DeepSeek Harness is a developer preview, so DSH-specific request shapes should stay behind one adapter.

Preferred order:

1. A public DSH protocol supporting create/list/resume/prompt/status/events/cancel.
2. A small DSH protocol-driver plugin using the documented `ctx.agents` create/resume seam.
3. A temporary product API adapter only when clearly marked experimental.

Current ACP is not sufficient for this role because its documented surface is fresh-session-only. The current restart-capable implementation therefore uses option 3: `ExperimentalWebHostBackend`, isolated behind the stable backend contract and restricted to loopback by default. It has been exercised against the official rc6 Web Host, including a full Host stop/restart over the same persisted session.

The Web Host currently exposes turn activity (`session.list.running`) but no Host boot id and no reliable live-idle attachment bit. Therefore `running=false` is deliberately treated as an ambiguous durable/non-running state, not cached as proof of a live Agent. Before steering such a session, the adapter calls the turn-free `session.models` resolver: a live-idle Agent is reused, while a session after an independent Host restart is cold-resumed. Consequently an MCP receipt with `action=resumed` means the attach/resume path was taken; it is not telemetry proving that a process-level cold reconstruction occurred.

This matters even when the gateway process itself never restarts. A real rc6 test kept one `ExperimentalWebHostBackend` object alive, completed a first turn, stopped only the DSH Host, restarted it with the same `DSH_HOME`, and then sent a second turn through that same backend object. The second control path returned `action=resumed`; the shared history ended with 37 events and two `turn/end` records. No gateway-lifetime attachment cache is therefore treated as a Host-lifetime fact.

## Process model

An MCP request should enqueue or steer a DSH session and return a receipt/session id. It should not remain open for the autonomous goal's whole lifetime. DSH continues independently, while observation belongs to separate status/event calls or streams. A later MCP/chat session reconnects using the durable session id.

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

OAuth discovery separates scopes clients may request from scopes the MCP resource requires. `offline_access` is advertised and may ride the authorization/refresh grant, while Bearer authorization for `/mcp` requires only `dsh:control`. The currently tested MCP Python SDK 2.0.0 accepts DCR public clients using `token_endpoint_auth_method=none`, but its generated authorization-server metadata does not advertise that method. The gateway regression-tests the actual public-client PKCE flow but does not fork or shadow the SDK metadata route solely to paper over that upstream discrepancy.

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
