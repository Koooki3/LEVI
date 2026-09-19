"""``uv run levi migrate``: move existing workspace state to the hash-free
naming scheme (see levi/naming.py and .state.md).

- ``datasets.json`` keyed by catalog name, old hash keys kept as aliases
  (``dataset_aliases.json``) so old ``/local/<hash>`` links keep working;
- the legacy ``<run>/dataset`` conversion layout re-homed: the dataset moves
  up to ``<run>/``, its reports into ``meta/``, the intermediate captures to
  ``jobs/<timestamp>/intermediate/``;
- reviews and diagnostics renamed to ``<dataset name>.json``;
- job records renamed to ``<timestamp>.*``;
- hash-suffixed annotated exports renamed to ``<name>_annotated``.

Dry run by default; ``--apply`` performs it. Idempotent, never deletes user
data (superseded files are moved aside), and refuses to run while the LEVI
service is up.
"""

import argparse
import json
import os
import re
from pathlib import Path

from . import catalog
from .naming import TIMESTAMP_PATTERN, timestamp_id, unique_name
from .paths import EXPORTS, configure

LEGACY_JOB = re.compile(r"(\d{8}-\d{6})_[0-9a-f]{6,}|[0-9a-f]{12,64}")
HASHED = re.compile(r"(.+)_[0-9a-f]{8,16}")


def _service_running(state: Path) -> bool:
    marker = state / "server.pid"
    if not marker.exists():
        return False
    try:
        os.kill(int(marker.read_text()), 0)
    except (ProcessLookupError, ValueError):
        return False
    return True


class Migration:
    def __init__(self, apply: bool):
        self.apply = apply
        self.actions: list[str] = []
        self.state = catalog.STATE

    def do(self, message: str, fn=None):
        self.actions.append(message)
        if self.apply and fn:
            fn()

    def move(self, src: Path, dst: Path):
        self.do(
            f"move {src} -> {dst}",
            lambda: (dst.parent.mkdir(parents=True, exist_ok=True), src.rename(dst)),
        )

    # --- steps --------------------------------------------------------------

    def rehome(self, items: dict):
        for item in items.values():
            path = Path(item["path"])
            run = path.parent
            if path.name != "dataset" or not (run / "preflight.json").exists():
                continue
            if not (path / "meta/info.json").exists():
                continue
            if any((run / name).exists() for name in ("meta", "data", "videos")):
                self.actions.append(
                    f"skip re-home of {run}: already has dataset folders"
                )
                continue
            intermediates = [
                run / n for n in ("capture", "filtered") if (run / n).is_dir()
            ]
            if intermediates:
                if self.apply:
                    stamp = timestamp_id(self.state / "jobs", create_dir=True)
                    target = self.state / "jobs" / stamp / "intermediate"
                else:
                    target = self.state / "jobs" / "<timestamp>" / "intermediate"
                for folder in intermediates:
                    name = "staged" if folder.name == "capture" else folder.name
                    self.move(folder, target / name)
            for child in sorted(path.iterdir()):
                self.move(child, run / child.name)
            for report, name in (
                ("preflight.json", "levi_preflight.json"),
                ("validation.json", "levi_validation.json"),
            ):
                if (run / report).exists():
                    self.move(run / report, run / "meta" / name)
            self.do(f"remove empty {path}", lambda p=path: p.rmdir())
            self.do(
                f"catalog path {path} -> {run}",
                lambda i=item, r=run: i.update(path=str(r)),
            )

    def rekey(self, items: dict) -> tuple[dict, dict]:
        aliases = dict(catalog.aliases())
        renamed = {}  # old name → new name (annotation folders follow)
        result = {}
        for key, item in items.items():
            if (
                key == item.get("name")
                and item.get("id") == f"local/{key}"
                and not catalog.GENERIC_FOLDER_NAMES & {key}
            ):
                result[key] = item
        for key, item in items.items():
            if key in result:
                continue
            path = Path(item["path"])
            if path.name == "dataset" and not self.apply:
                path = path.parent  # what rehome() will make of it
            name = unique_name(catalog._base_name(path), result.keys())
            old_name = item.get("name")
            if old_name and old_name != name:
                renamed[old_name] = name
            if key != name:
                aliases[key] = name
            if old_name and old_name != key and old_name != name:
                aliases.setdefault(old_name, name)
            self.actions.append(f"catalog {key!r} -> {name!r}")
            result[name] = {**item, "id": f"local/{name}", "name": name}
        return result, aliases, renamed

    def annotation_folders(self, renamed: dict):
        for base in ("annotations", "object_annotations"):
            for old, new in renamed.items():
                src, dst = self.state / base / old, self.state / base / new
                if src.is_dir() and not dst.exists():
                    self.move(src, dst)

    def per_dataset_files(self, folder: str, aliases: dict, names: set):
        root = self.state / folder
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.json")):
            if path.stem in names:
                continue
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            repo = value.get("repo_id") or ""
            key = repo.split("/", 1)[1] if repo.startswith("local/") else None
            name = aliases.get(key, key) if key else None
            if name not in names:
                match = HASHED.fullmatch(path.stem)
                name = match.group(1) if match and match.group(1) in names else None
            if not name:
                self.actions.append(f"leave {path} (no registered dataset)")
                continue
            target = root / f"{name}.json"
            value["repo_id"] = f"local/{name}"
            if target.exists():
                self.move(path, root / "superseded" / path.name)
                continue

            def write(p=path, t=target, v=value):
                catalog.atomic(t, v)
                p.unlink()

            self.do(
                f"rename {path.name} -> {target.name} (repo_id local/{name})", write
            )

    def jobs(self):
        root = self.state / "jobs"
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.json")):
            if path.name.count(".") != 1:
                continue
            old = path.stem
            match = LEGACY_JOB.fullmatch(old)
            if not match or re.fullmatch(TIMESTAMP_PATTERN, old):
                continue
            prefix = match.group(1) or "19700101-000000"
            new, n = f"{prefix}-000", 0
            while (root / f"{new}.json").exists():
                n += 1
                new = f"{prefix}-000-{n}"

            def rename(o=old, nw=new, p=path):
                value = json.loads(p.read_text())
                text = json.dumps(value).replace(o, nw)
                value = json.loads(text)
                for companion in root.glob(f"{o}.*"):
                    if companion.name != p.name:
                        companion.rename(root / companion.name.replace(o, nw, 1))
                catalog.atomic(root / f"{nw}.json", value)
                p.unlink()

            self.do(f"job {old} -> {new}", rename)

    def exports(self):
        if not EXPORTS.is_dir():
            return
        for path in sorted(EXPORTS.iterdir()):
            match = HASHED.fullmatch(path.name)
            if (
                not path.is_dir()
                or not match
                or not (path / ".levi-export.json").exists()
            ):
                continue
            base = match.group(1)
            name = base if base.endswith("_annotated") else f"{base}_annotated"
            if (EXPORTS / name).exists():
                self.actions.append(f"leave {path} ({name} exists)")
                continue
            self.move(path, EXPORTS / name)

    def run(self) -> list[str]:
        with catalog.locked():
            items = catalog.datasets()
            self.rehome(items)
            result, aliases, renamed = self.rekey(items)
            if self.apply and (result != items or aliases != catalog.aliases()):
                catalog.atomic(self.state / "datasets.json", result)
                catalog.atomic(self.state / "dataset_aliases.json", aliases)
        self.annotation_folders(renamed)
        names = set(result)
        full_aliases = {**aliases, **{k: k for k in names}}
        self.per_dataset_files("reviews", full_aliases, names)
        self.per_dataset_files("diagnostics", full_aliases, names)
        self.jobs()
        self.exports()
        return self.actions


def main():
    parser = argparse.ArgumentParser(
        description="Migrate LEVI workspace state to hash-free names"
    )
    parser.add_argument(
        "--apply", action="store_true", help="perform the changes (default: dry run)"
    )
    args = parser.parse_args()
    configure()
    if args.apply and _service_running(catalog.STATE):
        raise SystemExit("Stop the LEVI service (levi serve) before migrating")
    actions = Migration(args.apply).run()
    for line in actions:
        print(("" if args.apply else "[dry run] ") + line)
    if not actions:
        print("Nothing to migrate.")
    elif not args.apply:
        print("\nRe-run with --apply to perform these changes.")


if __name__ == "__main__":
    main()
