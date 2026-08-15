# Community skill acceptance

This is the product-level proof that DSH Skills can change how an already-connected ChatGPT Web agent works without changing the connector's public MCP tool surface.

## Representative skill

The recorded live run used the independent public `diagnosing-bugs` skill from `mattpocock/skills`, pinned to commit `8b78b531ab965735c5dc74f6f7a219e1e37326df`.

The skill is a normal filesystem `SKILL.md` bundle. It was copied unchanged into `$DSH_HOME/skills/diagnosing-bugs`; the gateway contains no skill-specific wrapper or prompt text.

Review third-party skill instructions before installation. Skills are instructions given to the reasoning agent and therefore belong inside the same trust boundary as other agent guidance.

## Hot-load procedure

1. Start the normal DSH Harness and OAuth gateway in default meta-only mode. Confirm the public MCP `tools/list` contains exactly `dsh_tool_catalog`, `dsh_tool_call`, `dsh_skill_catalog`, and `dsh_skill_load`, with `tools.listChanged=false`.
2. Connect ChatGPT Web and establish T0 with the DSH SkillRegistry empty.
3. Without refreshing/reconnecting ChatGPT and without restarting either the gateway or DSH, add the reviewed skill bundle under `$DSH_HOME/skills`.
4. Wait for DSH SkillFilesystem's native watcher to invalidate discovery. Confirm the live DSH bridge reports the new model-invocable skill.
5. From the same ChatGPT connector, call the already-approved `dsh_skill_catalog` and then `dsh_skill_load`.
6. Use the loaded instructions on an observable task whose workflow differs materially when the skill is followed. Keep all computer actions behind `dsh_tool_call` so ChatGPT remains the reasoning agent and DSH remains the Harness.
7. Re-check public `tools/list`: the connector must still expose only the four stable meta-tools.

## 2026-08-15 live run evidence

T0 had an empty live SkillRegistry. The DSH Harness process had already been running since `12:19:25` local host time and the gateway since `12:13:55`; neither process was restarted during the skill addition. Copying the pinned `diagnosing-bugs` bundle into `$DSH_HOME/skills` changed the native registry from zero skills to one through the filesystem watcher.

A real public OAuth/MCP check after the hot-add still returned exactly the four stable meta-tools and `tools.listChanged=false`. `dsh_skill_catalog` returned `diagnosing-bugs`, and `dsh_skill_load` returned the complete approximately 8.7-kilobyte instructions plus the native directory `resourceBase`.

The behavioral fixture was `/home/ubuntu/workspace/dsh-skill-debug-test`, baseline commit `3ad2cd4`. It contained a deterministic streaming-tool-call bug: interleaved argument fragments were appended to the most recently started call instead of the logical call identified by stable `index`.

The ChatGPT Web run followed the loaded skill's diagnostic ordering rather than immediately editing the obvious implementation. It first read `CONTEXT.md` and the relevant ADR, established and repeatedly ran the red-capable `python3 scripts/repro_interleaving.py` loop, minimized the failing event sequence, ranked four falsifiable hypotheses, used probes to confirm the single-active-slot hypothesis, added a regression test before the fix, then replaced `active_slot` with an `index -> slot` mapping and re-ran the original loop and test suite.

Independent host verification confirmed:

- the same regression input fails against baseline `3ad2cd4` with call arguments `A` and `BC`, while the required result is `AC` and `B`;
- the current working-tree fix returns `AC` and `B`;
- `PYTHONPATH=src python3 -m unittest discover -s tests -v` runs three tests and all pass;
- the original repro prints `PASS`;
- a separate sparse-index probe using `7, 42, 7` routes fragments correctly;
- a continuation for an unknown index still raises the original validation error;
- `git diff --check` passes and no `[DEBUG-...]` instrumentation remains.

The fixture intentionally remained uncommitted so the acceptance run could inspect the exact behavior diff. The production repository records the evidence, not the fixture's throwaway repair commit.

## Pass criterion

The Skill gate passes when a previously absent, independently authored DSH-compatible `SKILL.md` becomes discoverable and loadable by an already-connected ChatGPT Web conversation through the stable skill meta-tools, changes the agent's observable workflow, and leaves the public MCP surface unchanged.
