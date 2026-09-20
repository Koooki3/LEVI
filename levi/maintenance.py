"""Explicit, bounded cache cleanup; durable data and installed runtimes are excluded."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .paths import PROJECT, ROOT, STATE


def candidates(project=PROJECT, root=ROOT):
    paths = [
        project / ".next/cache",
        project / ".pytest_cache",
        project / ".ruff_cache",
        project / "tsconfig.tsbuildinfo",
        project / "tsconfig.test.tsbuildinfo",
        root / ".cache/levi",
    ]
    for directory in ["levi", "backend", "tests", "scripts"]:
        paths.extend((project / directory).rglob("__pycache__"))
    # Only the pinned Bun installer archives, never a general downloads folder.
    for platform in ["linux-x64", "linux-aarch64", "darwin-x64", "darwin-aarch64"]:
        paths.append(root / "tmp/downloads" / f"bun-{platform}.zip")

    def safe(path):
        base = project if path.is_relative_to(project) else root
        return (
            path.exists()
            and path.resolve().is_relative_to(base.resolve())
            and not any(
                p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(base)
            )
        )

    return sorted({p for p in paths if safe(p)})


def size(path):
    return (
        path.stat().st_size
        if path.is_file()
        else sum(
            p.stat().st_size
            for p in path.rglob("*")
            if p.is_file() and not p.is_symlink()
        )
    )


# Artifact trees keyed by catalog name; each entry is one dataset's sidecars.
PER_DATASET = ("annotations", "object_annotations", "views", "agent/datasets")


def orphans():
    """Artifacts and links left behind by datasets that are no longer registered.

    Removing a dataset from the catalog deliberately keeps its annotations --
    nobody's review work is deleted because a path moved. The cost is that the
    leftovers become invisible: an empty shell directory, or a redirect that
    now leads nowhere. Reporting them is the point; only the empty shells are
    ever offered for removal.
    """
    from .catalog import aliases, datasets

    known = set(datasets())
    stale_links = {
        old: target for old, target in aliases().items() if target not in known
    }
    empty, occupied = [], []
    for family in PER_DATASET:
        root = STATE / family
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in known:
                continue
            entry = {
                "path": str(child),
                "dataset": child.name,
                "bytes": size(child),
                "files": sum(1 for p in child.rglob("*") if p.is_file()),
            }
            (empty if entry["files"] == 0 else occupied).append(entry)
    return {
        "removable_empty": empty,
        "keeps_content": occupied,
        "stale_aliases": stale_links,
        "note": (
            "Artifacts with content are never removed automatically: register "
            "the dataset again to reach them, or delete the directory yourself."
        ),
    }


def clean(apply=False):
    marker = STATE / "server.pid"
    if marker.exists():
        try:
            pid = int(marker.read_text())
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            pass
        else:
            # Name the process and how to stop it: "the LEVI service" is often
            # the shared Agent Core, which outlives the web UI and has no
            # obvious owner in the terminal that hit this.
            raise ValueError(
                f"Stop the LEVI service before cleaning caches. It is running "
                f"as PID {pid}; stop it with: uv run levi stop"
            )
    leftovers = orphans()
    from .catalog import read

    registered = [
        Path(item["path"]).resolve()
        for item in read(STATE / "datasets.json", {}).values()
    ]
    eligible = [
        p
        for p in candidates()
        if not any(
            r == p.resolve() or r.is_relative_to(p.resolve()) for r in registered
        )
    ]
    entries = [{"path": str(p), "bytes": size(p)} for p in eligible]
    if apply:
        for row in entries:
            path = Path(row["path"])
            # Re-evaluate allowlist immediately before deleting; symlinks are never followed.
            if path not in candidates():
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    if apply:
        for row in leftovers["removable_empty"]:
            shell = Path(row["path"])
            # Only ever an empty directory, and only under the workbench.
            if (
                shell.is_dir()
                and shell.is_relative_to(STATE)
                and not any(shell.rglob("*"))
            ):
                shell.rmdir()
    return {
        "applied": apply,
        "bytes": sum(r["bytes"] for r in entries),
        "entries": entries,
        "orphans": leftovers,
        "preserved": [
            "datasets",
            "annotations",
            "reviews",
            "job reports",
            ".env",
            ".venv",
            ".runtime",
            "node_modules",
            "production build",
            "shared package and model caches",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = clean(args.apply)
    except ValueError as exc:
        # A precondition someone can act on is not a crash.
        print(f"[LEVI] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
