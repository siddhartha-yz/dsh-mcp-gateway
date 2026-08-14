# Deployment

The primary deployment implements the repository contract directly:

```text
public HTTPS reverse proxy / tunnel
              |
              v
      dsh-mcp-gateway
       127.0.0.1:18766
              |
              | OAuth-protected MCP -> loopback Harness bridge
              v
        DSH Web Host
        127.0.0.1:3080
              |
              v
        DSH Harness
    tools / skills / plugins
```

ChatGPT is the reasoning agent. The DSH process is a Harness host and **does not need a model-provider API key** for this path. The checked-in DSH service always loads `deploy/dsh/chatgpt-bridge.cordis.yml`; the public gateway points at it with `--dsh-harness-url`. The legacy autonomous DSH Web API adapter is documented separately in [`legacy-dsh-prototype.md`](legacy-dsh-prototype.md) and is not part of this deployment.

Both HTTP listeners remain loopback-only. Only the OAuth-protected gateway is placed behind public HTTPS; never expose port 3080 directly.

## Tested runtime boundary

The deployment lock currently uses `@deepseek-ai/dsh@0.1.0-rc.6` and Node `24.19.0`. The self-contained Node tree lives at `/opt/dsh-runtime/node`; do not silently substitute a distribution-global Node. Upgrade either pin deliberately and rerun the integration tests.

```text
/opt/dsh-runtime/                  pinned DSH CLI/runtime
/srv/dsh-mcp-gateway/              this repository + Python venv
/srv/dsh-workspace/                DSH host working directory
/var/lib/dsh-harness/              DSH_HOME
/var/lib/dsh-mcp-gateway/          OAuth state
/etc/dsh-mcp-gateway/*.env         private configuration, mode 0600
```

## Bootstrap

From a clean checkout:

```sh
sudo ./scripts/bootstrap-target-host.sh
```

The bootstrap prompts only for the exact public HTTPS origin and the gateway owner PIN/passphrase. Existing `DSH_MCP_PUBLIC_BASE_URL` and `DSH_MCP_GATEWAY_ADMIN_PIN` values may be supplied through the environment. It deliberately does **not** request `DEEPSEEK_API_KEY` or any other model credential.

Use `--no-start` to stop after installation/preflight and `--replace-source` only for a deliberate replacement of `/srv/dsh-mcp-gateway`.

The equivalent service accounts and state directories are:

```sh
sudo useradd --system --user-group --home /var/lib/dsh-harness --create-home dsh-agent
sudo useradd --system --user-group --home /var/lib/dsh-mcp-gateway --create-home dsh-gateway

sudo install -d -o dsh-agent -g dsh-agent -m 0700 /var/lib/dsh-harness
sudo install -d -o dsh-agent -g dsh-agent -m 0750 /srv/dsh-workspace
sudo install -d -o dsh-gateway -g dsh-gateway -m 0700 /var/lib/dsh-mcp-gateway
sudo install -d -o root -g root -m 0700 /etc/dsh-mcp-gateway
```

Install the verified Node `24.19.0` tree so these paths exist:

```text
/opt/dsh-runtime/node/bin/node
/opt/dsh-runtime/node/bin/npm
```

Then reproduce the pinned DSH graph:

```sh
python3 scripts/verify-dsh-runtime-lock.py
sudo install -m 0644 deploy/dsh-runtime/package.json /opt/dsh-runtime/package.json
sudo install -m 0644 deploy/dsh-runtime/package-lock.json /opt/dsh-runtime/package-lock.json
sudo env \
  PATH=/opt/dsh-runtime/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin \
  npm_config_registry=https://registry.npmjs.org/ \
  /opt/dsh-runtime/node/bin/npm ci \
  --prefix /opt/dsh-runtime --omit=dev --no-audit --no-fund
```

Install the gateway:

```sh
cd /srv/dsh-mcp-gateway
python3 -m venv .venv
.venv/bin/python -m pip install --constraint deploy/server-constraints.txt -e '.[server]'
```

## Configuration

`/etc/dsh-mcp-gateway/dsh.env` needs only Harness-host state for the primary path:

```env
DSH_HOME=/var/lib/dsh-harness
DSH_TELEMETRY_DISABLED=1
```

`/etc/dsh-mcp-gateway/gateway.env` owns only the public MCP/OAuth boundary:

```env
DSH_MCP_PUBLIC_BASE_URL=https://dsh.example.com
DSH_MCP_GATEWAY_ADMIN_PIN=<private high-entropy value, at least 12 characters>
```

Keep both files root-owned and mode `0600`. Do not add a model API key merely to make the Harness bridge run. Optional execution-provider configuration, including local-shell-mcp, belongs in the DSH environment because DSH owns that composition.

## Validate before start

Run the repository gates before starting services:

```sh
python3 scripts/verify-server-constraints.py
python3 scripts/verify-dsh-runtime-lock.py
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q src tests scripts
./scripts/verify-systemd.sh
python3 scripts/preflight-deployment.py
```

Install the units only after those checks pass:

```sh
sudo cp deploy/systemd/dsh-web-host.service /etc/systemd/system/
sudo cp deploy/systemd/dsh-mcp-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dsh-web-host.service dsh-mcp-gateway.service
```

The DSH unit launches:

```text
dsh web --patch /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml --host 127.0.0.1 --port 3080
```

The gateway unit launches with:

```text
--dsh-harness-url http://127.0.0.1:3080
```

This distinction is a release invariant: production must not silently fall back to the legacy `--dsh-web-url` autonomous-agent adapter.

## Health and readiness

```sh
curl -fsS http://127.0.0.1:18766/healthz
curl -fsS http://127.0.0.1:18766/readyz
```

`/healthz` checks the public gateway process. In primary Harness mode, `/readyz` verifies that the loopback DSH capability bridge can return its current `ctx.tools` catalog. It does not call a model provider.

The gateway uses `Wants=` rather than `Requires=` for the DSH Host. An independent DSH restart therefore does not tear down OAuth/MCP; readiness becomes unavailable until the Harness bridge returns.

## Public HTTPS and OAuth

Map the public origin to `http://127.0.0.1:18766`. A Cloudflare Tunnel ingress can use:

```yaml
ingress:
  - hostname: dsh.example.com
    service: http://127.0.0.1:18766
  - service: http_status:404
```

Set `DSH_MCP_PUBLIC_BASE_URL=https://dsh.example.com` to exactly that origin. Never route public traffic to `127.0.0.1:3080`.

After HTTPS is active:

```sh
curl -fsS https://dsh.example.com/.well-known/oauth-authorization-server
curl -fsS https://dsh.example.com/.well-known/oauth-protected-resource/mcp
curl -fsS https://dsh.example.com/healthz
curl -fsS https://dsh.example.com/readyz
python3 scripts/smoke-public-oauth.py --base-url https://dsh.example.com
```

## Optional local-shell-mcp execution provider

`deploy/dsh/local-shell-mcp.cordis.yml` mounts local-shell-mcp behind DSH through DSH's MCP client. The adjacent scoped filter exposes only the selected differentiated LSM capabilities, such as browser and dynamic-MCP execution tools. This is optional composition behind DSH, not a second public Harness.

Configure the commented `DSH_LSM_*` variables in `dsh.env`, apply that overlay in the DSH Host composition, and keep LSM itself private. Its workspace/policy boundary remains authoritative for the calls it executes.

## Optional durable session search

`deploy/dsh/session-search.cordis.yml` enables DSH's derived SQLite session index lazily. The supplied systemd drop-in intentionally includes **both** patches:

```text
--patch .../chatgpt-bridge.cordis.yml --patch .../session-search.cordis.yml
```

so enabling search cannot accidentally remove the required ChatGPT Harness bridge.

## Backup and upgrades

Canonical state domains are:

```text
/var/lib/dsh-harness/       DSH Harness state
/srv/dsh-workspace/         files modified through DSH/execution providers
/var/lib/dsh-mcp-gateway/   OAuth client/token state
/etc/dsh-mcp-gateway/       private deployment configuration
```

For a consistent backup, stop the public gateway first, then the DSH Host; restore ownership/modes before starting DSH and then the gateway. The software trees under `/opt/dsh-runtime` and `/srv/dsh-mcp-gateway` are reproducible from the pinned inputs and Git commit.

Before upgrading MCP or DSH: update pins in a branch, run the full tests, rerun the DSH community-tool projection smoke and public OAuth smoke, review projected tool schemas, and deploy only after the primary Harness path remains model-provider-free.
