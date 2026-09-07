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

## B1 implementation checkpoint — DSH GUI half proven

The first half of B1 has now been implemented and exercised without touching the production DSH service.

A repo-local dual-face DSH plugin lives at `dsh-chatgpt-web-bridge-plugin/`:

- its browser half declares the official `dsh.client` package metadata and registers a `ChatGPT Web Bridge` control in the shipped `sidebar.footer.action` slot;
- its host half owns a bounded in-memory outbound mailbox plus companion lifecycle events;
- GUI-originated outbound messages are leased with a claim TTL and require explicit `sent`/`failed` acknowledgement;
- the companion-facing ToolRuntime surface is mechanical only (`status`, `poll`, `heartbeat`, `ack`, `publish`) and cannot invoke a model or choose the next action;
- the browser-side mailbox routes use a per-process page capability token and bounded JSON bodies.

An isolated official `dsh web` profile was started on loopback using a separate `DSH_HOME` and pnpm store. DSH's own client-module loader automatically served and loaded the repo-local `client.js`; no frontend fork or replacement Vite app was required. Browser acceptance through the existing P5 `browser_session` proved that the official GUI rendered the Bridge control and panel, and clicking `Queue message` produced HTTP 201 and immediately changed the visible queue count from `0 active` to `1 active`.

This proves the intended GUI extension strategy is viable: P6 can stay inside the official DSH Web/theme/plugin ecosystem rather than maintaining a separate frontend.

## B1 implementation checkpoint — real `ui/message` proven

The opt-in MCP App companion was deployed on the experiment branch and exercised in a real ChatGPT Web conversation.

One ChatGPT product detail matters for the bridge lifecycle: the connected plugin did not automatically hot-refresh its MCP tool snapshot after deployment, but using the plugin settings **Refresh** action updated the same conversation from four model-visible tools to five. A fresh conversation is therefore not intrinsically required after an experimental tool-surface change; an explicit connector refresh is sufficient.

The initial companion rendered a deliberately manual `Send follow-up probe` control. Clicking it sent the exact standard MCP Apps host request `ui/message`; ChatGPT inserted the supplied user message into the current real conversation and immediately started another model turn. The resulting assistant turn returned the expected `B1_UI_MESSAGE_OK` marker. This proves the central B1 assumption: a rendered MCP App can start a genuine next ChatGPT Web turn without DOM automation or direct undocumented ChatGPT backend calls.

The manual button was only a transport proof and is not part of the intended architecture.

## B1 implementation checkpoint — automatic outbound relay

After the real host proof, the companion was changed from a button-driven probe into an automatic transport relay.

The experiment path is now:

```text
DSH Web GUI
  -> DSH-owned bridge mailbox
  -> app-only MCP transport tool
  -> rendered companion polls automatically
  -> ui/message
  -> real next ChatGPT Web turn
```

The production DSH Host patch on this experiment branch now loads `dsh-chatgpt-web-bridge-plugin/` directly, so the visible DSH Web GUI and the public gateway share the same in-process mailbox instead of using separate isolated test stores. The DSH Harness bridge explicitly reviews and exposes `chatgpt_web_bridge` to the gateway. The gateway then exposes a separate `chatgpt_web_bridge_transport` MCP tool with `visibility=["app"]`; it is callable by the companion but is not intended to expand the model-facing tool surface.

The rendered companion has no message textarea and no send button. After `ui/initialize` it requires both `hostCapabilities.message` and app-initiated `serverTools`, publishes `companion_ready`, and continuously uses non-overlapping `setTimeout` polls. When the DSH GUI queues a message, it automatically performs:

```text
poll
-> begin_send
-> ui/message
-> ack(sent | failed)
```

`begin_send` introduces a fail-closed `dispatching` state. Ordinary un-dispatched claims may expire and return to the queue, but once a message is about to cross the `ui/message` boundary it never automatically requeues. If the iframe disappears after ChatGPT accepted the message but before the acknowledgement reaches DSH, the queue remains visibly `dispatching` rather than risking a duplicate ChatGPT turn.

The companion remains transport-only: it contains no model invocation, task planner, AgentLoop, provider API, shell, or autonomous reasoning logic. Its resource declares an explicit empty external-resource CSP because the current implementation is self-contained.

### B1 real-host acceptance result

B1 outbound has now been proven end-to-end in the real ChatGPT Web host. With the v2 companion rendered once, a message queued through the production DSH GUI bridge path was automatically claimed, moved into fail-closed `dispatching`, forwarded through official `ui/message`, acknowledged `sent`, inserted as a real user message in the same ChatGPT conversation, and started the next model turn without any ChatGPT-side click. The turn returned the expected `B1_AUTO_RELAY_OK_1` marker. The companion heartbeat also remained live across the resulting ChatGPT turn, so the iframe was not destroyed merely because another model turn ran.

The official Apps receive surface is nevertheless insufficient for safe automatic continuation. The App receives tool-input/result/cancellation and host-context lifecycle, while `ui/message` hands the follow-up back to the host; the subsequent assistant transcript and a reliable assistant-turn-completed notification are not exposed back to the App. Therefore B1 is retained as the preferred outbound transport, but it is not used to guess completion on a timer.

## B2 implementation checkpoint — minimal read-only observer prepared

B2 keeps the proven B1 send path unchanged and adds only the missing receive side. The experimental observer lives at `browser-extension/dsh-chatgpt-web-observer/` and follows this path:

```text
hidden chatgpt.com tab
  -> read-only content script
  -> extension background relay
  -> localhost DSH GUI content script
  -> window.postMessage
  -> DSH client plugin
  -> authenticated local bridge observer route
```

The ChatGPT content script observes semantic DOM signals only. It uses `MutationObserver` plus a low-rate watchdog to identify generation start, a completed assistant message, completion, retry/error ambiguity, and SPA conversation-path changes. It never clicks, types, submits, invokes a model, or performs a network request. If the expected assistant/turn identity cannot be established after generation stops, it emits `bridge_degraded` instead of declaring completion.

The extension itself never receives the DSH bridge capability token. Firefox MV3 host access is explicitly limited to `https://chatgpt.com/*`, `http://127.0.0.1:3080/*`, and `http://localhost:3080/*`. The extension background accepts only its two internal named ports (`chatgpt-observer` and `dsh-gui-relay`) and performs no network requests. The localhost relay sends a bounded `window.postMessage`; only the already-loaded DSH client plugin validates that message and writes it to the authenticated local `/plugins/chatgpt-web-bridge/observer` route. Earlier experimental checks based on an inline DSH HTML marker and Firefox `runtime.Port.sender` URL metadata were removed after live testing showed they were not reliable seams.

Bridge store version 3 keeps observer identity/state separate from the B1 companion. `observer_heartbeat` updates liveness without filling lifecycle history, while durable observation events include `observer_ready`, `turn_started`, `assistant_message`, `turn_completed`, `blocked`, `bridge_degraded`, and `error`. The DSH Bridge panel now has a separate Read observer status card and labels companion/observer event sources distinctly.

The initial B2 static/protocol gate passes 10/10 bridge tests plus syntax checks for all extension and bridge scripts. Live acceptance is intentionally still pending: deploy bridge v3, temporarily load the extension in the user's Firefox, refresh the ChatGPT and DSH GUI tabs, verify `observer_ready`/heartbeats, and then run a real generated turn to confirm `turn_started -> assistant_message -> turn_completed` without false completion. Only after that evidence should mechanical auto-continuation be enabled.

## Multi-turn continuation controller checkpoint

The bridge now has a mechanical controller for the original long-running-task requirement. It does not inspect the goal text or make planning decisions. The DSH GUI must explicitly arm it with one `task_state` id; the model-facing bridge tool can report controller status but cannot arm or stop it.

On each B2 `turn_completed` event, the controller checks only lifecycle/state invariants:

```text
same ChatGPT conversation
+ companion healthy
+ read observer healthy and not degraded
+ no outbound message already active
+ selected task_state is still active
+ task_state revision advanced during the completed turn
+ this turn key has never been continued
=> enqueue one fixed B1 continuation message
```

`completed` stops with `task_completed`; `paused` stops with `task_paused`. Conversation drift, blocked/error/degraded observer state, stale task revision, missing transport health, or queue ambiguity all stop fail-closed. Controller armed state is intentionally in-memory and defaults to disabled after DSH Host restart.

`task_state` exposes the controller only a Cordis-internal read-only service with `get`; it exposes no mutation methods. ChatGPT remains responsible for deciding whether the goal is complete and updating `task_state` before ending each turn. The controller merely checks the recorded status/revision and starts another ChatGPT Web turn through the already-proven B1 `ui/message` path.

Protocol tests now simulate three consecutive completed turns, settle each automatically queued continuation through the companion mailbox, then change the task to `completed`; the fourth completion event stops the controller with exactly three continuations and no remaining outbound message. A separate regression proves that an active task whose revision did not advance causes `task_state_not_advanced` instead of an unbounded loop.

An isolated real DSH Web runtime also booted bridge version 4 with the new hard read-service dependency and returned a disabled controller in `/state`. Live ChatGPT acceptance is still required: prove B2's DOM lifecycle signal first, then arm a dedicated test task and demonstrate at least 3-5 real automatic turns ending because the task becomes completed.

Current live-debug handoff, including the Firefox observer blocker, ruled-out hypotheses, exact commits/CI, and the next direct-local-machine debugging procedure, is recorded in `docs/experiments/dsh-gui-chatgpt-web-handoff.md`.
