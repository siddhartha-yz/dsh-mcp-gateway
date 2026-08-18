#!/usr/bin/env bash
set -euo pipefail

# One-time promotion helper for an already-validated ChatGPT -> DSH live stack.
# It deliberately keeps the user's real workspace while moving Harness/OAuth/
# tunnel state into reboot-safe system locations managed by systemd.

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_DSH_HOME="/home/ubuntu/workspace/.dsh-chatgpt-live-home"
LIVE_GATEWAY_STATE="/home/ubuntu/workspace/.dsh-chatgpt-live-gateway/state"
LIVE_ADMIN_PIN_FILE="/home/ubuntu/workspace/.dsh-chatgpt-live-gateway/admin-pin"
LIVE_CLOUDFLARED_CONFIG="/home/ubuntu/workspace/.dsh-cloudflared/config.yml"
LIVE_CLOUDFLARED_CREDENTIALS="/home/ubuntu/workspace/.dsh-cloudflared/credentials.json"
WORKSPACE="/home/ubuntu/workspace"
WORKSPACE_USER="ubuntu"
WORKSPACE_GROUP="ubuntu"
TOOLS_SNAPSHOT=""
SKILLS_SNAPSHOT=""
PUBLIC_BASE_URL="${DSH_MCP_PUBLIC_BASE_URL:-}"
START_SERVICES=1

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/promote-live-host.sh [options]

Promote the already-validated temporary ChatGPT/DSH stack on this host into
the repository's reboot-safe systemd layout while preserving its DSH plugins,
filesystem Skills, OAuth refresh grants, public hostname, and real workspace.

The temporary gateway/Harness/tunnel processes MUST be stopped first. The
script fails closed if ports 18401/18766 are still listening or if the old
Cloudflare config is still owned by a running cloudflared process.

Options:
  --source PATH                  Source checkout to deploy.
  --live-dsh-home PATH           Existing DSH_HOME to migrate.
  --live-gateway-state PATH      Existing gateway OAuth state directory.
  --live-admin-pin-file PATH     Existing owner PIN file (mode 0600).
  --cloudflared-config PATH      Existing named-tunnel config.
  --cloudflared-credentials PATH Existing named-tunnel credentials JSON.
  --workspace PATH               Real workspace to retain (default /home/ubuntu/workspace).
  --workspace-user USER          DSH service identity for that workspace (default ubuntu).
  --workspace-group GROUP        DSH service group (default ubuntu).
  --public-base-url URL          Exact HTTPS origin; otherwise derive from tunnel hostname.
  --tools-snapshot PATH          Optional pre-stop bridge tools JSON snapshot.
  --skills-snapshot PATH         Optional pre-stop bridge skills JSON snapshot.
  --no-start                     Install/migrate/verify but do not enable/start services.
  -h, --help                     Show this help.

The script never requests or installs a model-provider API key.
EOF
}

while (($#)); do
  case "$1" in
    --source) SOURCE_ROOT="$(cd "$2" && pwd)"; shift 2 ;;
    --live-dsh-home) LIVE_DSH_HOME="$(realpath "$2")"; shift 2 ;;
    --live-gateway-state) LIVE_GATEWAY_STATE="$(realpath "$2")"; shift 2 ;;
    --live-admin-pin-file) LIVE_ADMIN_PIN_FILE="$(realpath "$2")"; shift 2 ;;
    --cloudflared-config) LIVE_CLOUDFLARED_CONFIG="$(realpath "$2")"; shift 2 ;;
    --cloudflared-credentials) LIVE_CLOUDFLARED_CREDENTIALS="$(realpath "$2")"; shift 2 ;;
    --workspace) WORKSPACE="$(realpath "$2")"; shift 2 ;;
    --workspace-user) WORKSPACE_USER="$2"; shift 2 ;;
    --workspace-group) WORKSPACE_GROUP="$2"; shift 2 ;;
    --public-base-url) PUBLIC_BASE_URL="$2"; shift 2 ;;
    --tools-snapshot) TOOLS_SNAPSHOT="$(realpath "$2")"; shift 2 ;;
    --skills-snapshot) SKILLS_SNAPSHOT="$(realpath "$2")"; shift 2 ;;
    --no-start) START_SERVICES=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "promote-live-host.sh must run as root (for example with sudo)." >&2
  exit 1
fi

for command in python3 git curl runuser systemctl systemd-analyze sha256sum; do
  command -v "$command" >/dev/null 2>&1 || { echo "missing required command: $command" >&2; exit 1; }
done
[[ -x /usr/local/bin/cloudflared ]] || { echo "/usr/local/bin/cloudflared is missing" >&2; exit 1; }
getent passwd "$WORKSPACE_USER" >/dev/null || { echo "workspace user does not exist: $WORKSPACE_USER" >&2; exit 1; }
getent group "$WORKSPACE_GROUP" >/dev/null || { echo "workspace group does not exist: $WORKSPACE_GROUP" >&2; exit 1; }

for path in \
  "$LIVE_DSH_HOME/profiles/web/package.json" \
  "$LIVE_GATEWAY_STATE/oauth.sqlite3" \
  "$LIVE_ADMIN_PIN_FILE" \
  "$LIVE_CLOUDFLARED_CONFIG" \
  "$LIVE_CLOUDFLARED_CREDENTIALS" \
  "$SOURCE_ROOT/scripts/bootstrap-target-host.sh" \
  "$SOURCE_ROOT/deploy/systemd/dsh-cloudflared.service"; do
  [[ -e "$path" ]] || { echo "required live/deployment input is missing: $path" >&2; exit 1; }
done
[[ -d "$WORKSPACE" ]] || { echo "workspace is missing: $WORKSPACE" >&2; exit 1; }

if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "source checkout has tracked changes; commit them before promotion" >&2
  exit 1
fi

if command -v ss >/dev/null 2>&1; then
  if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)(18401|18766)$'; then
    echo "temporary DSH/gateway listener is still active on port 18401 or 18766; stop tracked jobs first" >&2
    exit 1
  fi
fi
OLD_TUNNEL_PID=""
while read -r pid; do
  [[ "$pid" =~ ^[0-9]+$ ]] || continue
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ "$command_line" == *"--config $LIVE_CLOUDFLARED_CONFIG"* ]]; then
    OLD_TUNNEL_PID="$pid"
    break
  fi
done < <(pgrep -x cloudflared || true)
if [[ -n "$OLD_TUNNEL_PID" ]]; then
  echo "temporary Cloudflare tunnel is still running with $LIVE_CLOUDFLARED_CONFIG (pid $OLD_TUNNEL_PID); stop it first" >&2
  exit 1
fi

PIN_MODE="$(stat -c '%a' "$LIVE_ADMIN_PIN_FILE")"
[[ "$PIN_MODE" == "600" ]] || { echo "admin PIN file must be mode 0600" >&2; exit 1; }
ADMIN_PIN="$(cat "$LIVE_ADMIN_PIN_FILE")"
[[ ${#ADMIN_PIN} -ge 12 ]] || { echo "admin PIN is shorter than 12 characters" >&2; exit 1; }

if [[ -z "$PUBLIC_BASE_URL" ]]; then
  HOSTNAME="$(awk '/^[[:space:]]*-[[:space:]]+hostname:[[:space:]]*/ {print $3; exit}' "$LIVE_CLOUDFLARED_CONFIG")"
  [[ -n "$HOSTNAME" ]] || { echo "cannot derive public hostname from Cloudflare config" >&2; exit 1; }
  PUBLIC_BASE_URL="https://$HOSTNAME"
fi
[[ "$PUBLIC_BASE_URL" =~ ^https://[^/]+/?$ ]] || { echo "public base URL must be an HTTPS origin" >&2; exit 1; }

for snapshot in "$TOOLS_SNAPSHOT" "$SKILLS_SNAPSHOT"; do
  [[ -z "$snapshot" || -f "$snapshot" ]] || { echo "snapshot does not exist: $snapshot" >&2; exit 1; }
done

SOURCE_COMMIT="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
LIVE_OAUTH_SHA256="$(sha256sum "$LIVE_GATEWAY_STATE/oauth.sqlite3" | awk '{print $1}')"

echo "Installing pinned runtime/gateway commit $SOURCE_COMMIT without starting services..."
env \
  DSH_MCP_PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
  DSH_MCP_GATEWAY_ADMIN_PIN="$ADMIN_PIN" \
  "$SOURCE_ROOT/scripts/bootstrap-target-host.sh" \
    --source "$SOURCE_ROOT" --no-start --replace-source

echo "Migrating DSH durable state and making local plugin artifacts self-contained..."
find /var/lib/dsh-harness -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -a "$LIVE_DSH_HOME"/. /var/lib/dsh-harness/

python3 - <<'PY'
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

root = Path('/var/lib/dsh-harness')
package_path = root / 'profiles/web/package.json'
package = json.loads(package_path.read_text(encoding='utf-8'))
dependencies = package.get('dependencies')
if not isinstance(dependencies, dict):
    raise SystemExit('DSH web profile has no dependencies object')

artifacts = root / 'plugin-artifacts'
artifacts.mkdir(parents=True, exist_ok=True)
rewritten = 0
manifest = []
for name, spec in list(dependencies.items()):
    if not isinstance(spec, str):
        continue
    original_spec = spec
    destination = None
    if spec.startswith('file:'):
        source = Path(spec[5:])
        if not source.is_file():
            raise SystemExit(f'local plugin artifact is missing for {name}: {source}')
        destination = artifacts / source.name
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                raise SystemExit(f'plugin artifact basename collision: {destination.name}')
        else:
            shutil.copy2(source, destination)
    elif spec.startswith(('github:', 'git+https://github.com/', 'https://github.com/')):
        installed = root / 'profiles/web/node_modules' / Path(name)
        if not installed.is_dir():
            raise SystemExit(f'cannot localize git dependency {name}; installed package is missing at {installed}')
        env = {
            **os.environ,
            'PATH': '/opt/dsh-runtime/node/bin:/usr/local/bin:/usr/bin:/bin',
            'npm_config_cache': '/var/lib/dsh-harness/npm-pack-cache',
        }
        packed = subprocess.run(
            [
                '/opt/dsh-runtime/node/bin/npm',
                'pack',
                '--ignore-scripts',
                '--json',
                '--pack-destination',
                str(artifacts),
                str(installed),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        payload = json.loads(packed.stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise SystemExit(f'unexpected npm pack response while localizing {name}')
        filename = payload[0].get('filename')
        if not isinstance(filename, str) or not filename:
            raise SystemExit(f'npm pack did not return a filename while localizing {name}')
        destination = artifacts / filename
        if not destination.is_file():
            raise SystemExit(f'npm pack output is missing for {name}: {destination}')
    else:
        continue

    dependencies[name] = f'file:{destination}'
    rewritten += 1
    manifest.append(
        {
            'name': name,
            'source_spec': original_spec,
            'artifact': destination.name,
            'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
        }
    )

package_path.write_text(json.dumps(package, indent=2) + '\n', encoding='utf-8')
manifest.sort(key=lambda item: item['name'])
(artifacts / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
print(f'rebound {rewritten} local plugin artifact(s) into {artifacts}')
PY

rm -rf /var/lib/dsh-harness/profiles/web/node_modules /var/lib/dsh-harness/profiles/web/pnpm-lock.yaml
install -d -o "$WORKSPACE_USER" -g "$WORKSPACE_GROUP" -m 0700 \
  /var/lib/dsh-harness/pnpm-home \
  /var/lib/dsh-harness/pnpm-store \
  /var/lib/dsh-harness/npm-cache \
  /var/lib/dsh-harness/xdg-data \
  /var/lib/dsh-harness/xdg-cache \
  /var/lib/dsh-harness/xdg-state
chown -R "$WORKSPACE_USER:$WORKSPACE_GROUP" /var/lib/dsh-harness
chmod 0700 /var/lib/dsh-harness

runuser -u "$WORKSPACE_USER" -- env \
  DSH_HOME=/var/lib/dsh-harness \
  PNPM_HOME=/var/lib/dsh-harness/pnpm-home \
  XDG_DATA_HOME=/var/lib/dsh-harness/xdg-data \
  XDG_CACHE_HOME=/var/lib/dsh-harness/xdg-cache \
  XDG_STATE_HOME=/var/lib/dsh-harness/xdg-state \
  npm_config_store_dir=/var/lib/dsh-harness/pnpm-store \
  npm_config_registry=https://registry.npmjs.org/ \
  PATH=/opt/dsh-runtime/node_modules/.bin:/opt/dsh-runtime/node/bin:/usr/local/bin:/usr/bin:/bin \
  /opt/dsh-runtime/node_modules/.bin/pnpm install \
    --dir /var/lib/dsh-harness/profiles/web --no-frozen-lockfile

if grep -R -F -- "$LIVE_DSH_HOME" \
  /var/lib/dsh-harness/profiles/web/package.json \
  /var/lib/dsh-harness/profiles/web/pnpm-lock.yaml >/dev/null; then
  echo "migrated DSH profile still references the temporary DSH_HOME" >&2
  exit 1
fi
if grep -R -F '/home/ubuntu/workspace/.dsh-community-acceptance' \
  /var/lib/dsh-harness/profiles/web/package.json \
  /var/lib/dsh-harness/profiles/web/pnpm-lock.yaml >/dev/null; then
  echo "migrated DSH profile still references acceptance-only plugin artifacts" >&2
  exit 1
fi
if grep -E 'github:|git\+https://github\.com/|https://github\.com/' \
  /var/lib/dsh-harness/profiles/web/package.json \
  /var/lib/dsh-harness/profiles/web/pnpm-lock.yaml >/dev/null; then
  echo "migrated DSH profile still has a network git dependency instead of a local artifact" >&2
  exit 1
fi

echo "Migrating OAuth state byte-for-byte..."
find /var/lib/dsh-mcp-gateway -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -a "$LIVE_GATEWAY_STATE"/. /var/lib/dsh-mcp-gateway/
chown -R dsh-gateway:dsh-gateway /var/lib/dsh-mcp-gateway
chmod 0700 /var/lib/dsh-mcp-gateway
MIGRATED_OAUTH_SHA256="$(sha256sum /var/lib/dsh-mcp-gateway/oauth.sqlite3 | awk '{print $1}')"
[[ "$MIGRATED_OAUTH_SHA256" == "$LIVE_OAUTH_SHA256" ]] || { echo "OAuth SQLite copy checksum mismatch" >&2; exit 1; }

cat > /etc/dsh-mcp-gateway/dsh.env <<'EOF'
DSH_HOME=/var/lib/dsh-harness
DSH_TELEMETRY_DISABLED=1
PNPM_HOME=/var/lib/dsh-harness/pnpm-home
XDG_DATA_HOME=/var/lib/dsh-harness/xdg-data
XDG_CACHE_HOME=/var/lib/dsh-harness/xdg-cache
XDG_STATE_HOME=/var/lib/dsh-harness/xdg-state
npm_config_store_dir=/var/lib/dsh-harness/pnpm-store
npm_config_cache=/var/lib/dsh-harness/npm-cache
EOF
chmod 0600 /etc/dsh-mcp-gateway/dsh.env
chown root:root /etc/dsh-mcp-gateway/dsh.env

echo "Installing personal-workspace systemd override..."
install -d -m 0755 /etc/systemd/system/dsh-web-host.service.d
cat > /etc/systemd/system/dsh-web-host.service.d/personal-workspace.conf <<EOF
[Service]
User=$WORKSPACE_USER
Group=$WORKSPACE_GROUP
WorkingDirectory=$WORKSPACE
ProtectHome=read-only
ReadWritePaths=
ReadWritePaths=$WORKSPACE /var/lib/dsh-harness
EOF
chmod 0644 /etc/systemd/system/dsh-web-host.service.d/personal-workspace.conf

echo "Installing named Cloudflare tunnel under a dedicated service account..."
if ! getent passwd dsh-tunnel >/dev/null; then
  useradd --system --user-group --home /nonexistent --no-create-home --shell /usr/sbin/nologin dsh-tunnel
fi
install -d -o root -g dsh-tunnel -m 0750 /etc/dsh-cloudflared
install -o root -g dsh-tunnel -m 0640 "$LIVE_CLOUDFLARED_CREDENTIALS" /etc/dsh-cloudflared/credentials.json
python3 - "$LIVE_CLOUDFLARED_CONFIG" /etc/dsh-cloudflared/config.yml <<'PY'
import re
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding='utf-8')
updated, count = re.subn(
    r'(?m)^(\s*credentials-file:\s*).+$',
    r'\1/etc/dsh-cloudflared/credentials.json',
    source,
)
if count != 1:
    raise SystemExit(f'expected exactly one credentials-file entry, found {count}')
Path(sys.argv[2]).write_text(updated, encoding='utf-8')
PY
chown root:dsh-tunnel /etc/dsh-cloudflared/config.yml
chmod 0640 /etc/dsh-cloudflared/config.yml
install -m 0644 /srv/dsh-mcp-gateway/deploy/systemd/dsh-cloudflared.service /etc/systemd/system/dsh-cloudflared.service

echo "Running deployment verification before any service is started..."
/srv/dsh-mcp-gateway/scripts/verify-systemd.sh
SYSTEMD_UNIT_PATH=/etc/systemd/system:/usr/lib/systemd/system:/lib/systemd/system \
  systemd-analyze verify \
  dsh-web-host.service \
  dsh-mcp-gateway.service \
  dsh-cloudflared.service
python3 /srv/dsh-mcp-gateway/scripts/preflight-deployment.py \
  --workspace "$WORKSPACE" \
  --workspace-mode "$(stat -c '%a' "$WORKSPACE")" \
  --dsh-user "$WORKSPACE_USER" \
  --dsh-group "$WORKSPACE_GROUP"

systemctl daemon-reload

if ((START_SERVICES == 0)); then
  echo "Promotion install/migration/preflight complete; services were not started (--no-start)."
  exit 0
fi

echo "Enabling and starting DSH -> gateway -> tunnel..."
systemctl enable dsh-web-host.service dsh-mcp-gateway.service dsh-cloudflared.service
systemctl start dsh-web-host.service
for _ in $(seq 1 120); do
  curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/tools >/tmp/dsh-promote-tools.json 2>/dev/null && break
  sleep .25
done
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/skills >/tmp/dsh-promote-skills.json

if [[ -n "$TOOLS_SNAPSHOT" ]]; then
  python3 - "$TOOLS_SNAPSHOT" /tmp/dsh-promote-tools.json <<'PY'
import json, sys
before = {x['name'] for x in json.load(open(sys.argv[1]))['tools']}
after = {x['name'] for x in json.load(open(sys.argv[2]))['tools']}
if before != after:
    raise SystemExit(f'tool catalog changed across promotion: missing={sorted(before-after)} added={sorted(after-before)}')
print(f'tool catalog preserved: {len(after)} tools')
PY
fi
if [[ -n "$SKILLS_SNAPSHOT" ]]; then
  python3 - "$SKILLS_SNAPSHOT" /tmp/dsh-promote-skills.json <<'PY'
import json, sys
before = {x['name'] for x in json.load(open(sys.argv[1]))['skills']}
after = {x['name'] for x in json.load(open(sys.argv[2]))['skills']}
if before != after:
    raise SystemExit(f'SkillRegistry changed across promotion: missing={sorted(before-after)} added={sorted(after-before)}')
print(f'SkillRegistry preserved: {len(after)} skills')
PY
fi

systemctl start dsh-mcp-gateway.service
for _ in $(seq 1 120); do
  curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz >/tmp/dsh-promote-ready.json 2>/dev/null && break
  sleep .25
done
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz >/tmp/dsh-promote-ready.json
cat /tmp/dsh-promote-ready.json
echo

systemctl start dsh-cloudflared.service
for _ in $(seq 1 120); do
  curl -fsS --connect-timeout 5 --max-time 10 "$PUBLIC_BASE_URL/readyz" >/tmp/dsh-promote-public.json 2>/dev/null && break
  sleep .5
done
curl -fsS --connect-timeout 5 --max-time 10 "$PUBLIC_BASE_URL/readyz" >/tmp/dsh-promote-public.json
cat /tmp/dsh-promote-public.json
echo

echo "Promotion complete at commit $SOURCE_COMMIT."
echo "Next: verify the existing ChatGPT App works without reconnect/rescan, then perform the OS reboot drill."
