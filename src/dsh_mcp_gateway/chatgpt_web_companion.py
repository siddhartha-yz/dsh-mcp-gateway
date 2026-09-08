"""Experimental MCP App companion for the DSH GUI <-> ChatGPT Web bridge.

The companion is intentionally transport-only.  It never invokes a model,
chooses a next action, or owns a task loop.  Once rendered in the real ChatGPT
conversation it automatically polls the DSH-owned bridge mailbox through an
app-only MCP tool and forwards queued text with the official ``ui/message``
host request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

COMPANION_RESOURCE_URI = "ui://dsh-chatgpt-web-bridge/companion-v3.html"
COMPANION_TOOL_NAME = "open_chatgpt_web_bridge_companion"
COMPANION_TOOL_NAME_V3 = "open_chatgpt_web_bridge_companion_v3"
TRANSPORT_TOOL_NAME = "chatgpt_web_bridge_transport"
BRIDGE_DSH_TOOL_NAME = "chatgpt_web_bridge"

_ALLOWED_TRANSPORT_ACTIONS = frozenset({"status", "poll", "heartbeat", "begin_send", "ack", "publish"})

# The wire shape mirrors @modelcontextprotocol/ext-apps 1.7.5 as used by the
# LSM reference implementation: initialize the iframe, call app-visible server
# tools through tools/call, and submit conversation messages through ui/message.
# Keeping this probe self-contained makes the host behavior easy to audit.
COMPANION_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSH ChatGPT Web Bridge</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; padding: 12px; background: transparent; color: inherit; }
main { display: grid; gap: 8px; }
h1 { font-size: 13px; margin: 0; }
p { font-size: 11px; line-height: 1.45; margin: 0; opacity: .72; }
.status { font-size: 12px; padding: 8px 10px; border: 1px solid currentColor; border-radius: 8px; opacity: .86; }
code { font-family: ui-monospace, monospace; }
</style>
</head>
<body>
<main>
  <h1>DSH · ChatGPT Web bridge</h1>
  <div id="status" class="status">Connecting to ChatGPT host…</div>
  <p>Automatic B1 relay. DSH owns the queue; this companion only forwards queued text through <code>ui/message</code>.</p>
</main>
<script>
(() => {
  'use strict';
  const PROTOCOL_VERSION = '2026-01-26';
  const TRANSPORT_TOOL = 'chatgpt_web_bridge_transport';
  const POLL_MS = 1500;
  const RETRY_MS = 5000;
  const statusNode = document.getElementById('status');
  const clientId = globalThis.crypto?.randomUUID?.() || `companion-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  let hostId = null;
  let candidateHostId = null;
  globalThis.addEventListener('message', (event) => {
    const value = event.data;
    if (!value || value.source !== 'dsh-chatgpt-web-observer-extension' || value.type !== 'host-instance') return;
    if (typeof value.hostId !== 'string' || value.hostId.length < 1 || value.hostId.length > 128) return;
    candidateHostId = value.hostId;
  });
  let nextId = 1;
  const pending = new Map();
  let timer = null;
  let stopped = false;

  function setStatus(text) { statusNode.textContent = text; }
  function post(message) { window.parent.postMessage(message, '*'); }

  function request(method, params, timeoutMs = 20000) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        pending.delete(id);
        reject(new Error(method + ' timed out'));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timeout });
      post({ jsonrpc: '2.0', id, method, params });
    });
  }

  function notify(method, params = {}) {
    post({ jsonrpc: '2.0', method, params });
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== '2.0') return;

    if (Object.prototype.hasOwnProperty.call(message, 'id') && !message.method) {
      const waiter = pending.get(message.id);
      if (!waiter) return;
      pending.delete(message.id);
      window.clearTimeout(waiter.timeout);
      if (message.error) waiter.reject(new Error(message.error.message || 'MCP host request failed'));
      else waiter.resolve(message.result);
      return;
    }

    if (message.method === 'ping' && Object.prototype.hasOwnProperty.call(message, 'id')) {
      post({ jsonrpc: '2.0', id: message.id, result: {} });
    }
  });

  function firstText(result) {
    const block = Array.isArray(result?.content) ? result.content.find((item) => item?.type === 'text') : null;
    return typeof block?.text === 'string' ? block.text : '';
  }

  async function callServerTool(name, args) {
    const result = await request('tools/call', { name, arguments: args }, 30000);
    if (!result || result.isError) throw new Error(firstText(result) || `${name} failed`);
    return result.structuredContent || {};
  }

  async function bridge(args) {
    const structured = await callServerTool(TRANSPORT_TOOL, args);
    if (!Object.prototype.hasOwnProperty.call(structured, 'value')) {
      throw new Error('DSH bridge transport returned no structured value');
    }
    return structured.value;
  }

  async function resolveValidatedHostId() {
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const candidate = candidateHostId;
      if (candidate !== null) {
        const status = await bridge({ action: 'status' });
        const observers = Array.isArray(status?.observers) ? status.observers : [];
        const now = typeof status?.now === 'number' ? status.now : Date.now();
        const matched = observers.find((observer) =>
          observer?.hostId === candidate
          && typeof observer?.lastSeenAt === 'number'
          && now - observer.lastSeenAt <= 15000
        );
        if (matched) {
          hostId = candidate;
          return hostId;
        }
      }
      await new Promise((resolve) => window.setTimeout(resolve, 150));
    }
    throw new Error('No validated browser-host identity from the DSH observer extension');
  }

  async function settle(messageId, outcome, error = null) {
    const args = {
      action: 'ack',
      client_id: clientId,
      host_id: hostId,
      message_id: messageId,
      outcome,
    };
    if (error) args.error = String(error).slice(0, 2000);
    return bridge(args);
  }

  async function dispatch(message) {
    if (!message || typeof message.id !== 'string' || typeof message.text !== 'string') {
      throw new Error('DSH bridge returned an invalid queued message');
    }

    // Crossing into dispatching is intentionally fail-closed.  A claimed
    // message can expire and be retried, but a dispatching message never does:
    // if this iframe dies after ChatGPT accepted ui/message and before ack, DSH
    // leaves the item visibly uncertain instead of risking a duplicate turn.
    await bridge({ action: 'begin_send', client_id: clientId, host_id: hostId, message_id: message.id });
    setStatus('Dispatching queued DSH message to ChatGPT…');
    try {
      await request('ui/message', {
        role: 'user',
        content: [{ type: 'text', text: message.text }],
      }, 30000);
    } catch (error) {
      try { await settle(message.id, 'failed', error instanceof Error ? error.message : String(error)); } catch {}
      throw error;
    }
    await settle(message.id, 'sent');
    setStatus('Connected · message accepted · waiting for DSH GUI');
  }

  function schedule(delay) {
    if (stopped) return;
    timer = window.setTimeout(pump, delay);
  }

  async function pump() {
    if (stopped) return;
    let delay = POLL_MS;
    try {
      const polled = await bridge({ action: 'poll', client_id: clientId, host_id: hostId });
      if (polled?.message) await dispatch(polled.message);
      else setStatus('Connected · automatic relay armed · waiting for DSH GUI');
    } catch (error) {
      setStatus('Bridge degraded: ' + (error instanceof Error ? error.message : String(error)));
      delay = RETRY_MS;
    }
    schedule(delay);
  }

  async function connect() {
    const result = await request('ui/initialize', {
      appCapabilities: {},
      appInfo: { name: 'dsh-chatgpt-web-bridge-companion', version: '0.0.3' },
      protocolVersion: PROTOCOL_VERSION,
    });
    notify('ui/notifications/initialized');

    const capabilities = result?.hostCapabilities || {};
    if (!capabilities.message) throw new Error('ChatGPT host does not advertise ui/message');
    if (!capabilities.serverTools) throw new Error('ChatGPT host does not advertise app-initiated server tools');

    await resolveValidatedHostId();

    await bridge({
      action: 'publish',
      client_id: clientId,
      host_id: hostId,
      event_type: 'companion_ready',
      payload: { transport: 'ui/message', relay: 'automatic', version: 2 },
    });
    setStatus('Connected · automatic relay armed · waiting for DSH GUI');
    schedule(0);
  }

  function stop() {
    stopped = true;
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  }

  window.addEventListener('pagehide', stop, { once: true });
  connect().catch((error) => {
    setStatus('Host bridge unavailable: ' + (error instanceof Error ? error.message : String(error)));
  });
})();
</script>
</body>
</html>
"""


def build_chatgpt_web_companion_apps(
    bridge_call: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> Any:
    """Build the opt-in MCP Apps extension used only by the B1 experiment."""
    try:
        from mcp.server.apps import Apps, ResourceCsp
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise RuntimeError("MCP Apps support is unavailable in the installed MCP server") from exc

    apps = Apps()

    @apps.tool(
        resource_uri=COMPANION_RESOURCE_URI,
        visibility=["model", "app"],
        name=COMPANION_TOOL_NAME,
        title="Open ChatGPT Web bridge companion",
        description=(
            "Open the experimental MCP App companion for the DSH GUI bridge. Once rendered it automatically relays "
            "DSH-queued messages through ChatGPT Web's official ui/message host request; no button press is required."
        ),
    )
    def open_chatgpt_web_bridge_companion() -> dict[str, Any]:
        return {
            "status": "armed",
            "experiment": "B1-auto-relay",
            "instruction": "Keep the companion rendered; it polls the DSH GUI bridge automatically.",
        }

    @apps.tool(
        resource_uri=COMPANION_RESOURCE_URI,
        visibility=["model", "app"],
        name=COMPANION_TOOL_NAME_V3,
        title="Open ChatGPT Web bridge companion v3",
        description="Versioned companion entrypoint used to force ChatGPT to discover the current MCP App resource.",
    )
    def open_chatgpt_web_bridge_companion_v3() -> dict[str, Any]:
        return open_chatgpt_web_bridge_companion()

    @apps.tool(
        resource_uri=COMPANION_RESOURCE_URI,
        visibility=["app"],
        name=TRANSPORT_TOOL_NAME,
        title="DSH ChatGPT Web bridge transport",
        description="App-only mechanical transport between the companion and the DSH-owned bridge mailbox.",
    )
    def chatgpt_web_bridge_transport(
        action: str,
        client_id: str | None = None,
        host_id: str | None = None,
        message_id: str | None = None,
        outcome: str | None = None,
        error: str | None = None,
        event_type: str | None = None,
        text: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action not in _ALLOWED_TRANSPORT_ACTIONS:
            raise ValueError("unsupported ChatGPT Web bridge transport action")
        arguments: dict[str, Any] = {"action": action}
        for key, value in (
            ("client_id", client_id),
            ("host_id", host_id),
            ("message_id", message_id),
            ("outcome", outcome),
            ("error", error),
            ("event_type", event_type),
            ("text", text),
            ("payload", payload),
        ):
            if value is not None:
                arguments[key] = value

        result = bridge_call(BRIDGE_DSH_TOOL_NAME, arguments)
        if not isinstance(result, dict) or result.get("isError") is True:
            raise RuntimeError("DSH ChatGPT Web bridge transport call failed")
        if "value" not in result:
            raise RuntimeError("DSH ChatGPT Web bridge transport returned no value")
        return {"value": result["value"]}

    # The companion is self-contained and needs no external network/resource
    # origins.  Keep its CSP explicit even though the dedicated widget domain is
    # intentionally left to the ChatGPT host during this private experiment.
    apps.add_html_resource(
        COMPANION_RESOURCE_URI,
        COMPANION_HTML,
        name="DSH ChatGPT Web bridge companion",
        title="DSH ChatGPT Web bridge companion",
        description="Automatic transport-only ui/message companion for the DSH GUI experiment.",
        csp=ResourceCsp(connectDomains=[], resourceDomains=[], frameDomains=[], baseUriDomains=[]),
        prefers_border=True,
    )
    return apps
