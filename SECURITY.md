# Security policy

## Supported versions

Security fixes are provided for the latest tagged release on the default branch. Development commits may change without compatibility guarantees until they are tagged.

## Reporting a vulnerability

Do not publish OAuth tokens, tunnel credentials, owner PINs, private workspace data, or a working exploit in a public issue.

If this repository exposes GitHub's private vulnerability-reporting flow, use **Security → Report a vulnerability**. Otherwise contact the maintainer privately through GitHub before disclosing sensitive details. Public issues are appropriate only for non-sensitive hardening questions that do not reveal credentials or an exploitable secret.

Include the affected commit/tag, deployment mode, reproduction conditions, and whether the issue crosses the public OAuth/MCP boundary, the loopback DSH bridge, or a downstream execution provider.

## Security boundary

The supported public boundary is the OAuth-protected MCP gateway. The DSH bridge and optional execution providers are expected to remain private/loopback-bound. The default `meta-only` surface does not require a model-provider API key; operators should not add one merely to run the ChatGPT-to-DSH Harness path.

Backups created by `scripts/backup-host-state.sh` contain OAuth grants and tunnel credentials. Keep them private and encrypt them before off-host storage.
