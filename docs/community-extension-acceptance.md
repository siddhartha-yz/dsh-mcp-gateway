# Community extension acceptance

The recorded runs below predate the P2 external capability profile. Their raw catalog counts remain historical evidence; under P2 a community ToolRuntime capability must also be admitted by the DSH-side ChatGPT profile (or already be part of its reviewed defaults) before `dsh_tool_catalog` / `dsh_tool_call` can expose it.

This is the product-level proof for the repository contract in [`../AGENTS.md`](../AGENTS.md). It deliberately assumes ChatGPT has already approved and cached the connector's MCP tool snapshot.

## Representative extension

Use `dsh-find-plugin` `0.3.6` from `awesome-dsh-plugin/dsh-find-plugin`, pinned to commit `e75dc2e865c3cfbfd336f7b4bb753fec25d373e1` for the first recorded acceptance run.

It is a useful compatibility specimen because it is an independent community package, declares a normal DSH bundle patch, injects only the DSH `tools` service, and registers `find_dsh_plugin` through `ctx.tools.register(defineTool(...))`. The gateway must not contain any code that names or imports this plugin.

Review the pinned source before installation. Community plugins are third-party code.

## Frozen-snapshot procedure

1. Start the pinned DSH runtime with `dsh-bridge-plugin` and the normal preset composition. Start the OAuth MCP gateway in `--dsh-harness-url` mode with the default `--tool-surface meta-only` behavior.
2. Connect a normal ChatGPT Web conversation. Record the connector's initial visible tool snapshot and confirm it contains exactly the four stable meta-tools: `dsh_tool_catalog`, `dsh_tool_call`, `dsh_skill_catalog`, and `dsh_skill_load`. Confirm the server does not advertise `tools.listChanged`.
3. Confirm `find_dsh_plugin` is absent from that initial snapshot and absent from `dsh_tool_catalog`.
4. Without editing, re-publishing, re-approving, reconnecting, or refreshing the ChatGPT connector, install the pinned community extension into the DSH profile used by the bridge. Restart only the DSH process if the DSH plugin installer requires it; the gateway and ChatGPT connector stay unchanged.
   `dsh plugin --profile web add 'github:awesome-dsh-plugin/dsh-find-plugin#e75dc2e865c3cfbfd336f7b4bb753fec25d373e1'`
5. From the same ChatGPT connector, call the already-approved `dsh_tool_catalog`. It must now report `find_dsh_plugin` with its live DSH schema.
6. Call the already-approved `dsh_tool_call` with `name="find_dsh_plugin"` and a harmless query such as `terminal`. The result must come through DSH `ToolRuntime.execute(...)` and be visible to ChatGPT.
7. Verify there is still no plugin-specific wrapper in this repository and that `find_dsh_plugin` is still absent from MCP `tools/list` even though the live DSH catalog contains it.
8. Record the exact DSH version, plugin version/commit, gateway commit, fixed MCP tool snapshot, catalog result, and generic-call result. Test `--tool-surface projected` separately; it is not part of this correctness proof.

## Pass criteria

The acceptance run passes only when the same already-connected ChatGPT Web connector discovers and successfully invokes the newly installed community capability through stable meta-tools while the public MCP surface remains exactly the four fixed meta-tools. Dynamic first-class projection is a separate opt-in UX test, not a release dependency.

Unit coverage in `tests/test_harness_bridge.py` reproduces the frozen-snapshot semantics without a live ChatGPT client; this document is for the real product-level run.

## 2026-08-15 live run evidence

The first live run established a frozen T0 catalog of 25 DSH tools with `find_dsh_plugin` absent. The reviewed plugin was then installed at the pinned commit through DSH's native `plugin --profile web add` path and only the DSH Harness process was restarted. T1 contained 26 tools and `find_dsh_plugin` was present.

Using the already-issued OAuth access grant against the unchanged gateway, `dsh_tool_catalog` discovered `find_dsh_plugin` and `dsh_tool_call` successfully invoked it through DSH ToolRuntime, returning live plugin search results. The user's ChatGPT Web conversation then reproduced the same generic path successfully without directly invoking `find_dsh_plugin`. That run occurred while the gateway still supported optional dynamic projection, so the stricter meta-only rerun below remains the release-quality proof.

### Strict meta-only rerun

Gateway commit `ee88b94` changed the shipped/default public tool surface to protocol-level meta-only. Before installing the strict test extensions, a real public OAuth/MCP `initialize` + `tools/list` against `https://dsh.example.com/mcp` returned exactly:

- `dsh_tool_catalog`
- `dsh_tool_call`
- `dsh_skill_catalog`
- `dsh_skill_load`

The advertised `tools.listChanged` capability was `false`. At that T0, the DSH-internal catalog contained 30 tools and did not contain the four strict-test extensions below.

Four independent reviewed community tools were then installed into the DSH `web` profile while leaving the gateway, OAuth state, and already-configured ChatGPT app unchanged:

- `time` from `omdsh-dev/dsh-tool-time`, commit `bc55c350f01679f25cf4c32e281455f14a81d3cd`;
- `encoding` from `omdsh-dev/dsh-tool-encoding`, commit `5baa75fcbe980c8193c7117b9b67ed04a43b15d6`;
- `csv` from `omdsh-dev/dsh-tool-csv`, commit `93657cbf6a48be25f8d31a0abf24aa0c5658033a`;
- `schema` from `omdsh-dev/dsh-tool-schema`, commit `8d9a652144938012f03d1ef8b43e3728898152c9`.

Only the DSH Harness process was restarted. The internal catalog increased from 30 to 34 tools and contained all four additions. A second real public OAuth/MCP `initialize` + `tools/list` still returned exactly the same four meta-tools with `tools.listChanged=false`; none of the 34 DSH-internal tools became a first-class MCP tool.

From a normal ChatGPT Web conversation using that unchanged app, ChatGPT then used the fixed meta-tool path to execute a hidden-fixture workflow covering `read`, the four newly installed community tools, and `write`: base64 failure/recovery, RFC 4180 CSV parsing/querying, JSON Schema validation and default application, timezone conversion/difference, SHA-256 hashing, result write, and read-back verification. The run did not directly invoke an internal DSH tool as a first-class MCP action.

The resulting `/home/ubuntu/workspace/dsh-meta-only-hard-test/result.json` was independently checked on the host against values recomputed from the hidden input. The complete result matched (`result_equals_expected=True`), including the intentional schema error path `/3/id`, SHA-256 `5e6a71848352bb0cd41e7bf8869a13bcbf6b39e202b487613e9bfb2b69e3a71a`, elapsed time `19500`, and the schema-injected defaults `status="verified"` and `schema_version=1`.

The ChatGPT execution audit reported two attempts to call internal `read` through `dsh_tool_call` with absolute workspace paths being rejected by a platform pre-dispatch safety check before entering DSH. Retrying the exact same allowed files with workspace-relative paths succeeded. This did not alter the meta-only proof, but it is a real integration boundary: ChatGPT-facing instructions and future UX should prefer workspace-relative paths when possible instead of assuming an absolute path will always reach DSH.
