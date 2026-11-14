"""Adapters from the runs of the three arms to ``levi.eval.arm_record.v2``.

``direct`` (arm A), ``prior-levi`` (arm B) and ``levi2`` (arm C) each turn the
files a run left behind into one record that ``levi.eval.compare`` reads. They
read only the paths named, never call a model or open a port, write a new file
(never over an existing one) and keep what the files do not say as ``null``.

    python -m levi.eval.adapters direct --round round.json --result direct.json \\
        --wall-basis excludes_queue --quality-path derived_outcome.overall.accuracy \\
        --out direct_record.json
    python -m levi.eval.adapters prior-levi --round round.json --result run.json \\
        --ledger ledger.json --wall-basis excludes_queue --out prior_record.json
    python -m levi.eval.adapters levi2 --round round.json --route-log routes.jsonl \\
        --wall-basis includes_queue --out levi2_record.json

Layouts and what stays ``null``: ``hub`` (A, B), ``levi2`` (C), ``common``
(the round declaration, the rules).
"""

from .cli import main

__all__ = ["main"]
