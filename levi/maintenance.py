"""Explicit, bounded cache cleanup; durable data and installed runtimes are excluded."""

import argparse
import json
import os
import shutil
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

    return sorted(set(p for p in paths if safe(p)))


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


def clean(apply=False):
    marker = STATE / "server.pid"
    if marker.exists():
        try:
            os.kill(int(marker.read_text()), 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("Stop the LEVI service before cleaning caches")
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
    return {
        "applied": apply,
        "bytes": sum(r["bytes"] for r in entries),
        "entries": entries,
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
    print(json.dumps(clean(args.apply), ensure_ascii=False, indent=2))
