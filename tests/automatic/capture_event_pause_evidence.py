"""Capture one <=3 second local CPU check with raw output and source identity."""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("label")
    parser.add_argument("--kernel-root", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not command:
        parser.error("one explicit CPU check command is required")
    folder = ROOT / ".state/tmp/intervention-evidence-20261011"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".janitor-keep").touch()
    output, metadata = folder / f"{args.label}.log", folder / f"{args.label}.json"
    if output.exists() or metadata.exists():
        raise ValueError("evidence labels are immutable; use a new label")
    kernel = Path(args.kernel_root).resolve()
    source_files = [
        ROOT / name
        for name in (
            "levi/domain/aeri.py",
            "levi/automatic/orchestrator.py",
            "levi/automatic/intervention.py",
            "levi/automatic/termination.py",
            "levi/automatic/adapters/legacy_live.py",
            "levi/automatic/recorder.py",
            "levi/live/gpumgr.py",
            "levi/live/sessions.py",
            "levi/live/config.py",
            "levi/automatic/state_machine.py",
            "tests/automatic/test_event_pause_orchestrator_runtime.py",
            "tests/automatic/test_event_pause_judge.py",
            "tests/automatic/aeri_harness.py",
            "tests/automatic/test_aeri_state_machine.py",
            "tests/automatic/test_aeri_termination.py",
            "docs/AUTOMATIC_PIPELINE.md",
            "docs/AUTOMATIC_PIPELINE.zh-CN.md",
            "tests/automatic/capture_event_pause_evidence.py",
        )
    ]
    source_files += [
        kernel / name
        for name in (
            "levi/domain/aeri.py",
            "levi/live/runtime_ownership.py",
            "levi/live/workload_broker.py",
        )
    ]
    sources = {str(path): digest(path) for path in source_files}

    def head(root):
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=3
        ).strip()

    record = {
        "at": datetime.datetime.now(datetime.UTC).isoformat(),
        "argv": command,
        "cwd": str(ROOT),
        "timeout_seconds": 3,
        "source_head": head(ROOT),
        "kernel_head": head(kernel),
        "source_sha256": sources,
        "source_manifest_sha256": hashlib.sha256(
            json.dumps(sources, sort_keys=True).encode()
        ).hexdigest(),
        "environment": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
            "LEVI_JOINT_KERNEL_ROOT": str(kernel),
        },
    }
    env = {**os.environ, **record["environment"]}
    with output.open("wb") as stream:
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=3,
                check=False,
            )
            record["returncode"] = completed.returncode
        except subprocess.TimeoutExpired:
            record["returncode"] = 124
            record["timed_out"] = True
    record["log_sha256"] = digest(output)
    metadata.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(output.read_text(), end="")
    print("evidence: " + str(metadata))
    return record["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
