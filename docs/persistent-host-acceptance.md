# Persistent host acceptance

This record covers the promotion of the previously validated temporary ChatGPT -> DSH stack into the long-running systemd deployment on the intended host. It is a service-level deployment acceptance, not yet the operating-system reboot acceptance.

## 2026-08-15 promotion

The promotion source was commit `85e6d4149e8aeafba9a09c5220ea9a77fc3c45c3`.

Before cutover the live Harness catalog was snapshotted at 34 DSH-internal tools and one model-invocable filesystem Skill, `diagnosing-bugs`. The temporary DSH Harness, OAuth gateway, and DSH named Cloudflare tunnel were then stopped before durable state was copied.

The checked-in promotion flow installed the pinned production runtime and gateway, migrated state, and started three systemd services:

- `dsh-web-host.service` under the explicit personal-workspace override, running as `ubuntu` from `/home/ubuntu/workspace` while keeping `DSH_HOME=/var/lib/dsh-harness`;
- `dsh-mcp-gateway.service` as `dsh-gateway`, bound to loopback port 18766 and explicitly using `--tool-surface meta-only`;
- `dsh-cloudflared.service` as `dsh-tunnel`, using the named tunnel configuration under `/etc/dsh-cloudflared`.

All three processes were observed in their own `/system.slice/<unit>.service` cgroups and all three units were linked from `multi-user.target.wants`, proving that the live processes are systemd-owned and enabled for normal boot.

The installed gateway commit marker under `/srv/dsh-mcp-gateway` matched the promotion source commit.

## Durable DSH ecosystem state

After promotion the production Harness returned exactly 34 internal tools. The nine community tools used by the earlier acceptance tests were all present:

- `find_dsh_plugin`
- `calculator`
- `json`
- `regex`
- `stat`
- `time`
- `encoding`
- `csv`
- `schema`

The SkillRegistry still returned exactly `diagnosing-bugs` from `user-dsh`.

All nine community plugin dependencies were rebound to local tarballs under `/var/lib/dsh-harness/plugin-artifacts`. The production profile contains no references to the old `.dsh-community-acceptance` or `.dsh-chatgpt-live-home` paths. A provenance manifest records the source specification and SHA-256 for each localized artifact. The production profile therefore does not need the temporary acceptance directories in order to restart.

## Public service verification

Local gateway `/healthz` and `/readyz` returned 200. Five consecutive public probes to `https://dsh.example.com/readyz` returned 200 after the named tunnel was moved under systemd.

The public release smoke was updated to the current meta-only Harness contract and then run against the real production origin. It passed the following checks:

- `healthz=200`
- `readyz=200`
- OAuth authorization-server metadata and protected-resource metadata
- dynamic public-client registration
- PKCE authorization and owner approval
- authorization-code token exchange with `offline_access`
- unauthenticated MCP initialize rejected with 401
- authenticated MCP initialize negotiated protocol `2025-11-25`
- `capabilities.tools.listChanged=false`
- `tools/list` returned exactly the four fixed meta-tools
- protected `dsh_tool_catalog` returned 34 live DSH tools
- refresh-token rotation succeeded and replay of the old refresh token returned `invalid_grant`

One isolated dynamic OAuth client registration from the smoke remains persisted, as documented by the smoke script. It is not the ChatGPT client and does not affect the user's existing refresh grant.

## Remaining reboot gate

This promotion proves that the desired systemd services are installed, enabled, serving the production domain, and backed by self-contained durable DSH/OAuth state. It does not by itself prove that the host can power-cycle and reconstruct the same state.

Before the operating-system reboot drill, the already-connected ChatGPT Web App should make one successful meta-tool call without reconnecting or rescanning. After that baseline, reboot the host and repeat the same call without reconnecting or rescanning. The server-side acceptance after boot should verify new process start times, 34 internal tools, `diagnosing-bugs`, the four-tool MCP surface, public readiness, and OAuth refresh recovery.
