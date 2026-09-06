"""Experimental MCP App probe for ChatGPT Web follow-up messaging.

This module deliberately contains no model invocation, task loop, or DSH execution.
It exists only to test the official MCP Apps ``ui/message`` host request from a
real ChatGPT Web conversation before the DSH GUI experiment commits to that
transport.
"""

from __future__ import annotations

from typing import Any

COMPANION_RESOURCE_URI = "ui://dsh-chatgpt-web-bridge/companion.html"
COMPANION_TOOL_NAME = "open_chatgpt_web_bridge_companion"

# ext-apps 1.7.5 (the version used by the LSM reference implementation) sends
# this protocol version in App.connect(). Keep the raw probe intentionally tiny:
# initialize, announce initialized, then expose one explicit ui/message button.
COMPANION_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSH ChatGPT Web Bridge Probe</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; padding: 14px; background: transparent; color: inherit; }
main { display: grid; gap: 10px; }
h1 { font-size: 14px; margin: 0; }
p { font-size: 12px; line-height: 1.45; margin: 0; opacity: .78; }
.status { font-size: 12px; padding: 8px 10px; border: 1px solid currentColor; border-radius: 8px; opacity: .82; }
textarea { width: 100%; box-sizing: border-box; resize: vertical; min-height: 88px; padding: 9px; border-radius: 8px; border: 1px solid currentColor; background: transparent; color: inherit; font: inherit; }
button { justify-self: start; border: 0; border-radius: 8px; padding: 8px 12px; font: inherit; font-weight: 600; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
code { font-family: ui-monospace, monospace; }
</style>
</head>
<body>
<main>
  <h1>DSH · ChatGPT Web follow-up probe</h1>
  <p>This experimental MCP App uses the official <code>ui/message</code> host request. It does not call a model or DSH by itself.</p>
  <div id="status" class="status">Connecting to ChatGPT host…</div>
  <textarea id="message">Continue the current DSH GUI bridge experiment. Reply with exactly: B1_UI_MESSAGE_OK</textarea>
  <button id="send" type="button" disabled>Send follow-up probe</button>
</main>
<script>
(() => {
  'use strict';
  const PROTOCOL_VERSION = '2026-01-26';
  const statusNode = document.getElementById('status');
  const messageNode = document.getElementById('message');
  const sendNode = document.getElementById('send');
  let nextId = 1;
  const pending = new Map();
  let initialized = false;
  let messageSupported = false;

  function setStatus(text) { statusNode.textContent = text; }
  function post(message) { window.parent.postMessage(message, '*'); }

  function request(method, params) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        pending.delete(id);
        reject(new Error(method + ' timed out'));
      }, 15000);
      pending.set(id, { resolve, reject, timer });
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
      window.clearTimeout(waiter.timer);
      if (message.error) waiter.reject(new Error(message.error.message || 'MCP host request failed'));
      else waiter.resolve(message.result);
      return;
    }

    if (message.method === 'ping' && Object.prototype.hasOwnProperty.call(message, 'id')) {
      post({ jsonrpc: '2.0', id: message.id, result: {} });
    }
  });

  async function connect() {
    const result = await request('ui/initialize', {
      appCapabilities: {},
      appInfo: { name: 'dsh-chatgpt-web-bridge-probe', version: '0.0.1' },
      protocolVersion: PROTOCOL_VERSION,
    });
    notify('ui/notifications/initialized');
    initialized = true;
    messageSupported = Boolean(result && result.hostCapabilities && result.hostCapabilities.message);
    if (messageSupported) {
      setStatus('Connected · ChatGPT host advertises ui/message');
      sendNode.disabled = false;
    } else {
      setStatus('Connected, but host did not advertise ui/message');
    }
  }

  sendNode.addEventListener('click', async () => {
    if (!initialized || !messageSupported || sendNode.disabled) return;
    const text = messageNode.value.trim();
    if (!text) return;
    sendNode.disabled = true;
    setStatus('Sending ui/message…');
    try {
      await request('ui/message', {
        role: 'user',
        content: [{ type: 'text', text }],
      });
      setStatus('ui/message accepted by ChatGPT host');
    } catch (error) {
      setStatus('ui/message failed: ' + (error instanceof Error ? error.message : String(error)));
      sendNode.disabled = false;
    }
  });

  connect().catch((error) => {
    setStatus('Host bridge unavailable: ' + (error instanceof Error ? error.message : String(error)));
  });
})();
</script>
</body>
</html>
"""


def build_chatgpt_web_companion_apps() -> Any:
    """Build the opt-in MCP Apps extension used only by the B1 experiment."""
    try:
        from mcp.server.apps import Apps
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise RuntimeError("MCP Apps support is unavailable in the installed MCP server") from exc

    apps = Apps()

    @apps.tool(
        resource_uri=COMPANION_RESOURCE_URI,
        visibility=["model", "app"],
        name=COMPANION_TOOL_NAME,
        title="Open ChatGPT Web bridge probe",
        description=(
            "Open the experimental MCP App that tests whether ChatGPT Web accepts the official ui/message "
            "follow-up request. The app performs no model invocation and no DSH execution itself."
        ),
    )
    def open_chatgpt_web_bridge_companion() -> dict[str, Any]:
        return {
            "status": "ready",
            "experiment": "B1-ui-message",
            "instruction": "Use the rendered companion UI to send the explicit follow-up probe.",
        }

    apps.add_html_resource(
        COMPANION_RESOURCE_URI,
        COMPANION_HTML,
        name="DSH ChatGPT Web bridge companion",
        title="DSH ChatGPT Web bridge companion",
        description="Minimal self-contained ui/message transport probe.",
        prefers_border=True,
    )
    return apps
