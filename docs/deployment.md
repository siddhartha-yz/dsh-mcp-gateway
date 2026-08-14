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

The raw DSH Web Host is not an authenticated public API and must stay on loopback or another explicitly trusted private network. The gateway is also loopback-bound in the example deployment; public HTTPS terminates at a reverse proxy or tunnel.

## Tested runtime boundary

The current experimental Web Host adapter is tested against `@deepseek-ai/dsh@0.1.0-rc.6`. Pin that version for a deployment that should match the repository's integration evidence. DSH is a developer-preview dependency, so upgrade it deliberately and run the gateway test suite before changing the pin.

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
sudo useradd --system --home /var/lib/dsh-harness --create-home dsh-agent
sudo useradd --system --home /var/lib/dsh-mcp-gateway --create-home dsh-gateway

sudo install -d -o dsh-agent -g dsh-agent -m 0700 /var/lib/dsh-harness
sudo install -d -o dsh-agent -g dsh-agent -m 0750 /srv/dsh-workspace
sudo install -d -o dsh-gateway -g dsh-gateway -m 0700 /var/lib/dsh-mcp-gateway
sudo install -d -o root -g root -m 0700 /etc/dsh-mcp-gateway
```

Install the DSH runtime at the path used by the service template and pin the tested release:

```sh
sudo npm install --prefix /opt/dsh-runtime @deepseek-ai/dsh@0.1.0-rc.6
```

Install this gateway into `/srv/dsh-mcp-gateway` and create its virtual environment:

```sh
cd /srv/dsh-mcp-gateway
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
```

Before relying on the deployment, run the runtime-compatible test suite:

```sh
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q src tests
./scripts/verify-systemd.sh
```

`verify-systemd.sh` is unprivileged: it validates the checked-in unit directives with `systemd-analyze verify` while substituting only the example `ExecStart` binary paths in temporary copies.

Linting belongs to the contributor/CI environment (`python -m pip install -e '.[dev]' && ruff check src tests`) rather than the minimal production runtime.

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

Install the units:

```sh
sudo cp deploy/systemd/dsh-web-host.service /etc/systemd/system/
sudo cp deploy/systemd/dsh-mcp-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dsh-web-host.service dsh-mcp-gateway.service
```

The gateway uses `Wants=` rather than `Requires=` for the DSH Host. That is intentional: an independent DSH Host restart should not restart the public gateway. Gateway startup also does not synchronously probe DSH, so a slow or temporarily unavailable Host does not put the public process into a restart loop. While DSH is unavailable, `/healthz` remains 200 but `/readyz` returns 503. Once the Host returns, an existing non-running durable session is routed through the idempotent attach/resume probe. A loopback smoke test exercised this exact transition without restarting the gateway: DSH absent produced `healthz=200`/`readyz=503`, then bringing the dependency up changed `readyz` to 200 in the same gateway process.

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

The authorization-server metadata should advertise `offline_access`; the protected-resource metadata should require only `dsh:control`. Dynamic client registration, PKCE authorization, refresh-token issuance/rotation, and a two-MCP-session DSH continuation flow are covered by repository tests and development smoke tests. DCR storage is bounded to 256 persisted clients by default; set `--max-registered-clients N` on the gateway service if the deployment intentionally needs a different cap. Reaching the cap rejects only new registrations; existing registered clients continue to authenticate. Pending authorization requests are separately bounded to 512 globally and 8 per client, with expired rows pruned on each authorize write before the atomic capacity check.

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
