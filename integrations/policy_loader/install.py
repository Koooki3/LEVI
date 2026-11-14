"""Install the policy_loader files into an openpi checkout's ``lab/policy_loader/``; never edits existing openpi code.

    python3 integrations/policy_loader/install.py --openpi-root /path/to/openpi           # dry run
    python3 integrations/policy_loader/install.py --openpi-root /path/to/openpi --apply

Writes only ``lab/policy_loader/{serve_loader,reset_r1_loader}.py`` and ``installation-receipt.json`` (the files'
sha256, written last). ``lab/`` of an openpi checkout is git-excluded, so nothing reaches an openpi commit. It refuses
symlinks and a different existing file (to update, delete the installed file first), checks that the checkout has the
``openpi.serving`` server and ``scripts/serve_policy.py`` the loader entry relies on, and never installs packages,
starts a service or opens a port. Run it only when the openpi checkout's owner agrees: this repository never installs by
itself (docs/POLICY_LOADER.md).
"""

import argparse
import ast
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

FILES = ("serve_loader.py", "reset_r1_loader.py")
DEST = "lab/policy_loader"
RECEIPT = "installation-receipt.json"
RECEIPT_SCHEMA = "policy_loader.installation.v1"
# What the loader entry imports or mirrors in the openpi checkout (checked to exist, never read for content).
NEEDED = ("src/openpi/serving/websocket_policy_server.py", "scripts/serve_policy.py")


def no_links(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("an absolute path without parent traversal is required")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError("symlink refused: " + str(part))


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install(root, apply=False) -> dict:
    root = Path(root)
    no_links(root)
    if not root.is_dir():
        raise ValueError("openpi checkout missing: " + str(root))
    for rel in NEEDED:
        no_links(root / rel)
        if not (root / rel).is_file():
            raise ValueError(
                f"{rel} is missing: not an openpi checkout this loader can serve from"
            )
    dest = root / DEST
    no_links(dest)
    source = Path(__file__).resolve().parent
    records = []
    for name in FILES:
        data = (source / name).read_bytes()
        ast.parse(data)
        target = dest / name
        no_links(target)
        if target.exists() and (not target.is_file() or target.read_bytes() != data):
            raise ValueError(
                "refusing to overwrite a different installed file: " + name
            )
        records.append({"name": name, "sha256": hashlib.sha256(data).hexdigest()})
    receipt = {"schema": RECEIPT_SCHEMA, "files": records}
    if apply:
        dest.mkdir(parents=True, exist_ok=True)
        for row in records:
            target = dest / row["name"]
            if not target.exists():
                _write_new(target, (source / row["name"]).read_bytes())
            if hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError(
                    "installed file differs from its source: " + row["name"]
                )
        receipt["installed_iso"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
        tmp = dest / (RECEIPT + ".tmp")
        no_links(tmp)
        if tmp.exists():
            tmp.unlink()
        _write_new(tmp, text)
        os.replace(tmp, dest / RECEIPT)
    return {"applied": apply, **receipt}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--openpi-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.openpi_root, args.apply), indent=2))
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
