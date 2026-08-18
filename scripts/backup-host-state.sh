#!/usr/bin/env bash
set -euo pipefail

OUTPUT=""
OUTPUT_OWNER="root"
WORKSPACE="/home/ubuntu/workspace"
WORKSPACE_PATHS=()

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/backup-host-state.sh --output PATH [options]

Create a consistent offline backup of the DSH Harness and OAuth gateway state.
Only the three DSH services are stopped briefly; unrelated host projects and
services are not touched. The Cloudflare tunnel is stopped first so no new
public MCP request can arrive while state is being copied.

Options:
  --output PATH          New backup directory to create (required).
  --output-owner USER    Chown the completed backup to USER (default: root).
  --workspace PATH       Workspace root recorded in the manifest.
  --workspace-path REL   Include one representative workspace path; repeatable.
  -h, --help             Show this help.

The backup contains OAuth tokens, gateway configuration, and Cloudflare tunnel
credentials. Keep it mode 0700/0600 and encrypt it before moving it off-host.
Arbitrary user projects are not implicitly archived: pass explicit representative
workspace paths here, and use each project's normal Git/backup policy for the
full workspace.
EOF
}

while (($#)); do
  case "$1" in
    --output) [[ $# -ge 2 ]] || { echo "--output requires a path" >&2; exit 2; }; OUTPUT="$2"; shift 2 ;;
    --output-owner) [[ $# -ge 2 ]] || { echo "--output-owner requires a user" >&2; exit 2; }; OUTPUT_OWNER="$2"; shift 2 ;;
    --workspace) [[ $# -ge 2 ]] || { echo "--workspace requires a path" >&2; exit 2; }; WORKSPACE="$2"; shift 2 ;;
    --workspace-path) [[ $# -ge 2 ]] || { echo "--workspace-path requires a relative path" >&2; exit 2; }; WORKSPACE_PATHS+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ ${EUID} -eq 0 ]] || { echo "backup-host-state.sh must run as root" >&2; exit 1; }
[[ -n "$OUTPUT" ]] || { echo "--output is required" >&2; exit 2; }
getent passwd "$OUTPUT_OWNER" >/dev/null || { echo "output owner does not exist: $OUTPUT_OWNER" >&2; exit 1; }
[[ -d "$WORKSPACE" ]] || { echo "workspace is missing: $WORKSPACE" >&2; exit 1; }

for command in curl python3 tar sha256sum systemctl; do
  command -v "$command" >/dev/null 2>&1 || { echo "missing required command: $command" >&2; exit 1; }
done
for path in \
  /var/lib/dsh-harness \
  /var/lib/dsh-mcp-gateway \
  /etc/dsh-mcp-gateway \
  /etc/dsh-cloudflared \
  /srv/dsh-mcp-gateway/.deployed-git-commit; do
  [[ -e "$path" ]] || { echo "required production path is missing: $path" >&2; exit 1; }
done

OUTPUT="$(python3 - "$OUTPUT" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)"
[[ ! -e "$OUTPUT" ]] || { echo "backup output already exists: $OUTPUT" >&2; exit 1; }
install -d -m 0700 "$OUTPUT"

# Validate explicitly selected workspace paths before service interruption.
python3 - "$WORKSPACE" "$OUTPUT" "${WORKSPACE_PATHS[@]}" <<'PY'
import os, sys
root=os.path.realpath(sys.argv[1]); output=os.path.realpath(sys.argv[2])
for raw in sys.argv[3:]:
    if not raw or os.path.isabs(raw):
        raise SystemExit(f"workspace path must be non-empty and relative: {raw!r}")
    candidate=os.path.join(root, raw)
    if os.path.islink(candidate):
        raise SystemExit(f"workspace path must not be a symlink: {raw}")
    target=os.path.realpath(candidate)
    if os.path.commonpath([root, target]) != root:
        raise SystemExit(f"workspace path escapes root: {raw}")
    if target == root:
        raise SystemExit("workspace root cannot be selected implicitly; pass explicit paths")
    if os.path.commonpath([target, output]) == target:
        raise SystemExit(f"backup output is inside selected workspace path: {raw}")
    if not os.path.exists(target):
        raise SystemExit(f"workspace path does not exist: {raw}")
PY

# Snapshot the live capability surface before making the backup offline.
curl -fsS http://127.0.0.1:3080/api/chatgpt-bridge/tools > "$OUTPUT/tools-before.json"
curl -fsS http://127.0.0.1:3080/api/chatgpt-bridge/skills > "$OUTPUT/skills-before.json"
chmod 0600 "$OUTPUT/tools-before.json" "$OUTPUT/skills-before.json"

HOST_WAS_ACTIVE=0
GATEWAY_WAS_ACTIVE=0
TUNNEL_WAS_ACTIVE=0
systemctl is-active --quiet dsh-web-host.service && HOST_WAS_ACTIVE=1 || true
systemctl is-active --quiet dsh-mcp-gateway.service && GATEWAY_WAS_ACTIVE=1 || true
systemctl is-active --quiet dsh-cloudflared.service && TUNNEL_WAS_ACTIVE=1 || true

restore_services() {
  local original_rc=$?
  local restart_rc=0
  set +e
  if ((HOST_WAS_ACTIVE)); then
    systemctl start dsh-web-host.service || restart_rc=1
    for _ in $(seq 1 80); do
      curl -fsS http://127.0.0.1:3080/api/chatgpt-bridge/tools >/dev/null 2>&1 && break
      sleep .25
    done
    curl -fsS http://127.0.0.1:3080/api/chatgpt-bridge/tools >/dev/null 2>&1 || restart_rc=1
  fi
  if ((GATEWAY_WAS_ACTIVE)); then
    systemctl start dsh-mcp-gateway.service || restart_rc=1
    for _ in $(seq 1 80); do
      curl -fsS http://127.0.0.1:18766/readyz >/dev/null 2>&1 && break
      sleep .25
    done
    curl -fsS http://127.0.0.1:18766/readyz >/dev/null 2>&1 || restart_rc=1
  fi
  if ((TUNNEL_WAS_ACTIVE)); then
    systemctl start dsh-cloudflared.service || restart_rc=1
  fi
  set -e
  if ((original_rc != 0)); then
    return "$original_rc"
  fi
  return "$restart_rc"
}
trap restore_services EXIT

# Make the state quiescent. No unrelated host service is touched.
((TUNNEL_WAS_ACTIVE)) && systemctl stop dsh-cloudflared.service
((GATEWAY_WAS_ACTIVE)) && systemctl stop dsh-mcp-gateway.service
((HOST_WAS_ACTIVE)) && systemctl stop dsh-web-host.service

# Preserve the production paths in each archive so a real restore can extract at /.
tar --numeric-owner -C / -czf "$OUTPUT/dsh-home.tar.gz" var/lib/dsh-harness
tar --numeric-owner -C / -czf "$OUTPUT/gateway-state.tar.gz" var/lib/dsh-mcp-gateway

CONFIG_PATHS=(
  etc/dsh-mcp-gateway
  etc/dsh-cloudflared
  etc/systemd/system/dsh-web-host.service
  etc/systemd/system/dsh-mcp-gateway.service
  etc/systemd/system/dsh-cloudflared.service
)
[[ -d /etc/systemd/system/dsh-web-host.service.d ]] && CONFIG_PATHS+=(etc/systemd/system/dsh-web-host.service.d)
tar --numeric-owner -C / -czf "$OUTPUT/config.tar.gz" "${CONFIG_PATHS[@]}"

if ((${#WORKSPACE_PATHS[@]})); then
  tar --numeric-owner -C "$WORKSPACE" -czf "$OUTPUT/workspace-selected.tar.gz" -- "${WORKSPACE_PATHS[@]}"
else
  tar -czf "$OUTPUT/workspace-selected.tar.gz" --files-from /dev/null
fi
chmod 0600 "$OUTPUT"/*.tar.gz

# Record a non-secret manifest plus exact selected-workspace hashes.
python3 - "$OUTPUT" "$WORKSPACE" "${WORKSPACE_PATHS[@]}" <<'PY'
from __future__ import annotations
import datetime, hashlib, json, os, pathlib, subprocess, sys
out=pathlib.Path(sys.argv[1]); workspace=pathlib.Path(sys.argv[2]).resolve(); selected=sys.argv[3:]
tools=json.loads((out/'tools-before.json').read_text())['tools']
skills=json.loads((out/'skills-before.json').read_text())['skills']

def hash_path(rel: str):
    target=workspace/rel
    if target.is_symlink():
        raise SystemExit(f'workspace path became a symlink during backup: {rel}')
    target=target.resolve()
    rows=[]
    if target.is_file():
        rows.append({'path': rel, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'size': target.stat().st_size})
    elif target.is_dir():
        for p in sorted(target.rglob('*')):
            if p.is_file() and not p.is_symlink():
                r=p.relative_to(workspace).as_posix()
                rows.append({'path': r, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'size': p.stat().st_size})
    else:
        raise SystemExit(f'unsupported workspace path type: {rel}')
    return rows
workspace_files=[]
for rel in selected: workspace_files.extend(hash_path(rel))
public_base=''
for line in pathlib.Path('/etc/dsh-mcp-gateway/gateway.env').read_text().splitlines():
    if line.startswith('DSH_MCP_PUBLIC_BASE_URL='):
        public_base=line.split('=',1)[1].strip().strip('"\'')
manifest={
 'schema_version': 1,
 'created_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'deployed_commit': pathlib.Path('/srv/dsh-mcp-gateway/.deployed-git-commit').read_text().strip(),
 'dsh_version': json.loads(pathlib.Path('/opt/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json').read_text())['version'],
 'node_version': subprocess.check_output(['/opt/dsh-runtime/node/bin/node','--version'], text=True).strip(),
 'public_base_url': public_base,
 'workspace_root': str(workspace),
 'workspace_paths': selected,
 'workspace_files': workspace_files,
 'tool_names': sorted(x['name'] for x in tools),
 'skill_names': sorted(x['name'] for x in skills),
}
(out/'MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n')
os.chmod(out/'MANIFEST.json',0o600)
print(f"backup manifest: tools={len(manifest['tool_names'])} skills={len(manifest['skill_names'])} workspace_files={len(workspace_files)}")
PY

(
  cd "$OUTPUT"
  sha256sum MANIFEST.json tools-before.json skills-before.json dsh-home.tar.gz gateway-state.tar.gz config.tar.gz workspace-selected.tar.gz > SHA256SUMS
  chmod 0600 SHA256SUMS
)

# Restart exactly the DSH services that were active before this backup.
trap - EXIT
restore_services

chown -R "$OUTPUT_OWNER:$(id -gn "$OUTPUT_OWNER")" "$OUTPUT"
chmod 0700 "$OUTPUT"
find "$OUTPUT" -type f -exec chmod 0600 {} +

echo "BACKUP PASS: $OUTPUT"
echo "Only DSH services were briefly quiesced; unrelated host services were not touched."
