#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_LIVE=/opt/dsh-runtime
SOURCE_LIVE=/srv/dsh-mcp-gateway
DSH_HOME=/var/lib/dsh-harness
GATEWAY_STATE=/var/lib/dsh-mcp-gateway
CONFIG_DIR=/etc/dsh-mcp-gateway
SYSTEMD_DIR=/etc/systemd/system
DSH_SERVICE=dsh-web-host.service
GATEWAY_SERVICE=dsh-mcp-gateway.service

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/upgrade-live-host.sh [--source PATH]

Stage and atomically switch an already-productionized dsh-mcp-gateway host to
one exact clean repository commit. Existing DSH_HOME, OAuth/config state,
systemd units/drop-ins, workspace ownership, and public tunnel state are left
untouched. If the new DSH Host or gateway fails readiness, the old runtime and
source tree are restored automatically.
EOF
}

while (($#)); do
  case "$1" in
    --source)
      [[ $# -ge 2 ]] || { echo "--source requires a path" >&2; exit 2; }
      SOURCE_ROOT="$(cd "$2" && pwd)"
      shift 2
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
  echo "upgrade-live-host.sh must run as root (for example with sudo)." >&2
  exit 1
fi

for command in git tar python3 systemctl curl install stat timeout mktemp; do
  command -v "$command" >/dev/null 2>&1 || { echo "missing required command: $command" >&2; exit 1; }
done

[[ -d "$SOURCE_ROOT/.git" ]] || { echo "source must be a git checkout: $SOURCE_ROOT" >&2; exit 1; }
[[ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=no)" ]] || {
  echo "source checkout has tracked changes; commit them before live upgrade" >&2
  exit 1
}
TARGET_COMMIT="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"

for path in \
  "$RUNTIME_LIVE/node/bin/node" \
  "$RUNTIME_LIVE/package.json" \
  "$RUNTIME_LIVE/package-lock.json" \
  "$SOURCE_LIVE/.deployed-git-commit" \
  "$SOURCE_ROOT/deploy/dsh-runtime/package.json" \
  "$SOURCE_ROOT/deploy/dsh-runtime/package-lock.json" \
  "$SOURCE_ROOT/deploy/server-constraints.txt" \
  "$SOURCE_ROOT/scripts/preflight-deployment.py" \
  "$SOURCE_ROOT/scripts/verify-dsh-runtime-lock.py"; do
  [[ -e "$path" ]] || { echo "required live/source path missing: $path" >&2; exit 1; }
done

python3 "$SOURCE_ROOT/scripts/verify-dsh-runtime-lock.py"

DSH_USER="$(systemctl show "$DSH_SERVICE" -p User --value)"
DSH_GROUP="$(systemctl show "$DSH_SERVICE" -p Group --value)"
WORKSPACE="$(systemctl show "$DSH_SERVICE" -p WorkingDirectory --value)"
GATEWAY_USER="$(systemctl show "$GATEWAY_SERVICE" -p User --value)"
GATEWAY_GROUP="$(systemctl show "$GATEWAY_SERVICE" -p Group --value)"
[[ -n "$DSH_USER" && -n "$DSH_GROUP" && -n "$WORKSPACE" ]] || {
  echo "cannot resolve effective DSH service identity/workspace" >&2
  exit 1
}
[[ -n "$GATEWAY_USER" && -n "$GATEWAY_GROUP" ]] || {
  echo "cannot resolve effective gateway service identity" >&2
  exit 1
}
[[ -d "$WORKSPACE" ]] || { echo "effective DSH workspace is missing: $WORKSPACE" >&2; exit 1; }
WORKSPACE_MODE="$(stat -c '%a' "$WORKSPACE")"

TMP_ID="$$-$(date +%s)"
RUNTIME_STAGE="/opt/.dsh-runtime-stage-$TMP_ID"
SOURCE_STAGE="/srv/.dsh-mcp-gateway-stage-$TMP_ID"
RUNTIME_OLD="/opt/.dsh-runtime-old-$TMP_ID"
SOURCE_OLD="/srv/.dsh-mcp-gateway-old-$TMP_ID"
SERVICES_STOPPED=0
RUNTIME_SWAPPED=0
SOURCE_SWAPPED=0
SUCCESS=0

cleanup_paths() {
  rm -rf "$RUNTIME_STAGE" "$SOURCE_STAGE"
  if ((SUCCESS)); then
    rm -rf "$RUNTIME_OLD" "$SOURCE_OLD"
  fi
}

start_old_or_new_services() {
  systemctl start "$DSH_SERVICE"
  systemctl start "$GATEWAY_SERVICE"
}

rollback() {
  local original_rc=$1
  set +e
  echo "live upgrade failed; rolling back" >&2
  systemctl stop "$GATEWAY_SERVICE" "$DSH_SERVICE" >/dev/null 2>&1 || true

  if ((SOURCE_SWAPPED)); then
    rm -rf "$SOURCE_LIVE"
    mv "$SOURCE_OLD" "$SOURCE_LIVE"
    SOURCE_SWAPPED=0
  elif [[ -d "$SOURCE_OLD" && ! -e "$SOURCE_LIVE" ]]; then
    mv "$SOURCE_OLD" "$SOURCE_LIVE"
  fi

  if ((RUNTIME_SWAPPED)); then
    rm -rf "$RUNTIME_LIVE"
    mv "$RUNTIME_OLD" "$RUNTIME_LIVE"
    RUNTIME_SWAPPED=0
  elif [[ -d "$RUNTIME_OLD" && ! -e "$RUNTIME_LIVE" ]]; then
    mv "$RUNTIME_OLD" "$RUNTIME_LIVE"
  fi

  if ((SERVICES_STOPPED)); then
    systemctl daemon-reload || true
    start_old_or_new_services || true
  fi
  echo "rollback attempt finished" >&2
  set -e
  return "$original_rc"
}

on_exit() {
  local rc=$?
  trap - EXIT
  if ((rc != 0 && SUCCESS == 0)); then
    rollback "$rc" || true
  fi
  cleanup_paths
  exit "$rc"
}
trap on_exit EXIT

rm -rf "$RUNTIME_STAGE" "$SOURCE_STAGE" "$RUNTIME_OLD" "$SOURCE_OLD"
install -d -o root -g root -m 0755 "$RUNTIME_STAGE" "$SOURCE_STAGE"

# Stage a complete runtime before interrupting the live Host. Reuse the pinned
# Node binary tree by copying it; npm state is built independently of DSH_HOME.
echo "Staging DSH runtime for $TARGET_COMMIT..."
cp -a "$RUNTIME_LIVE/node" "$RUNTIME_STAGE/node"
install -m 0644 "$SOURCE_ROOT/deploy/dsh-runtime/package.json" "$RUNTIME_STAGE/package.json"
install -m 0644 "$SOURCE_ROOT/deploy/dsh-runtime/package-lock.json" "$RUNTIME_STAGE/package-lock.json"
install -d -o root -g root -m 0755 /var/cache/dsh-runtime-npm
(
  unset npm_config_store_dir npm_config_cache
  export PATH="$RUNTIME_STAGE/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"
  export npm_config_registry=https://registry.npmjs.org/
  export npm_config_cache=/var/cache/dsh-runtime-npm
  timeout --signal=TERM --kill-after=10s 600s \
    "$RUNTIME_STAGE/node/bin/npm" ci \
    --prefix "$RUNTIME_STAGE" \
    --omit=dev \
    --no-audit \
    --no-fund
)
EXPECTED_DSH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dependencies"]["@deepseek-ai/dsh"])' "$SOURCE_ROOT/deploy/dsh-runtime/package.json")"
ACTUAL_DSH="$(PATH="$RUNTIME_STAGE/node/bin:$RUNTIME_STAGE/node_modules/.bin:/usr/bin:/bin" "$RUNTIME_STAGE/node_modules/.bin/dsh" --version)"
[[ "$ACTUAL_DSH" == "$EXPECTED_DSH" ]] || {
  echo "staged DSH version $ACTUAL_DSH does not match $EXPECTED_DSH" >&2
  exit 1
}

# Stage the exact source commit and a fresh gateway virtualenv. No mutable
# production state lives in this tree.
echo "Staging gateway source $TARGET_COMMIT..."
git -C "$SOURCE_ROOT" archive "$TARGET_COMMIT" | tar -x -C "$SOURCE_STAGE"
printf '%s\n' "$TARGET_COMMIT" > "$SOURCE_STAGE/.deployed-git-commit"
chmod 0644 "$SOURCE_STAGE/.deployed-git-commit"
python3 -m venv "$SOURCE_STAGE/.venv"
timeout --signal=TERM --kill-after=10s 600s \
  "$SOURCE_STAGE/.venv/bin/python" -m pip install \
  --constraint "$SOURCE_STAGE/deploy/server-constraints.txt" \
  "$SOURCE_STAGE[server]"

# Preflight the staged immutable pieces against the live state and the effective
# service identity. This is what prevents a personal-workspace deployment from
# being judged using the default dsh-agent ownership contract.
python3 "$SOURCE_STAGE/scripts/preflight-deployment.py" \
  --dsh-runtime "$RUNTIME_STAGE" \
  --gateway-root "$SOURCE_STAGE" \
  --workspace "$WORKSPACE" \
  --workspace-mode "$WORKSPACE_MODE" \
  --dsh-home "$DSH_HOME" \
  --gateway-state "$GATEWAY_STATE" \
  --config-dir "$CONFIG_DIR" \
  --systemd-dir "$SYSTEMD_DIR" \
  --dsh-user "$DSH_USER" \
  --dsh-group "$DSH_GROUP" \
  --gateway-user "$GATEWAY_USER" \
  --gateway-group "$GATEWAY_GROUP"

# Only the switch below interrupts the ChatGPT -> DSH path.
echo "Stopping live DSH Host and gateway..."
systemctl stop "$GATEWAY_SERVICE" "$DSH_SERVICE"
SERVICES_STOPPED=1

mv "$RUNTIME_LIVE" "$RUNTIME_OLD"
if ! mv "$RUNTIME_STAGE" "$RUNTIME_LIVE"; then
  mv "$RUNTIME_OLD" "$RUNTIME_LIVE"
  exit 1
fi
RUNTIME_SWAPPED=1

mv "$SOURCE_LIVE" "$SOURCE_OLD"
if ! mv "$SOURCE_STAGE" "$SOURCE_LIVE"; then
  mv "$SOURCE_OLD" "$SOURCE_LIVE"
  exit 1
fi
SOURCE_SWAPPED=1

# Console-script shebangs created inside a venv contain the absolute staging
# path. Repoint the one systemd executes after the source tree moves live.
python3 - "$SOURCE_LIVE/.venv/bin/dsh-mcp-gateway" "$SOURCE_LIVE/.venv/bin/python" <<'PY'
import pathlib, sys
script = pathlib.Path(sys.argv[1])
interpreter = sys.argv[2]
lines = script.read_text(encoding="utf-8").splitlines(keepends=True)
if not lines or not lines[0].startswith("#!"):
    raise SystemExit("gateway console script has no shebang")
lines[0] = f"#!{interpreter}\n"
script.write_text("".join(lines), encoding="utf-8")
PY

systemctl daemon-reload
systemctl start "$DSH_SERVICE"
for _ in $(seq 1 30); do
  if curl -fsS --connect-timeout 1 --max-time 3 http://127.0.0.1:3080/api/chatgpt-bridge/tools >/dev/null 2>&1 \
    && curl -fsS --connect-timeout 1 --max-time 3 http://127.0.0.1:3080/api/chatgpt-bridge/skills >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/tools >/dev/null
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/skills >/dev/null

systemctl start "$GATEWAY_SERVICE"
for _ in $(seq 1 30); do
  if curl -fsS --connect-timeout 1 --max-time 3 http://127.0.0.1:18766/readyz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/healthz >/dev/null
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz >/dev/null

LIVE_DSH="$(PATH="$RUNTIME_LIVE/node/bin:$RUNTIME_LIVE/node_modules/.bin:/usr/bin:/bin" "$RUNTIME_LIVE/node_modules/.bin/dsh" --version)"
LIVE_COMMIT="$(cat "$SOURCE_LIVE/.deployed-git-commit")"
[[ "$LIVE_DSH" == "$EXPECTED_DSH" ]] || { echo "unexpected live DSH version: $LIVE_DSH" >&2; exit 1; }
[[ "$LIVE_COMMIT" == "$TARGET_COMMIT" ]] || { echo "unexpected live source commit: $LIVE_COMMIT" >&2; exit 1; }
[[ "$(systemctl is-active "$DSH_SERVICE")" == active ]]
[[ "$(systemctl is-active "$GATEWAY_SERVICE")" == active ]]

SUCCESS=1
echo "Live upgrade successful: DSH=$LIVE_DSH commit=$LIVE_COMMIT"
