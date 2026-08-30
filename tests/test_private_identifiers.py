from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-private-identifiers.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_private_identifiers", CHECKER)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load private identifier checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrivateIdentifierCheckerTests(unittest.TestCase):
    def test_symlink_is_scanned_as_link_text_without_following_target(self) -> None:
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            outside = root / "outside"
            outside.write_text("outside-secret-data", encoding="utf-8")
            link = root / "tracked-link"
            link.symlink_to(outside)

            self.assertEqual(checker.read_tracked_bytes(link), str(outside).encode())

    def test_symlink_to_special_file_does_not_read_special_file(self) -> None:
        checker = load_checker()
        link = None
        with tempfile.TemporaryDirectory() as tmp:
            link = pathlib.Path(tmp) / "tracked-link"
            link.symlink_to("/dev/zero")

            self.assertEqual(checker.read_tracked_bytes(link), b"/dev/zero")


if __name__ == "__main__":
    unittest.main()
