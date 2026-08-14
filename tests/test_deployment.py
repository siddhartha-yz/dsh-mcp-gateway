from __future__ import annotations

import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
DSH_DEPLOY = ROOT / "deploy" / "dsh"


def read_unit(name: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    loaded = parser.read(SYSTEMD / name, encoding="utf-8")
    if not loaded:
        raise AssertionError(f"missing systemd unit: {name}")
    return parser


class DeploymentTemplateTests(unittest.TestCase):
    def test_gateway_stays_loopback_and_does_not_require_host_lifetime(self) -> None:
        unit = read_unit("dsh-mcp-gateway.service")
        unit_section = unit["Unit"]
        service = unit["Service"]
        command = service["ExecStart"]

        self.assertIn("dsh-web-host.service", unit_section["Wants"])
        self.assertNotIn("Requires", unit_section)
        self.assertEqual(service["User"], "dsh-gateway")
        self.assertEqual(service["Group"], "dsh-gateway")
        self.assertEqual(service["EnvironmentFile"], "/etc/dsh-mcp-gateway/gateway.env")
        self.assertIn("--dsh-web-url http://127.0.0.1:3080", command)
        self.assertIn("--bind-host 127.0.0.1", command)
        self.assertNotIn("--allow-non-loopback-bind", command)
        self.assertIn("--port 18766", command)
        self.assertEqual(service["Restart"], "on-failure")
        self.assertEqual(service["RestartSec"], "3s")
        self.assertEqual(service["TimeoutStopSec"], "30s")
        self.assertEqual(service["UMask"], "0077")
        self.assertEqual(service["StateDirectory"], "dsh-mcp-gateway")
        self.assertEqual(service["StateDirectoryMode"], "0700")
        self.assertEqual(service["NoNewPrivileges"], "true")
        self.assertEqual(service["PrivateTmp"], "true")
        self.assertEqual(service["PrivateDevices"], "true")
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["ProtectHome"], "true")
        self.assertEqual(service["ProtectKernelTunables"], "true")
        self.assertEqual(service["ProtectKernelModules"], "true")
        self.assertEqual(service["ProtectControlGroups"], "true")
        self.assertEqual(service["RestrictSUIDSGID"], "true")
        self.assertEqual(service["LockPersonality"], "true")
        self.assertEqual(service["RestrictAddressFamilies"], "AF_UNIX AF_INET AF_INET6")

    def test_dsh_web_host_is_loopback_only_and_separately_restartable(self) -> None:
        unit = read_unit("dsh-web-host.service")
        service = unit["Service"]
        command = service["ExecStart"]

        self.assertEqual(service["User"], "dsh-agent")
        self.assertEqual(service["Group"], "dsh-agent")
        self.assertEqual(service["EnvironmentFile"], "/etc/dsh-mcp-gateway/dsh.env")
        self.assertEqual(service["WorkingDirectory"], "/srv/dsh-workspace")
        self.assertIn("--host 127.0.0.1", command)
        self.assertNotIn("--trusted-host", command)
        self.assertIn("--port 3080", command)
        self.assertEqual(service["Restart"], "on-failure")
        self.assertEqual(service["RestartSec"], "3s")
        self.assertEqual(service["TimeoutStopSec"], "30s")
        self.assertEqual(service["UMask"], "0077")
        self.assertEqual(service["NoNewPrivileges"], "true")
        self.assertEqual(service["PrivateTmp"], "true")
        self.assertEqual(service["ProtectSystem"], "full")
        self.assertEqual(service["ProtectHome"], "true")
        self.assertIn("/var/lib/dsh-harness", service["ReadWritePaths"])
        self.assertIn("/srv/dsh-workspace", service["ReadWritePaths"])

    def test_optional_session_search_overlay_is_durable_and_lazy(self) -> None:
        overlay = (DSH_DEPLOY / "session-search.cordis.yml").read_text(encoding="utf-8")
        drop_in = (SYSTEMD / "dsh-web-host-search.conf.example").read_text(encoding="utf-8")

        self.assertIn("id: session-query-sqlite", overlay)
        self.assertIn("dshHomePath('derived/session-query.sqlite3')", overlay)
        self.assertIn("openAt: first-search", overlay)
        self.assertNotIn("openAt: startup", overlay)
        self.assertIn("--patch /srv/dsh-mcp-gateway/deploy/dsh/session-search.cordis.yml", drop_in)
        self.assertIn("--host 127.0.0.1", drop_in)
        self.assertNotIn("--host 0.0.0.0", drop_in)

    def test_optional_local_shell_overlay_is_private_and_workspace_restricted(self) -> None:
        overlay = (DSH_DEPLOY / "local-shell-mcp.cordis.yml").read_text(encoding="utf-8")
        filter_plugin = (DSH_DEPLOY / "plugins" / "lsm-tool-filter.mjs").read_text(encoding="utf-8")

        self.assertIn("name: '@deepseek-ai/dsh-mcp-client'", overlay)
        self.assertIn("serverName: lsm", overlay)
        self.assertIn("transport: stdio", overlay)
        self.assertIn("command: !!js process.env.DSH_LSM_COMMAND", overlay)
        self.assertIn("cwd: !!js process.env.DSH_LSM_WORKSPACE_ROOT", overlay)
        self.assertIn("LOCAL_SHELL_MCP_AUTH_MODE: 'none'", overlay)
        self.assertIn("LOCAL_SHELL_MCP_REMOTE_ENABLED: 'false'", overlay)
        self.assertIn("LOCAL_SHELL_MCP_UI_ENABLED: 'false'", overlay)
        self.assertIn("LOCAL_SHELL_MCP_FILE_DOWNLOAD_ENABLED: 'false'", overlay)
        self.assertIn("LOCAL_SHELL_MCP_ALLOW_FULL_CONTAINER: 'false'", overlay)
        self.assertIn("failOnStartupError: true", overlay)
        self.assertIn(
            "name: /srv/dsh-mcp-gateway/deploy/dsh/plugins/lsm-tool-filter.mjs",
            overlay,
        )
        allowed = {
            line.strip()[2:]
            for line in overlay.splitlines()
            if line.strip().startswith("- ")
            and line.strip()[2:]
            in {
                "browser_session",
                "browser_snapshot",
                "browser_act",
                "browser_run_script",
                "mcp_tool_search",
                "mcp_tool_inspect",
                "mcp_tool_call",
            }
        }
        self.assertEqual(
            allowed,
            {
                "browser_session",
                "browser_snapshot",
                "browser_act",
                "browser_run_script",
                "mcp_tool_search",
                "mcp_tool_inspect",
                "mcp_tool_call",
            },
        )
        self.assertNotIn("- mcp_manage", overlay)
        self.assertNotIn("- run_shell_tool", overlay)
        self.assertNotIn("- list_files", overlay)
        self.assertIn("ctx.on('agent/created'", filter_plugin)
        self.assertIn("ctx.tools.schemas(agent)", filter_plugin)
        self.assertIn("agent.ctx.tools.restrict({ deny })", filter_plugin)
        self.assertNotIn("restrict({ allow })", filter_plugin)
        self.assertNotIn("/home/ubuntu", overlay + filter_plugin)
        self.assertNotIn("transport: streamable-http", overlay)

    def test_environment_examples_contain_no_committed_secret_values(self) -> None:
        gateway_env = (SYSTEMD / "gateway.env.example").read_text(encoding="utf-8")
        dsh_env = (SYSTEMD / "dsh.env.example").read_text(encoding="utf-8")

        self.assertIn("DSH_MCP_GATEWAY_ADMIN_PIN=\n", gateway_env)
        self.assertIn("DEEPSEEK_API_KEY=\n", dsh_env)
        self.assertIn("# DSH_LSM_COMMAND=", dsh_env)
        self.assertIn("# DSH_LSM_WORKSPACE_ROOT=", dsh_env)
        self.assertNotIn("poc-key", gateway_env + dsh_env)
        self.assertNotIn("trycloudflare.com", gateway_env + dsh_env)


if __name__ == "__main__":
    unittest.main()
