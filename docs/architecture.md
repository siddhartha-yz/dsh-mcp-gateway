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
        +-- goal.resume(ref)
        `-- goal.pause(ref)
```

The CAS ref is re-read for every mutation. A goal may change phase or revision between two control calls; such races are reported by DSH rather than hidden with an automatic retry. In particular, a resumed goal may become blocked or complete before a later pause request arrives.

## Security boundary

The DSH Web Host is treated as an unauthenticated internal runtime endpoint and is loopback-only by default. Public clients terminate at the OAuth-protected MCP gateway; production deployment should place HTTPS in front of the gateway rather than exposing the raw DSH Host.

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
