from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_publishes_existing_or_new_version_tags(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('      - "v*"', text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("contents: write", text)
        self.assertIn('git checkout --detach "$TAG"', text)
        self.assertIn('test -f "docs/releases/$TAG.md"', text)
        self.assertIn("python -m build --wheel --outdir dist", text)
        self.assertIn("sha256sum *.whl > SHA256SUMS", text)
        self.assertIn('gh release create "$TAG" dist/*', text)
        self.assertIn('gh release edit "$TAG"', text)
        self.assertIn('gh release upload "$TAG" dist/*', text)
        self.assertIn("--verify-tag", text)
        self.assertIn("--latest", text)
        self.assertIn("GH_TOKEN: ${{ github.token }}", text)

    def test_release_workflow_bootstraps_when_first_added_to_main(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("branches:\n      - main", text)
        self.assertIn('paths:\n      - ".github/workflows/release.yml"', text)
        self.assertIn("git tag --list 'v*' --sort=-v:refname", text)


if __name__ == "__main__":
    unittest.main()
