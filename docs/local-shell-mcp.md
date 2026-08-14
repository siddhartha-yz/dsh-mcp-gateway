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

## Current tool surface

This integration is intentionally opt-in. local-shell-mcp 4.0.0 publishes 43 MCP tools internally, while DSH rc6's `dsh-mcp-client` has no per-server filter. The overlay therefore adds a tiny host plugin that uses DSH's agent-scoped `ctx.tools.restrict()` seam to hide redundant LSM tools coherently from model presentation, lookup, and execution.

The default model-facing LSM subset is seven differentiated capabilities:

```text
browser_session
browser_snapshot
browser_act
browser_run_script
mcp_tool_search
mcp_tool_inspect
mcp_tool_call
```

The first four preserve LSM's persistent Playwright/browser layer. The last three expose its already-configured dynamic MCP clients without exposing `mcp_manage`, so the agent cannot add arbitrary MCP servers through this default composition. DSH's own shell/files/jobs/skills/todo tools remain visible instead of duplicating their LSM counterparts.

In the real rc6 smoke test, the unfiltered composition produced 68 model-facing tools (25 DSH + 43 LSM). The filtered composition produced exactly 32 (25 DSH + 7 LSM), while the allowed nested LSM call still executed successfully. The same 32-tool surface was restored after a complete DSH Host restart and cold session resume.

`deploy/dsh/plugins/lsm-tool-filter.mjs` is a model-facing composition filter, not a security boundary. The LSM workspace/policy still owns authority. The plugin dynamically denies every `mcp__lsm__*` tool present when an agent is created except the configured allow set, so newly published LSM tools are hidden by default for new agents rather than silently expanding the model surface. Edit `allowRawNames` in the overlay if a deployment intentionally needs more LSM capabilities.

## Overlay

`deploy/dsh/local-shell-mcp.cordis.yml` inserts one official `@deepseek-ai/dsh-mcp-client` row using stdio plus the scoped visibility filter above. The DSH Host process must provide:

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

The existing DSH Host unit already grants write access to `/srv/dsh-workspace` and `/var/lib/dsh-harness`; no broader `ReadWritePaths` entry is needed for this layout. The filter loader entry uses the deployment's fixed repository path `/srv/dsh-mcp-gateway/deploy/dsh/plugins/lsm-tool-filter.mjs`, matching the systemd templates. Cordis loader entry `name` values must already be strings, so this path cannot be supplied through the `!!js process.env...` expressions used for ordinary plugin config values.

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

The rc6 smoke tests used an isolated LSM 4.0.0 stdio child and verified both the raw composition and the narrowed production overlay:

1. LSM initialized over stdio as `local-shell-mcp 4.0.0` and listed 43 tools.
2. Before filtering, a DSH Web session advertised all 43 LSM tools beside 25 DSH tools (68 total); a real `mcp__lsm__list_files` call reached the child, its audit log returned `marker.txt`, and the result returned through DSH durable history to the next model step.
3. With the scoped filter enabled, the model catalog contained exactly 32 tools: the same 25 DSH tools plus the seven configured LSM tools.
4. `mcp__lsm__list_files`, `mcp__lsm__run_shell_tool`, and `mcp__lsm__mcp_manage` were absent, while all seven configured names were present.
5. The fake model called the retained `mcp__lsm__mcp_tool_search`; the isolated LSM audit log recorded a successful `mcp_tool_search`, and DSH durable history carried the call/result to a completed assistant step.
6. After stopping only the DSH Host and restarting it with the same `DSH_HOME`, the same session cold-resumed with `action=resumed` and the model catalog was still exactly 32 tools with the same seven LSM names.
7. Stopping the DSH Host disposed the stdio transport and the spawned LSM child exited; no child process remained.

This proves the composition boundary and restart stability, not production equivalence between DSH's native tools and LSM. The two systems should keep owning their differentiated capabilities rather than duplicating runtime state inside the gateway.
