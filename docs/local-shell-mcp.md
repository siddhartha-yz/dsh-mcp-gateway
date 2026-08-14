# Optional local-shell-mcp execution backend

`dsh-mcp-gateway` does not depend on local-shell-mcp. DeepSeek Harness can, however, use a separately installed local-shell-mcp process as one of its MCP tool providers.

The tested same-host composition is:

```text
ChatGPT / MCP client
        |
        v
 dsh-mcp-gateway
        |
        v
 DeepSeek Harness
        |
        | stdio MCP client
        v
 local-shell-mcp
        |
        v
 restricted execution workspace
```

The public gateway and the internal execution MCP are different trust boundaries. The LSM child is spawned by DSH over stdio; it does not need the ChatGPT-facing OAuth credentials and does not traverse the public reverse proxy.

## Current trade-off

This integration is intentionally opt-in. local-shell-mcp 4.0.0 currently publishes its complete MCP catalog and DSH rc6's `dsh-mcp-client` has no per-server tool filter. In the real composition smoke test, LSM contributed 43 tools and the model-facing request contained 68 tools in total. That is functional but adds schema/token cost and duplicates some capabilities DSH already owns natively.

Use this overlay when the extra LSM capabilities are worth that cost. It is not enabled by the base deployment. A future narrower composition should use DSH's agent-scoped `ctx.tools.restrict()` seam or an upstream per-server MCP filter rather than hiding schemas only at prompt-render time.

## Overlay

`deploy/dsh/local-shell-mcp.cordis.yml` inserts one official `@deepseek-ai/dsh-mcp-client` row using stdio. The DSH Host process must provide:

```text
DSH_LSM_COMMAND
DSH_LSM_WORKSPACE_ROOT
DSH_LSM_STATE_DIR
DSH_LSM_AUDIT_LOG_PATH
DSH_LSM_AGENT_CONFIG_DIR
```

The overlay deliberately starts the child with:

```text
local-shell-mcp --mode stdio --no-remote
```

and forces these LSM settings:

```text
AUTH_MODE=none
REMOTE_ENABLED=false
UI_ENABLED=false
FILE_DOWNLOAD_ENABLED=false
ALLOW_FULL_CONTAINER=false
```

OAuth is unnecessary on a private stdio pipe. Remote-worker routes, Web UI, and public file links require an HTTP service plane and are therefore disabled in this stdio composition. Workspace/path restrictions remain enabled. Browser tools may be used when the selected LSM installation has its normal Playwright/browser runtime prerequisites installed.

A practical systemd layout using the existing service sandbox is:

```text
DSH_LSM_COMMAND=/opt/local-shell-mcp/.venv/bin/local-shell-mcp
DSH_LSM_WORKSPACE_ROOT=/srv/dsh-workspace
DSH_LSM_STATE_DIR=/var/lib/dsh-harness/local-shell-mcp
DSH_LSM_AUDIT_LOG_PATH=/var/lib/dsh-harness/local-shell-mcp/audit.jsonl
DSH_LSM_AGENT_CONFIG_DIR=/var/lib/dsh-harness/local-shell-mcp/agent_config
```

The existing DSH Host unit already grants write access to `/srv/dsh-workspace` and `/var/lib/dsh-harness`; no broader `ReadWritePaths` entry is needed for this layout.

## Enable under systemd

After installing a compatible local-shell-mcp runtime and filling the optional variables in `/etc/dsh-mcp-gateway/dsh.env`, replace the DSH Host `ExecStart` with one that adds the overlay before app arguments:

```ini
[Service]
ExecStart=
ExecStart=/opt/dsh-runtime/node_modules/.bin/dsh web --patch /srv/dsh-mcp-gateway/deploy/dsh/local-shell-mcp.cordis.yml --host 127.0.0.1 --port 3080
```

Then run:

```sh
sudo systemctl daemon-reload
sudo systemctl restart dsh-web-host.service
curl -fsS http://127.0.0.1:18766/readyz
```

If full-text session search is also enabled, put both patch flags in the same effective `ExecStart`, for example:

```text
... dsh web \
  --patch /srv/dsh-mcp-gateway/deploy/dsh/session-search.cordis.yml \
  --patch /srv/dsh-mcp-gateway/deploy/dsh/local-shell-mcp.cordis.yml \
  --host 127.0.0.1 --port 3080
```

Do not install two independent systemd drop-ins that both reset `ExecStart`; combine the desired launcher patches into one effective command.

## Integration evidence

The rc6 smoke test used an isolated LSM 4.0.0 stdio child with a single `marker.txt` file in its restricted workspace. The following were verified independently:

1. LSM initialized over stdio as `local-shell-mcp 4.0.0` and listed 43 tools.
2. A DSH Web session's model request advertised `mcp__lsm__list_files`; the full model-facing catalog contained 68 tools.
3. The fake model issued a real call to `mcp__lsm__list_files`.
4. The isolated LSM audit log recorded `list_files` completing successfully in a few milliseconds and returning `marker.txt`.
5. DSH appended the MCP tool call/result to durable session history and the next model step returned `nested-lsm-marker-observed`.
6. Stopping the DSH Host disposed the stdio transport and the spawned LSM child exited; no child process remained.

This proves the composition boundary, not production equivalence between DSH's native tools and LSM. The two systems should keep owning their differentiated capabilities rather than duplicating runtime state inside the gateway.
