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

python3 - \
  /var/lib/dsh-harness \
  /var/lib/dsh-mcp-gateway \
  /etc/dsh-mcp-gateway \
  /etc/dsh-cloudflared \
  /srv/dsh-mcp-gateway \
  /var/lib/dsh-harness/profiles/web/package.json \
  /var/lib/dsh-mcp-gateway/oauth.sqlite3 \
  /etc/dsh-mcp-gateway/gateway.env \
  /etc/dsh-cloudflared/credentials.json \
  /srv/dsh-mcp-gateway/.deployed-git-commit <<'PY'
import pathlib, stat, sys

roots = [pathlib.Path(raw) for raw in sys.argv[1:6]]
for path in roots:
    try:
        opened = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"required production state root is unavailable: {path}: {exc}") from exc
    if path.is_symlink() or resolved != path or not stat.S_ISDIR(opened.st_mode):
        raise SystemExit(f"required production state root is not a real directory: {path}")

for raw, root in zip(sys.argv[6:], roots, strict=True):
    path = pathlib.Path(raw)
    try:
        opened = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"required production state file is unavailable: {path}: {exc}") from exc
    if (
        path.is_symlink()
        or not resolved.is_relative_to(resolved_root)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
    ):
        raise SystemExit(f"required production state file is not a private regular file: {path}")
PY

OUTPUT="$(python3 - "$OUTPUT" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)"
[[ ! -e "$OUTPUT" ]] || { echo "backup output already exists: $OUTPUT" >&2; exit 1; }

# Validate explicitly selected workspace paths before creating backup output or interrupting services.
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

OUTPUT_ID="$(python3 - "$OUTPUT" create-output <<'PY'
import os, pathlib, sys
path = pathlib.Path(sys.argv[1])
if not path.is_absolute():
    raise SystemExit("backup output path must be absolute")
parts = path.parts[1:]
fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
try:
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if last:
            os.mkdir(part, mode=0o700, dir_fd=fd)
        try:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            if last:
                raise
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError:
            if last:
                try:
                    os.rmdir(part, dir_fd=fd)
                except OSError:
                    pass
            raise
        os.close(fd)
        fd = child
    os.fchmod(fd, 0o700)
    opened = os.fstat(fd)
    print(f"{opened.st_dev}:{opened.st_ino}")
finally:
    os.close(fd)
PY
)"
exec {OUTPUT_FD}<"$OUTPUT"
OUTPUT_IO="/proc/self/fd/$OUTPUT_FD"
python3 - "$OUTPUT_IO" "${OUTPUT_ID}" <<'PY'
import os, sys
opened = os.stat(sys.argv[1])
actual = f"{opened.st_dev}:{opened.st_ino}"
if actual != sys.argv[2]:
    raise SystemExit("backup output path changed after secure creation")
PY
BACKUP_COMPLETE=0
cleanup_partial_output() {
  local original_rc=$?
  if ((original_rc != 0 && BACKUP_COMPLETE == 0)); then
    find -H "$OUTPUT_IO" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    rmdir -- "$OUTPUT" 2>/dev/null || true
  fi
  return "$original_rc"
}
trap cleanup_partial_output EXIT

# Snapshot the live capability surface before making the backup offline.
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/tools > "$OUTPUT_IO/tools-before.json"
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/skills > "$OUTPUT_IO/skills-before.json"
chmod 0600 "$OUTPUT_IO/tools-before.json" "$OUTPUT_IO/skills-before.json"

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
      curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/tools >/dev/null 2>&1 && break
      sleep .25
    done
    curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/tools >/dev/null 2>&1 || restart_rc=1
  fi
  if ((GATEWAY_WAS_ACTIVE)); then
    systemctl start dsh-mcp-gateway.service || restart_rc=1
    for _ in $(seq 1 80); do
      curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz >/dev/null 2>&1 && break
      sleep .25
    done
    curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz >/dev/null 2>&1 || restart_rc=1
  fi
  if ((TUNNEL_WAS_ACTIVE)); then
    systemctl start dsh-cloudflared.service || restart_rc=1
  fi
  if ((original_rc != 0 || restart_rc != 0)) && ((BACKUP_COMPLETE == 0)); then
    find -H "$OUTPUT_IO" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    rmdir -- "$OUTPUT" 2>/dev/null || true
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
tar --numeric-owner -C / -czf "$OUTPUT_IO/dsh-home.tar.gz" var/lib/dsh-harness
tar --numeric-owner -C / -czf "$OUTPUT_IO/gateway-state.tar.gz" var/lib/dsh-mcp-gateway

CONFIG_PATHS=(
  etc/dsh-mcp-gateway
  etc/dsh-cloudflared
  etc/systemd/system/dsh-web-host.service
  etc/systemd/system/dsh-mcp-gateway.service
  etc/systemd/system/dsh-cloudflared.service
)
[[ -d /etc/systemd/system/dsh-web-host.service.d ]] && CONFIG_PATHS+=(etc/systemd/system/dsh-web-host.service.d)
tar --numeric-owner -C / -czf "$OUTPUT_IO/config.tar.gz" "${CONFIG_PATHS[@]}"

python3 - "$OUTPUT_IO/workspace-selected.tar.gz" "$WORKSPACE" "${WORKSPACE_PATHS[@]}" <<'PY'
from __future__ import annotations
import os, pathlib, stat, sys, tarfile

archive = sys.argv[1]
workspace = os.path.abspath(sys.argv[2])
selected = sys.argv[3:]


def open_absolute_directory(path: str) -> int:
    if not os.path.isabs(path):
        raise SystemExit('workspace path must be absolute')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in pathlib.Path(path).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def tar_info(name: str, st: os.stat_result) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = stat.S_IMODE(st.st_mode)
    info.uid = st.st_uid
    info.gid = st.st_gid
    info.mtime = int(st.st_mtime)
    return info


def add_opened(tf: tarfile.TarFile, fd: int, name: str) -> None:
    st = os.fstat(fd)
    info = tar_info(name, st)
    if stat.S_ISREG(st.st_mode):
        info.type = tarfile.REGTYPE
        info.size = st.st_size
        with os.fdopen(os.dup(fd), 'rb') as source:
            tf.addfile(info, source)
        return
    if not stat.S_ISDIR(st.st_mode):
        raise SystemExit(f'unsupported workspace path type: {name}')
    info.type = tarfile.DIRTYPE
    tf.addfile(info)
    with os.scandir(fd) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            child_name = f'{name}/{entry.name}'
            child_stat = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(child_stat.st_mode):
                link_info = tar_info(child_name, child_stat)
                link_info.type = tarfile.SYMTYPE
                link_info.linkname = os.readlink(entry.name, dir_fd=fd)
                tf.addfile(link_info)
                continue
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if stat.S_ISDIR(child_stat.st_mode):
                flags |= os.O_DIRECTORY
            child_fd = os.open(entry.name, flags, dir_fd=fd)
            try:
                add_opened(tf, child_fd, child_name)
            finally:
                os.close(child_fd)


root_fd = open_absolute_directory(workspace)
try:
    with tarfile.open(archive, 'w:gz') as tf:
        for raw in selected:
            parts = pathlib.PurePosixPath(raw).parts
            if not raw or os.path.isabs(raw) or any(part in {'.', '..'} for part in parts):
                raise SystemExit(f'workspace path must be a normalized relative path: {raw!r}')
            parent_fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    os.close(parent_fd)
                    parent_fd = child
                leaf_stat = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(leaf_stat.st_mode):
                    raise SystemExit(f'workspace path must not be a symlink: {raw}')
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if stat.S_ISDIR(leaf_stat.st_mode):
                    flags |= os.O_DIRECTORY
                leaf_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
                try:
                    add_opened(tf, leaf_fd, raw)
                finally:
                    os.close(leaf_fd)
            finally:
                os.close(parent_fd)
finally:
    os.close(root_fd)
PY
chmod 0600 "$OUTPUT_IO"/*.tar.gz

# Record a non-secret manifest plus exact hashes of the securely archived workspace files.
python3 - "$OUTPUT_IO" "$WORKSPACE" "${WORKSPACE_PATHS[@]}" <<'PY'
from __future__ import annotations
import datetime, hashlib, json, os, pathlib, subprocess, sys, tarfile
out=pathlib.Path(sys.argv[1]); workspace=pathlib.Path(os.path.abspath(sys.argv[2])); selected=sys.argv[3:]
tools=json.loads((out/'tools-before.json').read_text())['tools']
skills=json.loads((out/'skills-before.json').read_text())['skills']
workspace_files=[]
with tarfile.open(out/'workspace-selected.tar.gz', 'r:gz') as tf:
    for member in tf.getmembers():
        if not member.isfile():
            continue
        source=tf.extractfile(member)
        if source is None:
            raise SystemExit(f'failed to read archived workspace file: {member.name}')
        digest=hashlib.sha256(); size=0
        with source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk); size += len(chunk)
        workspace_files.append({'path': member.name, 'sha256': digest.hexdigest(), 'size': size})
public_base=''
for line in pathlib.Path('/etc/dsh-mcp-gateway/gateway.env').read_text().splitlines():
    if line.startswith('DSH_MCP_PUBLIC_BASE_URL='):
        public_base=line.split('=',1)[1].strip().strip('"\'')
manifest={
 'schema_version': 1,
 'created_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'deployed_commit': pathlib.Path('/srv/dsh-mcp-gateway/.deployed-git-commit').read_text().strip(),
 'dsh_version': json.loads(pathlib.Path('/opt/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json').read_text())['version'],
 'node_version': subprocess.check_output(
     ['/opt/dsh-runtime/node/bin/node', '--version'],
     text=True,
     timeout=5,
 ).strip(),
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
  cd "$OUTPUT_IO"
  sha256sum MANIFEST.json tools-before.json skills-before.json dsh-home.tar.gz gateway-state.tar.gz config.tar.gz workspace-selected.tar.gz > SHA256SUMS
  chmod 0600 SHA256SUMS
)

# Restart exactly the DSH services that were active before this backup.
trap - EXIT
restore_services
trap cleanup_partial_output EXIT

[[ ! -L "$OUTPUT" && "$OUTPUT" -ef "$OUTPUT_IO" ]] || {
  echo "backup output path changed during backup: $OUTPUT" >&2
  exit 1
}
chown -R "$OUTPUT_OWNER:$(id -gn "$OUTPUT_OWNER")" "$OUTPUT_IO"
chmod 0700 "$OUTPUT_IO"
find -H "$OUTPUT_IO" -type f -exec chmod 0600 {} +
BACKUP_COMPLETE=1
trap - EXIT

echo "BACKUP PASS: $OUTPUT"
echo "Only DSH services were briefly quiesced; unrelated host services were not touched."
