# Repository architecture contract

This repository exists to **give ChatGPT Web a mature DSH Harness**.

This is the highest-priority product invariant. If an implementation, document, test, deployment plan, or proposed feature conflicts with it, change that work rather than changing this invariant unless the repository owner explicitly revises the product direction.

## Canonical data path

```text
ChatGPT Web Chat
    |
    | MCP over the public access layer
    v
ChatGPT <-> DSH adapter
    |
    v
DSH Harness / Runtime
    |
    +-- DSH tools
    +-- DSH skills / community extensions
    +-- DSH sessions / jobs / policy / MCP clients
    `-- optional execution providers
            |
            +-- local machine capabilities
            `-- selected local-shell-mcp capabilities when useful
```

## Responsibility split

### ChatGPT

ChatGPT Web is the only primary reasoning/model agent. The project must not require a second LLM provider to perform normal work.

### This repository

The primary product is the thinnest practical MCP/access adapter that exposes a DSH-managed capability surface to ChatGPT Web. Installing a compatible DSH community extension should, ideally, make that capability available to ChatGPT without writing a bespoke ChatGPT wrapper for every extension.

ChatGPT clients may keep an approved MCP tool surface as a snapshot and may not immediately adopt later `tools/list_changed` additions. Therefore extension availability must not depend on dynamic first-class tool refresh. A small stable set of meta-tools (catalog/discovery + generic invocation) is the correctness path. First-class projection of individual DSH tools is a best-effort UX optimization only.

The tool catalog consumed by those meta-tools is an explicit DSH-side external ChatGPT capability profile, not a raw dump of every ToolRuntime entry. Tools whose semantics depend on DSH owning the AgentLoop/session lifecycle must remain unavailable to ChatGPT until their external-agent semantics are reviewed. Discovery and execution must enforce the same profile, and the Python OAuth gateway must not duplicate that allowlist. Community ToolRuntime capabilities may be added through an explicit DSH-bridge opt-in after review; DSH AgentLoop lifecycle tools are not eligible for that generic escape hatch.

The shipped/default gateway mode is **meta-only**: its ChatGPT-facing MCP tool list stays fixed to the stable DSH meta-tools and it does not advertise a tool-list change subscription. Dynamic first-class projection must require explicit operator opt-in and must remain a separately tested UX mode.

The adapter may also contain the public-access engineering required by ChatGPT Web, such as OAuth integration, MCP transport glue, capability projection, and compatibility code that cannot live inside DSH itself.

### DSH

DSH is the harness/runtime authority. Prefer DSH-native abstractions for tools, skills, sessions, jobs, policy, MCP clients, lifecycle, and other harness concerns when they exist and are suitable.

Do not independently rebuild a competing harness in the gateway merely because local-shell-mcp or an earlier prototype had such code.

### local-shell-mcp

local-shell-mcp is not the primary harness. Its highest-value contributions to this project are reference implementations and selected differentiated execution capabilities, especially:

- public-tunnel deployment patterns;
- OAuth / remote-MCP integration patterns for ChatGPT Web;
- remote worker design and implementation ideas;
- browser or other local execution capabilities that DSH does not yet cover as well.

LSM may be used as an optional execution provider behind DSH where that is advantageous. Its recent harness/runtime layer is not the architectural center of this project.

## Explicit non-goals

Do not turn the product into any of the following:

```text
ChatGPT -> gateway-owned second harness -> DSH
ChatGPT -> autonomous DSH AgentLoop/LLM -> computer
ChatGPT -> local-shell-mcp harness -> DSH
```

Normal operation must not require `DEEPSEEK_API_KEY` or another model API key. DSH must not secretly replace ChatGPT as the reasoning agent.

Automatic ChatGPT continuation / `app.sendMessage` experimentation is not a current architectural priority. It can be revisited later, but it must not drive the harness design or justify rebuilding DSH functionality in the gateway.

## Decision rule for every new capability

Before implementing a harness feature, answer in this order:

1. Does DSH already provide the abstraction or extension point?
2. Can the ChatGPT adapter expose that DSH capability generically?
3. Is local-shell-mcp useful only as an execution/access implementation or reference here?
4. Only if the capability genuinely belongs in none of those places should this repository own new runtime logic.

When in doubt, prefer integration over duplication.

## Product-level acceptance test

A central acceptance criterion is:

> Install a representative community DSH extension with little or no modification, expose it through the adapter, and successfully use that capability from a normal ChatGPT Web conversation.

The acceptance test must pass **without requiring the ChatGPT app/connector to be re-published, re-approved, or to refresh its first-class MCP tool snapshot**. Discovering the new capability through stable meta-tools and invoking it generically is sufficient for correctness. Dynamic first-class projection is tested separately as an optional client UX enhancement.

Progress that does not move toward this test should be treated as secondary unless it fixes a prerequisite or regression.
