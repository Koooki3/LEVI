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
        help="Regenerate the repository-owned schema snapshots (contracts.json "
        "and the AERI schemas together: nothing is written when the AERI "
        "change is refused)",
    )
    parser.add_argument(
        "--accept-breaking",
        action="store_true",
        help="With --write: also rewrite AERI schemas whose change is breaking "
        "within their major version; only before that version is released",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Ref whose AERI snapshots (at the merge base with HEAD) the models "
        "are compared with; default: $LEVI_CONTRACT_BASE, else local main, "
        "else origin/main. Unreadable means failure",
    )
    args = parser.parse_args(arguments)
    from . import aeri

    if args.accept_breaking and not args.write:
        parser.error("--accept-breaking only goes with --write")
    if args.accept_breaking and aeri.RELEASED:
        parser.error(
            f"AERI v{aeri.MAJOR} is released: a breaking change needs a new major"
        )
    root = Path(__file__).resolve().parents[2]
    destination = root / "docs/architecture/contracts.json"
    expected = render()
    if args.write:
        refusals = aeri.write_snapshots(root, accept_breaking=args.accept_breaking)
        for line in refusals:
            print(line)
        if refusals:
            print("Nothing written: a breaking change needs a new major version")
            return 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(expected)
        print(f"Updated docs/architecture/contracts.json and {aeri.SNAPSHOT_DIR}/")
        return 0
    problems = aeri.check_snapshots(root)
    against, notes = aeri.check_against_base(root, args.base)
    more, more_notes = aeri.check_campaign_against_base(root, args.base)
    against, notes = against + more, notes + more_notes
    for line in notes:
        print(f"note: {line}")
    for line in problems + against:
        print(line)
    if problems or not destination.exists() or destination.read_text() != expected:
        print("Contract snapshot drift. Run: uv run levi dev check-contracts --write")
        return 1
    if against:
        if any(line.startswith("cannot read the base") for line in against):
            print(
                "The base branch's AERI snapshots could not be read: nothing compared"
            )
        else:
            print(
                "AERI contracts differ from the base branch in a way v1 does not allow"
            )
        return 1
    print(
        "Contract snapshot matches Python schemas; this is not full architecture acceptance."
    )
    return 0
