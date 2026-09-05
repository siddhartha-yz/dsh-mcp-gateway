# Restart recovery acceptance

The 2026-08-15 counts below are **pre-P2 historical evidence**. P2 intentionally projects a smaller external ChatGPT catalog while leaving the underlying DSH ToolRuntime composition intact.

This drill verifies that the ChatGPT-facing DSH Harness composition survives real
process death and cold process recreation from durable state. It is narrower than
an operating-system reboot: the Cloudflare named tunnel and the host OS remain
running throughout this drill.

## 2026-08-15 live process-restart drill

Baseline before stopping either process:

- gateway commit: `3a70f94`;
- DSH-internal tool catalog: 34 tools;
- community tools present: `find_dsh_plugin`, `calculator`, `json`, `regex`,
  `stat`, `time`, `encoding`, `csv`, and `schema`;
- model-invocable SkillRegistry: `diagnosing-bugs` from the user DSH skill root;
- public ChatGPT MCP surface: the four fixed meta-tools;
- the ChatGPT OAuth client retained a durable refresh token in the gateway SQLite
  state.

The live gateway and DSH Harness tracked jobs were then both terminated. Direct
connections to loopback ports 18766 and 18401 failed, and the unchanged public
Cloudflare tunnel returned HTTP 502, proving the old processes were not still
serving traffic.

The Harness was restarted first using only its existing profile and `DSH_HOME`.
No plugin or skill was reinstalled. It recovered exactly 34 tools, all nine listed
community tools, and the `diagnosing-bugs` Skill. The new Harness process had a
new start time/PID.

The gateway was then restarted using the same persisted OAuth SQLite state. Its
readiness check returned 200 and repeated public `/readyz` probes returned 200.
OAuth client and refresh-token records survived the process recreation.

## Refresh-token persistence drill

The ChatGPT user's own refresh token was deliberately not consumed by the test.
Instead, an isolated OAuth public client completed DCR + PKCE authorization and
received its own `offline_access` refresh token. Before restart, that client
successfully initialized MCP and saw exactly:

- `dsh_tool_catalog`
- `dsh_tool_call`
- `dsh_skill_catalog`
- `dsh_skill_load`

The gateway was then cold-stopped again while the DSH Harness stayed running.
Loopback port 18766 became unreachable and the public tunnel returned HTTP 502.
After recreating the gateway process from the same SQLite state, the refresh token
issued before the stop successfully exchanged for a new access/refresh token pair.
Replaying the old refresh token returned `invalid_grant`, preserving single-use
rotation semantics across the process boundary.

The new access token then initialized MCP after restart, observed the same four
fixed meta-tools with `tools.listChanged=false`, discovered 34 DSH-internal tools
through `dsh_tool_catalog`, and discovered `diagnosing-bugs` through
`dsh_skill_catalog`.

The isolated OAuth drill client and its tokens were removed after the test. The
ChatGPT user's durable refresh grant was left untouched.

## Real ChatGPT client recovery

After the isolated drill client was removed, the gateway database was left with
the real ChatGPT client's durable refresh grant but no live access token. The
user then returned to the already-connected ChatGPT Web conversation and invoked
`dsh_skill_catalog` without reconnecting, rescanning tools, or reauthorizing the
App. The call succeeded and returned the hot-loaded `diagnosing-bugs` Skill.

Immediately after that call, the OAuth database showed one newly issued access
token and one refresh token for the same ChatGPT client and the same grant id.
There were no authorization codes or pending authorization requests. The new
access token expires at `2026-08-15T08:05:37Z`; the rotated refresh token expires
at `2026-09-14T07:05:37Z`. This proves the real ChatGPT client recovered through
the persisted refresh-token grant after the gateway process restart rather than
through a new authorization flow.

## Result

Process-level recovery is proven for the composed Harness, installed DSH plugins,
filesystem Skills, gateway OAuth state, refresh-token rotation, real ChatGPT Web
client auto-refresh, and the public meta-only MCP contract.

This does **not** satisfy the operating-system reboot gate. A host reboot still
needs to prove that the chosen service manager starts the named tunnel, DSH
Harness, and gateway automatically and in the correct dependency order.
