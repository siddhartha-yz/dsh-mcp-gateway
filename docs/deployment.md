# Deployment

This repository is designed around three independently managed boundaries:

```text
public HTTPS reverse proxy / tunnel
              |
              v
      dsh-mcp-gateway
       127.0.0.1:18766
              |
              v
        DSH Web Host
        127.0.0.1:3080
              |
              v
       agent workspace
```

The raw DSH Web Host is not an authenticated public API and must stay on loopback or another explicitly trusted private network. The gateway is also loopback-bound in the example deployment; public HTTPS terminates at a reverse proxy or tunnel. `--dsh-web-url` names the Host HTTP(S) origin only (for example `http://127.0.0.1:3080`): do not include credentials, an `/api` path prefix, params, query, or fragment. The gateway owns the `/api/<method>` suffix and rejects ambiguous targets at startup.

## Tested runtime boundary

The current experimental Web Host adapter is tested against `@deepseek-ai/dsh@0.1.0-rc.6` using Node `24.19.0`. The rc6 package does not declare an `engines` requirement, so Node 24.19.0 is not claimed as an upstream minimum; it is the exact known-good runtime pin for this repository's current release drills. The example systemd unit therefore uses a self-contained Node tree at `/opt/dsh-runtime/node` instead of inheriting a distribution-global Node. Upgrade either pin deliberately and rerun the gateway/integration evidence before changing it.

A practical layout is:

```text
/opt/dsh-runtime/                  pinned DSH CLI/runtime
/srv/dsh-mcp-gateway/              this repository + Python venv
/srv/dsh-workspace/                agent working directory
/var/lib/dsh-harness/              DSH_HOME / durable DSH session state
/var/lib/dsh-mcp-gateway/          OAuth SQLite state
/etc/dsh-mcp-gateway/*.env         secrets/configuration, mode 0600
```

## Install

The commands below are examples for a dedicated Linux host. Adjust user/group ownership and package-management policy to the target machine.

```sh
sudo useradd --system --user-group --home /var/lib/dsh-harness --create-home dsh-agent
sudo useradd --system --user-group --home /var/lib/dsh-mcp-gateway --create-home dsh-gateway

sudo install -d -o dsh-agent -g dsh-agent -m 0700 /var/lib/dsh-harness
sudo install -d -o dsh-agent -g dsh-agent -m 0750 /srv/dsh-workspace
sudo install -d -o dsh-gateway -g dsh-gateway -m 0700 /var/lib/dsh-mcp-gateway
sudo install -d -o root -g root -m 0700 /etc/dsh-mcp-gateway
```

Install a verified self-contained Node `24.19.0` distribution under `/opt/dsh-runtime/node` using your host's approved package/download-verification process. The resulting layout must contain at least:

```text
/opt/dsh-runtime/node/bin/node
/opt/dsh-runtime/node/bin/npm
```

Do not rely on a global `/usr/bin/node`: the checked-in systemd unit explicitly prepends `/opt/dsh-runtime/node/bin` to its service PATH, and the deployment preflight requires that exact Node version by default. The repository also records the exact npm dependency graph that produced the real rc6 integration runtime. Verify it, copy those manifests into `/opt/dsh-runtime`, then install with `npm ci` so transitive packages are reproduced from `package-lock.json` instead of re-resolved:

```sh
python3 scripts/verify-dsh-runtime-lock.py
sudo install -m 0644 deploy/dsh-runtime/package.json /opt/dsh-runtime/package.json
sudo install -m 0644 deploy/dsh-runtime/package-lock.json /opt/dsh-runtime/package-lock.json
sudo env \
  PATH=/opt/dsh-runtime/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin \
  npm_config_registry=https://registry.npmjs.org/ \
  /opt/dsh-runtime/node/bin/npm ci \
  --prefix /opt/dsh-runtime \
  --omit=dev \
  --no-audit \
  --no-fund
```

The registry is explicit on purpose. On the current integration host, inheriting its default Tencent npm mirror made a clean locked install fail with `ECONNRESET`; the same empty-directory `npm ci` completed against `registry.npmjs.org`. A deployment may substitute a vetted mirror, but do so explicitly and keep npm's lockfile integrity verification enabled rather than inheriting an unknown host-global registry setting.

The checked-in graph has also been reconstructed from an empty directory on Linux x86_64/glibc with Node `24.19.0` + npm `11.17.0`: npm installed 530 runtime packages, DSH resolved to `0.1.0-rc.6`, the Web Host answered `host.describe`/`session.list`, an idle Agent and model catalog initialized, a real model-driven `bash` tool call persisted `clean-bash-ok`, and `node-pty` successfully spawned a PTY. npm emitted an `allow-scripts` review warning for five lifecycle-script packages, but its debug log showed those pre/install/postinstall scripts actually ran with exit code 0 in this tested install. This is Linux x86_64 integration evidence, not a claim that every platform-conditioned package in the npm lock has been exercised.

Install this gateway into `/srv/dsh-mcp-gateway` and create its virtual environment:

```sh
cd /srv/dsh-mcp-gateway
python3 -m venv .venv
.venv/bin/python -m pip install --constraint deploy/server-constraints.txt -e '.[server]'
```

`pyproject.toml` intentionally keeps the broader `mcp>=2,<3` compatibility range so the normal CI lane detects regressions against newer MCP v2 releases. `deploy/server-constraints.txt` is the separate known-good deployment snapshot and is exercised by its own clean Python 3.12 CI job. The same lane runs `scripts/verify-server-constraints.py`, which rejects a newly added direct `server` dependency unless the snapshot contains an exact `==` pin for it; this prevents a forgotten constraint update from silently turning the supposedly locked deployment back into a floating install. Regenerate that snapshot deliberately when upgrading the server dependency graph; do not treat an incidental reinstall as an upgrade.

Before relying on the deployment, verify that every direct `server` runtime requirement is covered by an exact known-good pin, then run the runtime-compatible test suite:

```sh
python3 scripts/verify-server-constraints.py
python3 scripts/verify-dsh-runtime-lock.py
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q src tests scripts
./scripts/verify-systemd.sh
```

`verify-systemd.sh` is unprivileged: it validates the checked-in unit directives with `systemd-analyze verify` while substituting only the example `ExecStart` binary paths in temporary copies.

Linting belongs to the contributor/CI environment (`python -m pip install -e '.[dev]' && ruff check src tests scripts`) rather than the minimal production runtime.

## Secrets and environment

Copy the example files from `deploy/systemd/`:

```sh
sudo cp deploy/systemd/dsh.env.example /etc/dsh-mcp-gateway/dsh.env
sudo cp deploy/systemd/gateway.env.example /etc/dsh-mcp-gateway/gateway.env
sudo chmod 0600 /etc/dsh-mcp-gateway/dsh.env /etc/dsh-mcp-gateway/gateway.env
```

Set at least:

- `DEEPSEEK_API_KEY` in `dsh.env`;
- `DSH_MCP_GATEWAY_ADMIN_PIN` in `gateway.env`; it must be at least 12 characters, and a randomly generated high-entropy value is preferred over a short numeric PIN;
- `DSH_MCP_PUBLIC_BASE_URL` to the exact public HTTPS origin;
- `DSH_WORKSPACE` to the agent workspace path.

Do not put these values in the repository, unit files, shell history, or reverse-proxy configuration.

## systemd

Install the unit files, then run the unprivileged deployment preflight before starting either service:

```sh
sudo cp deploy/systemd/dsh-web-host.service /etc/systemd/system/
sudo cp deploy/systemd/dsh-mcp-gateway.service /etc/systemd/system/

python3 scripts/preflight-deployment.py

sudo systemctl daemon-reload
sudo systemctl enable --now dsh-web-host.service dsh-mcp-gateway.service
```

`preflight-deployment.py` does not call `sudo` or `systemctl`. It checks the documented service users/groups, target directories and exact owner/mode expectations, the pinned DSH package version, gateway Python/console-script presence, required environment keys and secret-file modes without printing secret values, and whether the installed unit files still match the checked-in templates. It exits nonzero until the layout is ready. `--json` provides the same secret-free result for automation, and path/user options allow staging layouts to be checked without using the production filesystem roots.

The gateway uses `Wants=` rather than `Requires=` for the DSH Host. That is intentional: an independent DSH Host restart should not restart the public gateway. Gateway startup also does not synchronously probe DSH, so a slow or temporarily unavailable Host does not put the public process into a restart loop. While DSH is unavailable, `/healthz` remains 200 but `/readyz` returns 503. Readiness uses a dedicated 1-second `host.describe` timeout rather than the normal 10-second control-RPC timeout, so a connected but wedged Host cannot hold each probe open for a full business-operation timeout. Once the Host returns, an existing non-running durable session is routed through the idempotent attach/resume probe. Loopback smoke tests exercised both lifecycle directions without restarting the gateway: DSH absent produced `healthz=200`/`readyz=503`, bringing the dependency up changed `readyz` to 200 in the same gateway process, and a deliberately 2-second Host response produced `readyz=503` in about 1 second while the backend's ordinary RPC timeout remained 10 seconds.

Run one active gateway service instance for this deployment and one DSH Web Host for the configured `DSH_HOME`. Same-session write ordering is enforced with process-local locks; this release does not claim multi-replica admission semantics or support multiple DSH Host writers sharing the same durable state.

Check local service state:

```sh
curl -fsS http://127.0.0.1:18766/healthz
curl -fsS http://127.0.0.1:18766/readyz
```

Expected healthy responses are minimal JSON objects; readiness deliberately does not expose DSH provider, workspace, or transport details.

## Optional durable session search

DSH rc6 deliberately ships Web full-text session search disabled (`openAt: never`). The gateway keeps `dsh_search` in a stable tool catalog, but the call fails with a clear capability error unless the DSH deployment opts into the session-query SQLite index.

This repository includes `deploy/dsh/session-search.cordis.yml`, an optional official Cordis overlay that changes only the existing `session-query-sqlite` row:

```yaml
- id: session-query-sqlite
  config:
    path: !!js dshHomePath('derived/session-query.sqlite3')
    openAt: first-search
```

`first-search` keeps normal Web startup free of the SQLite import/open cost and builds or reconciles the index only when content search is first used. The database is a disposable derived FTS5 index, not canonical session persistence. In a real rc6 test, search found a remembered phrase before and after a complete Host restart; deleting the entire derived index and restarting still rebuilt it from durable session logs and returned the same session.

To enable it in the example systemd deployment, install the provided drop-in:

```sh
sudo install -d -m 0755 /etc/systemd/system/dsh-web-host.service.d
sudo cp deploy/systemd/dsh-web-host-search.conf.example \
  /etc/systemd/system/dsh-web-host.service.d/search.conf
sudo systemctl daemon-reload
sudo systemctl restart dsh-web-host.service
```

The index contains searchable projections of user/assistant session content, so protect `$DSH_HOME/derived` with the same filesystem trust boundary as the session logs. Do not point the derived-index path at the session-persistence database and do not share one index path between multiple DSH processes.

## Reverse proxy or tunnel

The external HTTPS origin must map to `http://127.0.0.1:18766`. For example, a Cloudflare Tunnel ingress can contain:

```yaml
ingress:
  - hostname: dsh.example.com
    service: http://127.0.0.1:18766
  - service: http_status:404
```

Configure `DSH_MCP_PUBLIC_BASE_URL=https://dsh.example.com` to exactly match that origin. The gateway remains loopback-bound while MCP DNS-rebinding protection explicitly allowlists the declared public Host/Origin plus loopback values.

Never expose `127.0.0.1:3080` through the reverse proxy.

## OAuth and MCP checks

After public HTTPS is active:

```sh
curl -fsS https://dsh.example.com/.well-known/oauth-authorization-server
curl -fsS https://dsh.example.com/.well-known/oauth-protected-resource/mcp
curl -fsS https://dsh.example.com/healthz
curl -fsS https://dsh.example.com/readyz
```

The authorization-server metadata should advertise `offline_access`; the protected-resource metadata should require only `dsh:control`. Once the real public origin is configured, run the one-shot release smoke from the repository root:

```sh
python3 scripts/smoke-public-oauth.py --base-url https://dsh.example.com
```

It prompts for the owner PIN with terminal echo disabled. If an operator deliberately reads the root-only deployment env instead, `--pin-file /etc/dsh-mcp-gateway/gateway.env` accepts that `0600` file without printing the PIN. The smoke requires HTTPS (HTTP is available only behind an explicit loopback-only test flag), verifies health/readiness and OAuth/resource metadata, keeps OAuth endpoints on the declared origin, sends the declared `Origin` on MCP requests, runs DCR + PKCE + owner approval + token exchange, checks unauthenticated MCP rejection, initializes an authenticated MCP session using the negotiated protocol version, verifies the expected tool catalog plus `dsh_list`, and rotates/replay-checks the refresh token. It intentionally leaves one registered public OAuth client in the database, so treat it as a release drill rather than a periodic probe.

Dynamic client registration, PKCE authorization, refresh-token issuance/rotation, and a two-MCP-session DSH continuation flow are also covered by repository tests and development smoke tests. DCR storage is bounded to 256 persisted clients by default; set `--max-registered-clients N` on the gateway service if the deployment intentionally needs a different cap. Reaching the cap rejects only new registrations; existing registered clients continue to authenticate. One normalized client record is also capped at 32 KiB of persisted UTF-8 JSON by default; raise `--max-client-metadata-bytes N` only for a known client that legitimately needs larger metadata. A separate application-level guard rejects raw `POST /register` bodies above 64 KiB by default (`--max-registration-request-bytes N`) before the MCP SDK parses them, including chunked requests without `Content-Length`. Keep reverse-proxy/WAF request-body and rate limits as an earlier defense-in-depth layer and to cover the rest of the public HTTP surface; the gateway no longer relies on that perimeter alone for DCR body size. Pending authorization requests are separately bounded to 512 globally and 8 per client, with expired rows pruned on each authorize write before the atomic capacity check. OAuth write transactions also opportunistically prune expired pending requests, authorization codes, access tokens, and refresh tokens, preventing normal long-lived refresh activity from accumulating dead rows indefinitely.

## Backup and restore

For a consistent full-state backup, quiesce both writers first. Stop the public gateway so no new control/OAuth writes can arrive, then stop the DSH Host so autonomous rounds and workspace/session-log writes have settled:

```sh
sudo systemctl stop dsh-mcp-gateway.service
sudo systemctl stop dsh-web-host.service
```

Back up these trust domains together:

```text
/var/lib/dsh-harness/       DSH_HOME: durable sessions, goals, projections, optional derived state
/srv/dsh-workspace/         files actually modified by the agent
/var/lib/dsh-mcp-gateway/   OAuth clients, codes/tokens, gateway state
/etc/dsh-mcp-gateway/       deployment configuration and secrets; protect separately as sensitive material
```

The DSH runtime under `/opt/dsh-runtime` and the gateway source/venv under `/srv/dsh-mcp-gateway` are reproducible software artifacts rather than canonical task state; record the deployed gateway Git commit, DSH release, Node/Python versions, and rebuild them from the pinned deployment inputs. The optional search database under `$DSH_HOME/derived` is derived state and may be omitted if the restore procedure is prepared to rebuild it from durable session logs.

Restore while both services are stopped, preserve the documented ownership/modes (`dsh-agent` for DSH/workspace state, `dsh-gateway` for gateway state, root-only `0600` environment files), then start the Host before the gateway:

```sh
sudo systemctl start dsh-web-host.service
sudo systemctl start dsh-mcp-gateway.service
```

OAuth state and DSH task state have deliberately separate failure domains. Losing `/var/lib/dsh-mcp-gateway` invalidates remembered dynamic clients/tokens and requires client registration/authorization again, but it does not delete DSH sessions. Losing `DSH_HOME` is the destructive session-history loss. Restored OAuth tokens remain bound to the configured issuer/resource, so changing the public base URL intentionally makes old tokens unusable. Finally, a restored or restarted DSH process does **not** regain process-local goal continuation authority: a durable goal may still say `phase=active` while remaining disarmed until an explicit `dsh_goal_resume`.

## Restart semantics

A process restart is not equivalent to authorizing autonomous continuation:

```text
DSH Host restart
    -> durable session can be attached/cold-resumed
    -> durable goal phase/revision/history remain
    -> goal activation is process-local and remains disarmed
    -> explicit dsh_goal_resume re-arms autonomous rounds
```

This is an intentional safety boundary. Do not add a service-level hook that automatically resumes every active durable goal after boot unless a separate durable authorization policy is designed and reviewed.

## Upgrades

Before upgrading MCP or DSH dependencies:

1. update the dependency pin in a branch;
2. run all repository tests;
3. rerun cold-resume and public OAuth/MCP smoke tests when DSH wire behavior changes;
4. review tool schemas/annotations because ChatGPT/custom MCP app deployments may retain an approved snapshot until refreshed;
5. deploy only after CI is green.
