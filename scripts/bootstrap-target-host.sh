#!/usr/bin/env bash
set -euo pipefail

NODE_VERSION="24.19.0"
DSH_VERSION="0.1.0-rc.6"
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START_SERVICES=1
REPLACE_SOURCE=0

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/bootstrap-target-host.sh [options]

Install the pinned DSH runtime and dsh-mcp-gateway into the documented
single-host production layout, run the repository preflight, then start the
systemd services.

Options:
  --source PATH       Source git checkout (default: repository containing script)
  --no-start          Install and preflight only; do not enable/start services
  --replace-source    Replace an existing /srv/dsh-mcp-gateway source tree
  -h, --help          Show this help

Configuration is read from existing environment variables when present:
  DSH_MCP_PUBLIC_BASE_URL
  DSH_MCP_GATEWAY_ADMIN_PIN

Missing values are prompted for without echoing secret values. The script does
not configure the public HTTPS reverse proxy itself; DSH_MCP_PUBLIC_BASE_URL
must be the exact HTTPS origin that will front 127.0.0.1:18766.
EOF
}

while (($#)); do
  case "$1" in
    --source)
      [[ $# -ge 2 ]] || { echo "--source requires a path" >&2; exit 2; }
      SOURCE_ROOT="$(cd "$2" && pwd)"
      shift 2
      ;;
    --no-start)
      START_SERVICES=0
      shift
      ;;
    --replace-source)
      REPLACE_SOURCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "bootstrap-target-host.sh must run as root (for example with sudo)." >&2
  exit 1
fi

for path in \
  "$SOURCE_ROOT/pyproject.toml" \
  "$SOURCE_ROOT/deploy/dsh-runtime/package.json" \
  "$SOURCE_ROOT/deploy/dsh-runtime/package-lock.json" \
  "$SOURCE_ROOT/deploy/server-constraints.txt" \
  "$SOURCE_ROOT/deploy/dsh/chatgpt-bridge.cordis.yml" \
  "$SOURCE_ROOT/dsh-bridge-plugin/index.js" \
  "$SOURCE_ROOT/deploy/systemd/dsh-web-host.service" \
  "$SOURCE_ROOT/deploy/systemd/dsh-mcp-gateway.service" \
  "$SOURCE_ROOT/scripts/preflight-deployment.py" \
  "$SOURCE_ROOT/scripts/validate-public-origin.py"; do
  [[ -f "$path" ]] || { echo "required repository file is missing: $path" >&2; exit 1; }
done

if ! git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "source must be a git checkout so deployment can install one exact commit" >&2
  exit 1
fi
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "source checkout has tracked changes; commit or stash them before deployment" >&2
  exit 1
fi
SOURCE_COMMIT="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"

detect_arch() {
  case "$(uname -m)" in
    x86_64) echo x64 ;;
    aarch64|arm64) echo arm64 ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; return 1 ;;
  esac
}
NODE_ARCH="$(detect_arch)"

need_command() {
  command -v "$1" >/dev/null 2>&1 || return 1
}

install_base_prereqs() {
  local missing=0
  for cmd in curl git tar sha256sum python3 timeout; do
    if ! need_command "$cmd"; then
      echo "missing prerequisite command: $cmd" >&2
      missing=1
    fi
  done
  if ((missing)); then
    if ! need_command apt-get; then
      echo "install the missing commands above and rerun" >&2
      exit 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates curl git tar xz-utils python3 python3-venv
  fi

  local probe
  probe="$(mktemp -d)"
  if ! python3 -m venv "$probe/venv" >/dev/null 2>&1; then
    rm -rf "$probe"
    if ! need_command apt-get; then
      echo "python3 cannot create venvs; install the platform python3-venv package" >&2
      exit 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3-venv
    probe="$(mktemp -d)"
    python3 -m venv "$probe/venv" >/dev/null
  fi
  rm -rf "$probe"
}

install_base_prereqs

if ! getent passwd dsh-agent >/dev/null; then
  useradd --system --user-group --home /var/lib/dsh-harness --create-home dsh-agent
fi
if ! getent passwd dsh-gateway >/dev/null; then
  useradd --system --user-group --home /var/lib/dsh-mcp-gateway --create-home dsh-gateway
fi

install -d -o dsh-agent -g dsh-agent -m 0700 /var/lib/dsh-harness
install -d -o dsh-agent -g dsh-agent -m 0750 /srv/dsh-workspace
install -d -o dsh-gateway -g dsh-gateway -m 0700 /var/lib/dsh-mcp-gateway
install -d -o root -g root -m 0700 /etc/dsh-mcp-gateway
install -d -o root -g root -m 0755 /opt/dsh-runtime

install_node() {
  local node=/opt/dsh-runtime/node/bin/node
  if [[ -x "$node" ]]; then
    local current
    current="$(timeout --signal=TERM --kill-after=2s 5s "$node" --version)"
    if [[ "$current" == "v$NODE_VERSION" ]]; then
      echo "Node $NODE_VERSION already installed; reusing it."
      return
    fi
    echo "existing Node version $current does not match v$NODE_VERSION" >&2
    exit 1
  fi

  local tmp filename base checksum
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  filename="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
  base="https://nodejs.org/dist/v${NODE_VERSION}"
  curl --fail --silent --show-error --location --connect-timeout 10 --max-time 600 "$base/$filename" -o "$tmp/$filename"
  curl --fail --silent --show-error --location --connect-timeout 10 --max-time 600 "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  checksum="$(awk -v f="$filename" '$2 == f {print $1}' "$tmp/SHASUMS256.txt")"
  [[ -n "$checksum" ]] || { echo "Node checksum entry not found for $filename" >&2; exit 1; }
  printf '%s  %s\n' "$checksum" "$tmp/$filename" | sha256sum --check --status
  rm -rf /opt/dsh-runtime/node
  install -d -m 0755 /opt/dsh-runtime/node
  tar -xJf "$tmp/$filename" --strip-components=1 -C /opt/dsh-runtime/node
  [[ "$(timeout --signal=TERM --kill-after=2s 5s /opt/dsh-runtime/node/bin/node --version)" == "v$NODE_VERSION" ]]
  rm -rf "$tmp"
  trap - RETURN
}

install_node

install -m 0644 "$SOURCE_ROOT/deploy/dsh-runtime/package.json" /opt/dsh-runtime/package.json
install -m 0644 "$SOURCE_ROOT/deploy/dsh-runtime/package-lock.json" /opt/dsh-runtime/package-lock.json
export PATH="/opt/dsh-runtime/node/bin:$PATH"
export npm_config_registry="https://registry.npmjs.org/"
timeout --signal=TERM --kill-after=10s 600s \
  /opt/dsh-runtime/node/bin/npm ci \
  --prefix /opt/dsh-runtime \
  --omit=dev \
  --no-audit \
  --no-fund
ACTUAL_DSH_VERSION="$(timeout --signal=TERM --kill-after=2s 5s /opt/dsh-runtime/node/bin/node -p "require('/opt/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json').version")"
[[ "$ACTUAL_DSH_VERSION" == "$DSH_VERSION" ]] || {
  echo "installed DSH version $ACTUAL_DSH_VERSION does not match $DSH_VERSION" >&2
  exit 1
}

if [[ -e /srv/dsh-mcp-gateway ]]; then
  if ((REPLACE_SOURCE == 0)); then
    echo "/srv/dsh-mcp-gateway already exists; rerun with --replace-source for a deliberate replacement" >&2
    exit 1
  fi
  rm -rf /srv/dsh-mcp-gateway
fi
install -d -o root -g root -m 0755 /srv/dsh-mcp-gateway
git -C "$SOURCE_ROOT" archive "$SOURCE_COMMIT" | tar -x -C /srv/dsh-mcp-gateway
printf '%s\n' "$SOURCE_COMMIT" > /srv/dsh-mcp-gateway/.deployed-git-commit
chmod 0644 /srv/dsh-mcp-gateway/.deployed-git-commit

python3 -m venv /srv/dsh-mcp-gateway/.venv
timeout --signal=TERM --kill-after=10s 600s \
  /srv/dsh-mcp-gateway/.venv/bin/python -m pip install \
  --constraint /srv/dsh-mcp-gateway/deploy/server-constraints.txt \
  -e '/srv/dsh-mcp-gateway[server]'

if [[ -z "${DSH_MCP_PUBLIC_BASE_URL:-}" ]]; then
  read -r -p "Exact public HTTPS origin (for example https://dsh.example.com): " DSH_MCP_PUBLIC_BASE_URL
fi
if [[ -z "${DSH_MCP_GATEWAY_ADMIN_PIN:-}" ]]; then
  read -r -s -p "Gateway owner PIN/passphrase (at least 12 characters): " DSH_MCP_GATEWAY_ADMIN_PIN
  echo
fi

python3 "$SOURCE_ROOT/scripts/validate-public-origin.py" "$DSH_MCP_PUBLIC_BASE_URL" || {
  echo "DSH_MCP_PUBLIC_BASE_URL must be an HTTPS origin without user info, path, params, query, or fragment" >&2
  exit 1
}
[[ ${#DSH_MCP_GATEWAY_ADMIN_PIN} -ge 12 ]] || {
  echo "DSH_MCP_GATEWAY_ADMIN_PIN must contain at least 12 characters" >&2
  exit 1
}

umask 077
cat > /etc/dsh-mcp-gateway/dsh.env <<EOF
DSH_HOME=/var/lib/dsh-harness
DSH_TELEMETRY_DISABLED=1
EOF
cat > /etc/dsh-mcp-gateway/gateway.env <<EOF
DSH_MCP_PUBLIC_BASE_URL=$DSH_MCP_PUBLIC_BASE_URL
DSH_MCP_GATEWAY_ADMIN_PIN=$DSH_MCP_GATEWAY_ADMIN_PIN
EOF
chmod 0600 /etc/dsh-mcp-gateway/dsh.env /etc/dsh-mcp-gateway/gateway.env
chown root:root /etc/dsh-mcp-gateway/dsh.env /etc/dsh-mcp-gateway/gateway.env

install -m 0644 /srv/dsh-mcp-gateway/deploy/systemd/dsh-web-host.service /etc/systemd/system/dsh-web-host.service
install -m 0644 /srv/dsh-mcp-gateway/deploy/systemd/dsh-mcp-gateway.service /etc/systemd/system/dsh-mcp-gateway.service

python3 /srv/dsh-mcp-gateway/scripts/preflight-deployment.py

if ((START_SERVICES)); then
  systemctl daemon-reload
  systemctl enable --now dsh-web-host.service dsh-mcp-gateway.service
  curl --fail --silent --show-error --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/healthz
  echo
  curl --fail --silent --show-error --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz
  echo
fi

echo "Installed dsh-mcp-gateway commit $SOURCE_COMMIT"
if ((START_SERVICES)); then
  echo "Local services are running. Configure the public HTTPS origin to proxy to http://127.0.0.1:18766, then run scripts/smoke-public-oauth.py."
else
  echo "Install/preflight complete; services were not started (--no-start)."
fi
