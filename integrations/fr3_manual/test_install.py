"""Only temporary fixture packages; never calls CLI apply/shared paths."""

import contextlib
import difflib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.fr3_manual import install as I


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        hashes = {}
        for name in I.SHARED:
            before = b"def value():\n    return 1\n"
            after = b"def value():\n    return 2\n"
            (self.package / name).write_bytes(before)
            (self.package / name).chmod(0o640)
            diff = "".join(
                difflib.unified_diff(
                    before.decode().splitlines(True),
                    after.decode().splitlines(True),
                    fromfile="a/" + name,
                    tofile="b/" + name,
                )
            )
            (self.artifacts / (name + ".patch")).write_text(diff)
            hashes[name] = {
                "baseline_sha256": I.digest(before),
                "patched_sha256": I.digest(after),
            }
        (self.artifacts / "baseline_hashes.json").write_text(json.dumps(hashes))
        for origin in I.HELPERS:
            (self.artifacts / origin).write_text("VALUE = 1\n")

    def prepare(self):
        return I.prepare(self.package, self.artifacts)

    def test_check_is_read_only_and_reports_exact_hashes(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        plan = self.prepare()
        self.assertEqual(len(plan.entries), 4)
        self.assertEqual(plan.entries[0].report()["action"], "patch")
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )

    def test_apply_backs_up_preserves_mode_and_is_idempotent(self):
        plan = self.prepare()
        archive = self.root / "archive"
        result = I.apply(plan, archive)
        self.assertTrue(result["complete"])
        for entry in plan.entries:
            self.assertEqual((self.package / entry.name).read_bytes(), entry.after)
            self.assertEqual(
                (self.package / entry.name).stat().st_mode & 0o777, entry.mode
            )
            if entry.before is not None:
                self.assertEqual((archive / entry.name).read_bytes(), entry.before)
        repeated = self.prepare()
        self.assertTrue(
            all(e.report()["action"] == "already_installed" for e in repeated.entries)
        )

    def test_unknown_helper_or_baseline_drift_refused(self):
        (self.package / "fr3_manual_protocol.py").write_text("UNKNOWN = 1\n")
        with self.assertRaisesRegex(I.Refused, "unknown existing helper"):
            self.prepare()
        (self.package / "fr3_manual_protocol.py").unlink()
        (self.package / I.SHARED[0]).write_text("# unrelated edit\n")
        with self.assertRaisesRegex(I.Refused, "baseline drift"):
            self.prepare()

    def test_exact_patch_rejects_fuzz_and_bad_counts(self):
        name = I.SHARED[0]
        raw = (self.package / name).read_bytes()
        diff = (self.artifacts / (name + ".patch")).read_bytes()
        with self.assertRaises(I.Refused):
            I.exact_patch(raw.replace(b"return 1", b"return 3"), diff, name)
        with self.assertRaises(I.Refused):
            I.exact_patch(raw, diff.replace(b"@@ -1,2", b"@@ -1,3"), name)

    def test_symlink_file_and_ancestor_refused(self):
        target = self.package / I.SHARED[0]
        target.unlink()
        target.symlink_to(self.package / I.SHARED[1])
        with self.assertRaisesRegex(I.Refused, "symlink"):
            self.prepare()
        link = self.root / "alias"
        link.symlink_to(self.package, target_is_directory=True)
        with self.assertRaisesRegex(I.Refused, "symlink"):
            I.prepare(link, self.artifacts)

    def test_source_or_target_drift_before_apply_writes_nothing(self):
        plan = self.prepare()
        (self.artifacts / "protocol.py").write_text("VALUE = 2\n")
        with self.assertRaisesRegex(I.Refused, "artifact drift"):
            I.apply(plan, self.root / "archive")
        self.assertFalse((self.root / "archive").exists())

    def test_target_drift_during_apply_stops_with_recovery_archive(self):
        plan = self.prepare()
        original = I._atomic

        def mutate(path, data, mode, **kwargs):
            original(path, data, mode, **kwargs)
            if path == self.package / I.SHARED[0]:
                (self.package / I.SHARED[1]).write_text("# concurrent change\n")

        with (
            patch.object(I, "_atomic", mutate),
            self.assertRaisesRegex(I.Refused, "Do not restart services"),
        ):
            I.apply(plan, self.root / "archive")
        manifest = json.loads((self.root / "archive" / "manifest.json").read_text())
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["installed"], [I.SHARED[0]])
        self.assertEqual(
            (self.package / I.SHARED[1]).read_text(), "# concurrent change\n"
        )

    def test_rename_succeeded_then_directory_fsync_failed_is_pending_and_classified(
        self,
    ):
        plan = self.prepare()
        package_inode = self.package.stat().st_ino
        original_fsync = I.os.fsync

        def fail_package_directory(fd):
            info = I.os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == package_inode:
                raise OSError("injected directory fsync failure after rename")
            return original_fsync(fd)

        with (
            patch.object(I.os, "fsync", fail_package_directory),
            self.assertRaisesRegex(I.Refused, "Inspect ALL plan targets"),
        ):
            I.apply(plan, self.root / "archive")
        manifest = json.loads((self.root / "archive" / "manifest.json").read_text())
        self.assertEqual(manifest["pending"], I.SHARED[0])
        self.assertEqual(manifest["installed"], [])
        observed = I.recovery_status(plan)
        self.assertEqual(observed[0]["classification"], "installed")
        self.assertTrue(
            all(row["classification"] == "original" for row in observed[1:])
        )
        self.assertIn("ALL files", manifest["recovery"])

    def test_manifest_update_failed_after_target_rename_retains_write_ahead(self):
        plan = self.prepare()
        original_atomic = I._atomic

        def fail_commit_manifest(path, data, mode, **kwargs):
            if path.name == "manifest.json":
                doc = json.loads(data)
                if I.SHARED[0] in doc["installed"]:
                    raise OSError("injected manifest commit failure")
            return original_atomic(path, data, mode, **kwargs)

        with (
            patch.object(I, "_atomic", fail_commit_manifest),
            self.assertRaisesRegex(I.Refused, "Inspect ALL plan targets"),
        ):
            I.apply(plan, self.root / "archive")
        manifest = json.loads((self.root / "archive" / "manifest.json").read_text())
        self.assertEqual(manifest["pending"], I.SHARED[0])
        self.assertEqual(manifest["installed"], [])
        observed = I.recovery_status(plan)
        self.assertEqual(observed[0]["classification"], "installed")
        self.assertTrue(
            all(row["classification"] == "original" for row in observed[1:])
        )

    def test_existing_archive_and_symlink_archive_refused(self):
        plan = self.prepare()
        archive = self.root / "archive"
        archive.mkdir()
        with self.assertRaises(I.Refused):
            I.apply(plan, archive)
        link = self.root / "archive-link"
        link.symlink_to(archive, target_is_directory=True)
        with self.assertRaisesRegex(I.Refused, "symlink"):
            I.apply(plan, link)

    def cli(self, *args):
        with (
            patch.object(I, "__file__", str(self.artifacts / "install.py")),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return I.main(list(args))

    def test_cli_requires_explicit_package(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            I.main(["--check"])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_explicit_package_check_is_read_only(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(
            I, "apply", side_effect=AssertionError("check attempted apply")
        ):
            self.assertEqual(self.cli("--package", str(self.package)), 0)
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )

    def test_cli_apply_requires_explicit_archive_root(self):
        archive = self.root / "archive"
        self.assertEqual(
            self.cli(
                "--apply", "--package", str(self.package), "--archive", str(archive)
            ),
            2,
        )
        self.assertFalse(archive.exists())

    def test_cli_archive_cannot_escape_authorized_root(self):
        archive_root = self.root / "allowed"
        archive_root.mkdir()
        archive = self.root / "outside"
        self.assertEqual(
            self.cli(
                "--apply",
                "--package",
                str(self.package),
                "--archive-root",
                str(archive_root),
                "--archive",
                str(archive),
            ),
            2,
        )
        self.assertFalse(archive.exists())
        self.assertEqual(
            self.cli(
                "--apply",
                "--package",
                str(self.package),
                "--archive-root",
                str(archive_root),
                "--archive",
                str(archive_root),
            ),
            2,
        )

    def test_cli_explicit_apply_and_idempotent_exact_install(self):
        archive_root = self.root / "allowed"
        archive_root.mkdir()
        archive = archive_root / "backup"
        args = (
            "--apply",
            "--package",
            str(self.package),
            "--archive-root",
            str(archive_root),
            "--archive",
            str(archive),
        )
        self.assertEqual(self.cli(*args), 0)
        self.assertTrue(json.loads((archive / "manifest.json").read_text())["complete"])
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(self.cli(*args), 0)
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )

    def test_cli_rejects_symlink_package(self):
        alias = self.root / "package-link"
        alias.symlink_to(self.package, target_is_directory=True)
        self.assertEqual(self.cli("--check", "--package", str(alias)), 2)

    def test_invalid_helper_syntax_never_modifies_target(self):
        (self.artifacts / "adapter.py").write_text("def broken(\n")
        with self.assertRaises(SyntaxError):
            self.prepare()
        self.assertFalse((self.package / "fr3_manual_adapter.py").exists())


if __name__ == "__main__":
    unittest.main()
