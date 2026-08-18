#!/usr/bin/env bash
set -euo pipefail

BACKUP=""
RESTORE_ROOT=""
DSH_PORT=18422
GATEWAY_PORT=18778

usage() {
  cat <<'EOF'
Usage: ./scripts/verify-backup-restore.sh --backup PATH --restore-root PATH [options]

Restore a backup into an isolated directory and temporary loopback ports. The
production DSH services are not stopped or modified. The verifier rebuilds the
DSH web profile offline from the backed-up plugin artifacts, starts a restored
Harness and OAuth gateway, rotates a cloned ChatGPT refresh grant, and calls the
four-tool meta-only MCP surface.

Options:
  --backup PATH        Backup directory from backup-host-state.sh (required).
  --restore-root PATH  New temporary restore directory (required).
  --dsh-port PORT      Isolated DSH bridge port (default 18422).
  --gateway-port PORT  Isolated gateway port (default 18778).
  -h, --help           Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --backup) [[ $# -ge 2 ]] || { echo "--backup requires a path" >&2; exit 2; }; BACKUP="$2"; shift 2 ;;
    --restore-root) [[ $# -ge 2 ]] || { echo "--restore-root requires a path" >&2; exit 2; }; RESTORE_ROOT="$2"; shift 2 ;;
    --dsh-port) [[ $# -ge 2 ]] || { echo "--dsh-port requires a value" >&2; exit 2; }; DSH_PORT="$2"; shift 2 ;;
    --gateway-port) [[ $# -ge 2 ]] || { echo "--gateway-port requires a value" >&2; exit 2; }; GATEWAY_PORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$BACKUP" && -n "$RESTORE_ROOT" ]] || { echo "--backup and --restore-root are required" >&2; exit 2; }
[[ "$DSH_PORT" =~ ^[0-9]+$ && "$GATEWAY_PORT" =~ ^[0-9]+$ ]] || { echo "ports must be integers" >&2; exit 2; }
((DSH_PORT > 0 && DSH_PORT <= 65535 && GATEWAY_PORT > 0 && GATEWAY_PORT <= 65535 && DSH_PORT != GATEWAY_PORT)) || { echo "invalid or duplicate ports" >&2; exit 2; }

for command in curl python3 tar sha256sum; do
  command -v "$command" >/dev/null 2>&1 || { echo "missing required command: $command" >&2; exit 1; }
done
for path in \
  "$BACKUP/MANIFEST.json" \
  "$BACKUP/SHA256SUMS" \
  "$BACKUP/dsh-home.tar.gz" \
  "$BACKUP/gateway-state.tar.gz" \
  "$BACKUP/config.tar.gz" \
  "$BACKUP/workspace-selected.tar.gz" \
  /opt/dsh-runtime/node_modules/.bin/dsh \
  /opt/dsh-runtime/node_modules/.bin/pnpm \
  /srv/dsh-mcp-gateway/.venv/bin/dsh-mcp-gateway \
  /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml; do
  [[ -e "$path" ]] || { echo "required restore input is missing: $path" >&2; exit 1; }
done

BACKUP="$(cd "$BACKUP" && pwd)"
RESTORE_ROOT="$(python3 - "$RESTORE_ROOT" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)"
[[ ! -e "$RESTORE_ROOT" ]] || { echo "restore root already exists: $RESTORE_ROOT" >&2; exit 1; }

for port in "$DSH_PORT" "$GATEWAY_PORT"; do
  if curl -fsS --max-time 1 "http://127.0.0.1:$port/" >/dev/null 2>&1; then
    echo "temporary port is already serving HTTP: $port" >&2
    exit 1
  fi
done

(
  cd "$BACKUP"
  sha256sum -c SHA256SUMS
)

install -d -m 0700 "$RESTORE_ROOT/system" "$RESTORE_ROOT/workspace" "$RESTORE_ROOT/logs"
tar --no-same-owner -xzf "$BACKUP/dsh-home.tar.gz" -C "$RESTORE_ROOT/system"
tar --no-same-owner -xzf "$BACKUP/gateway-state.tar.gz" -C "$RESTORE_ROOT/system"
tar --no-same-owner -xzf "$BACKUP/config.tar.gz" -C "$RESTORE_ROOT/system"
tar --no-same-owner -xzf "$BACKUP/workspace-selected.tar.gz" -C "$RESTORE_ROOT/workspace"
chmod -R u+rwX,go-rwx "$RESTORE_ROOT"

DSH_HOME_RESTORED="$RESTORE_ROOT/system/var/lib/dsh-harness"
GATEWAY_STATE_RESTORED="$RESTORE_ROOT/system/var/lib/dsh-mcp-gateway"
[[ -f "$DSH_HOME_RESTORED/profiles/web/package.json" ]] || { echo "restored DSH profile missing" >&2; exit 1; }
[[ -f "$GATEWAY_STATE_RESTORED/oauth.sqlite3" ]] || { echo "restored OAuth database missing" >&2; exit 1; }
[[ -f "$RESTORE_ROOT/system/etc/dsh-mcp-gateway/gateway.env" ]] || { echo "restored gateway config missing" >&2; exit 1; }
[[ -f "$RESTORE_ROOT/system/etc/dsh-cloudflared/credentials.json" ]] || { echo "restored tunnel credentials missing" >&2; exit 1; }

# Verify the explicitly selected workspace data before starting any process.
python3 - "$BACKUP/MANIFEST.json" "$RESTORE_ROOT/workspace" <<'PY'
import hashlib, json, pathlib, sys
manifest=json.load(open(sys.argv[1]))
root=pathlib.Path(sys.argv[2])
resolved_root=root.resolve()
for row in manifest.get('workspace_files', []):
    p=root/row['path']
    try:
        resolved=p.resolve(strict=True)
    except OSError:
        raise SystemExit(f"restored workspace file missing: {row['path']}")
    if not resolved.is_relative_to(resolved_root):
        raise SystemExit(f"restored workspace path escapes restore root through symlink: {row['path']}")
    if not resolved.is_file(): raise SystemExit(f"restored workspace file missing: {row['path']}")
    digest=hashlib.sha256(resolved.read_bytes()).hexdigest()
    if digest != row['sha256']: raise SystemExit(f"restored workspace hash mismatch: {row['path']}")
print(f"workspace_restore=PASS files={len(manifest.get('workspace_files', []))}")
PY

# Rebase all local DSH plugin artifacts into the isolated restored DSH_HOME and
# rebuild the profile with --offline. This proves the backup is self-contained
# rather than accidentally reading /var/lib/dsh-harness from production.
python3 - "$DSH_HOME_RESTORED" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1]).resolve()
p=root/'profiles/web/package.json'; data=json.loads(p.read_text())
deps=data.get('dependencies', {})
old='file:/var/lib/dsh-harness/plugin-artifacts/'
new='file:'+str(root/'plugin-artifacts')+'/'
count=0
for name,spec in list(deps.items()):
    if not isinstance(spec,str) or not spec.startswith(old):
        raise SystemExit(f"dependency is not a backed-up local artifact: {name} -> {spec}")
    deps[name]=new+spec[len(old):]
    artifact=pathlib.Path(deps[name][5:])
    if not artifact.is_file(): raise SystemExit(f"restored plugin artifact missing: {artifact.name}")
    count+=1
p.write_text(json.dumps(data,indent=2)+'\n')
print(f"plugin_artifacts_rebased={count}")
PY
rm -rf "$DSH_HOME_RESTORED/profiles/web/node_modules" "$DSH_HOME_RESTORED/profiles/web/pnpm-lock.yaml"
install -d -m 0700 \
  "$RESTORE_ROOT/pnpm-home" "$RESTORE_ROOT/pnpm-store" \
  "$RESTORE_ROOT/xdg-data" "$RESTORE_ROOT/xdg-cache" "$RESTORE_ROOT/xdg-state"
DSH_HOME="$DSH_HOME_RESTORED" \
PNPM_HOME="$RESTORE_ROOT/pnpm-home" \
XDG_DATA_HOME="$RESTORE_ROOT/xdg-data" \
XDG_CACHE_HOME="$RESTORE_ROOT/xdg-cache" \
XDG_STATE_HOME="$RESTORE_ROOT/xdg-state" \
npm_config_store_dir="$RESTORE_ROOT/pnpm-store" \
PATH="/opt/dsh-runtime/node_modules/.bin:/opt/dsh-runtime/node/bin:/usr/local/bin:/usr/bin:/bin" \
  /opt/dsh-runtime/node_modules/.bin/pnpm install \
    --dir "$DSH_HOME_RESTORED/profiles/web" --offline --no-frozen-lockfile \
    >"$RESTORE_ROOT/logs/pnpm.log" 2>&1
echo "offline_profile_rebuild=PASS"

DSH_PID=""
GATEWAY_PID=""
cleanup() {
  set +e
  [[ -n "$GATEWAY_PID" ]] && kill "$GATEWAY_PID" 2>/dev/null
  [[ -n "$DSH_PID" ]] && kill "$DSH_PID" 2>/dev/null
  [[ -n "$GATEWAY_PID" ]] && wait "$GATEWAY_PID" 2>/dev/null
  [[ -n "$DSH_PID" ]] && wait "$DSH_PID" 2>/dev/null
}
trap cleanup EXIT

(
  cd "$RESTORE_ROOT/workspace"
  export DSH_HOME="$DSH_HOME_RESTORED"
  export DSH_TELEMETRY_DISABLED=1
  export PATH=/opt/dsh-runtime/node_modules/.bin:/opt/dsh-runtime/node/bin:/usr/local/bin:/usr/bin:/bin
  exec /opt/dsh-runtime/node_modules/.bin/dsh web \
    --patch /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml \
    --host 127.0.0.1 --port "$DSH_PORT"
) >"$RESTORE_ROOT/logs/dsh.log" 2>&1 &
DSH_PID=$!

for _ in $(seq 1 120); do
  curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$DSH_PORT/api/chatgpt-bridge/tools" > "$RESTORE_ROOT/tools-restored.json" 2>/dev/null && break
  sleep .25
done
curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$DSH_PORT/api/chatgpt-bridge/skills" > "$RESTORE_ROOT/skills-restored.json"

python3 - "$BACKUP/MANIFEST.json" "$RESTORE_ROOT/tools-restored.json" "$RESTORE_ROOT/skills-restored.json" <<'PY'
import json, sys
m=json.load(open(sys.argv[1])); tools=json.load(open(sys.argv[2]))['tools']; skills=json.load(open(sys.argv[3]))['skills']
tool_names=sorted(x['name'] for x in tools); skill_names=sorted(x['name'] for x in skills)
if tool_names != m['tool_names']: raise SystemExit('restored DSH tool catalog differs from backup manifest')
if skill_names != m['skill_names']: raise SystemExit('restored SkillRegistry differs from backup manifest')
print(f"dsh_restore=PASS tools={len(tool_names)} skills={len(skill_names)}")
PY

PUBLIC_BASE="$(python3 - "$BACKUP/MANIFEST.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['public_base_url'])
PY
)"
[[ "$PUBLIC_BASE" == https://* ]] || { echo "backup manifest has invalid public base URL" >&2; exit 1; }
DRILL_PIN="restore-drill-only-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(12))
PY
)"
(
  export DSH_MCP_GATEWAY_ADMIN_PIN="$DRILL_PIN"
  exec /srv/dsh-mcp-gateway/.venv/bin/dsh-mcp-gateway \
    --dsh-harness-url "http://127.0.0.1:$DSH_PORT" \
    --tool-surface meta-only \
    --public-base-url "$PUBLIC_BASE" \
    --state-dir "$GATEWAY_STATE_RESTORED" \
    --bind-host 127.0.0.1 --port "$GATEWAY_PORT"
) >"$RESTORE_ROOT/logs/gateway.log" 2>&1 &
GATEWAY_PID=$!

for _ in $(seq 1 120); do
  curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$GATEWAY_PORT/readyz" >/dev/null 2>&1 && break
  sleep .25
done
curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$GATEWAY_PORT/readyz" >/dev/null

# Exercise a cloned real ChatGPT refresh grant against the isolated restored
# gateway. Token values never leave this Python process or appear in output.
python3 - "$BACKUP/MANIFEST.json" "$GATEWAY_STATE_RESTORED/oauth.sqlite3" "$GATEWAY_PORT" <<'PY'
import http.client, json, sqlite3, sys, urllib.parse
from urllib.parse import urlparse
manifest=json.load(open(sys.argv[1])); db=sys.argv[2]; port=int(sys.argv[3]); public=manifest['public_base_url'].rstrip('/')
conn=sqlite3.connect(db)
clients={row[0]: json.loads(row[1]) for row in conn.execute('select client_id,client_json from oauth_clients')}
chatgpt=[cid for cid,meta in clients.items() if meta.get('client_name')=='ChatGPT']
if not chatgpt: raise SystemExit('restored OAuth state has no ChatGPT client')
row=None
for cid in chatgpt:
    row=conn.execute('select client_id,token,resource from refresh_tokens where client_id=? limit 1',(cid,)).fetchone()
    if row: break
if not row: raise SystemExit('restored OAuth state has no ChatGPT refresh grant')
client_id, refresh_token, resource=row
host=urlparse(public).netloc

def request(method,path,body=None,headers=None):
    h={'Host':host,'User-Agent':'DSH-Backup-Restore-Drill/0.1',**(headers or {})}
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=10); c.request(method,path,body=body,headers=h)
    r=c.getresponse(); data=r.read(); out=(r.status,dict(r.getheaders()),data); c.close(); return out

form=urllib.parse.urlencode({'grant_type':'refresh_token','client_id':client_id,'refresh_token':refresh_token,'scope':'dsh:control offline_access','resource':resource})
status,_,body=request('POST','/token',form,{'Content-Type':'application/x-www-form-urlencoded','Origin':public})
if status != 200: raise SystemExit(f'restored refresh grant failed: HTTP {status}')
tokens=json.loads(body); access=tokens['access_token']
if tokens.get('refresh_token') == refresh_token: raise SystemExit('restored refresh grant did not rotate')
init={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2026-07-28','capabilities':{},'clientInfo':{'name':'backup-restore-drill','version':'0.1'}}}
status,headers,body=request('POST','/mcp',json.dumps(init),{'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':f'Bearer {access}','Origin':public})
if status != 200: raise SystemExit(f'restored MCP initialize failed: HTTP {status}')
result=json.loads(body)['result']; sid=headers.get('mcp-session-id'); protocol=result['protocolVersion']
if result.get('capabilities',{}).get('tools',{}).get('listChanged') is not False: raise SystemExit('restored MCP unexpectedly advertises tools.listChanged')
base={'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':f'Bearer {access}','Origin':public,'mcp-session-id':sid,'mcp-protocol-version':protocol}
status,_,_=request('POST','/mcp',json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'}),base)
if status != 202: raise SystemExit(f'restored MCP initialized notification failed: HTTP {status}')
status,_,body=request('POST','/mcp',json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}),base)
if status != 200: raise SystemExit(f'restored MCP tools/list failed: HTTP {status}')
names=sorted(x['name'] for x in json.loads(body)['result']['tools'])
expected=sorted(['dsh_tool_catalog','dsh_tool_call','dsh_skill_catalog','dsh_skill_load'])
if names != expected: raise SystemExit(f'restored MCP tool surface differs: {names}')
status,_,body=request('POST','/mcp',json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'dsh_tool_catalog','arguments':{}}}),base)
call=json.loads(body)['result'] if status==200 else None
if not isinstance(call,dict) or call.get('isError') is True: raise SystemExit('restored dsh_tool_catalog call failed')
if call['structuredContent']['count'] != len(manifest['tool_names']): raise SystemExit('restored catalog count differs from manifest')
status,_,body=request('POST','/mcp',json.dumps({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'dsh_skill_catalog','arguments':{}}}),base)
skill_call=json.loads(body)['result'] if status==200 else None
if not isinstance(skill_call,dict) or skill_call.get('isError') is True: raise SystemExit('restored dsh_skill_catalog call failed')
restored_skills=sorted(x['name'] for x in skill_call['structuredContent']['skills'])
if restored_skills != manifest['skill_names']: raise SystemExit('restored skill catalog differs from manifest')
print(f"oauth_mcp_restore=PASS tools={len(names)} catalog={len(manifest['tool_names'])} skills={len(restored_skills)}")
PY

echo "RESTORE DRILL PASS: $RESTORE_ROOT"
echo "Production DSH services were not modified by this verifier."
