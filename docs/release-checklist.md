# Release checklist

This checklist follows the repository contract in [`../AGENTS.md`](../AGENTS.md): the release target is **ChatGPT Web connected to a mature DSH Harness**, not an autonomous second-model runtime. The old gateway-owned session/goal prototypes were removed from the production tree in P0 and remain available through git history.

A checked item requires executable or recorded integration evidence; documentation alone is not enough.

## Proven Harness gates

- [x] Stable `dsh_tool_catalog` + `dsh_tool_call` meta-tools discover and invoke the live DSH `ToolRuntime` catalog without bespoke Python wrappers or a refreshed MCP tool list.
- [x] Projected calls execute through DSH `ToolRuntime.execute(...)`, preserving DSH guards, policy, result normalization, and scoped restrictions.
- [x] Catalog/skill discovery uses DSH's preset standing scope without starting an Agent, Session, or turn. Tool execution lazily uses a metadata-only capability Agent/session keyed by a non-reversible hash of workspace cwd + preset id + resolved composition path + DSH's `mtimeMs`/size generation stamp plus a content digest. Stable generations resume across DSH restarts; default-preset or composition hot reloads receive a new helper, and setup fails closed if a generation changes while the helper is being created. No prompt or model-provider API key is used, and ChatGPT remains the only reasoning agent.
- [x] DSH community skills are discovered and loaded through the native scoped `SkillRegistry` with model-invocation policy preserved.
- [x] DSH image results and attachment-backed images reach ChatGPT as MCP image content without per-tool wrappers.
- [x] DSH `additionalContexts` survive the external-model boundary, including policy/guard reminders and nested multimodal contexts.
- [x] Default Harness mode is protocol-level meta-only: MCP `tools/list` stays fixed to the four stable DSH meta-tools and the modern MCP capability surface does not advertise `tools.listChanged`.
- [x] First-class DSH tool projection remains available only as explicit `--tool-surface projected` UX opt-in. In that mode live DSH `tools/change` invalidations advance a restart-safe `(bridge instance, tool revision)` token and the gateway publishes MCP `tools/list_changed` for subscription-capable clients.
- [x] Embedded OAuth persists DCR clients/tokens, supports PKCE public clients, rotates refresh tokens, revokes grant families, bounds anonymous registration state, and keeps the DSH Host private behind the OAuth-protected MCP gateway.
- [x] Public HTTPS development smoke has exercised OAuth/MCP through a reverse proxy while the gateway and raw DSH Host remained loopback-bound; Host/Origin rebinding checks are regression-tested.
- [x] The exact DSH runtime is pinned to `@deepseek-ai/dsh@0.1.2-rc.1`; the checked lock contains 583 integrity-pinned package entries, the deployment uses Node 24.19.0, and the npm lifecycle-script allow/deny policy is explicitly reviewed and regression-checked.
- [x] Optional local-shell-mcp composition stays behind DSH and narrows its overlapping tool surface, keeping LSM as an execution/access provider rather than the primary Harness.
- [x] Process-level cold restart recovery is proven for the DSH Harness, installed community tools, filesystem Skills, gateway OAuth SQLite state, refresh-token rotation, the real ChatGPT Web client's automatic refresh-token recovery without reconnect/rescan/reauthorization, and the fixed four-tool MCP contract; see [`restart-recovery-acceptance.md`](restart-recovery-acceptance.md). This is intentionally separate from the still-pending host reboot drill.

## v0.1 release gates

Checked items below are required for the first release. Host-wide and optional-client UX experiments that do not affect the default meta-only correctness path are listed separately under post-v0.1 follow-ups.

- [x] Install the pinned DSH runtime and gateway on the intended host using the checked-in deployment templates plus the documented personal-workspace override; promotion preflight passed, systemd owns the live processes, durable state is under `/var/lib`, and the Harness/gateway remain loopback-bound. See [`persistent-host-acceptance.md`](persistent-host-acceptance.md).
- [x] Put the real public HTTPS named tunnel/domain in front of the loopback gateway and make the current meta-only `scripts/smoke-public-oauth.py` pass against `https://dsh.example.com`, including exact four-tool MCP surface, 34-tool DSH catalog, PKCE, `offline_access`, and single-use refresh rotation. See [`persistent-host-acceptance.md`](persistent-host-acceptance.md).
- [x] Connect the production MCP endpoint from a normal ChatGPT Web conversation and verify the already-connected App continues through the persistent systemd deployment without reconnect/rescan: the real ChatGPT client reported 34 DSH-internal tools, including `time`, `encoding`, `csv`, and `schema`, plus the `diagnosing-bugs` Skill. The production DSH environment remains model-provider-free.
- [x] Perform the strict product-level acceptance test from `AGENTS.md` using independent community extensions and the procedure in [`community-extension-acceptance.md`](community-extension-acceptance.md): ChatGPT saw exactly four meta-tools with `tools.listChanged=false`; four previously absent community tools were then installed behind the unchanged gateway and used through catalog/call without changing the public MCP schema. The multi-tool result was independently verified against the hidden fixture.
- [x] Perform the strict DSH Skill ecosystem acceptance test in [`community-skill-acceptance.md`](community-skill-acceptance.md): start with an empty SkillRegistry, hot-add an independent community `SKILL.md` without restarting DSH/gateway or refreshing ChatGPT, load it through the two fixed skill meta-tools, and verify that its instructions materially change ChatGPT's debugging workflow while public `tools/list` remains the same four tools.
- [x] Perform an offline backup/restore drill for DSH configuration/state, selected representative workspace data, and OAuth state; verify an isolated restored Harness exposes the same 34-tool catalog, `diagnosing-bugs` Skill, exact four-tool MCP surface, and a working cloned ChatGPT refresh grant. See [`backup-restore-acceptance.md`](backup-restore-acceptance.md).
- [x] Select `v0.1.0` as the first release version and write release notes in [`releases/v0.1.0.md`](releases/v0.1.0.md). The package/runtime version is `0.1.0`; the annotated Git tag is created from the final release commit after all final tests/build checks pass.
- [x] Third-party distribution policy is explicit: the repository uses the MIT License and includes `SECURITY.md` with supported-version, private-reporting, deployment-boundary, and backup-secret guidance.

## Post-v0.1 follow-ups

- [ ] Separately test the optional projected UX path: while ChatGPT remains connected, add/remove a compatible DSH tool and observe whether the client honors emitted `tools/list_changed`. If ChatGPT keeps a frozen tool snapshot, record that as an expected client boundary. The default meta-only path does not depend on this behavior.
- [ ] During a normal host maintenance window, perform an operating-system reboot drill and verify the DSH Harness, gateway, OAuth state, configured DSH plugins/skills, and public tunnel recover automatically. This is intentionally deferred because the acceptance host also runs unrelated important projects; process-level cold restart and systemd enablement are already proven.

## Known boundaries

- Dynamic first-class tool refresh exists only in explicit `--tool-surface projected` mode and depends on the MCP client honoring the modern tool-list change subscription mechanism. Default meta-only mode has no such channel.
- In the strict ChatGPT Web acceptance run, two absolute-workspace-path `read` invocations wrapped by `dsh_tool_call` were rejected by a platform pre-dispatch safety check, while equivalent workspace-relative paths succeeded. Prefer workspace-relative paths in ChatGPT-facing workflows and treat absolute-path reachability as client-policy-dependent rather than a DSH guarantee.
- The DSH bridge is loopback-internal. Public authentication and transport security terminate at `dsh-mcp-gateway`; exposing the raw bridge is outside the supported security boundary.
- DSH is still a developer-preview dependency. DSH-specific compatibility code should remain concentrated in the small bridge seam so an upstream contract change does not spread through the public MCP layer.
