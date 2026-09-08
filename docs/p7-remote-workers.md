# P7 — DSH remote workers

Status: complete and independently production-usable, 2026-09-08

## Goal

Attach a machine that can make outbound HTTPS requests to the DSH host without installing DSH on that machine. ChatGPT remains the reasoning agent and DSH remains the execution/runtime authority. P7 was originally motivated by the need to inspect the local Firefox host during P6 debugging, but the resulting remote execution capability is independent of P6 and remains useful on its own.

## Reference and scope

The protocol deliberately adapts only the proven remote-worker mechanics from `local-shell-mcp`:

- one-time expiring invite;
- persistent per-worker bearer identity;
- outbound long polling and heartbeat;
- bounded per-worker dispatch queue;
- result submission tied to the assigned worker/job;
- list, rename, and revoke lifecycle.

It does **not** copy LSM's MCP server, model/harness loop, generic job layer, browser abstraction, transfer subsystem, or broad tool surface.

P7 v1 intentionally stays narrow: remote administration plus bounded shell/filesystem execution. Persistent terminal/browser routing remains deferred until an independent concrete need justifies it.

## Architecture

```text
ChatGPT Web
   |
   | OAuth MCP: dsh_tool_call
   v
DSH ToolRuntime
   |
   | remote_machine / remote_exec
   v
DSH remote-worker plugin  <---- DSH storageDomain (worker registry)
   ^
   | loopback-only transport routes on DSH Web Host
   |
dsh-mcp-gateway
   ^
   | public HTTPS /remote/* edge routes only
   |
Ubuntu worker ---- outbound long poll / heartbeat / result ---->
```

The gateway is only the public HTTP edge. It owns no worker registry, queue, authorization decision, or execution policy. Public worker requests are normalized and forwarded to loopback-only DSH routes. The DSH plugin owns registry persistence, invitations, queues, pending jobs, identity checks, dispatch, and ToolRuntime exposure.

This avoids a second harness and also avoids exposing the internal worker transport as a ChatGPT-callable tool.

## DSH ToolRuntime surface

### `remote_machine`

Actions:

- `invite`: create a one-time invite and return a Linux join command;
- `list`: report registered workers and online/offline status;
- `rename`: rename a registered worker without rotating its identity;
- `revoke`: remove a worker identity and fail/cancel queued work.

### `remote_exec`

Required fields: `machine`, `action`.

Initial actions:

- `shell`: bounded command execution with cwd/timeout/output limit;
- `read`: UTF-8 line-range read;
- `write`: bounded UTF-8 replace/create;
- `edit`: exact literal replacement with optional replace-all;
- `glob`: bounded file glob;
- `grep`: bounded ripgrep search when `rg` is installed, with a Python fallback for literal text search.

`workdir` is the worker's default cwd, **not a filesystem sandbox**. Enrollment explicitly delegates the OS user's authority on the remote machine to DSH. The worker process runs with that user's permissions. No elevation mechanism is included.

## Public worker protocol

Version: `1`.

Public gateway routes:

- `GET /remote/join.sh`
- `GET /remote/worker.py`
- `POST /remote/v1/register`
- `POST /remote/v1/resume`
- `POST /remote/v1/poll`
- `POST /remote/v1/heartbeat`
- `POST /remote/v1/result`

The POST routes are mechanical proxies to DSH Web Host routes under `/api/chatgpt-remote-workers/v1/*`.

Registration consumes an invite and returns `{name, token, poll_timeout_s, heartbeat_interval_s}`. The worker stores the token in a mode-0600 JSON file. Subsequent requests use `Authorization: Bearer <token>`.

A poll returns either a heartbeat response or one job:

```json
{
  "job": {
    "id": "job_...",
    "action": "shell",
    "arguments": {},
    "expires_at": "2026-09-07T...Z"
  }
}
```

Results include the same `job_id`; DSH accepts them only from the worker assigned to that job.

## Security boundaries

1. Invitations are random, one-time, expire quickly, and are kept only in process memory.
2. Worker bearer tokens are random; the raw token is persisted only in the enrolled worker's mode-0600 identity file, while DSH persists only its SHA-256 digest.
3. The DSH worker transport endpoint is loopback-only because it is registered on the existing DSH Web Host. Only the gateway publishes `/remote/*`.
4. The gateway validates request size/content type and forwards only the fixed protocol actions. It cannot invoke arbitrary DSH tools on behalf of a worker.
5. Every remote job is bound to exactly one registered machine and has an expiry.
6. Queue depth, request bytes, output bytes, command timeout, file-write size, and returned search results are bounded.
7. Revocation invalidates future worker requests and rejects/settles outstanding work.
8. Worker code has no model, scheduler, autonomous loop beyond polling, DSH dependency, privilege escalation, or inbound listener.
9. Remote execution is intentionally the enrolled OS user's authority. DSH's *local* sandbox cannot claim to confine a different host; the explicit enrollment boundary is the authorization boundary.

## Persistence and restart behavior

The worker registry is stored in DSH `storageDomain`; invitations and in-flight queues are intentionally ephemeral. After a DSH restart, enrolled workers resume using their persistent token, become online again, and start polling. In-flight jobs fail with the controller restart rather than being replayed implicitly.

## Failure semantics

- unknown/revoked token: HTTP 401;
- invalid/expired/used invite: HTTP 400/409 as appropriate;
- offline worker: ToolRuntime error before enqueue;
- full worker queue: ToolRuntime resource-limit error;
- expired job before execution: worker submits a timeout result;
- tool-call cancellation/timeout before worker claim: queued job is removed;
- timeout after claim: DSH marks the job cancelled; heartbeat tells the worker to cancel the local task where possible;
- late or duplicate result: rejected as not pending.

## Acceptance sequence

1. Unit-test registry/invite/poll/result/revoke and bounded queue behavior.
2. Unit-test worker execution actions and identity persistence.
3. Unit-test gateway proxy normalization and join script.
4. Add deployment wiring and architecture-contract checks.
5. Deploy P7 to the live host.
6. Generate an invite, enroll the user's local Ubuntu desktop, and verify list + shell + read.
7. Verify the enrolled desktop remains independently useful through `remote_machine` and `remote_exec`, regardless of P6 status.
