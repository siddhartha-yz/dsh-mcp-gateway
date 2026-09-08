#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'install-dev-hot-reload: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail 'run this installer with sudo'

SOURCE_ROOT=''
DEV_USER='ubuntu'
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      [[ $# -ge 2 ]] || fail '--source requires a path'
      SOURCE_ROOT=$2
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || fail '--user requires a username'
      DEV_USER=$2
      shift 2
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ -n "$SOURCE_ROOT" ]] || fail '--source is required'
id "$DEV_USER" >/dev/null 2>&1 || fail "unknown user: $DEV_USER"
SOURCE_ROOT=$(realpath -e -- "$SOURCE_ROOT")
[[ -d "$SOURCE_ROOT" ]] || fail 'source is not a directory'
[[ -d "$SOURCE_ROOT/.git" || -f "$SOURCE_ROOT/.git" ]] || fail 'source is not a git worktree'
[[ -f "$SOURCE_ROOT/scripts/dev-hot-reload-root.sh" ]] || fail 'source does not contain dev-hot-reload-root.sh'
[[ -f "$SOURCE_ROOT/dsh-chatgpt-web-bridge-plugin/index.js" ]] || fail 'bridge plugin is missing'
[[ -f "$SOURCE_ROOT/src/dsh_mcp_gateway/chatgpt_web_companion.py" ]] || fail 'gateway source is missing'

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
  "$SOURCE_ROOT/scripts/dev-hot-reload-root.sh" \
  /usr/local/libexec/dsh-mcp-gateway-dev-refresh

install -d -o root -g root -m 0700 /etc/dsh-mcp-gateway
printf 'SOURCE_ROOT=%q\n' "$SOURCE_ROOT" > /etc/dsh-mcp-gateway/dev-hot-reload.conf
chown root:root /etc/dsh-mcp-gateway/dev-hot-reload.conf
chmod 0600 /etc/dsh-mcp-gateway/dev-hot-reload.conf

sudoers_tmp=$(mktemp)
trap 'rm -f "$sudoers_tmp"' EXIT
cat > "$sudoers_tmp" <<EOF
# Narrow P6 development hot-refresh lane. The helper hard-codes its root-owned
# source configuration and accepts only the three literal component arguments.
$DEV_USER ALL=(root) NOPASSWD: /usr/local/libexec/dsh-mcp-gateway-dev-refresh bridge
$DEV_USER ALL=(root) NOPASSWD: /usr/local/libexec/dsh-mcp-gateway-dev-refresh gateway
$DEV_USER ALL=(root) NOPASSWD: /usr/local/libexec/dsh-mcp-gateway-dev-refresh all
EOF
chmod 0440 "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/dsh-mcp-gateway-dev-refresh
visudo -cf /etc/sudoers.d/dsh-mcp-gateway-dev-refresh >/dev/null

printf 'Installed DSH dev hot-refresh lane.\n'
printf 'Source: %s\n' "$SOURCE_ROOT"
printf 'User: %s\n' "$DEV_USER"
printf 'Future refreshes: sudo -n /usr/local/libexec/dsh-mcp-gateway-dev-refresh {bridge|gateway|all}\n'
printf 'Use the full guarded upgrade for dependencies, systemd units, runtime, or release promotion.\n'
