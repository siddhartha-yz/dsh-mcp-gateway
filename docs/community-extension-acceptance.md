# Community extension acceptance

This is the product-level proof for the repository contract in [`../AGENTS.md`](../AGENTS.md). It deliberately assumes ChatGPT has already approved and cached the connector's MCP tool snapshot.

## Representative extension

Use `dsh-find-plugin` `0.3.6` from `awesome-dsh-plugin/dsh-find-plugin`, pinned to commit `e75dc2e865c3cfbfd336f7b4bb753fec25d373e1` for the first recorded acceptance run.

It is a useful compatibility specimen because it is an independent community package, declares a normal DSH bundle patch, injects only the DSH `tools` service, and registers `find_dsh_plugin` through `ctx.tools.register(defineTool(...))`. The gateway must not contain any code that names or imports this plugin.

Review the pinned source before installation. Community plugins are third-party code.

## Frozen-snapshot procedure

1. Start the pinned DSH runtime with `dsh-bridge-plugin` and the normal preset composition. Start the OAuth MCP gateway in `--dsh-harness-url` mode.
2. Connect a normal ChatGPT Web conversation. Record the connector's initial visible tool snapshot and confirm the four stable meta-tools are present: `dsh_tool_catalog`, `dsh_tool_call`, `dsh_skill_catalog`, and `dsh_skill_load`.
3. Confirm `find_dsh_plugin` is absent from that initial snapshot and absent from `dsh_tool_catalog`.
4. Without editing, re-publishing, re-approving, reconnecting, or refreshing the ChatGPT connector, install the pinned community extension into the DSH profile used by the bridge. Restart only the DSH process if the DSH plugin installer requires it; the gateway and ChatGPT connector stay unchanged.
   `dsh plugin --profile web add 'github:awesome-dsh-plugin/dsh-find-plugin#e75dc2e865c3cfbfd336f7b4bb753fec25d373e1'`
5. From the same ChatGPT connector, call the already-approved `dsh_tool_catalog`. It must now report `find_dsh_plugin` with its live DSH schema.
6. Call the already-approved `dsh_tool_call` with `name="find_dsh_plugin"` and a harmless query such as `terminal`. The result must come through DSH `ToolRuntime.execute(...)` and be visible to ChatGPT.
7. Verify there is still no plugin-specific wrapper in this repository. A first-class `find_dsh_plugin` entry may appear if ChatGPT honors `tools/list_changed`, but that is optional and must not be needed for steps 5-6.
8. Record the exact DSH version, plugin version/commit, gateway commit, initial tool snapshot, catalog result, generic-call result, and whether ChatGPT happened to refresh first-class tools.

## Pass criteria

The acceptance run passes only when the same already-connected ChatGPT Web connector discovers and successfully invokes the newly installed community capability through stable meta-tools. Dynamic first-class projection is reported separately as client behavior, not as a release dependency.

Unit coverage in `tests/test_harness_bridge.py` reproduces the frozen-snapshot semantics without a live ChatGPT client; this document is for the real product-level run.

## 2026-08-15 live run evidence

The first live run established a frozen T0 catalog of 25 DSH tools with `find_dsh_plugin` absent. The reviewed plugin was then installed at the pinned commit through DSH's native `plugin --profile web add` path and only the DSH Harness process was restarted. T1 contained 26 tools and `find_dsh_plugin` was present.

Using the already-issued OAuth access grant against the unchanged gateway, `dsh_tool_catalog` discovered `find_dsh_plugin` and `dsh_tool_call` successfully invoked it through DSH ToolRuntime, returning live plugin search results. No plugin-specific gateway wrapper was added. The remaining product-level confirmation is the same operation from the user's already-connected ChatGPT Web conversation without refreshing its connector tool snapshot.
