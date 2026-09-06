# Experiment: DSH Web GUI as the primary user surface for ChatGPT Web

Status: experimental design, not production architecture.

Branch: `experiment/dsh-gui-chat-resume`

## Goal

Make the official DSH Web GUI the surface the human actually uses while keeping **ChatGPT Web chat as the primary reasoning agent** and **DSH as the harness/runtime authority**.

The user-facing requirement is stronger than merely auto-clicking `continue`:

```text
Human
  -> DSH Web GUI
       -> ChatGPT-Web bridge
            -> hidden/background ChatGPT Web conversation
                 -> ChatGPT reasoning
                 -> stable dsh_* meta tools
                      -> DSH Harness
```

It is acceptable for a real ChatGPT Web tab/window to exist in the background. It does not need to be the visible UI. The experiment succeeds if the human can primarily stay in DSH Web GUI while ChatGPT Web remains the reasoning plane.

This branch deliberately does **not** switch to Workspace Agents API, Responses API, or another direct model/provider API. Those approaches change the reasoning plane and violate the current project principle that the main agent is ChatGPT Web chat.

## Why start from the official DSH Web GUI

DSH already ships a real Web GUI through `dsh web`; this project should modify that surface rather than build a replacement frontend.

The pinned DSH `0.1.2-rc.1` runtime is already decomposed into browser-side plugins. The shipped Web graph includes, among others:

- `@deepseek-ai/dsh-client-modules`
- `@deepseek-ai/dsh-client-connection`
- `@deepseek-ai/dsh-client-ui-theme`
- `@deepseek-ai/dsh-client-ui-layout`
- `@deepseek-ai/dsh-client-ui-conversation`
- `@deepseek-ai/dsh-client-ui-chat`
- `@deepseek-ai/dsh-client-ui-sidebar`
- `@deepseek-ai/dsh-client-ui-settings-plugin-inventory`
- `@deepseek-ai/dsh-client-hmr`

The Web bundle explicitly describes a browser plugin roster and serves `/plugins/<id>/client.js`. This makes a small client plugin/overlay a better test vehicle than a fork of the whole DSH frontend. It also preserves DSH's own UI/theme/plugin ecosystem as much as possible.

Tentative UI-side shape:

```text
DSH Web GUI
  + repo-local ChatGPT bridge client plugin
  + normal DSH theme/layout/conversation/tool views
  + bridge status / hidden-host status / resume controls
```

The first prototype should avoid rewriting the official chat UI. Prefer adding or replacing the smallest transport/control seam that can prove the architecture.

## Requirements

A useful prototype should eventually provide:

1. DSH Web GUI is the visible primary panel.
2. A ChatGPT Web conversation remains the reasoning agent.
3. DSH tool calls still travel through the existing stable `dsh_*` meta-tool bridge.
4. A long engineering task can continue for multiple ChatGPT turns without the human repeatedly typing `continue`.
5. The bridge knows whether a ChatGPT turn is running, finished, blocked, or failed; it must not blindly send on a timer.
6. DSH task/shell/browser state remains observable in the DSH GUI.
7. Ideally, user messages and assistant text are mirrored into the DSH GUI so ordinary interaction no longer requires opening the ChatGPT tab.
8. The bridge is transport/control only. It must not introduce another model, reasoning loop, or second harness.

## Transport fallback ladder

The experiment should proceed by increasing invasiveness only when the cleaner mechanism is insufficient. Failure of one layer is not failure of the overall idea.

### B1 — Hidden ChatGPT Web + official MCP App host bridge

Use a minimal companion MCP App inside the real ChatGPT conversation. The app communicates with DSH through a small mailbox/event channel and uses the official MCP Apps host bridge to send a follow-up message.

Relevant existing LSM mechanism:

```ts
await app.sendMessage({
  role: "user",
  content: [{ type: "text", text: "Continue the current task." }],
})
```

The underlying host request is `ui/message`.

Proposed path:

```text
DSH GUI
  -> DSH bridge mailbox: outbound message / continue request
  -> companion MCP App running in hidden ChatGPT tab
  -> app.sendMessage(...)
  -> next ChatGPT Web turn
```

Advantages:

- uses an official host bridge rather than simulating clicks;
- keeps the real ChatGPT conversation and account/session semantics;
- can potentially solve the recurring manual `continue` problem with very little ChatGPT-page coupling.

Known limitation to probe first:

- `ui/message` clearly provides an outbound follow-up path, but it is not yet established that an independent DSH GUI can obtain the full assistant transcript/turn lifecycle through official MCP App events alone.

Therefore B1 is considered complete only if it can support enough bidirectional state for the intended DSH GUI. If outbound continuation works but assistant output is not available, keep B1 for sending and move only the missing receive side to B2.

Minimum B1 acceptance probe:

1. one ChatGPT Web conversation is open in the background;
2. DSH GUI/backend asks the companion app to send a follow-up;
3. ChatGPT starts a real new turn;
4. the new turn can still invoke DSH meta tools;
5. repeat automatically for at least 3-5 sequential turns based on explicit turn completion, not a fixed timer;
6. verify whether assistant text and turn-completion state can be mirrored back without DOM injection.

### B2 — Hybrid: official outbound bridge + minimal injected observer

If the official host bridge can reliably send messages but cannot expose enough assistant-side state, retain `ui/message` for outbound traffic and inject only the smallest read-side observer into the hidden ChatGPT page.

```text
outbound: DSH GUI -> MCP App -> ui/message
inbound:  ChatGPT page -> injected observer -> DSH bridge -> DSH GUI
```

The injected observer should initially be read-only. It may watch:

- current conversation identity;
- whether generation is active;
- completed assistant message text;
- error/retry/interruption states;
- appearance of the next stable turn.

This is preferred over full page automation because the fragile page coupling is limited to observation while message submission still uses the official host interface.

Acceptance requirement: DOM/UI changes should fail closed and visibly report `bridge_degraded`, not silently send duplicate messages.

### B3 — Full ChatGPT front-end injection bridge

If official MCP App facilities cannot provide a workable independent control surface, run the real ChatGPT Web frontend in a hidden/background browser and inject a dedicated bridge script/extension into that page.

The injected layer acts as an adapter between ChatGPT's visible conversation UI and DSH:

```text
DSH GUI <-> local/secure bridge <-> injected ChatGPT page adapter
```

Responsibilities may include:

- place user text into the actual ChatGPT composer and submit it;
- detect streaming/generation start and completion;
- mirror assistant text and errors into DSH GUI;
- preserve the real conversation rather than emulating it in DSH;
- issue `continue` only after a proven completed turn and only while the DSH task remains active;
- expose a health/version probe so selector or frontend breakage is obvious.

Implementation preference:

- browser extension/userscript/content-script or an equivalent explicit page adapter;
- semantic DOM/state detection rather than screen coordinates;
- mutation/event driven observation rather than polling where possible;
- stable fallbacks for selectors;
- idempotency keys so reconnects do not duplicate user messages;
- no direct dependence on undocumented ChatGPT backend endpoints unless a later experiment proves there is no safer route.

This is the most likely route to a genuinely independent-looking DSH GUI if official host APIs are insufficient.

### B4 — Browser automation against the hidden real frontend

If page injection itself is impractical, use a controlled browser automation layer against the hidden ChatGPT Web tab. This can be Playwright/CDP-style automation, but it must be implemented as a state machine, not as `sleep -> click continue`.

State examples:

```text
idle
sending
running
completed
blocked
retryable_error
fatal_bridge_error
```

A continuation is legal only when all of the following hold:

- previous turn is conclusively complete;
- no send/generation is currently active;
- the associated DSH task is still active and requests continuation;
- the intended conversation identity matches;
- the exact continuation message has not already been sent for that turn.

This route is more fragile than B1-B3 because it depends on the actual page DOM and interaction behavior, but it is still much more robust than a timer-driven auto-click script.

### B5 — Dumb auto-`continue` script as disposable proof only

A script that periodically finds the composer and sends `continue` is allowed only as a short-lived diagnostic to answer questions such as "does another ChatGPT turn actually resume useful work?".

It is not an acceptable final transport because it cannot reliably distinguish:

- a still-running turn;
- a completed turn;
- an approval/question that needs human input;
- a transient frontend/network error;
- the wrong conversation/tab;
- a duplicated continuation.

If used at all, keep it out of production and replace it as soon as a higher layer is viable.

## Automatic continuation policy

The bridge must not implement autonomous reasoning. Its continuation logic is mechanical lifecycle control only.

Suggested rule:

```text
if ChatGPT turn completed
and DSH task is active
and task checkpoint says work remains
and no human-input blocker is present
and this turn has not already been continued
then send a fixed continuation message
else wait
```

The continuation message should be intentionally low-intelligence, for example:

```text
Continue the current task from the latest DSH task state. Do not stop merely to report progress; stop only when the task is complete or human input is genuinely required.
```

ChatGPT still decides what to do next. DSH still executes/guards tools. The bridge only starts the next ChatGPT turn.

## DSH GUI modification strategy

Do not fork or redesign the entire DSH Web frontend during the experiment.

Preferred sequence:

1. Start an isolated test instance of official `dsh web` on a non-production port/profile.
2. Add one repo-local browser/client plugin for ChatGPT-bridge status and controls.
3. Reuse the existing DSH conversation/layout/theme/tool surfaces.
4. Add only the minimum UI needed to select/link a ChatGPT conversation, show bridge health, send a message, and enable/disable automatic continuation.
5. Only after the transport works, decide whether to replace the DSH chat composer/transcript transport so the DSH GUI can act as the full daily chat surface.

This preserves the ability to adopt DSH UI/theme/plugin changes from upstream instead of maintaining a permanent frontend fork.

## Separation from production

This experiment must stay isolated from the stable deployment until explicitly promoted.

- Work only on `experiment/dsh-gui-chat-resume`.
- Do not change the production `main` deployment merely to test GUI ideas.
- Use a separate port/profile/service or local development process for DSH Web GUI experiments.
- Do not overwrite the current production ChatGPT bridge or `dsh_*` meta-tool behavior.
- Any ChatGPT-page injection must be removable without changing DSH runtime state.

## Explicit non-goals

For this experiment, do not pursue:

- OpenAI Responses API as the main reasoning transport;
- Workspace Agents API as the main reasoning transport;
- a DSH-owned LLM AgentLoop replacing ChatGPT Web;
- a second provider/model inside the gateway;
- a full custom GUI built from scratch;
- direct undocumented ChatGPT backend API reverse engineering as the first approach;
- timer-only auto-clicking as the final architecture.

## First implementation milestone

Before polishing any GUI, prove the transport on the least invasive path.

Milestone `B1-probe`:

1. launch an isolated official DSH Web GUI instance;
2. add a minimal bridge panel/client plugin;
3. keep one real ChatGPT Web conversation alive in a background tab/window;
4. connect a minimal companion MCP App to a DSH mailbox/event channel;
5. from the DSH GUI, request one `ui/message` follow-up into that conversation;
6. prove the new ChatGPT turn can invoke the existing DSH meta tools;
7. determine exactly what assistant text/lifecycle data the official app bridge exposes;
8. repeat across 3-5 turns without manual `continue`;
9. record whether B1 is sufficient, requires B2 for receive-side observation, or should be abandoned for B3.

Only after this probe should the branch commit to a larger implementation.
