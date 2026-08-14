from __future__ import annotations

import configparser
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
DSH_DEPLOY = ROOT / "deploy" / "dsh"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"
PREFLIGHT = ROOT / "scripts" / "preflight-deployment.py"


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

    def test_deployment_docs_make_service_groups_explicit_and_require_preflight(self) -> None:
        deployment = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        self.assertIn("useradd --system --user-group --home /var/lib/dsh-harness", deployment)
        self.assertIn("useradd --system --user-group --home /var/lib/dsh-mcp-gateway", deployment)
        self.assertIn("python3 scripts/preflight-deployment.py", deployment)
        self.assertLess(
            deployment.index("python3 scripts/preflight-deployment.py"),
            deployment.index("systemctl enable --now dsh-web-host.service"),
        )

    def test_environment_examples_contain_no_committed_secret_values(self) -> None:
        gateway_env = (SYSTEMD / "gateway.env.example").read_text(encoding="utf-8")
        dsh_env = (SYSTEMD / "dsh.env.example").read_text(encoding="utf-8")

        self.assertIn("DSH_MCP_GATEWAY_ADMIN_PIN=\n", gateway_env)
        self.assertIn("DEEPSEEK_API_KEY=\n", dsh_env)
        self.assertIn("# DSH_LSM_COMMAND=", dsh_env)
        self.assertIn("# DSH_LSM_WORKSPACE_ROOT=", dsh_env)
        self.assertNotIn("poc-key", gateway_env + dsh_env)
        self.assertNotIn("trycloudflare.com", gateway_env + dsh_env)


class DeploymentPreflightTests(unittest.TestCase):
    @staticmethod
    def secret_marker(kind: str) -> str:
        return f"test-{kind}-never-print"

    def build_layout(self, root: Path) -> dict[str, Path]:
        paths = {
            "node": root / "bin" / "node",
            "dsh_runtime": root / "opt" / "dsh-runtime",
            "gateway_root": root / "srv" / "dsh-mcp-gateway",
            "workspace": root / "srv" / "dsh-workspace",
            "dsh_home": root / "var" / "lib" / "dsh-harness",
            "gateway_state": root / "var" / "lib" / "dsh-mcp-gateway",
            "config_dir": root / "etc" / "dsh-mcp-gateway",
            "systemd_dir": root / "etc" / "systemd" / "system",
        }
        for key, path in paths.items():
            if key != "node":
                path.mkdir(parents=True, exist_ok=True)
        paths["node"].parent.mkdir(parents=True, exist_ok=True)
        paths["node"].write_text("#!/bin/sh\necho v24.19.0\n", encoding="utf-8")
        paths["node"].chmod(0o755)

        paths["workspace"].chmod(0o750)
        paths["dsh_home"].chmod(0o700)
        paths["gateway_state"].chmod(0o700)
        paths["config_dir"].chmod(0o700)

        dsh_bin = paths["dsh_runtime"] / "node_modules" / ".bin" / "dsh"
        dsh_bin.parent.mkdir(parents=True)
        dsh_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dsh_bin.chmod(0o755)
        package = paths["dsh_runtime"] / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
        package.parent.mkdir(parents=True)
        package.write_text(json.dumps({"version": "0.1.0-rc.6"}), encoding="utf-8")

        venv_bin = paths["gateway_root"] / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(Path(sys.executable))
        gateway_cli = venv_bin / "dsh-mcp-gateway"
        gateway_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gateway_cli.chmod(0o755)
        (paths["gateway_root"] / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
        deploy_dir = paths["gateway_root"] / "deploy"
        (deploy_dir / "systemd").mkdir(parents=True)
        shutil.copy2(ROOT / "deploy" / "server-constraints.txt", deploy_dir / "server-constraints.txt")
        for filename in ("dsh-web-host.service", "dsh-mcp-gateway.service"):
            shutil.copy2(SYSTEMD / filename, deploy_dir / "systemd" / filename)
            shutil.copy2(SYSTEMD / filename, paths["systemd_dir"] / filename)

        dsh_env = paths["config_dir"] / "dsh.env"
        dsh_env.write_text(
            "\n".join(
                (
                    f"DSH_HOME={paths['dsh_home']}",
                    "DEEPSEEK_BASE_URL=https://api.deepseek.com",
                    f"DEEPSEEK_API_KEY={self.secret_marker('api-key')}",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        gateway_env = paths["config_dir"] / "gateway.env"
        gateway_env.write_text(
            "\n".join(
                (
                    f"DSH_WORKSPACE={paths['workspace']}",
                    "DSH_MCP_PUBLIC_BASE_URL=https://dsh.example.com",
                    f"DSH_MCP_GATEWAY_ADMIN_PIN={self.secret_marker('admin-pin')}",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        dsh_env.chmod(0o600)
        gateway_env.chmod(0o600)
        return paths

    def preflight_command(self, paths: dict[str, Path]) -> list[str]:
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        return [
            sys.executable,
            str(PREFLIGHT),
            "--dsh-runtime",
            str(paths["dsh_runtime"]),
            "--gateway-root",
            str(paths["gateway_root"]),
            "--workspace",
            str(paths["workspace"]),
            "--dsh-home",
            str(paths["dsh_home"]),
            "--gateway-state",
            str(paths["gateway_state"]),
            "--config-dir",
            str(paths["config_dir"]),
            "--systemd-dir",
            str(paths["systemd_dir"]),
            "--dsh-user",
            user,
            "--dsh-group",
            group,
            "--gateway-user",
            user,
            "--gateway-group",
            group,
            "--config-owner",
            user,
            "--config-group",
            group,
            "--node-executable",
            str(paths["node"]),
            "--json",
        ]

    def run_preflight(self, paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.preflight_command(paths),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_preflight_accepts_complete_layout_without_exposing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            result = self.run_preflight(paths)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["failed"], 0)
            self.assertNotIn(self.secret_marker("api-key"), result.stdout)
            self.assertNotIn(self.secret_marker("admin-pin"), result.stdout)

    def test_preflight_reports_secret_file_mode_and_missing_value_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            dsh_env = paths["config_dir"] / "dsh.env"
            dsh_env.write_text(
                f"DSH_HOME={paths['dsh_home']}\nDEEPSEEK_BASE_URL=https://api.deepseek.com\nDEEPSEEK_API_KEY=\n",
                encoding="utf-8",
            )
            dsh_env.chmod(0o644)

            result = self.run_preflight(paths)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            failed = {check["name"] for check in report["checks"] if not check["ok"]}
            self.assertIn("DSH env file ownership/mode", failed)
            self.assertIn("DSH env DEEPSEEK_API_KEY", failed)
            self.assertNotIn(self.secret_marker("api-key"), result.stdout)
            self.assertNotIn(self.secret_marker("admin-pin"), result.stdout)

    def test_preflight_detects_dsh_version_and_installed_unit_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            package = paths["dsh_runtime"] / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
            package.write_text(json.dumps({"version": "0.1.0-rc.999"}), encoding="utf-8")
            installed = paths["systemd_dir"] / "dsh-web-host.service"
            installed.write_text(installed.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

            result = self.run_preflight(paths)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            failed = {check["name"] for check in report["checks"] if not check["ok"]}
            self.assertIn("DSH pinned version", failed)
            self.assertIn("installed dsh-web-host.service", failed)


if __name__ == "__main__":
    unittest.main()
