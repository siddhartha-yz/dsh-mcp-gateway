#!/usr/bin/env bash
set -euo pipefail

CONFIG=/etc/dsh-mcp-gateway/dev-hot-reload.conf
LIVE_ROOT=/srv/dsh-mcp-gateway

fail() {
  printf 'dsh-dev-hot-reload: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail 'must run as root'
[[ $# -eq 1 && $1 == gateway ]] || fail 'usage: dsh-mcp-gateway-dev-refresh gateway'

[[ -f "$CONFIG" ]] || fail "missing $CONFIG"
# shellcheck disable=SC1090
source "$CONFIG"
[[ ${SOURCE_ROOT:-} == /* ]] || fail 'SOURCE_ROOT must be absolute'
SOURCE_ROOT=$(realpath -e -- "$SOURCE_ROOT")
[[ -d "$SOURCE_ROOT/.git" || -f "$SOURCE_ROOT/.git" ]] || fail 'SOURCE_ROOT is not a git worktree'
[[ -d "$LIVE_ROOT" && -x "$LIVE_ROOT/.venv/bin/python" ]] || fail 'live gateway is not installed'

reject_special_files() {
  local root=$1
  local bad
  bad=$(find "$root" -mindepth 1 \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit)
  [[ -z "$bad" ]] || fail "refusing non-regular source entry: $bad"
}

site_package_dir() {
  local candidates=()
  local seen=''
  local path real
  shopt -s nullglob
  for path in "$LIVE_ROOT"/.venv/lib/python*/site-packages/dsh_mcp_gateway "$LIVE_ROOT"/.venv/lib64/python*/site-packages/dsh_mcp_gateway; do
    [[ -d "$path" ]] || continue
    real=$(realpath -e -- "$path")
    case " $seen " in
      *" $real "*) ;;
      *) candidates+=("$real"); seen+=" $real" ;;
    esac
  done
  shopt -u nullglob
  [[ ${#candidates[@]} -eq 1 ]] || fail "expected one live dsh_mcp_gateway site-package directory, found ${#candidates[@]}"
  printf '%s\n' "${candidates[0]}"
}

install_gateway() {
  local src="$SOURCE_ROOT/src/dsh_mcp_gateway"
  local live_src="$LIVE_ROOT/src/dsh_mcp_gateway"
  local site
  [[ -d "$src" && -d "$live_src" ]] || fail 'gateway source directories are missing'
  reject_special_files "$src"
  site=$(site_package_dir)

  local copied=0
  local path base
  while IFS= read -r -d '' path; do
    base=${path##*/}
    install -o root -g root -m 0644 -- "$path" "$live_src/$base"
    install -o root -g root -m 0644 -- "$path" "$site/$base"
    copied=$((copied + 1))
  done < <(find "$src" -maxdepth 1 -type f -name '*.py' -print0 | sort -z)
  [[ $copied -gt 0 ]] || fail 'no gateway Python modules found'

  systemctl restart dsh-mcp-gateway.service
  systemctl is-active --quiet dsh-mcp-gateway.service || fail 'dsh-mcp-gateway failed after refresh'
  printf 'gateway refreshed (%d Python modules)\n' "$copied"
}

install_gateway

head=$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || true)
dirty=$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=no 2>/dev/null || true)
printf 'source_commit=%s dirty=%s\n' "${head:-unknown}" "$([[ -n "$dirty" ]] && echo yes || echo no)"
