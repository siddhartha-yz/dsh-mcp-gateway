from __future__ import annotations

import configparser
import grp
import hashlib
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
DSH_DEPLOY = ROOT / "deploy" / "dsh"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"
DSH_LOCK_VERIFY = ROOT / "scripts" / "verify-dsh-runtime-lock.py"
PREFLIGHT = ROOT / "scripts" / "preflight-deployment.py"
PROMOTE_LIVE = ROOT / "scripts" / "promote-live-host.sh"
BOOTSTRAP_HOST = ROOT / "scripts" / "bootstrap-target-host.sh"
BACKUP_HOST = ROOT / "scripts" / "backup-host-state.sh"
VERIFY_BACKUP = ROOT / "scripts" / "verify-backup-restore.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GATEWAY_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def read_unit(name: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    loaded = parser.read(SYSTEMD / name, encoding="utf-8")
    if not loaded:
        raise AssertionError(f"missing systemd unit: {name}")
    return parser


def extract_python_heredoc(path: Path, invocation: str) -> str:
    script = path.read_text(encoding="utf-8")
    marker = f"{invocation}\n"
    start = script.index(marker) + len(marker)
    end = script.index("\nPY\n", start)
    return script[start:end]


class DeploymentTemplateTests(unittest.TestCase):
    def test_ci_syntax_checks_all_shipped_shell_scripts(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("run: bash -n scripts/*.sh", workflow)
        self.assertNotIn("run: bash -n scripts/bootstrap-target-host.sh", workflow)

    def test_ci_syntax_checks_production_javascript_with_pinned_node(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("actions/setup-node@v6", workflow)
        self.assertIn('node-version: "24.19.0"', workflow)
        self.assertIn("node --check dsh-bridge-plugin/index.js", workflow)
        self.assertIn("node --check deploy/dsh/plugins/lsm-tool-filter.mjs", workflow)
        self.assertIn("node --check tests/test_lsm_tool_filter.mjs", workflow)
        self.assertIn("run: node tests/test_lsm_tool_filter.mjs", workflow)

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
        self.assertIn("--dsh-harness-url http://127.0.0.1:3080", command)
        self.assertIn("--tool-surface meta-only", command)
        self.assertNotIn("--dsh-web-url", command)
        self.assertNotIn("--dsh-cwd", command)
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
        self.assertEqual(
            service["Environment"],
            "PATH=/opt/dsh-runtime/node_modules/.bin:/opt/dsh-runtime/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin",
        )
        self.assertEqual(service["WorkingDirectory"], "/srv/dsh-workspace")
        self.assertIn("--patch /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml", command)
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

    def test_cloudflared_unit_is_dedicated_and_depends_softly_on_gateway(self) -> None:
        unit = read_unit("dsh-cloudflared.service")
        unit_section = unit["Unit"]
        service = unit["Service"]

        self.assertIn("dsh-mcp-gateway.service", unit_section["Wants"])
        self.assertNotIn("Requires", unit_section)
        self.assertEqual(service["User"], "dsh-tunnel")
        self.assertEqual(service["Group"], "dsh-tunnel")
        self.assertIn("--config /etc/dsh-cloudflared/config.yml run", service["ExecStart"])
        self.assertEqual(service["Restart"], "always")
        self.assertEqual(service["UMask"], "0077")
        self.assertEqual(service["NoNewPrivileges"], "true")
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["ProtectHome"], "true")

    def test_bootstrap_bounds_network_downloads_and_post_start_readiness_probes(self) -> None:
        script = BOOTSTRAP_HOST.read_text(encoding="utf-8")

        self.assertIn(
            'curl --fail --silent --show-error --location --connect-timeout 10 --max-time 600 "$base/$filename"',
            script,
        )
        self.assertIn(
            'curl --fail --silent --show-error --location --connect-timeout 10 --max-time 600 "$base/SHASUMS256.txt"',
            script,
        )
        self.assertIn(
            "curl --fail --silent --show-error --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/healthz",
            script,
        )
        self.assertIn(
            "curl --fail --silent --show-error --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz",
            script,
        )

    def test_live_promotion_preserves_real_workspace_and_migrates_state(self) -> None:
        script = PROMOTE_LIVE.read_text(encoding="utf-8")

        self.assertIn('WORKSPACE="/home/ubuntu/workspace"', script)
        self.assertIn("ProtectHome=read-only", script)
        self.assertIn("ReadWritePaths=$WORKSPACE /var/lib/dsh-harness", script)
        self.assertIn("artifacts = root / 'plugin-artifacts'", script)
        self.assertIn("source-manifest.json", script)
        self.assertIn("npm',\n                'pack'", script)
        self.assertIn("--ignore-scripts", script)
        self.assertIn("network git dependency instead of a local artifact", script)
        self.assertIn("npm_config_store_dir=/var/lib/dsh-harness/pnpm-store", script)
        self.assertIn("npm_config_cache=/var/lib/dsh-harness/npm-cache", script)
        self.assertIn("SYSTEMD_UNIT_PATH=/etc/systemd/system", script)
        self.assertIn("/var/lib/dsh-mcp-gateway/oauth.sqlite3", script)
        self.assertIn("OAuth SQLite copy checksum mismatch", script)
        self.assertIn("/etc/dsh-cloudflared/credentials.json", script)
        self.assertIn("temporary DSH/gateway listener is still active", script)
        self.assertIn("pgrep -x cloudflared", script)
        self.assertNotIn("pgrep -af cloudflared", script)
        self.assertIn("/proc/$pid/cmdline", script)
        self.assertIn("tool catalog changed across promotion", script)
        self.assertIn("SkillRegistry changed across promotion", script)
        self.assertNotIn("DEEPSEEK_API_" + "KEY=", script)

    def test_live_promotion_fails_closed_when_readiness_poll_never_succeeds(self) -> None:
        script = PROMOTE_LIVE.read_text(encoding="utf-8")

        host_probe = (
            'curl -fsS --connect-timeout 2 --max-time 5 '
            'http://127.0.0.1:3080/api/chatgpt-bridge/tools '
            '>/tmp/dsh-promote-tools.json'
        )
        gateway_probe = (
            'curl -fsS --connect-timeout 2 --max-time 5 '
            'http://127.0.0.1:18766/readyz '
            '>/tmp/dsh-promote-ready.json'
        )
        public_probe = (
            'curl -fsS --connect-timeout 5 --max-time 10 '
            '"$PUBLIC_BASE_URL/readyz" >/tmp/dsh-promote-public.json'
        )

        # Each endpoint is polled in a retry loop and then probed once more
        # under `set -e` so exhausting every retry cannot fall through merely
        # because `cat` of a truncated temporary file succeeds.
        self.assertGreaterEqual(script.count(host_probe), 1)
        self.assertGreaterEqual(script.count(gateway_probe), 2)
        self.assertGreaterEqual(script.count(public_probe), 2)

    def test_backup_restore_scripts_scope_out_unrelated_host_projects_and_verify_real_oauth(self) -> None:
        backup = BACKUP_HOST.read_text(encoding="utf-8")
        restore = VERIFY_BACKUP.read_text(encoding="utf-8")

        self.assertIn("systemctl stop dsh-cloudflared.service", backup)
        self.assertIn("systemctl stop dsh-mcp-gateway.service", backup)
        self.assertIn("systemctl stop dsh-web-host.service", backup)
        self.assertNotIn("shutdown", backup)
        self.assertNotIn("systemctl stop --all", backup)
        self.assertIn("workspace path must be non-empty and relative", backup)
        self.assertIn("workspace-selected.tar.gz", backup)
        self.assertIn("gateway-state.tar.gz", backup)
        self.assertIn("config.tar.gz", backup)
        self.assertIn("SHA256SUMS", backup)
        self.assertIn(
            "curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3080/api/chatgpt-bridge/tools",
            backup,
        )
        self.assertIn(
            "curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:18766/readyz",
            backup,
        )
        self.assertIn("encrypt it before moving it off-host", backup)
        self.assertNotIn("DEEPSEEK_API_" + "KEY=", backup + restore)

        self.assertIn("--offline", restore)
        self.assertIn("dependency is not a backed-up local artifact", restore)
        self.assertIn("restored OAuth state has no ChatGPT refresh grant", restore)
        self.assertIn("grant_type':'refresh_token'", restore)
        self.assertIn("dsh_tool_catalog", restore)
        self.assertIn("dsh_skill_catalog", restore)
        self.assertIn("tools.listChanged", restore)
        self.assertIn("Production DSH services were not modified", restore)
        self.assertIn(
            'curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$DSH_PORT/api/chatgpt-bridge/tools"',
            restore,
        )
        self.assertIn(
            'curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$GATEWAY_PORT/readyz"',
            restore,
        )

    def test_backup_validates_workspace_selection_before_creating_output(self) -> None:
        backup = BACKUP_HOST.read_text(encoding="utf-8")
        validation = 'python3 - "$WORKSPACE" "$OUTPUT" "${WORKSPACE_PATHS[@]}" <<\'PY\''
        create_output = 'install -d -m 0700 "$OUTPUT"'
        self.assertLess(
            backup.index(validation),
            backup.index(create_output),
            "invalid workspace selections must fail before leaving a partial backup output directory",
        )

    def test_failed_backup_snapshot_removes_partial_output(self) -> None:
        backup = BACKUP_HOST.read_text(encoding="utf-8")
        start = backup.index('OUTPUT="$(python3 - "$OUTPUT"')
        end = backup.index("# Make the state quiescent")
        pre_quiesce = backup[start:end]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            output = root / "backup-output"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
            fake_curl.chmod(0o755)
            fake_systemctl = fake_bin / "systemctl"
            fake_systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_systemctl.chmod(0o755)

            script = "\n".join(
                [
                    "set -euo pipefail",
                    f"OUTPUT={shlex.quote(str(output))}",
                    f"WORKSPACE={shlex.quote(str(workspace))}",
                    "WORKSPACE_PATHS=()",
                    pre_quiesce,
                ]
            )
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
            result = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists(), "failed backup snapshot must not leave an unusable output directory")

    def test_workspace_backup_and_restore_reject_selected_symlinks(self) -> None:
        backup_validation = extract_python_heredoc(
            BACKUP_HOST,
            'python3 - "$WORKSPACE" "$OUTPUT" "${WORKSPACE_PATHS[@]}" <<\'PY\'',
        )
        restore_validation = extract_python_heredoc(
            VERIFY_BACKUP,
            'python3 - "$BACKUP/MANIFEST.json" "$RESTORE_ROOT/workspace" <<\'PY\'',
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "live-workspace"
            restore = root / "restored-workspace"
            output = root / "backup-output"
            workspace.mkdir()
            restore.mkdir()
            target = workspace / "target.txt"
            target.write_text("sentinel\n", encoding="utf-8")
            selected = workspace / "selected-link"
            selected.symlink_to(target)

            backup_check = subprocess.run(
                [sys.executable, "-", str(workspace), str(output), selected.name],
                input=backup_validation,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(backup_check.returncode, 0)
            self.assertIn("symlink", backup_check.stdout + backup_check.stderr)

            restored_link = restore / selected.name
            restored_link.symlink_to(target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            manifest = root / "MANIFEST.json"
            manifest.write_text(
                json.dumps(
                    {
                        "workspace_files": [
                            {
                                "path": selected.name,
                                "sha256": digest,
                                "size": target.stat().st_size,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            restore_check = subprocess.run(
                [sys.executable, "-", str(manifest), str(restore)],
                input=restore_validation,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(restore_check.returncode, 0)
            self.assertIn("symlink", restore_check.stdout + restore_check.stderr)

            # An unmanifested nested symlink must also not escape the isolated
            # restore workspace. Directory selections can legitimately contain
            # symlinks that were omitted from workspace_files hashing.
            nested_dir = restore / "selected-dir"
            nested_dir.mkdir()
            escaping_link = nested_dir / "outside-link"
            escaping_link.symlink_to(target)
            empty_manifest = root / "EMPTY-MANIFEST.json"
            empty_manifest.write_text(json.dumps({"workspace_files": []}), encoding="utf-8")
            nested_restore_check = subprocess.run(
                [sys.executable, "-", str(empty_manifest), str(restore)],
                input=restore_validation,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(nested_restore_check.returncode, 0)
            self.assertIn("escapes restore root through symlink", nested_restore_check.stdout + nested_restore_check.stderr)

    def test_workspace_restore_rejects_broken_nested_symlinks(self) -> None:
        restore_validation = extract_python_heredoc(
            VERIFY_BACKUP,
            'python3 - "$BACKUP/MANIFEST.json" "$RESTORE_ROOT/workspace" <<\'PY\'',
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore = root / "restored-workspace"
            restore.mkdir()
            nested_dir = restore / "selected-dir"
            nested_dir.mkdir()
            broken_link = nested_dir / "missing-link"
            broken_link.symlink_to("../shared/missing.txt")
            manifest = root / "MANIFEST.json"
            manifest.write_text(json.dumps({"workspace_files": []}), encoding="utf-8")

            restore_check = subprocess.run(
                [sys.executable, "-", str(manifest), str(restore)],
                input=restore_validation,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(restore_check.returncode, 0)
            self.assertIn("broken restored workspace symlink", restore_check.stdout + restore_check.stderr)

    def test_optional_session_search_overlay_is_durable_and_lazy(self) -> None:
        overlay = (DSH_DEPLOY / "session-search.cordis.yml").read_text(encoding="utf-8")
        drop_in = (SYSTEMD / "dsh-web-host-search.conf.example").read_text(encoding="utf-8")

        self.assertIn("id: session-query-sqlite", overlay)
        self.assertIn("dshHomePath('derived/session-query.sqlite3')", overlay)
        self.assertIn("openAt: first-search", overlay)
        self.assertNotIn("openAt: startup", overlay)
        self.assertIn("--patch /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml", drop_in)
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
        self.assertIn("/opt/dsh-runtime/node/bin/node", deployment)
        self.assertIn("/opt/dsh-runtime/node/bin/npm ci", deployment)
        self.assertIn("/opt/dsh-runtime/node_modules/.bin/pnpm", deployment)
        self.assertIn("dsh plugin --profile web add", deployment)
        self.assertIn("npm_config_registry=https://registry.npmjs.org/", deployment)
        self.assertIn("python3 scripts/verify-dsh-runtime-lock.py", deployment)
        self.assertIn("python3 scripts/preflight-deployment.py", deployment)
        self.assertIn("python3 scripts/smoke-public-oauth.py --base-url https://dsh.example.com", deployment)
        self.assertLess(
            deployment.index("python3 scripts/preflight-deployment.py"),
            deployment.index("systemctl enable --now dsh-web-host.service"),
        )

    def test_dsh_runtime_lock_verifier_accepts_repository_lock_and_rejects_root_drift(self) -> None:
        accepted = subprocess.run(
            [sys.executable, str(DSH_LOCK_VERIFY)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("dsh-runtime-lock-ok", accepted.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy2(ROOT / "deploy" / "dsh-runtime" / "package.json", root / "package.json")
            shutil.copy2(ROOT / "deploy" / "dsh-runtime" / "package-lock.json", root / "package-lock.json")
            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
            package["dependencies"]["@deepseek-ai/dsh"] = "0.1.0-rc.999"
            (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
            rejected = subprocess.run(
                [sys.executable, str(DSH_LOCK_VERIFY), "--root", str(root)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("exact tested DSH and pnpm dependencies", rejected.stderr)

    def test_environment_examples_contain_no_committed_secret_values(self) -> None:
        gateway_env = (SYSTEMD / "gateway.env.example").read_text(encoding="utf-8")
        dsh_env = (SYSTEMD / "dsh.env.example").read_text(encoding="utf-8")

        self.assertIn("DSH_MCP_GATEWAY_ADMIN_PIN=\n", gateway_env)
        self.assertNotIn("DSH_WORKSPACE", gateway_env)
        self.assertNotIn("DEEPSEEK_API_KEY", dsh_env)
        self.assertNotIn("DEEPSEEK_BASE_URL", dsh_env)
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
            "node": root / "opt" / "dsh-runtime" / "node" / "bin" / "node",
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
        (paths["gateway_root"] / "pyproject.toml").write_text(
            f"[project]\nname='test'\nversion='{GATEWAY_VERSION}'\n",
            encoding="utf-8",
        )
        deploy_dir = paths["gateway_root"] / "deploy"
        (deploy_dir / "systemd").mkdir(parents=True)
        (deploy_dir / "dsh-runtime").mkdir(parents=True)
        shutil.copy2(ROOT / "deploy" / "server-constraints.txt", deploy_dir / "server-constraints.txt")
        for filename in ("package.json", "package-lock.json"):
            shutil.copy2(ROOT / "deploy" / "dsh-runtime" / filename, deploy_dir / "dsh-runtime" / filename)
            shutil.copy2(ROOT / "deploy" / "dsh-runtime" / filename, paths["dsh_runtime"] / filename)
        for filename in ("dsh-web-host.service", "dsh-mcp-gateway.service"):
            shutil.copy2(SYSTEMD / filename, deploy_dir / "systemd" / filename)
            shutil.copy2(SYSTEMD / filename, paths["systemd_dir"] / filename)

        dsh_env = paths["config_dir"] / "dsh.env"
        dsh_env.write_text(
            "\n".join(
                (
                    f"DSH_HOME={paths['dsh_home']}",
                    "DSH_TELEMETRY_DISABLED=1",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        gateway_env = paths["config_dir"] / "gateway.env"
        gateway_env.write_text(
            "\n".join(
                (
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
            self.assertNotIn(self.secret_marker("admin-pin"), result.stdout)

    def test_preflight_accepts_explicit_personal_workspace_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            paths["workspace"].chmod(0o700)
            command = self.preflight_command(paths)
            command[command.index("--json"):command.index("--json")] = ["--workspace-mode", "0700"]
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_preflight_reports_secret_file_mode_and_rejects_legacy_model_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            dsh_env = paths["config_dir"] / "dsh.env"
            dsh_env.write_text(
                f"DSH_HOME={paths['dsh_home']}\nDEEPSEEK_API_KEY=legacy-credential\n",
                encoding="utf-8",
            )
            dsh_env.chmod(0o644)

            result = self.run_preflight(paths)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            failed = {check["name"] for check in report["checks"] if not check["ok"]}
            self.assertIn("DSH env file ownership/mode", failed)
            self.assertIn("DSH env excludes DEEPSEEK_API_KEY", failed)
            self.assertNotIn("legacy-credential", result.stdout)
            self.assertNotIn(self.secret_marker("admin-pin"), result.stdout)

    def test_preflight_reports_permission_denied_without_traceback_or_secret_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            config_dir = paths["config_dir"]
            try:
                config_dir.chmod(0o000)
                result = self.run_preflight(paths)
            finally:
                config_dir.chmod(0o700)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "")
            report = json.loads(result.stdout)
            failed = {check["name"] for check in report["checks"] if not check["ok"]}
            self.assertIn("DSH env file", failed)
            self.assertIn("gateway env file", failed)
            self.assertIn("DSH env parse", failed)
            self.assertIn("gateway env parse", failed)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertNotIn(self.secret_marker("admin-pin"), result.stdout + result.stderr)

    def test_preflight_detects_dsh_version_and_installed_unit_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            package = paths["dsh_runtime"] / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
            package.write_text(json.dumps({"version": "0.1.0-rc.999"}), encoding="utf-8")
            paths["node"].write_text("#!/bin/sh\necho v99.0.0\n", encoding="utf-8")
            paths["node"].chmod(0o755)
            installed_lock = paths["dsh_runtime"] / "package-lock.json"
            installed_lock.write_text(installed_lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            installed = paths["systemd_dir"] / "dsh-web-host.service"
            installed.write_text(installed.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

            result = self.run_preflight(paths)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            failed = {check["name"] for check in report["checks"] if not check["ok"]}
            self.assertIn("Node pinned version", failed)
            self.assertIn("DSH pinned version", failed)
            self.assertIn("installed DSH package-lock.json", failed)
            self.assertIn("installed dsh-web-host.service", failed)

    def test_preflight_detects_gateway_release_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.build_layout(Path(tmp))
            (paths["gateway_root"] / "pyproject.toml").write_text(
                "[project]\nname='test'\nversion='0.0.1.dev0'\n",
                encoding="utf-8",
            )

            result = self.run_preflight(paths)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            failed = {check["name"] for check in report["checks"] if not check["ok"]}
            self.assertIn("gateway project version", failed)
            check = next(check for check in report["checks"] if check["name"] == "gateway project version")
            self.assertIn("0.0.1.dev0", check["detail"])
            self.assertIn(GATEWAY_VERSION, check["detail"])


if __name__ == "__main__":
    unittest.main()
