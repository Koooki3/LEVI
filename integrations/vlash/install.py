"""Install only new, isolated VLASH adapters; never edits existing client code."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path

FILES = ("runtime.py", "serve_policy_vlash.py", "run_collect_vlash.py")


def no_links(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("an absolute path without parent traversal is required")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError("symlink refused")


def install(root, apply=False):
    no_links(root)
    if not root.is_dir():
        raise ValueError("openpi checkout missing")
    for name, required in (
        ("run_robotiq_client.py", "scheduler_factory"),
        ("run_robotiq_client_collect_data.py", "server_validator"),
    ):
        target = root / "examples/fr3_local" / name
        no_links(target)
        tree = ast.parse(target.read_text())
        wanted = "_run_episode" if name == "run_robotiq_client.py" else "main"
        fn = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == wanted
        )
        if required not in [a.arg for a in fn.args.args + fn.args.kwonlyargs]:
            raise ValueError("approved client extension missing: " + name)
    dest = root / "lab/vlash"
    no_links(dest)
    source = Path(__file__).parent
    records = []
    for name in FILES:
        data = (source / name).read_bytes()
        ast.parse(data)
        target = dest / name
        no_links(target)
        if target.exists() and (not target.is_file() or target.read_bytes() != data):
            raise ValueError(
                "refusing to overwrite different isolated adapter: " + name
            )
        records.append({"name": name, "sha256": hashlib.sha256(data).hexdigest()})
    if apply:
        dest.mkdir(parents=True, exist_ok=True)
        for row in records:
            target = dest / row["name"]
            if target.exists():
                continue
            fd = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write((source / row["name"]).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        for row in records:
            assert (
                hashlib.sha256((dest / row["name"]).read_bytes()).hexdigest()
                == row["sha256"]
            )
    return {"applied": apply, "files": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openpi-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.openpi_root, args.apply), indent=2))
