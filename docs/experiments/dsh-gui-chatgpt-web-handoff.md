# P6 handoff: DSH GUI ↔ ChatGPT Web bridge

Status: historical debugging handoff. The isolated exact-three live acceptance passed on 2026-09-08; the current three-part acceptance contract and evidence are recorded in `docs/experiments/dsh-gui-chatgpt-web.md`.

Branch: `experiment/dsh-gui-chat-resume`

## Goal

Use the official DSH Web GUI as the primary human-facing surface while keeping the real ChatGPT Web conversation as the only primary reasoning agent and DSH as the harness/runtime authority. Long tasks must be able to continue across multiple ChatGPT turns without the human manually typing `continue` each time.

## Proven working

### B1 outbound transport is proven in the real ChatGPT Web host

The DSH GUI can queue a message into the shared DSH bridge mailbox. A rendered MCP Apps companion automatically performs:

```text
poll -> begin_send -> ui/message -> ack
```

A real production acceptance inserted a user message into the same ChatGPT conversation and automatically started the next model turn. The turn returned `B1_AUTO_RELAY_OK_1` with no ChatGPT-side send/click action.

Important consequence: sending the next turn is solved. Do not replace B1 with DOM clicking or a timer.

### Mechanical multi-turn controller is implemented

Bridge v4 contains a DSH-GUI-only continuation controller. It can only enqueue one fixed continuation when all of these invariants hold:

```text
turn_completed
+ same conversation
+ companion healthy
+ observer healthy and not degraded
+ selected task_state is active
+ task_state revision advanced during this turn
+ no outbound message already active
+ this turn key has never been continued
```

It fails closed on completed/paused tasks, stale task revision, duplicate turn, conversation drift, observer blocker/degradation/error, missing transport health, or queue ambiguity. It cannot reason about the goal and cannot mutate `task_state`.

Protocol tests simulate three automatic continuations and then stop when the task becomes `completed`.

## Historical blocker: B2 read observer had not connected on the user's Firefox

B2 is deliberately read-only. Its job is only to observe the real ChatGPT Web page and emit:

```text
observer_ready
observer_heartbeat
turn_started
assistant_message
turn_completed
blocked / bridge_degraded / error
```

The server-side bridge, DSH GUI observer route, and controller are live, but the production bridge currently reports:

```text
observer.observerId = null
observer.lastSeenAt = null
observer.conversationId = null
```

At that checkpoint no Observer event had reached DSH yet, so Auto Continue correctly remained disabled. This blocker was resolved later; see the current acceptance record in `dsh-gui-chatgpt-web.md`.

## Live deployment state

Production DSH was successfully upgraded to:

```text
DSH = 0.1.2-rc.1
commit = 696dde5dcfc04795e19da016e0de3642ab242be1
bridge = v4
```

The later commits below are Firefox-extension-only fixes and have **not required another server deployment**:

- `cdf5112895a825ebea57aeec7b0e901c1da44850` — remove an invalid DSH HTML marker check from the localhost relay. Production DSH loads the bridge client dynamically, so the marker was not present in `document.scripts`.
- `3d0d82f98a9119c8969c4f89874f093e6d0a8f58` — remove brittle Firefox `runtime.Port.sender` URL checks; rely on extension-owned content-script scope instead.
- `d58f25faed99440e25abf627a285bcfdf976f5c1` — explicitly add Firefox MV3 `host_permissions` for `https://chatgpt.com/*`, `http://127.0.0.1:3080/*`, and `http://localhost:3080/*`.

Exact-HEAD CI for `d58f25f` passed 5/5 (`34078082860`). Full local gate passed 200 Python tests + 14 bridge/controller JS tests + private-identifier checks.

## What has already been ruled out

Do not repeat these investigations without new evidence:

1. **Server not running / wrong bridge version** — ruled out. Production bridge v4 is reachable at the tunneled DSH GUI.
2. **B1 companion transport fundamentally broken** — ruled out by `B1_AUTO_RELAY_OK_1`.
3. **Production DSH HTML contains a bridge marker usable by the relay** — false; production HTML had `marker-in-html=no` and `inline-script-count=0`. The relay check was removed.
4. **Firefox `runtime.Port.sender.url` is a reliable authorization seam** — not portable enough; removed.
5. **Host permissions are absent from the current Firefox extension** — no longer true. Firefox Add-ons → Permissions & Data showed all three requested site permissions enabled.
6. **More blind page refreshes are useful** — no. After reinstall/reload and explicit host permissions, DSH still received zero Observer events.

## Current user-side topology

The user's Firefox is on a local Ubuntu desktop, not the cloud host. Production DSH GUI is reached through a local SSH tunnel and opened as:

```text
http://127.0.0.1:3080
```

A temporary Firefox extension is loaded from the local copy of:

```text
browser-extension/dsh-chatgpt-web-observer/
```

Firefox's Add-ons permissions page visibly shows enabled access for:

```text
https://chatgpt.com
http://127.0.0.1:3080
http://localhost:3080
```

Despite that, the DSH Bridge panel still shows `Read observer: Waiting / not connected`.

## Required next debugging approach

The user is moving a remote-worker capability from `local-shell-mcp` and intends to invite the local Ubuntu desktop so the agent can inspect the machine directly. Once that capability exists, stop asking the user to perform repetitive manual diagnostics. Use the remote worker to inspect the following end-to-end chain directly:

```text
ChatGPT tab
  -> chatgpt-observer.js loaded?
  -> runtime.connect('chatgpt-observer') alive?
  -> background.js receives observer port/message?
  -> dsh-gui-relay.js loaded on 127.0.0.1:3080?
  -> runtime.connect('dsh-gui-relay') alive?
  -> background broadcasts event?
  -> localhost relay window.postMessage fires?
  -> DSH client listener receives message?
  -> POST /plugins/chatgpt-web-bridge/observer succeeds?
  -> bridge status gains observerId/lastSeenAt?
```

Prefer direct Firefox/extension diagnostics over more speculative code changes. In particular:

- inspect temporary-extension background console and page content-script consoles;
- verify content scripts are actually injected into both tabs;
- inspect `browser.runtime` port creation/disconnect errors;
- inspect page `window.postMessage` traffic on the DSH GUI;
- inspect network requests to `/plugins/chatgpt-web-bridge/observer`;
- inspect whether Firefox private-container/site-isolation behavior affects temporary extension injection;
- only modify code after locating the exact broken hop.

If the extension architecture itself proves unreliable in Firefox, keep the architectural invariant: **B1 official `ui/message` remains outbound; B2 remains read-only receive observation.** A replacement B2 transport may be considered, but do not regress to blind timer auto-continue or a second reasoning agent.

## Historical acceptance checklist — now satisfied

This was the remaining checklist at the time of the handoff. The later isolated exact-three run satisfied the transport, progress, and continuation requirements; see `dsh-gui-chatgpt-web.md` for the canonical result:

1. Observer reports a fresh `observer_ready` and heartbeat for the current conversation.
2. A real generated turn produces `turn_started -> assistant_message -> turn_completed` without false completion.
3. A dedicated active `task_state` is armed from DSH GUI.
4. At least 3–5 ChatGPT turns continue automatically with no ChatGPT-side human interaction.
5. Each working turn advances `task_state.revision`.
6. Final `task_state.complete` stops the controller automatically with an empty outbound queue.

## Key references

- B1 automatic outbound: `50e4672c0c52f9876223be1a29fb8b5528c43b86`, CI `34038910997`
- B2 initial observer: `5ed05059dfb72ef1a5d24f74475ad728246142cc`, CI `34041468362`
- v4 multi-turn controller / deployed server: `696dde5dcfc04795e19da016e0de3642ab242be1`, CI `34043883412`
- Firefox relay marker fix: `cdf5112895a825ebea57aeec7b0e901c1da44850`
- Firefox port metadata fix: `3d0d82f98a9119c8969c4f89874f093e6d0a8f58`
- Firefox host permissions fix: `d58f25faed99440e25abf627a285bcfdf976f5c1`, CI `34078082860`
- Main experiment notes: `docs/experiments/dsh-gui-chatgpt-web.md`
