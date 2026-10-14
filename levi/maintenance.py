"""Explicit, bounded cache cleanup; durable data and installed runtimes are excluded."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .paths import PROJECT, ROOT, STATE
from .workspace import CACHE_REL, Workspace


def candidates(project=PROJECT, root=ROOT, with_project=True):
    """The regenerable paths a clean may remove. ``with_project=False`` leaves out
    the checkout's own caches and lists only what lives under ``root``."""
    paths = []
    if with_project:
        paths += [
            project / ".next/cache",
            project / ".pytest_cache",
            project / ".ruff_cache",
            project / "tsconfig.tsbuildinfo",
            project / "tsconfig.test.tsbuildinfo",
        ]
    paths.append(root / ".cache/levi")
    if with_project:
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
PER_DATASET = (
    "annotations",
    "object_annotations",
    "views",
    "agent/datasets",
    "recap_values",
)


def _workspace(ws):
    """The explicit workspace, or the module's own ``ROOT``/``STATE`` read now.

    Reading the globals at call time keeps a caller (or test) that sets
    ``maintenance.STATE`` working; new callers pass a ``Workspace`` instead.
    """
    return ws if ws is not None else Workspace(ROOT, STATE, ROOT / CACHE_REL)


def orphans(ws=None):
    """Artifacts and links left behind by datasets that are no longer registered.

    Removing a dataset from the catalog deliberately keeps its annotations --
    nobody's review work is deleted because a path moved. The cost is that the
    leftovers become invisible: an empty shell directory, or a redirect that
    now leads nowhere. Reporting them is the point; only the empty shells are
    ever offered for removal.

    An explicit ``ws`` is for tests and for callers registered for it later;
    the CLI (``levi clean``) and the service never pass one, and
    ``tests/test_workspace_explicit.py`` checks that nothing does.
    """
    from .catalog import aliases, datasets, read

    if ws is None:
        # Legacy path: the catalog's own module-level STATE (tests patch it).
        registered, links, state = datasets(), aliases(), STATE
    else:
        ws = _refuse_unless_a_workspace(ws, apply=False)
        registered = read(ws.state / "datasets.json", {})
        links = read(ws.state / "dataset_aliases.json", {})
        state = ws.state
    known = set(registered)
    stale_links = {old: target for old, target in links.items() if target not in known}
    empty, occupied = [], []
    for family in PER_DATASET:
        root = state / family
        # A symlinked folder leads outside the workspace: never followed.
        if root.is_symlink() or not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_symlink() or not child.is_dir() or child.name in known:
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


def _refuse_unless_a_workspace(ws, apply):
    """Check an explicit ``Workspace`` before anything under it is read or
    removed, and return it with resolved paths (used from then on).

    A ``Workspace`` is not validated when it is built, and on this machine one
    OS account is shared by several people, so ownership tells nobody apart.
    The root is never the filesystem root, a home folder or a folder above one
    (HOME may be changed, so the account's own home counts too); the state is
    the standard layout under the root (``<root>/outputs/LEVI/workbench``);
    and removing anything needs the catalog LEVI itself writes there."""
    import pwd

    from .workspace import STATE_REL

    root = Path(ws.root).resolve()
    homes = {Path.home().resolve(), Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()}
    if root == Path("/") or any(home.is_relative_to(root) for home in homes):
        raise ValueError(f"Refusing to clean {ws.root}: not a LEVI workspace root")
    state = Path(ws.state).resolve()
    if state != (root / STATE_REL).resolve():
        raise ValueError(
            f"Refusing to clean {ws.root}: its state {ws.state} is not the standard layout {root / STATE_REL}"
        )
    if apply and not (state / "datasets.json").is_file():
        raise ValueError(
            f"Refusing to clean {ws.root}: {state / 'datasets.json'} does not exist, so it is not a LEVI workspace"
        )
    return Workspace(root=root, state=state, cache=Path(ws.cache))


def clean(apply=False, ws=None):
    """Plan (or with ``apply`` do) the bounded clean-up of one workspace.

    ``ws`` names the workspace whose datasets, running-service marker, orphans and
    ``<root>/.cache/levi`` are examined (``ws.cache`` is not consulted yet). The
    checkout's own regenerable caches (``.next/cache``, ``__pycache__``,
    ``.ruff_cache``...) belong to the project, not to a workspace, and the
    running-service check above looks only at ``ws``: so with an explicit ``ws``
    they are never listed or removed. The training-pool sweep works on the
    process-wide workspace, so with an explicit ``ws`` it is skipped (reported as
    such) rather than run against a different workspace than the one named.

    An explicit ``ws`` is for tests and for callers registered for it later;
    the CLI (``levi clean``) and the service never pass one, and
    ``tests/test_workspace_explicit.py`` checks that nothing does.
    """
    if ws is not None:
        ws = _refuse_unless_a_workspace(ws, apply)
    state = _workspace(ws).state
    marker = state / "server.pid"
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
    leftovers = orphans(ws)
    from .catalog import read

    # A given workspace names its own root and only its own root: the checkout's
    # caches are not touched on the strength of a service check made elsewhere.
    # Otherwise `candidates()` keeps the defaults it was imported with (a test
    # may replace the function).
    find = (
        candidates
        if ws is None
        else (lambda: candidates(PROJECT, ws.root, with_project=False))
    )
    registered = [
        Path(item["path"]).resolve()
        for item in read(state / "datasets.json", {}).values()
    ]
    eligible = [
        p
        for p in find()
        if not any(
            r == p.resolve() or r.is_relative_to(p.resolve()) for r in registered
        )
    ]
    entries = [{"path": str(p), "bytes": size(p)} for p in eligible]
    if apply:
        for row in entries:
            path = Path(row["path"])
            # Re-evaluate allowlist immediately before deleting; symlinks are never followed.
            if path not in find():
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
                and shell.is_relative_to(state)
                and not any(shell.rglob("*"))
            ):
                shell.rmdir()
    # Training-pool leftovers: expired partial exports, old job files and
    # stale temporaries (a running job's files and finished exports are never
    # touched; the service is stopped here anyway).
    from .pool import cleanup as pool_cleanup

    if ws is None:
        pool = pool_cleanup.sweep(dry_run=not apply)
    else:
        pool = {
            "dry_run": not apply,
            "removed": [],
            "bytes": 0,
            "skipped": "the pool sweep is process-wide; not run for an explicit workspace",
        }
    return {
        "applied": apply,
        "bytes": sum(r["bytes"] for r in entries) + pool["bytes"],
        "entries": entries,
        "pool": pool,
        "orphans": leftovers,
        "preserved": [
            "datasets",
            "annotations",
            "reviews",
            "job reports",
            "finished training-pool exports",
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
