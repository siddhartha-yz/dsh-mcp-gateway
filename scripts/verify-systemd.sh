#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

sed \
  's#^ExecStart=/opt/dsh-runtime/node_modules/.bin/dsh #ExecStart=/bin/true #' \
  "$ROOT/deploy/systemd/dsh-web-host.service" \
  > "$TMP/dsh-web-host.service"

sed \
  's#^ExecStart=/srv/dsh-mcp-gateway/.venv/bin/dsh-mcp-gateway #ExecStart=/bin/true #' \
  "$ROOT/deploy/systemd/dsh-mcp-gateway.service" \
  > "$TMP/dsh-mcp-gateway.service"

grep -q '^ExecStart=/bin/true ' "$TMP/dsh-web-host.service"
grep -q '^ExecStart=/bin/true ' "$TMP/dsh-mcp-gateway.service"

SYSTEMD_UNIT_PATH="$TMP:/usr/lib/systemd/system:/lib/systemd/system" \
  systemd-analyze verify \
  "$TMP/dsh-web-host.service" \
  "$TMP/dsh-mcp-gateway.service"
