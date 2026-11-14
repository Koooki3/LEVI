"""Arm A (``direct``) and arm B (``prior_levi``): the result file of the
``levi-vlm-eval`` driver, plus B's task ledger.

Both arms write one JSON result per run into the evaluation folder:

- A, ``direct.py`` (no LEVI): ``levi`` is ``false``; ``model``, ``backend``,
  ``config``, ``episodes`` (the ids), ``completed`` (ids without an error),
  ``phases`` (one row per problem: ``episode``, ``seconds``, ``tokens``,
  ``error``), ``run_elapsed_seconds`` (the whole loop's wall time), ``status``,
  and the graded blocks ``grades.<reference>.overall``, ``derived_outcome.overall``
  and ``review.overall``.
- B, ``drive.py`` (the prior LEVI harness): no ``levi`` key; ``episodes``,
  ``completed`` (a count), ``requests``, ``tokens`` (what LEVI metered, or
  ``null``), ``run_elapsed_seconds`` (**the model's time**, not the wall time),
  ``driver_wall_seconds`` (the driver's wall time), ``phases``, ``status``, the
  same graded blocks. Its task ledger ``levi.harness.ledger.v1`` (``ledger.json``
  under the dataset's ``tasks/<run>/``) adds ``wall_seconds`` (without the time
  spent waiting for a person), ``status`` and ``cost`` (``provider_kind``,
  ``model``, ``tokens.metered``).

What goes into the record, and what stays ``None``:

=====================  =====================================================
``local_tokens``       A: the sum of the phases' ``tokens``; ``None`` if any
                       problem has no phase or no count (the file's own
                       ``tokens`` total counts a missing one as 0, so it is not
                       used). B: ``tokens`` (and the ledger's ``tokens.metered``,
                       which must agree), else ``None``
``wall_s``             A: ``run_elapsed_seconds``. B: the ledger's
                       ``wall_seconds``, else ``driver_wall_seconds``, else
                       ``None``. Never B's ``run_elapsed_seconds``
``wall_s_basis``       the caller states it (``--wall-basis``): the files do
                       not say whether queueing is inside
``external_tokens``    all four ``0`` when the arm's own record shows a local
                       model (A: ``backend`` is ``vllm`` or ``ollama``; B: the
                       ledger's ``provider_kind`` is ``ollama`` or
                       ``openai-local``), or when the caller says so
                       (``--no-external-model``); otherwise ``None``
``quality`` and        a dotted path into the result (``--quality-path``,
``scored_count``       e.g. ``derived_outcome.overall.accuracy``); the count is
                       the ``episodes`` beside it unless
                       ``--scored-count-path`` says otherwise. Or a scorer's
                       file (``--scores``)
``blocked``            a code from a ``status`` that is not ``succeeded``
=====================  =====================================================

Not carried by these files, so not in the record: prefill and decode tokens
apart, guard refusals and retries, peak memory.
"""

from __future__ import annotations

import re

from . import common
from .common import AdapterError, Round

LEDGER_SCHEMA = "levi.harness.ledger.v1"
LOCAL_BACKENDS = ("vllm", "ollama")
LOCAL_PROVIDER_KINDS = ("ollama", "openai-local")
ZERO_TOKENS = {"input_new": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _status_code(result: dict, notes: list[str]) -> str | None:
    status = result.get("status")
    if status is None:
        notes.append("blocked: the result has no status, so it is left null")
        return None
    if not isinstance(status, str):
        raise AdapterError("status must be a string")
    if status == "succeeded":
        return None
    code = re.sub(r"[^A-Za-z0-9_.:+-]", "_", status)[:56]
    return f"status.{code}"


def _direct_tokens(result: dict, round_: Round, notes: list[str]):
    phases = result.get("phases")
    if phases is None:
        notes.append("local_tokens: the result has no phases, so it is unknown (null)")
        return None
    if not isinstance(phases, list):
        raise AdapterError("phases must be a list")
    seen = set()
    total = 0
    for row in phases:
        if not isinstance(row, dict) or "episode" not in row:
            raise AdapterError("a phase is not an object with an episode")
        seen.add(str(row["episode"]))
        value = common.count(row.get("tokens"), "phases[].tokens")
        if value is None:
            total = None
        elif total is not None:
            total += value
    if total is None or len(seen) != round_.episode_count:
        notes.append(
            "local_tokens: a problem has no token count or no phase, so the "
            "total is unknown (null), not a sum with zeros"
        )
        return None
    return total


def _ledger(path, result: dict, notes: list[str]) -> dict:
    ledger = common.read_json(path, "the ledger")
    if not isinstance(ledger, dict) or ledger.get("schema") != LEDGER_SCHEMA:
        raise AdapterError(f"the ledger is not a {LEDGER_SCHEMA} file")
    if "run_id" in result and ledger.get("run_id") != result["run_id"]:
        raise AdapterError("the ledger belongs to another run than the result")
    return ledger


def _common_result(path, round_: Round, what: str) -> dict:
    result = common.read_json(path, f"the {what} result")
    if not isinstance(result, dict):
        raise AdapterError(f"the {what} result must be a JSON object")
    round_.check_problems(result.get("episodes"), f"the {what} result")
    if result.get("model") is not None and result["model"] != round_.model:
        raise AdapterError(
            f"the {what} result's model differs from the declared model "
            "(the three arms must run the same one)"
        )
    return result


def direct(
    result_path,
    round_: Round,
    *,
    wall_basis,
    quality_path=None,
    scored_count_path=None,
    scores=None,
    no_external_model=False,
):
    """The ``direct`` record from ``direct.py``'s result file. Returns
    ``(record dict, notes)``."""
    notes: list[str] = []
    basis = common.check_basis(wall_basis)
    result = _common_result(result_path, round_, "direct")
    if result.get("levi") is not False:
        raise AdapterError(
            "this is not a direct-arm result (its levi flag is not false); "
            "use the prior-levi adapter for a LEVI run"
        )
    round_.check_decoding(result.get("config"), "the direct result")
    wall = common.number(
        result.get("run_elapsed_seconds"), "run_elapsed_seconds", positive=True
    )
    if wall is None:
        notes.append(
            "wall_s: the result has no run_elapsed_seconds, so it is unknown (null)"
        )
    backend = result.get("backend")
    external, source = None, None
    if no_external_model or backend in LOCAL_BACKENDS:
        external = dict(ZERO_TOKENS)
        source = (
            f"local_backend.{backend}"
            if backend in LOCAL_BACKENDS
            else "declared.no_external_model"
        )
    else:
        notes.append(
            "external_tokens: no local backend is recorded, so they are unknown (null)"
        )
    fields = common.quality_fields(
        result, quality_path, scored_count_path, scores, notes
    )
    row = common.build(
        round_,
        "direct",
        notes,
        wall_s=wall,
        wall_s_basis=basis,
        local_tokens=_direct_tokens(result, round_, notes),
        external_tokens=external,
        external_tokens_exact=True if external else None,
        external_tokens_source=source,
        blocked=_status_code(result, notes),
        **fields,
    )
    return row, notes


def levi_run_fields(
    result_path,
    round_: Round,
    *,
    ledger_path=None,
    quality_path=None,
    scored_count_path=None,
    scores=None,
    no_external_model=False,
    what="prior-levi",
) -> tuple[dict, list[str]]:
    """The record fields a LEVI harness run's result file (and ledger) give:
    ``wall_s``, ``local_tokens``, ``external_tokens*``, ``blocked``, quality
    and the scores. Shared by arms B and C. Returns ``(fields, notes)``."""
    notes: list[str] = []
    result = _common_result(result_path, round_, what)
    if "levi" in result:
        raise AdapterError(
            "this result has a levi flag, which only the direct arm's result "
            "writes; use the direct adapter for it"
        )
    ledger = _ledger(ledger_path, result, notes) if ledger_path else None
    cost = (ledger or {}).get("cost") or {}
    if ledger and cost.get("model") is not None and cost["model"] != round_.model:
        raise AdapterError("the ledger's model differs from the declared model")
    tokens = common.count(result.get("tokens"), "tokens")
    metered = common.count(
        (cost.get("tokens") or {}).get("metered"), "ledger tokens.metered"
    )
    if tokens is not None and metered is not None and tokens != metered:
        raise AdapterError(
            "the result's tokens and the ledger's tokens.metered disagree"
        )
    local = tokens if tokens is not None else metered
    if local is None:
        notes.append(
            "local_tokens: no metered token count in the files, so it is unknown (null)"
        )
    ledger_wall = common.number(
        (ledger or {}).get("wall_seconds"), "ledger wall_seconds", positive=True
    )
    driver_wall = common.number(
        result.get("driver_wall_seconds"), "driver_wall_seconds", positive=True
    )
    wall = ledger_wall if ledger_wall is not None else driver_wall
    if wall is None:
        notes.append(
            "wall_s: neither a ledger wall_seconds nor driver_wall_seconds is "
            "given (run_elapsed_seconds is the model's time, not used), so it is unknown (null)"
        )
    external, source = None, None
    kind = cost.get("provider_kind")
    if no_external_model or kind in LOCAL_PROVIDER_KINDS:
        external = dict(ZERO_TOKENS)
        source = (
            f"ledger.provider_kind.{kind}"
            if kind in LOCAL_PROVIDER_KINDS
            else "declared.no_external_model"
        )
    else:
        notes.append(
            "external_tokens: the files do not show a local provider (give the "
            "ledger, or --no-external-model), so they are unknown (null)"
        )
    blocked = _status_code(result, notes)
    if ledger and ledger.get("status") not in (None, "succeeded") and blocked is None:
        blocked = _status_code({"status": ledger["status"]}, notes)
    fields = {
        "wall_s": wall,
        "local_tokens": local,
        "external_tokens": external,
        "external_tokens_exact": True if external else None,
        "external_tokens_source": source,
        "blocked": blocked,
        **common.quality_fields(result, quality_path, scored_count_path, scores, notes),
    }
    return fields, notes


def prior_levi(result_path, round_: Round, *, wall_basis, **options):
    """The ``prior_levi`` record from ``drive.py``'s result file and, if given,
    the run's task ledger (options of ``levi_run_fields``). Returns
    ``(record dict, notes)``."""
    basis = common.check_basis(wall_basis)
    fields, notes = levi_run_fields(result_path, round_, **options)
    row = common.build(round_, "prior_levi", notes, wall_s_basis=basis, **fields)
    return row, notes
