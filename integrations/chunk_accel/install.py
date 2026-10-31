"""Install only new, isolated chunk-acceleration adapters into an openpi checkout; never edits existing client code."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path

FILES = ("accel.py", "run_collect_accel.py")


def no_links(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("an absolute path without parent traversal is required")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError("symlink refused")


def _defines(tree, name):
    return next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name
        ),
        None,
    )


def check_client(root):
    """What run_collect_accel.py wraps must exist as it expects: the policy query and the recorder's metadata, close
    (with extra) and abort (with detail)."""
    rc = ast.parse((root / "examples/fr3_local/run_robotiq_client.py").read_text())
    if _defines(rc, "_query_policy") is None:
        raise ValueError("client function _query_policy missing")
    co = ast.parse(
        (root / "examples/fr3_local/run_robotiq_client_collect_data.py").read_text()
    )
    rec = _defines(co, "RolloutRecorder")
    if rec is None:
        raise ValueError("RolloutRecorder missing")
    methods = {
        n.name: [a.arg for a in n.args.args + n.args.kwonlyargs]
        for n in rec.body
        if isinstance(n, ast.FunctionDef)
    }
    if (
        "_session_metadata" not in methods
        or "extra" not in methods.get("close", [])
        or "detail" not in methods.get("abort", [])
    ):
        raise ValueError(
            "RolloutRecorder no longer has _session_metadata, close(extra) and abort(detail)"
        )


def install(root, apply=False):
    no_links(root)
    if not root.is_dir():
        raise ValueError("openpi checkout missing")
    for name in ("run_robotiq_client.py", "run_robotiq_client_collect_data.py"):
        no_links(root / "examples/fr3_local" / name)
    check_client(root)
    dest = root / "lab/chunk_accel"
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
                "refusing to overwrite a different isolated adapter: " + name
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
