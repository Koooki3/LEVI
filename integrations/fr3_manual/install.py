"""Exact-baseline manual protocol installation. No service/ROS/network operations.

Default --check is read-only. --apply requires a new explicit local archive.
Atomicity is per file; errors retain the archive and explain partial recovery.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

SHARED = ("franka_server_ros2.py", "robotiq_gripper_server_ros2.py")
HELPERS = {
    "protocol.py": "fr3_manual_protocol.py",
    "adapter.py": "fr3_manual_adapter.py",
}
MAX_BYTES = 2 * 1024 * 1024


class Refused(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked_path(path, *, allow_missing=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise Refused("absolute path without '..' required")
    missing = False
    for part in [*reversed(list(path.parents)), path]:
        if missing:
            continue
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not allow_missing:
                raise Refused("path missing: " + str(part)) from None
            missing = True
            continue
        if stat.S_ISLNK(info.st_mode):
            raise Refused("symlink refused: " + str(part))
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise Refused("non-directory ancestor: " + str(part))
    return path


def _open_directory(path):
    """Pin every directory component with O_NOFOLLOW, including ancestors."""
    path = checked_path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(path, *, optional=False):
    path = checked_path(path, allow_missing=optional)
    directory = _open_directory(path.parent)
    try:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory
        )
    except FileNotFoundError:
        if optional:
            return None, None
        raise Refused("file missing: " + str(path)) from None
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise Refused("not a bounded regular file: " + str(path))
        data = b""
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        after = os.fstat(fd)
        if len(data) > MAX_BYTES or (
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Refused("file changed during read: " + str(path))
        return data, stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def exact_patch(original, patch, name):
    """Parse unified hunks with exact line indexes/context; never apply fuzz."""
    lines = original.decode("utf-8").splitlines(keepends=True)
    edits = patch.decode("utf-8").splitlines(keepends=True)
    if edits[:2] != ["--- a/" + name + "\n", "+++ b/" + name + "\n"]:
        raise Refused("patch header mismatch: " + name)
    out, cursor, i, hunks = [], 0, 2, 0
    while i < len(edits):
        header = re.fullmatch(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:[^\n]*)\n", edits[i]
        )
        if header is None:
            raise Refused("invalid patch hunk: " + name)
        old_start, old_count, new_start, new_count = [
            int(v) if v is not None else 1 for v in header.groups()
        ]
        target = old_start - 1 if old_start else 0
        if not cursor <= target <= len(lines):
            raise Refused("overlapping/out-of-range patch: " + name)
        out.extend(lines[cursor:target])
        cursor = target
        if len(out) != (new_start - 1 if new_start else 0):
            raise Refused("new patch index mismatch: " + name)
        consumed = produced = 0
        i += 1
        hunks += 1
        while i < len(edits) and not edits[i].startswith("@@ "):
            edit = edits[i]
            if not edit or edit[0] not in " +-":
                raise Refused("unsupported patch line: " + name)
            prefix, content = edit[0], edit[1:]
            if prefix in " -":
                if cursor >= len(lines) or lines[cursor] != content:
                    raise Refused("exact patch context mismatch: " + name)
                cursor += 1
                consumed += 1
            if prefix in " +":
                out.append(content)
                produced += 1
            i += 1
        if consumed != old_count or produced != new_count:
            raise Refused("patch hunk count mismatch: " + name)
    if not hunks:
        raise Refused("empty patch: " + name)
    out.extend(lines[cursor:])
    return "".join(out).encode("utf-8")


@dataclass(frozen=True)
class Entry:
    name: str
    before: bytes | None
    mode: int
    after: bytes

    def report(self):
        return {
            "name": self.name,
            "original_sha256": None if self.before is None else digest(self.before),
            "post_sha256": digest(self.after),
            "mode": oct(self.mode),
            "action": "already_installed"
            if self.before == self.after
            else "create"
            if self.before is None
            else "patch",
        }


@dataclass(frozen=True)
class Plan:
    package: Path
    artifacts: Path
    entries: tuple
    source_hashes: dict

    def report(self):
        return {
            "package": str(self.package),
            "files": [e.report() for e in self.entries],
        }


def prepare(package, artifacts):
    """Prepare the explicitly supplied package; no machine path is inferred."""
    package = checked_path(package)
    artifacts = checked_path(artifacts)
    sources = {}

    def source(name):
        raw, _ = read_regular(artifacts / name)
        sources[name] = digest(raw)
        return raw

    hashes = json.loads(source("baseline_hashes.json"))
    if set(hashes) != set(SHARED):
        raise Refused("unexpected baseline manifest scope")
    entries = []
    for name in SHARED:
        raw, mode = read_regular(package / name)
        spec = hashes[name]
        if set(spec) != {"baseline_sha256", "patched_sha256"}:
            raise Refused("invalid baseline manifest")
        patch = source(name + ".patch")
        if digest(raw) == spec["patched_sha256"]:
            after = raw
        elif digest(raw) == spec["baseline_sha256"]:
            after = exact_patch(raw, patch, name)
            if digest(after) != spec["patched_sha256"]:
                raise Refused("postimage hash mismatch: " + name)
        else:
            raise Refused("shared baseline drift: " + name)
        compile(after, str(package / name), "exec")
        entries.append(Entry(name, raw, mode, after))
    for origin, name in HELPERS.items():
        after = source(origin)
        compile(after, str(package / name), "exec")
        raw, mode = read_regular(package / name, optional=True)
        if raw is not None and raw != after:
            raise Refused("unknown existing helper: " + name)
        if raw is not None and mode != 0o644:
            raise Refused("helper mode drift: " + name)
        entries.append(Entry(name, raw, 0o644, after))
    return Plan(package, artifacts, tuple(entries), sources)


def _recheck(plan, installed):
    for name, expected in plan.source_hashes.items():
        raw, _ = read_regular(plan.artifacts / name)
        if digest(raw) != expected:
            raise Refused("source artifact drift: " + name)
    for entry in plan.entries:
        raw, mode = read_regular(
            plan.package / entry.name, optional=entry.before is None
        )
        expected = entry.after if entry.name in installed else entry.before
        if raw != expected or (raw is not None and mode != entry.mode):
            raise Refused("execution-time target drift: " + entry.name)


_UNSPECIFIED = object()


def _atomic(path, data, mode, *, expected=_UNSPECIFIED):
    """Exclusive temp plus rename in the same checked directory."""
    path = checked_path(path, allow_missing=True)
    parent = checked_path(path.parent)
    fd_dir = _open_directory(parent)
    temp = "." + path.name + ".manual-install-" + os.urandom(12).hex()
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=fd_dir,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short atomic write")
                view = view[written:]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        # Recheck immediately before rename; callers also recheck the full plan.
        checked_path(path, allow_missing=True)
        if expected is not _UNSPECIFIED:
            current, current_mode = read_regular(path, optional=expected is None)
            if current != expected or (current is not None and current_mode != mode):
                raise Refused(
                    "target drift immediately before replacement: " + str(path)
                )
        os.replace(temp, path.name, src_dir_fd=fd_dir, dst_dir_fd=fd_dir)
        os.fsync(fd_dir)
    finally:
        try:
            os.unlink(temp, dir_fd=fd_dir)
        except FileNotFoundError:
            pass
        os.close(fd_dir)


def _new_archive(path):
    """Create explicit archive through pinned no-follow directory descriptors."""
    checked_path(path, allow_missing=True)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for index, component in enumerate(path.parts[1:]):
            last = index == len(path.parts[1:]) - 1
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
            except FileExistsError:
                if last:
                    raise Refused("archive must be a new directory") from None
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def recovery_status(plan):
    """Inspect EVERY target; an installed list is never replacement evidence."""
    observed = []
    for entry in plan.entries:
        result = {
            "name": entry.name,
            "classification": "unknown",
            "actual_sha256": None,
        }
        try:
            raw, mode = read_regular(plan.package / entry.name, optional=True)
            result["actual_sha256"] = None if raw is None else digest(raw)
            result["actual_mode"] = None if mode is None else oct(mode)
            if raw == entry.after and mode == entry.mode:
                result["classification"] = "installed"
            elif raw == entry.before and (raw is None or mode == entry.mode):
                result["classification"] = "original"
        except (Refused, OSError) as exc:
            result["reason"] = str(exc)
        observed.append(result)
    return observed


def apply(plan, archive):
    """Back up exact originals first; retain explicit recovery data on failure.

    No auto rollback: a concurrent human edit must never be overwritten by an
    inferred rollback. Each file replacement is atomic, not a multi-file commit.
    """
    archive = checked_path(archive, allow_missing=True)
    if archive.exists():
        raise Refused("archive must be a new directory")
    _recheck(plan, set())
    _new_archive(archive)
    installed = set()
    manifest = {
        **plan.report(),
        "installed": [],
        "pending": None,
        "complete": False,
        "recovery": "Stop here. Do not restart services. Inspect ALL files in the plan, including pending and names absent from installed. Compare each actual hash and mode with original_sha256/post_sha256: classify original, installed, or unknown; never infer status from installed alone. For installed originals, restore archived bytes with recorded mode only after checking the current post hash. A newly created helper may be removed only after verifying its exact post hash. Preserve unknown files for root review. A rename or manifest write may have succeeded before reporting an error. Root driver must review recovery.",
    }
    try:
        for entry in plan.entries:
            if entry.before is not None:
                _atomic(archive / entry.name, entry.before, entry.mode)
        _atomic(
            archive / "manifest.json",
            json.dumps(manifest, indent=2).encode() + b"\n",
            0o644,
        )
        for entry in plan.entries:
            _recheck(plan, installed)
            if entry.before == entry.after:
                continue
            # Persist intent before any target rename. Even a successful rename
            # followed by fsync failure remains discoverable through pending.
            manifest["pending"] = entry.name
            _atomic(
                archive / "manifest.json",
                json.dumps(manifest, indent=2).encode() + b"\n",
                0o644,
            )
            _recheck(plan, installed)
            _atomic(
                plan.package / entry.name,
                entry.after,
                entry.mode,
                expected=entry.before,
            )
            installed.add(entry.name)
            manifest["installed"] = sorted(installed)
            manifest["pending"] = None
            _atomic(
                archive / "manifest.json",
                json.dumps(manifest, indent=2).encode() + b"\n",
                0o644,
            )
        _recheck(plan, installed)
        manifest["complete"] = True
        _atomic(
            archive / "manifest.json",
            json.dumps(manifest, indent=2).encode() + b"\n",
            0o644,
        )
    except Exception as exc:
        raise Refused(
            "installation interrupted; installed="
            + repr(sorted(installed))
            + "; archive="
            + str(archive)
            + ". Do not restart services. Inspect ALL plan targets, never installed alone. Observed="
            + json.dumps(recovery_status(plan), sort_keys=True)
            + ". Review manifest.json and archived originals before recovery. Cause: "
            + str(exc)
        ) from exc
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only check (default)")
    mode.add_argument(
        "--apply",
        action="store_true",
        help="explicitly write the reviewed fixed package",
    )
    parser.add_argument(
        "--package",
        type=Path,
        required=True,
        help="explicit authorized ROS package directory; no default target",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        help="existing authorized archive containment directory (required for apply)",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="new backup directory within --archive-root (required for apply)",
    )
    args = parser.parse_args(argv)
    try:
        # Explicit offline tool configuration does not authorize a target.
        # The caller must already have approval for package and archive paths.
        package = checked_path(args.package)
        artifacts = checked_path(Path(__file__).absolute().parent)
        plan = prepare(package, artifacts)
        print(json.dumps(plan.report(), indent=2))
        if args.apply:
            if args.archive is None or args.archive_root is None:
                raise Refused("--apply requires explicit --archive-root and --archive")
            archive_root = checked_path(args.archive_root)
            if not archive_root.is_dir():
                raise Refused("--archive-root must be an existing directory")
            checked_path(args.archive, allow_missing=True)
            if args.archive == archive_root or not args.archive.is_relative_to(
                archive_root
            ):
                raise Refused("archive must be a new child of explicit --archive-root")
            if all(e.before == e.after for e in plan.entries):
                print(
                    "Already installed exactly; no files changed and no archive created."
                )
            else:
                manifest = apply(plan, args.archive)
                print(
                    "Installed reviewed files; services were not restarted. Archive: "
                    + str(args.archive)
                )
                print(
                    json.dumps(
                        {
                            "complete": manifest["complete"],
                            "installed": manifest["installed"],
                        }
                    )
                )
        elif args.archive is not None or args.archive_root is not None:
            raise Refused("--archive and --archive-root are only accepted with --apply")
        return 0
    except (Refused, OSError, ValueError, SyntaxError, UnicodeDecodeError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
