# DSH ChatGPT Web Observer — B2 experiment

This extension is the read-only half of the P6 B2 transport experiment.

It does **not** send ChatGPT messages, click the page, invoke a model, or call a provider API. B1's official MCP Apps `ui/message` path remains the only outbound ChatGPT transport. The extension only observes the real `chatgpt.com` page and relays bounded lifecycle observations to an already-open local DSH Web GUI tab.

## Firefox temporary install

1. Open `about:debugging`.
2. Choose **This Firefox**.
3. Click **Load Temporary Add-on…**.
4. Select this directory's `manifest.json`.
5. Reload the real `chatgpt.com` conversation tab.
6. Reload the DSH Web GUI tab.

The DSH GUI must be opened through `http://127.0.0.1:3080/...` or `http://localhost:3080/...` for this experiment. Use an SSH local port-forward such as `-L 3080:127.0.0.1:3080`; the fixed port deliberately narrows which local page can receive observed ChatGPT text.

A temporary Firefox add-on is removed when Firefox exits. That is intentional for the experiment.

## Chrome / Chromium developer install

Open the Extensions page, enable Developer mode, choose **Load unpacked**, and select this directory. Reload both the ChatGPT and DSH GUI tabs afterward.

## Data path

```text
chatgpt.com DOM
  -> read-only content script
  -> extension background relay
  -> localhost DSH GUI content script
  -> window.postMessage
  -> DSH client plugin
  -> authenticated local /plugins/chatgpt-web-bridge/observer route
```

The extension never receives the DSH bridge capability token. It performs no network request itself. The DSH client plugin validates the relayed event and remains the only component that can write to the local bridge route.

## Observations

The first probe watches semantic ChatGPT UI signals for:

- conversation identity (current path);
- generation start;
- completed assistant text;
- generation completion;
- visible retry/error indicators;
- selector or lifecycle ambiguity (`bridge_degraded`).

The observer fails closed: ambiguity produces a degradation event and never causes a ChatGPT message to be sent.
