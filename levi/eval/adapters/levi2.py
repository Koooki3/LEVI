"""Arm C (``levi2``): the guided LEVI 2.0 harness.

There is **no single run file for arm C yet** (the graded answer has no unified
implementation; EXPERIMENT-3ARM-PLAN section 6). This adapter therefore reads
the pieces that exist, and a record field whose piece is not given stays
``None``:

``--route-log`` (required)
    JSON lines, **one row per problem**, with the keys of
    ``levi.decision.router``'s ``RouteDecision`` and ``Outcome``: ``episode``,
    ``route`` (``local``, ``escalate``, ``human``), ``reason``, ``state``
    (``answered_local``, ``answered_external``, ``pending_external``,
    ``with_human``) and, optionally, ``label_tier``, ``contract`` and ``p_cal``.
    No writer for such a file exists yet; this fixes what the runner must write.
    A row whose ``route`` and ``state`` do not belong together, a repeated or an
    undeclared problem, and a log that does not cover the declared problems are
    refused. It fills ``graded`` (counts and rates over ``episode_count``):
    ``local_answered_count``, ``escalated_count`` (escalated **and** answered by
    the panel), ``pending_count`` (escalated, not yet answered), ``with_human_count``,
    ``coverage`` (answered locally / problems) and ``escalation_rate``
    (escalated / problems).
``--result`` / ``--ledger`` (optional)
    the local part of the run in the layout of arm B's files (see ``hub``):
    ``local_tokens``, ``wall_s``, quality. Not verified for arm C: no such file
    from a LEVI 2.0 run has been seen.
``--external-tokens`` and ``--external-task`` (optional)
    a ``levi.eval.token_monitor.v1`` report and the task label to read in it:
    the four counts, ``exact`` and a source code. Without them
    ``external_tokens`` is ``None`` (unknown), never 0.

Not carried into the record: the clip proposer's ``params_sha256`` (the record
has no field for it; the freeze document holds it), the calibration, per-item
confidences.
"""

from __future__ import annotations

from ..compare import _show
from ..tokens import FIELDS as TOKEN_FIELDS
from . import common, hub
from .common import AdapterError, Round

MONITOR_SCHEMA = "levi.eval.token_monitor.v1"
ROW_KEYS = ("episode", "route", "reason", "state", "label_tier", "contract", "p_cal")
ROUTES = {"local": "answered_local", "human": "with_human"}
ESCALATED_STATES = ("answered_external", "pending_external")
STATES = ("answered_local", "answered_external", "pending_external", "with_human")


def _contract():
    from levi.decision import contracts

    return contracts


def route_counts(rows: list, round_: Round) -> dict:
    """``graded`` from the route log."""
    contracts = _contract()
    routes = {r.value for r in contracts.Route}
    reasons = {r.value for r in contracts.Reason}
    tiers = {t.value for t in contracts.LabelTier}
    seen: set[str] = set()
    tally = dict.fromkeys(STATES, 0)
    for number, row in enumerate(rows, 1):
        where = f"route log row {number}"
        if not isinstance(row, dict):
            raise AdapterError(f"{where} is not an object")
        extra = sorted(set(row) - set(ROW_KEYS), key=str)
        if extra:
            raise AdapterError(f"{where} has unknown keys {[_show(k) for k in extra]}")
        for key in ("episode", "route", "state"):
            if key not in row:
                raise AdapterError(f"{where} lacks {key}")
        episode = row["episode"]
        if isinstance(episode, bool) or not isinstance(episode, (int, str)):
            raise AdapterError(f"{where}: episode must be an int or a string")
        episode = str(episode)
        if episode in seen:
            raise AdapterError(
                f"{where}: a problem appears twice (one row per problem)"
            )
        seen.add(episode)
        if row["route"] not in routes or row["state"] not in STATES:
            raise AdapterError(f"{where}: route or state is not a known value")
        if row.get("reason") is not None and row["reason"] not in reasons:
            raise AdapterError(f"{where}: reason is not a known value")
        if row.get("label_tier") is not None and row["label_tier"] not in tiers:
            raise AdapterError(f"{where}: label_tier is not a known value")
        if row.get("contract") not in (None, contracts.ROUTE_CONTRACT):
            raise AdapterError(f"{where}: contract is not {contracts.ROUTE_CONTRACT}")
        if row.get("p_cal") is not None:
            common.number(row["p_cal"], f"{where} p_cal", lo=0, hi=1)
        if row["route"] in ROUTES:
            fits = row["state"] == ROUTES[row["route"]]
        else:  # escalate
            fits = row["state"] in ESCALATED_STATES
        if not fits:
            raise AdapterError(f"{where}: state does not belong to route")
        tally[row["state"]] += 1
    if (
        common.episode_set_hash(seen) != round_.episode_set_hash
        or len(seen) != round_.episode_count
    ):
        raise AdapterError(
            "the route log's problems are not the declared problems "
            f"({len(seen)} rows against {round_.episode_count} declared)"
        )
    n = round_.episode_count
    escalated = tally["answered_external"] + tally["pending_external"]
    return {
        "local_answered_count": tally["answered_local"],
        "escalated_count": tally["answered_external"],
        "pending_count": tally["pending_external"],
        "with_human_count": tally["with_human"],
        "coverage": tally["answered_local"] / n,
        "escalation_rate": escalated / n,
    }


def external_fields(path, label: str, notes: list[str]) -> dict:
    """``external_tokens*`` from one task of a token monitor report."""
    report = common.read_json(path, "the external-token report")
    if not isinstance(report, dict) or report.get("schema") != MONITOR_SCHEMA:
        raise AdapterError(f"the external-token report is not a {MONITOR_SCHEMA} file")
    tasks = report.get("tasks")
    if not isinstance(tasks, dict) or label not in tasks:
        raise AdapterError("the external-token report has no task with that label")
    task = tasks[label]
    tokens = task.get("tokens") if isinstance(task, dict) else None
    if not isinstance(tokens, dict) or set(tokens) != set(TOKEN_FIELDS):
        raise AdapterError("the report's task has no tokens with the four fields")
    exact = task.get("exact")
    if not isinstance(exact, bool):
        raise AdapterError("the report's task has no boolean exact")
    counts = {
        name: common.count(tokens[name], f"tokens.{name}") for name in TOKEN_FIELDS
    }
    if any(v is None for v in counts.values()):
        notes.append(
            "external_tokens: a field is null in the report (unknown, kept null)"
        )
    code = "token_monitor.v1:" + ("exact" if exact else "lower_bound")
    return {
        "external_tokens": counts,
        "external_tokens_exact": exact,
        "external_tokens_source": code,
    }


def levi2(
    route_log,
    round_: Round,
    *,
    wall_basis,
    result_path=None,
    ledger_path=None,
    external_path=None,
    external_task=None,
    quality_path=None,
    scored_count_path=None,
    scores=None,
):
    """The ``levi2`` record. Returns ``(record dict, notes)``."""
    basis = common.check_basis(wall_basis)
    notes: list[str] = []
    graded = route_counts(common.read_jsonl(route_log, "the route log"), round_)
    if result_path:
        fields, more = hub.levi_run_fields(
            result_path,
            round_,
            ledger_path=ledger_path,
            quality_path=quality_path,
            scored_count_path=scored_count_path,
            scores=scores,
            what="levi2",
        )
        notes += more
        # The local run's own external accounting is arm B's rule (a local
        # provider means zero); arm C escalates, so only the monitor report
        # may state its external tokens.
        for name in (
            "external_tokens",
            "external_tokens_exact",
            "external_tokens_source",
        ):
            fields[name] = None
    else:
        if ledger_path:
            raise AdapterError("--ledger needs --result")
        if quality_path or scored_count_path:
            raise AdapterError("--quality-path needs --result")
        fields = {
            "wall_s": None,
            "local_tokens": None,
            "blocked": None,
            **common.quality_fields({}, None, None, scores, notes),
        }
        notes.append("local_tokens and wall_s: no --result, so they are unknown (null)")
    if external_path:
        if not external_task:
            raise AdapterError("--external-tokens needs --external-task")
        fields.update(external_fields(external_path, external_task, notes))
    elif external_task:
        raise AdapterError("--external-task needs --external-tokens")
    else:
        notes.append(
            "external_tokens: no report given, so they are unknown (null), not 0"
        )
    row = common.build(
        round_, "levi2", notes, wall_s_basis=basis, graded=graded, **fields
    )
    return row, notes
