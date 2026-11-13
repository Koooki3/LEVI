"""Run the fixed offline scenario matrix without a web server or devices.

    python -m levi.automatic.offline_report --workspace /path/to/isolated/state --seed 42

This executes child workers and preserves their evidence. It neither calls an
LLM nor evaluates a physical grasp. A repeat request in the same workspace
returns the existing run; use a new workspace to evaluate another code version.
"""

import argparse
import time
from pathlib import Path

from levi.automatic.offline_worker import atomic_json
from levi.marvel0.reset_harness import TERMINAL, HarnessError, HarnessManager

EXPECTED = {
    "normal_random": "succeeded",
    "empty_grasp_retry": "succeeded",
    "held_unknown": "blocked",
    "stale_obs": "blocked",
    "handoff_mismatch": "blocked",
    "unreachable": "blocked",
    "stop": "stopped",
}


def run_matrix(workspace: Path, seed: int) -> dict:
    manager = HarnessManager(workspace / "offline-reset-harness")
    results = []
    began = time.monotonic()
    for scenario, expected in EXPECTED.items():
        deadline = time.monotonic() + 35
        while True:
            try:
                run = manager.start(f"matrix_{scenario}_{seed}", scenario, seed)
                break
            except HarnessError as error:
                if error.code != "run_active" or time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        while run["status"] not in TERMINAL:
            if time.monotonic() > deadline:
                raise TimeoutError("offline matrix worker timed out")
            time.sleep(0.01)
            run = manager.get(run["run_id"])
        results.append(
            {
                "scenario_id": scenario,
                "expected_status": expected,
                "matched": run["status"] == expected,
                "run": run,
            }
        )
    report = {
        "schema": "levi.reset_harness.matrix.v1",
        "execution_mode": "offline",
        "physical_execution": False,
        "provider": "scripted",
        "seed": seed,
        "passed": all(row["matched"] for row in results),
        "results": results,
        "wall_ms": round((time.monotonic() - began) * 1000),
        "llm_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
    }
    atomic_json(workspace / "reset-harness-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    report = run_matrix(args.workspace, args.seed)
    for row in report["results"]:
        print(
            f"{row['scenario_id']}: {row['run']['status']} (expected {row['expected_status']})"
        )
    print(f"offline matrix matched: {report['passed']}; wall_ms: {report['wall_ms']}")
    print(args.workspace / "reset-harness-report.json")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
