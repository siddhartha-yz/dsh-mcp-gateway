from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ACTIONS = ("register", "resume", "poll", "heartbeat", "result")
INTERNAL_PREFIX = "/api/chatgpt-remote-workers/v1"


def worker_source_path() -> Path:
    return Path(__file__).with_name("remote_worker_agent.py")


def worker_source() -> bytes:
    return worker_source_path().read_bytes()


def build_join_script(public_base: str, source: bytes | None = None) -> str:
    payload = worker_source() if source is None else source
    digest = hashlib.sha256(payload).hexdigest()
    return f"""#!/usr/bin/env bash
set -euo pipefail

SERVER={_shell_quote(public_base)}
WORKER_SHA256={_shell_quote(digest)}
invite=''
name=''
workdir=''
persist=0

while (($#)); do
  case "$1" in
    --invite)
      (($# >= 2)) || {{ echo 'missing value for --invite' >&2; exit 2; }}
      invite=$2; shift 2 ;;
    --name)
      (($# >= 2)) || {{ echo 'missing value for --name' >&2; exit 2; }}
      name=$2; shift 2 ;;
    --workdir)
      (($# >= 2)) || {{ echo 'missing value for --workdir' >&2; exit 2; }}
      workdir=$2; shift 2 ;;
    --persist)
      persist=1; shift ;;
    *)
      echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$invite" ]] || {{ echo '--invite is required' >&2; exit 2; }}
command -v python3 >/dev/null || {{ echo 'python3 is required' >&2; exit 2; }}
command -v curl >/dev/null || {{ echo 'curl is required' >&2; exit 2; }}
command -v sha256sum >/dev/null || {{ echo 'sha256sum is required' >&2; exit 2; }}

install_dir="$HOME/.local/lib/dsh-remote-worker"
worker="$install_dir/worker.py"
mkdir -p "$install_dir"
chmod 700 "$install_dir"
tmp=$(mktemp "$install_dir/.worker.XXXXXX")
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$SERVER/remote/worker.py" -o "$tmp"
printf '%s  %s\n' "$WORKER_SHA256" "$tmp" | sha256sum -c - >/dev/null
install -m 700 "$tmp" "$worker"
rm -f "$tmp"
trap - EXIT

enroll=(python3 "$worker" enroll --server "$SERVER" --invite "$invite")
[[ -z "$name" ]] || enroll+=(--name "$name")
[[ -z "$workdir" ]] || enroll+=(--workdir "$workdir")
"${{enroll[@]}}"

if ((persist)); then
  python3 "$worker" install-service --start
  python3 "$worker" status || true
else
  exec python3 "$worker" run
fi
"""


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


async def _read_limited(request, limit: int = MAX_REQUEST_BYTES) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ValueError("request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _forward(
    harness_base_url: str,
    action: str,
    body: bytes,
    authorization: str | None,
    timeout_s: float,
) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        f"{harness_base_url}{INTERNAL_PREFIX}/{action}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                return 502, json.dumps({"ok": False, "error": "upstream_response_too_large"}).encode()
            return response.status, data
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            return 502, json.dumps({"ok": False, "error": "upstream_response_too_large"}).encode()
        return exc.code, data


def install_remote_worker_routes(server, harness_bridge, public_base: str) -> None:
    try:
        from starlette.responses import JSONResponse, Response
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise RuntimeError("MCP server dependencies are unavailable") from exc

    source = worker_source()
    join_script = build_join_script(public_base, source)

    @server.custom_route("/remote/worker.py", methods=["GET"], include_in_schema=False)
    async def remote_worker_source(_request):
        return Response(
            source,
            media_type="text/x-python; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @server.custom_route("/remote/join.sh", methods=["GET"], include_in_schema=False)
    async def remote_worker_join(_request):
        return Response(
            join_script,
            media_type="text/x-shellscript; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    for action in ACTIONS:
        _install_proxy_route(server, harness_bridge, action, JSONResponse, Response)


def _install_proxy_route(server, harness_bridge, action: str, JSONResponse, Response) -> None:
    path = f"/remote/v1/{action}"

    @server.custom_route(path, methods=["POST"], include_in_schema=False)
    async def proxy(request):
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            return JSONResponse(
                {"ok": False, "error": "unsupported_media_type", "message": "application/json is required"},
                status_code=415,
                headers={"Cache-Control": "no-store"},
            )
        authorization = request.headers.get("authorization")
        if authorization is not None and (
            len(authorization) > 640 or not authorization.lower().startswith("bearer ")
        ):
            return JSONResponse(
                {"ok": False, "error": "invalid_authorization"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            body = await _read_limited(request)
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "request_too_large", "message": f"request exceeds {MAX_REQUEST_BYTES} bytes"},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        try:
            parsed = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_request", "message": "request body must be valid JSON"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        if not isinstance(parsed, dict):
            return JSONResponse(
                {"ok": False, "error": "invalid_request", "message": "request body must be a JSON object"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        normalized = json.dumps(parsed, separators=(",", ":")).encode("utf-8")
        timeout_s = 40.0 if action == "poll" else 20.0
        try:
            status, upstream_body = await asyncio.to_thread(
                _forward,
                harness_bridge.base_url,
                action,
                normalized,
                authorization,
                timeout_s,
            )
        except Exception:  # noqa: BLE001 - do not expose loopback/backend details publicly.
            return JSONResponse(
                {"ok": False, "error": "remote_controller_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            json.loads(upstream_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_upstream_response"},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
        return Response(
            upstream_body,
            status_code=status,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
