#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_NODE=/opt/dsh-runtime/node/bin
CACHE_DIR=/var/cache/dsh-runtime-npm
APPARMOR_PROFILE_TARGET=/etc/apparmor.d/dsh-browser-worker
LEGACY_APPARMOR_PROFILE=/etc/apparmor.d/dsh-chromium
BROWSER_SERVICE=dsh-browser-worker.service
DSH_SERVICE=dsh-web-host.service
SYSTEMD_DIR=/etc/systemd/system
LEGACY_BROWSER_CREDENTIAL=/etc/dsh-mcp-gateway/browser-worker.key

usage() {
  cat <<'USAGE'
Usage: sudo ./scripts/provision-browser-deps.sh [--source PATH]

Install the host OS libraries plus the dedicated browser-worker AppArmor/systemd
boundary required by P5 browser_session. The worker receives userns permission,
then applies NoNewPrivileges before Node starts; the main DSH Host keeps its own
NoNewPrivileges boundary unchanged. Normal application upgrades only verify
these host prerequisites and never invoke apt or change AppArmor policy.
USAGE
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
  echo "provision-browser-deps.sh must run as root (for example with sudo)." >&2
  exit 1
fi

PACKAGE_JSON="$SOURCE_ROOT/deploy/dsh-runtime/package.json"
APPARMOR_PROFILE_SOURCE="$SOURCE_ROOT/deploy/apparmor/dsh-browser-worker"
BROWSER_UNIT_SOURCE="$SOURCE_ROOT/deploy/systemd/$BROWSER_SERVICE"
for path in "$PACKAGE_JSON" "$APPARMOR_PROFILE_SOURCE" "$BROWSER_UNIT_SOURCE"; do
  [[ -f "$path" && ! -L "$path" ]] || { echo "missing or unsafe provisioning source: $path" >&2; exit 1; }
done
[[ -x "$RUNTIME_NODE/node" && -x "$RUNTIME_NODE/npx" ]] || {
  echo "live DSH Node runtime is missing under $RUNTIME_NODE" >&2
  exit 1
}
command -v setpriv >/dev/null 2>&1 || { echo "setpriv is required for the browser worker" >&2; exit 1; }

PLAYWRIGHT_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dependencies"]["playwright-core"])' "$PACKAGE_JSON")"
[[ "$PLAYWRIGHT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "invalid pinned playwright-core version: $PLAYWRIGHT_VERSION" >&2
  exit 1
}

install -d -o root -g root -m 0755 "$CACHE_DIR"
unset npm_config_store_dir npm_config_cache
export PATH="$RUNTIME_NODE:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"
export npm_config_registry=https://registry.npmjs.org/
export npm_config_cache="$CACHE_DIR"

echo "Installing host dependencies for playwright-core=$PLAYWRIGHT_VERSION Chromium..."
timeout --foreground --signal=TERM --kill-after=30s 1200s \
  "$RUNTIME_NODE/npx" --yes "playwright-core@$PLAYWRIGHT_VERSION" install-deps chromium

if [[ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]]; then
  command -v apparmor_parser >/dev/null 2>&1 || {
    echo "AppArmor user-namespace restriction is present but apparmor_parser is unavailable" >&2
    exit 1
  }
  command -v aa-exec >/dev/null 2>&1 || { echo "aa-exec is required to verify the browser worker userns profile" >&2; exit 1; }
  command -v unshare >/dev/null 2>&1 || { echo "unshare is required to verify the browser worker userns profile" >&2; exit 1; }
  apparmor_parser -Q -K "$APPARMOR_PROFILE_SOURCE"
  if [[ -f "$LEGACY_APPARMOR_PROFILE" ]]; then
    apparmor_parser -R -K "$LEGACY_APPARMOR_PROFILE" >/dev/null 2>&1 || true
    rm -f "$LEGACY_APPARMOR_PROFILE"
  fi
  install -o root -g root -m 0644 "$APPARMOR_PROFILE_SOURCE" "$APPARMOR_PROFILE_TARGET"
  apparmor_parser -r -K "$APPARMOR_PROFILE_TARGET"
  echo "Installed browser-worker AppArmor profile: $APPARMOR_PROFILE_TARGET"
fi

DSH_USER="$(systemctl show "$DSH_SERVICE" -p User --value)"
DSH_GROUP="$(systemctl show "$DSH_SERVICE" -p Group --value)"
[[ -n "$DSH_USER" && -n "$DSH_GROUP" ]] || { echo "cannot resolve effective DSH service identity" >&2; exit 1; }
id "$DSH_USER" >/dev/null 2>&1 || { echo "DSH service user does not exist: $DSH_USER" >&2; exit 1; }
getent group "$DSH_GROUP" >/dev/null 2>&1 || { echo "DSH service group does not exist: $DSH_GROUP" >&2; exit 1; }
DSH_UID="$(id -u "$DSH_USER")"
DSH_GID="$(getent group "$DSH_GROUP" | cut -d: -f3)"
[[ "$DSH_UID" =~ ^[0-9]+$ && "$DSH_GID" =~ ^[0-9]+$ ]] || { echo "cannot resolve numeric DSH service identity" >&2; exit 1; }

if [[ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]]; then
  if ! aa-exec -p dsh-browser-worker -- \
    setpriv --reuid "$DSH_UID" --regid "$DSH_GID" --clear-groups --no-new-privs \
    unshare --user --map-root-user /usr/bin/true; then
    echo "browser-worker AppArmor profile does not permit unprivileged user namespaces under NoNewPrivileges" >&2
    exit 1
  fi
  echo "Browser worker AppArmor userns probe passed under NoNewPrivileges."
fi

if [[ -e "$LEGACY_BROWSER_CREDENTIAL" || -L "$LEGACY_BROWSER_CREDENTIAL" ]]; then
  [[ -f "$LEGACY_BROWSER_CREDENTIAL" && ! -L "$LEGACY_BROWSER_CREDENTIAL" ]] || {
    echo "refusing to remove unsafe legacy browser worker credential path" >&2
    exit 1
  }
  rm -f "$LEGACY_BROWSER_CREDENTIAL"
  echo "Removed obsolete browser worker credential; worker authorization now uses Unix SO_PEERCRED."
fi

install -o root -g root -m 0644 "$BROWSER_UNIT_SOURCE" "$SYSTEMD_DIR/$BROWSER_SERVICE"
install -d -o root -g root -m 0755 "$SYSTEMD_DIR/$BROWSER_SERVICE.d"
cat > "$SYSTEMD_DIR/$BROWSER_SERVICE.d/identity.conf" <<EOF
[Service]
User=$DSH_USER
Group=$DSH_GROUP
EOF
chmod 0644 "$SYSTEMD_DIR/$BROWSER_SERVICE.d/identity.conf"
systemctl daemon-reload

echo "Browser worker provisioned for effective DSH identity $DSH_USER:$DSH_GROUP"
echo "Browser host dependencies provisioned for playwright-core=$PLAYWRIGHT_VERSION"
