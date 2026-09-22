"""Deterministic public contract snapshot; no service or model initialization."""

import json
from pathlib import Path


def catalog():
    from levi.agent.schema import ModelOutput, ProviderConfig, TaskContext

    from .contracts import Recipe, ToolSpec

    return {
        "schema_version": 1,
        "status": "incremental-contract-baseline",
        "contracts": {
            model.__name__: model.model_json_schema()
            for model in (ToolSpec, Recipe, ProviderConfig, TaskContext, ModelOutput)
        },
    }


def render():
    return json.dumps(catalog(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(arguments):
    import argparse

    parser = argparse.ArgumentParser(
        description="Check the public execution contract snapshot"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate the repository-owned schema snapshot",
    )
    args = parser.parse_args(arguments)
    destination = (
        Path(__file__).resolve().parents[2] / "docs/architecture/contracts.json"
    )
    expected = render()
    if args.write:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(expected)
        print("Updated docs/architecture/contracts.json")
        return 0
    if not destination.exists() or destination.read_text() != expected:
        print("Contract snapshot drift. Run: uv run levi dev check-contracts --write")
        return 1
    print(
        "Contract snapshot matches Python schemas; this is not full architecture acceptance."
    )
    return 0
