# Production backup/restore acceptance

Date: 2026-08-15

This acceptance validates that the production ChatGPT -> DSH deployment can be backed up and restored without stopping or modifying unrelated projects on the host.

## Scope

The supported DSH backup boundary is deliberately narrower than the user's entire `/home/ubuntu/workspace` tree:

- complete DSH durable state: `/var/lib/dsh-harness`;
- complete gateway OAuth state: `/var/lib/dsh-mcp-gateway`;
- private deployment configuration and named-tunnel configuration;
- explicitly selected workspace paths used as representative data-integrity fixtures.

Other repositories and project data in the shared workspace remain the responsibility of their own Git/backup policy. The backup drill must not recursively copy or stop unrelated projects merely to validate DSH recovery.

## Production backup

The checked-in `scripts/backup-host-state.sh` was run against the persistent systemd deployment. It briefly quiesced only:

- `dsh-cloudflared.service`;
- `dsh-mcp-gateway.service`;
- `dsh-web-host.service`.

No operating-system reboot was performed and no unrelated service was touched.

The production backup reported:

```text
backup manifest: tools=34 skills=1 workspace_files=2
BACKUP PASS: /home/ubuntu/workspace/.dsh-release-backup-v0.1.0-drill
Only DSH services were briefly quiesced; unrelated host services were not touched.
```

Representative workspace fixtures were:

- `dsh-skill-debug-test/CONTEXT.md`;
- `dsh-meta-only-hard-test/result.json`.

The backup contains SHA-256 checksums for its manifest, DSH state archive, OAuth state archive, configuration archive, selected workspace archive, and pre-backup tool/skill snapshots.

## Isolated restore

The real production backup was restored with `scripts/verify-backup-restore.sh` into a separate temporary root. The verifier used loopback ports `18422` for the restored DSH Harness and `18778` for the restored gateway. The production services remained live and unchanged during the restore verification.

Observed result:

```text
MANIFEST.json: OK
tools-before.json: OK
skills-before.json: OK
dsh-home.tar.gz: OK
gateway-state.tar.gz: OK
config.tar.gz: OK
workspace-selected.tar.gz: OK
workspace_restore=PASS files=2
plugin_artifacts_rebased=9
offline_profile_rebuild=PASS
dsh_restore=PASS tools=34 skills=1
oauth_mcp_restore=PASS tools=4 catalog=34 skills=1
RESTORE DRILL PASS: /home/ubuntu/workspace/.dsh-release-restore-v0.1.0-drill
Production DSH services were not modified by this verifier.
```

The restore verifier additionally proved that:

- all nine production community plugin artifacts can be rebound under the isolated restored DSH home;
- the profile can be rebuilt with pnpm in offline mode from the local content-addressable store and backed-up artifacts;
- the restored DSH Harness exposes the same 34-tool catalog;
- `diagnosing-bugs` is restored through the DSH SkillRegistry;
- a cloned ChatGPT refresh grant from the restored OAuth SQLite database can obtain a new access token;
- the restored MCP endpoint still exposes exactly the four meta-tools with `tools.listChanged=false`;
- `dsh_tool_catalog` through the restored OAuth/MCP path reports 34 tools;
- both selected workspace fixtures restore with matching hashes.

The OAuth rotation performed by the verifier happens only in the isolated copy of the database. It does not consume or mutate the production ChatGPT refresh grant.

## Post-drill production check

After the isolated restore drill, both local and public production readiness endpoints still returned healthy responses. The production DSH bridge still reported 34 tools and the `diagnosing-bugs` Skill.

## Result

The v0.1 backup/restore release gate is **passed**. A full operating-system reboot remains intentionally deferred to a normal host maintenance window because the machine also runs unrelated important projects; that deferred drill is not required for the v0.1 release.
