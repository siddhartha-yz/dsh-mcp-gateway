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

ChatGPT is the reasoning agent. The DSH process is a Harness host and **does not need a model-provider API key** for this path. The checked-in DSH service always loads `deploy/dsh/chatgpt-bridge.cordis.yml`; the public gateway points at it with `--dsh-harness-url` and explicitly uses `--tool-surface meta-only`. Gateway-owned autonomous DSH/session runtimes were removed in P0 and remain available only through git history.

Both HTTP listeners remain loopback-only. Only the OAuth-protected gateway is placed behind public HTTPS; never expose port 3080 directly.

## Tested runtime boundary

The deployment lock currently uses `@deepseek-ai/dsh@0.1.0-rc.6`, `pnpm@10.34.5`, and Node `24.19.0`. The self-contained Node tree lives at `/opt/dsh-runtime/node`; do not silently substitute a distribution-global Node. `pnpm` is shipped inside `/opt/dsh-runtime/node_modules/.bin` because DSH's native `dsh plugin` command requires it to manage community extensions. Upgrade these pins deliberately and rerun the integration tests.

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

### Promote an already-validated personal host

The generic bootstrap intentionally defaults to an isolated `dsh-agent` and
`/srv/dsh-workspace`. A personal host that has already been validated against a
real workspace should not silently switch to an empty isolated workspace merely
to gain systemd supervision. `scripts/promote-live-host.sh` is the one-time
cutover path for that case.

The promotion helper keeps the user's existing workspace/identity, moves
`DSH_HOME` to `/var/lib/dsh-harness`, moves OAuth state to
`/var/lib/dsh-mcp-gateway`, copies local plugin tarballs under
`DSH_HOME/plugin-artifacts`, localizes pinned Git/GitHub dependencies from their
already-validated installed package without running package scripts, writes a
`plugin-artifacts/source-manifest.json` provenance record, regenerates the
profile lock so no acceptance-only or network Git dependency remains, and
installs the existing named Cloudflare tunnel under a
dedicated `dsh-tunnel` account. The DSH systemd drop-in uses
`ProtectHome=read-only` plus a writable exception for the selected workspace;
this retains read-only access to the user's Git/SSH configuration while limiting
normal writes to the workspace and Harness state. Common XDG/npm/pnpm cache and
state locations are redirected into `/var/lib/dsh-harness`, so the read-only home
policy does not make normal package/tool execution depend on writable dotfiles.

For a strict before/after comparison, capture the live bridge responses before
stopping the temporary jobs:

```sh
curl -fsS http://127.0.0.1:18401/api/chatgpt-bridge/tools > /tmp/dsh-tools-before.json
curl -fsS http://127.0.0.1:18401/api/chatgpt-bridge/skills > /tmp/dsh-skills-before.json
```

Then stop the temporary Harness, gateway, and named-tunnel jobs and run:

```sh
sudo ./scripts/promote-live-host.sh \
  --tools-snapshot /tmp/dsh-tools-before.json \
  --skills-snapshot /tmp/dsh-skills-before.json
```

The script fails closed if the old listeners/tunnel are still running. It starts
the formal services in dependency order and compares the new ToolRuntime and
SkillRegistry names with the captured snapshots before declaring the cutover
complete. It does not create or request any model-provider credential.

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

The resulting runtime also contains `/opt/dsh-runtime/node_modules/.bin/pnpm`. The DSH systemd unit keeps that directory on `PATH`, so operators can install reviewed community extensions with DSH's own plugin manager rather than adding gateway-specific wrappers.

For example, after reviewing and pinning a community plugin, install it as the DSH service account into the same `DSH_HOME` used by the host:

```sh
sudo -u dsh-agent env \
  DSH_HOME=/var/lib/dsh-harness \
  PATH=/opt/dsh-runtime/node_modules/.bin:/opt/dsh-runtime/node/bin:/usr/local/bin:/usr/bin:/bin \
  /opt/dsh-runtime/node_modules/.bin/dsh plugin --profile web add \
  'github:OWNER/REPOSITORY#PINNED_COMMIT'
sudo systemctl restart dsh-web-host.service
```

Restarting DSH is allowed to load a newly installed bundle; the OAuth gateway and the already-approved ChatGPT connector do not need to restart or refresh. Stable meta-tools discover the new capability from the restarted DSH ToolRuntime.

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
--tool-surface meta-only
```

These distinctions are release invariants: production uses the DSH Harness bridge and must not silently enable first-class DSH projection. Operators can test projection separately with `--tool-surface projected`.

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

For a named Cloudflare Tunnel managed on the same host, the optional checked-in
`deploy/systemd/dsh-cloudflared.service` expects a rewritten config at
`/etc/dsh-cloudflared/config.yml` and credentials at
`/etc/dsh-cloudflared/credentials.json`. `promote-live-host.sh` performs that
migration for an existing tunnel and runs it as the dedicated `dsh-tunnel`
account; the tunnel only `Wants=` the gateway so independent gateway restarts do
not tear down the Cloudflare connector.

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
/var/lib/dsh-harness/       DSH Harness state, Skills, profile, local plugin artifacts
/var/lib/dsh-mcp-gateway/   OAuth clients/tokens
/etc/dsh-mcp-gateway/       private gateway configuration
/etc/dsh-cloudflared/       named-tunnel config/credentials when used
<workspace>/                user/project data exposed to the Harness
```

The workspace is deployment-specific: the isolated template uses `/srv/dsh-workspace`, while the validated personal-host override uses `/home/ubuntu/workspace`. The gateway does not claim ownership of every project under that workspace, so a DSH release backup must not silently duplicate tens of gigabytes of unrelated repositories. Use each project's Git/storage policy for full project backup and pass explicit representative workspace paths to the DSH state drill.

A consistent offline DSH backup can be created with:

```sh
sudo ./scripts/backup-host-state.sh \
  --output /path/to/private/dsh-backup \
  --output-owner "$USER" \
  --workspace /home/ubuntu/workspace \
  --workspace-path dsh-skill-debug-test/CONTEXT.md \
  --workspace-path dsh-meta-only-hard-test/result.json
```

The script briefly quiesces only `dsh-cloudflared`, `dsh-mcp-gateway`, and `dsh-web-host`, then restores whichever of those services were active. It archives DSH state, OAuth state, private gateway/tunnel configuration, and only the explicit workspace paths. The output contains OAuth tokens and tunnel credentials; keep it private and encrypt it before off-host storage.

Verify the backup without touching production by restoring it to temporary loopback ports:

```sh
./scripts/verify-backup-restore.sh \
  --backup /path/to/private/dsh-backup \
  --restore-root /path/to/isolated-restore
```

The verifier checks archive hashes, restores the selected workspace files, rebases all community plugins to the backed-up local artifacts, rebuilds the DSH profile with `pnpm --offline`, starts an isolated Harness/gateway, rotates a cloned real ChatGPT refresh grant, and verifies the fixed four-tool MCP surface plus the restored DSH tool/Skill catalogs. It never mutates the production state database.

For a real disaster restore, extract the archives at their documented absolute paths, restore ownership/modes, install the exact recorded software commit/runtime pins, then start DSH before the gateway and tunnel. The software trees under `/opt/dsh-runtime` and `/srv/dsh-mcp-gateway` remain reproducible from the pinned inputs and Git commit.

Before upgrading MCP or DSH: update pins in a branch, run the full tests, rerun the DSH community-tool projection smoke and public OAuth smoke, review projected tool schemas, and deploy only after the primary Harness path remains model-provider-free.
