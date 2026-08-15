# Release checklist

This checklist follows the repository contract in [`../AGENTS.md`](../AGENTS.md): the release target is **ChatGPT Web connected to a mature DSH Harness**, not an autonomous second-model runtime. Historical session/goal experiments remain documented in [`legacy-dsh-prototype.md`](legacy-dsh-prototype.md) and are not primary release gates.

A checked item requires executable or recorded integration evidence; documentation alone is not enough.

## Proven Harness gates

- [x] Stable `dsh_tool_catalog` + `dsh_tool_call` meta-tools discover and invoke the live DSH `ToolRuntime` catalog without bespoke Python wrappers or a refreshed MCP tool list.
- [x] Projected calls execute through DSH `ToolRuntime.execute(...)`, preserving DSH guards, policy, result normalization, and scoped restrictions.
- [x] The bridge uses a DSH agent-preset scope identity without submitting prompts or requiring a model-provider API key; ChatGPT remains the only reasoning agent.
- [x] DSH community skills are discovered and loaded through the native scoped `SkillRegistry` with model-invocation policy preserved.
- [x] DSH image results and attachment-backed images reach ChatGPT as MCP image content without per-tool wrappers.
- [x] DSH `additionalContexts` survive the external-model boundary, including policy/guard reminders and nested multimodal contexts.
- [x] First-class DSH tool projection remains available as an optional UX optimization. Live DSH `tools/change` invalidations advance a bridge catalog revision and the gateway publishes MCP `tools/list_changed` for subscription-capable clients; correctness does not depend on clients honoring it.
- [x] The modern MCP `2026-07-28` capability surface advertises `tools.listChanged=true` through the SDK subscription seam.
- [x] Embedded OAuth persists DCR clients/tokens, supports PKCE public clients, rotates refresh tokens, revokes grant families, bounds anonymous registration state, and keeps the DSH Host private behind the OAuth-protected MCP gateway.
- [x] Public HTTPS development smoke has exercised OAuth/MCP through a reverse proxy while the gateway and raw DSH Host remained loopback-bound; Host/Origin rebinding checks are regression-tested.
- [x] The exact DSH runtime is pinned to `@deepseek-ai/dsh@0.1.0-rc.6`; the checked lock contains 587 integrity-pinned packages and the deployment uses Node 24.19.0.
- [x] Optional local-shell-mcp composition stays behind DSH and narrows its overlapping tool surface, keeping LSM as an execution/access provider rather than the primary Harness.

## Release-blocking drills

These remain unchecked until performed against the intended long-running host and real ChatGPT Web UI.

- [ ] Install the pinned DSH runtime and gateway using the checked-in deployment templates on the target host; make `python3 scripts/preflight-deployment.py` pass first and verify both services use the documented Unix users, state directories, and loopback boundaries.
- [ ] Put the real public HTTPS reverse proxy/domain in front of the loopback gateway and make `python3 scripts/smoke-public-oauth.py --base-url https://<exact-origin>` pass with the exact production issuer/resource/Host/Origin configuration.
- [ ] Connect the production MCP endpoint from a normal ChatGPT Web conversation and verify DSH-native preset tools can be discovered and called with no `DEEPSEEK_API_KEY` or other second-model credential configured.
- [ ] Perform the product-level acceptance test from `AGENTS.md`: after ChatGPT has already approved/loaded the connector, install a representative community DSH extension with little or no modification, discover it through the already-present stable meta-tools, and successfully use it from ChatGPT Web without an extension-specific wrapper, connector re-publication/re-approval, or first-class tool-list refresh.
- [ ] Separately test the optional UX path: while ChatGPT remains connected, add/remove a compatible DSH tool and observe whether the client honors emitted `tools/list_changed`. If ChatGPT keeps a frozen tool snapshot, record that as an expected client boundary; the stable meta-tool acceptance test above must still pass.
- [ ] Perform an operating-system reboot drill and verify the DSH Harness, gateway, OAuth state, configured DSH plugins/skills, and projected ChatGPT capability surface recover automatically.
- [ ] Perform an offline backup/restore drill for DSH configuration/state, workspace data, and OAuth state; verify the restored Harness exposes the same intended capability surface.
- [ ] Decide the first release version/tag and write release notes from the exact commit that passed the drills above. Keep the package on a development version until these gates pass.
- [ ] If distributing the project for third-party use, choose and add an explicit license/security-support policy; do not infer a license from repository visibility.

## Known boundaries

- Dynamic first-class tool refresh depends on the MCP client honoring the modern tool-list change subscription mechanism. The gateway publishes the standard event but does not maintain a second shadow catalog solely to work around a client that ignores it.
- The DSH bridge is loopback-internal. Public authentication and transport security terminate at `dsh-mcp-gateway`; exposing the raw bridge is outside the supported security boundary.
- DSH is still a developer-preview dependency. DSH-specific compatibility code should remain concentrated in the small bridge seam so an upstream contract change does not spread through the public MCP layer.
