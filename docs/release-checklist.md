# Release checklist

This checklist defines the boundary between the current development prototype and a self-hosted release that can be relied on for long-running DSH work. A checked item needs executable or recorded integration evidence; documentation alone is not enough.

## Proven gates

- [x] Core session routing is fail-closed: live -> reuse, persisted -> resume, absent -> create; a failed persisted resume never falls back to create.
- [x] Same-session mutating control admission is serialized within one gateway process while unrelated sessions remain concurrent.
- [x] Official DSH `@deepseek-ai/dsh@0.1.0-rc.6` cold-resumes the same durable session after the whole Web Host process restarts with the same `DSH_HOME`; pre/post-restart turns remain in one history.
- [x] DSH goal restart semantics are explicit: durable phase/revision/history survive, process-local continuation authority does not; `dsh_goal_resume` is required to re-arm work after Host restart.
- [x] Compact `dsh_messages` reads human/model transcript from a cold session without attaching/resuming the Agent, and its pagination skips filtered plugin-only pages without reordering messages.
- [x] Session content search can rediscover a durable session after Host restart; the optional derived FTS index can be deleted and rebuilt from canonical session logs.
- [x] Optional local-shell-mcp composition is tool-filtered and the filtered model-facing surface survives DSH Host restart/cold resume.
- [x] Embedded OAuth persists DCR clients/tokens, canonicalizes issuer/resource binding, rotates refresh tokens, revokes grant families, bounds DCR/pending state, and prunes expired short-lived state.
- [x] Public-client HTTP OAuth regression covers DCR -> PKCE -> owner approval -> token -> unauthenticated MCP 401 -> authenticated MCP initialize -> negotiated protocol version -> initialized notification -> tools/list -> protected `dsh_list` tool call.
- [x] Rebuilding the OAuth/MCP server around the same SQLite state preserves old access-token authentication; a new MCP transport session is negotiated and the pre-restart refresh token rotates once with replay rejected as `invalid_grant`.
- [x] Anonymous DCR input is bounded before parsing (raw `POST /register` body) and before persistence (normalized client metadata), including streamed bodies without `Content-Length`.
- [x] Gateway liveness is independent from DSH readiness; a missing/wedged Host leaves `/healthz` live and `/readyz` bounded to the dedicated short probe timeout.
- [x] Wheel/core CLI smoke, Python 3.11/3.12 tests, all-extras imports, locked server dependency graph, systemd parser checks, and the repository test suite run in CI.
- [x] Public HTTPS development smoke has exercised OAuth/MCP through a reverse proxy while the gateway and raw DSH Host remained loopback-bound.

## Release-blocking drills

These are deliberately left unchecked until performed against the intended long-running host rather than a temporary integration environment.

- [ ] Install the pinned DSH runtime and gateway using the checked-in systemd templates on the target host; make `python3 scripts/preflight-deployment.py` pass first, then verify both services start as their dedicated Unix users with the documented state directories and permissions.
- [ ] Put the real public HTTPS reverse proxy/domain in front of the loopback gateway and repeat DCR/PKCE/MCP initialization with that exact issuer/resource/Host/Origin configuration.
- [ ] Perform an operating-system reboot drill: verify DSH Host and gateway start automatically, OAuth state remains usable, an existing durable session is cold-readable, and an explicit continuation succeeds.
- [ ] Perform an offline backup/restore drill for `DSH_HOME`, workspace, OAuth state, and configuration; verify restored session history and OAuth behavior match the documented failure boundaries.
- [ ] Perform the final ChatGPT UI handoff: ChatGPT conversation A starts a DSH task, the conversation/MCP connection ends, conversation B rediscovers/reads the same DSH session and continues it without relying on A's context.
- [ ] Decide the first release version/tag and write release notes from the exact commit that passed the drills above. Keep the package on a development version until these gates pass.
- [ ] If distributing the project for third-party use, choose and add an explicit license/security-support policy; do not infer a license from repository visibility.

## Known non-blocking upstream boundary

The restart-capable adapter still targets the DSH developer-preview Web Host contract. rc6 `session.list` returns the full persisted-session set and has no implemented cursor or pure exact-id summary RPC, so backend presence checks remain O(N). The network host-event stream is WebSocket-only and requires generation-aware reconnect/rebaseline logic; the gateway intentionally does not add a second connection runtime merely to hide this v1 upstream shape. `dsh_list` bounds what reaches the MCP/model context even though the Host-side lookup remains O(N).
