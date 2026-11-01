"""Install only new, isolated chunk-acceleration adapters into an openpi checkout; never edits existing client code."""

import argparse
import ast
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parent))
import client_check  # stdlib only; the same check run_collect_phase.py repeats at every start

# v1/v2 (accel.py, run_collect_accel.py) first: their installed bytes are never rewritten (a different existing file is
# refused). The rest is the v3 phase clock: the core, the schedulers, the entry and the client check they share.
FILES = (
    "accel.py",
    "run_collect_accel.py",
    "phase.py",
    "schedulers.py",
    "run_collect_phase.py",
    "client_check.py",
)
RECEIPT = "installation-receipt.json"
RECEIPT_SCHEMA = "chunk_accel.installation.v1"


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
    """What the adapters wrap must exist as they expect.

    v1/v2 (run_collect_accel.py): the policy query, the episode, the action check, the eval log's header, and the
    recorder's metadata, close (with extra) and abort (with detail).
    v3 (run_collect_phase.py, client_check.py): also the RTC hook (_rtc_chunker called by _run_episode), the calls the
    loop makes on the scheduler and the recorder, the policy payload helpers, RtcChunker.__init__'s three fields, the
    collector's scheduler_factory / server_validator, and the VLASH runtime's symbols.
    Returns {name: sha256} of the four client files (rc, rtcc, coll, vrt), hashed from the bytes that were parsed."""
    rc = ast.parse((root / "examples/fr3_local/run_robotiq_client.py").read_text())
    for fn in ("_query_policy", "_run_episode", "_clamp_action", "EvalLog"):
        if _defines(rc, fn) is None:
            raise ValueError("client function missing: " + fn)
    log = _defines(rc, "EvalLog")
    if not any(
        isinstance(n, ast.Attribute) and n.attr == "header" for n in ast.walk(log)
    ):
        raise ValueError("EvalLog no longer keeps a header")
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
    return client_check.check_client(root)


def _write_receipt(path, receipt):
    """Atomic write of the receipt next to the adapters (a new file, never an adapter)."""
    no_links(path)
    tmp = path.with_name(path.name + ".tmp")
    no_links(tmp)
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def install(root, apply=False):
    no_links(root)
    if not root.is_dir():
        raise ValueError("openpi checkout missing")
    for rel in client_check.CLIENT_FILES.values():
        no_links(root / rel)
    shas = check_client(root)
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
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "files": records,
        "client": {
            name: {"path": client_check.CLIENT_FILES[name], "sha256": sha}
            for name, sha in shas.items()
        },
        "vrt_reviewed_sha256": client_check.VRT_REVIEWED_SHA256,
        "vrt_matches_reviewed": shas["vrt"] == client_check.VRT_REVIEWED_SHA256,
    }
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
        # last: the receipt run_collect_phase.py compares the client files against (code_drift). It is rewritten on
        # every apply, so applying again after the client changed re-baselines it (deliberately a human step).
        receipt["installed_iso"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_receipt(dest / RECEIPT, receipt)
    return {"applied": apply, **receipt}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openpi-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.openpi_root, args.apply), indent=2))
