#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_NODE=/opt/dsh-runtime/node/bin
CACHE_DIR=/var/cache/dsh-runtime-npm
APPARMOR_PROFILE_TARGET=/etc/apparmor.d/dsh-chromium

usage() {
  cat <<'USAGE'
Usage: sudo ./scripts/provision-browser-deps.sh [--source PATH]

Install the host OS libraries and scoped AppArmor user-namespace allowance
required by the pinned Playwright Chromium used by P5 browser_session. This is
an explicit host provisioning step; normal application upgrades only verify
these prerequisites and never invoke apt or change AppArmor policy implicitly.
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
APPARMOR_PROFILE_SOURCE="$SOURCE_ROOT/deploy/apparmor/dsh-chromium"
[[ -f "$PACKAGE_JSON" ]] || { echo "missing runtime package manifest: $PACKAGE_JSON" >&2; exit 1; }
[[ -f "$APPARMOR_PROFILE_SOURCE" && ! -L "$APPARMOR_PROFILE_SOURCE" ]] || {
  echo "missing or unsafe AppArmor profile source: $APPARMOR_PROFILE_SOURCE" >&2
  exit 1
}
[[ -x "$RUNTIME_NODE/node" && -x "$RUNTIME_NODE/npx" ]] || {
  echo "live DSH Node runtime is missing under $RUNTIME_NODE" >&2
  exit 1
}

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
  apparmor_parser -Q -K "$APPARMOR_PROFILE_SOURCE"
  install -o root -g root -m 0644 "$APPARMOR_PROFILE_SOURCE" "$APPARMOR_PROFILE_TARGET"
  apparmor_parser -r -K "$APPARMOR_PROFILE_TARGET"
  echo "Installed scoped Chromium AppArmor userns profile: $APPARMOR_PROFILE_TARGET"
fi

echo "Browser host dependencies provisioned for playwright-core=$PLAYWRIGHT_VERSION"
