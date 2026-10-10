"""The reset-mode matrix (T-CL-01, design X2 §2): how each capability of an
automatic run behaves under each reset mode, kept in one table so the
"policy evaluation only" mode (``human_assisted``: a person puts the scene
back) stays the same pipeline as the mode with a reset policy
(``single_reset_policy``) for as long as both exist.

``MODE_MATRIX`` maps a capability to one cell per mode in ``RESET_MODES``:

- ``same``: the capability behaves the same in this mode;
- ``differs:<one sentence>``: it exists in this mode but behaves differently
  (the sentence says how);
- ``n/a:<reason>``: it does not exist in this mode (the reason is required).

The capabilities are enumerated from the code by the functions in
``SOURCES``; a capability the code has and the matrix does not is a failure
of the registration guard (``tests/automatic/test_mode_matrix_guard.py``),
and so is a ``same``/``differs`` cell no collected test covers in that mode.
A test says what it covers with ``@pytest.mark.mode_matrix("<capability>",
...)``; its mode comes from the parametrised ``reset_mode`` fixture
(``tests/automatic/conftest.py``) or, for a test written for one mode,
from ``modes=("<mode>", ...)`` on the marker. ``audit`` is the guard's
logic, kept here so it can be tested against a deliberately broken
registry.

Sources not enumerated yet (a later task appends a function to
``SOURCES``): the recorder's session roles and manifest fields (T-CL-02),
the ``/api/levi/automatic/*`` routes (T-CL-11) and the job file's keys
(``JobSpec``, T-CL-06).

``python -m levi.automatic.modes`` prints the snapshot
(``tests/automatic/snapshots/mode_matrix.json``); ``--markdown en|zh``
prints the generated documentation section, which ``levi docs sync``
writes into ``docs/AUTOMATIC_PIPELINE.md`` and
``docs/AUTOMATIC_PIPELINE.zh-CN.md`` (``levi docs check`` compares).
"""

import argparse
import json
import sys

RESET_MODES = ("single_reset_policy", "human_assisted")
SINGLE, HUMAN = RESET_MODES
RESET_STATES = frozenset({"RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"})
# Every transition into, within or out of a reset episode: the ones
# human_assisted never takes (design X2 §1.2). Written out, not derived, so
# a new reset transition is a decision (the reachability test compares this
# set with the state machine's table and with what fake runs reach).
RESET_ONLY_TRANSITIONS = frozenset(
    {
        ("VERIFY_INITIAL", "RESET_ACTIVE"),
        ("SCENE_ASSESS", "RESET_ACTIVE"),
        ("RESET_ACTIVE", "RESET_VERIFY"),
        ("RESET_ACTIVE", "FAULT_LOCKED"),
        ("RESET_VERIFY", "RESET_FINALIZE"),
        ("RESET_VERIFY", "FAULT_LOCKED"),
        ("RESET_FINALIZE", "VERIFY_INITIAL"),
        ("RESET_FINALIZE", "WAIT_HUMAN"),
        ("RESET_FINALIZE", "FAULT_LOCKED"),
    }
)
SNAPSHOT_SCHEMA = "levi.aeri.mode_matrix.v1"
DOCS_SECTION = "aeri-reset-modes"
DOCS_FILES = {
    "en": "docs/AUTOMATIC_PIPELINE.md",
    "zh": "docs/AUTOMATIC_PIPELINE.zh-CN.md",
}
SAME = "same"
KINDS = ("same", "differs", "n/a")


def differs(why: str) -> str:
    return f"differs:{why}"


def na(why: str) -> str:
    return f"n/a:{why}"


def _both(cell: str) -> dict:
    return {mode: cell for mode in RESET_MODES}


def _per(single: str, human: str) -> dict:
    return {SINGLE: single, HUMAN: human}


def _transition(a: str, b: str) -> str:
    return f"transition:{a}->{b}"


# --- the matrix ----------------------------------------------------------------------------------

_NO_RESET = na("no reset policy runs: a scene that is not ready waits for a person")
_RESET_ONLY = differs("only this mode runs reset episodes")
_SCENE_TO_PERSON = _per(
    differs(
        "only when the reset policy is disabled, out of attempts, or "
        "on_unknown is wait_human (an unplanned intervention)"
    ),
    differs(
        "the normal path of a scene that is not ready: a person resets it "
        "(a planned intervention)"
    ),
)

_SAME_TRANSITIONS = (
    ("PREFLIGHT", "VERIFY_INITIAL"),
    ("PREFLIGHT", "WAIT_HUMAN"),
    ("PREFLIGHT", "FAULT_LOCKED"),
    ("VERIFY_INITIAL", "FORWARD_ACTIVE"),
    ("VERIFY_INITIAL", "COMPLETED"),
    ("VERIFY_INITIAL", "FAULT_LOCKED"),
    ("FORWARD_ACTIVE", "FORWARD_STOPPING"),
    ("FORWARD_ACTIVE", "FAULT_LOCKED"),
    ("FORWARD_STOPPING", "FORWARD_FINALIZE"),
    ("FORWARD_STOPPING", "FAULT_LOCKED"),
    ("FORWARD_FINALIZE", "ROBOT_HOME"),
    ("FORWARD_FINALIZE", "FAULT_LOCKED"),
    ("ROBOT_HOME", "SCENE_ASSESS"),
    ("ROBOT_HOME", "WAIT_HUMAN"),
    ("ROBOT_HOME", "FAULT_LOCKED"),
    ("SCENE_ASSESS", "FORWARD_ACTIVE"),
    ("SCENE_ASSESS", "COMPLETED"),
    ("SCENE_ASSESS", "FAULT_LOCKED"),
    ("WAIT_HUMAN", "PREFLIGHT"),
    ("WAIT_HUMAN", "COMPLETED"),
    ("WAIT_HUMAN", "FAULT_LOCKED"),
    ("FAULT_LOCKED", "PREFLIGHT"),
    ("FAULT_LOCKED", "FAULT_LOCKED"),
)

MODE_MATRIX: dict = {
    # The state machine (state_machine.TRANSITIONS): one table for both modes.
    **{_transition(a, b): _both(SAME) for a, b in _SAME_TRANSITIONS},
    _transition("VERIFY_INITIAL", "WAIT_HUMAN"): _SCENE_TO_PERSON,
    _transition("SCENE_ASSESS", "WAIT_HUMAN"): _SCENE_TO_PERSON,
    **{
        _transition(a, b): _per(_RESET_ONLY, _NO_RESET)
        for a, b in sorted(RESET_ONLY_TRANSITIONS)
    },
    # The reset arbitration (reset_manager.check_plan / check_after).
    "arbitration:plan:ready": _both(SAME),
    "arbitration:plan:reset_required": _per(
        differs("runs the reset policy while attempts remain, then asks a person"),
        differs("asks a person (scene_reset_required)"),
    ),
    "arbitration:plan:unknown": _per(
        differs("runs the reset policy, or asks a person with on_unknown wait_human"),
        differs("asks a person (scene_unknown)"),
    ),
    "arbitration:plan:unavailable": _per(
        differs("as unknown: never skips the reset"),
        differs("asks a person (scene_unknown)"),
    ),
    **{
        f"arbitration:after_reset:{outcome}": _per(_RESET_ONLY, _NO_RESET)
        for outcome in (
            "reset_verified",
            "scene_reset_required",
            "scene_unknown",
            "reset_horizon_exhausted",
            "operator_stop",
            "policy_error",
            "watchdog_timeout",
        )
    },
    # metrics.report: every top-level key.
    "metrics:schema": _both(SAME),
    "metrics:truth": _both(SAME),
    "metrics:truth_labels": _both(SAME),
    "metrics:note": _both(SAME),
    "metrics:reset_mode": _both(SAME),
    "metrics:scene_check": _both(SAME),
    "metrics:mode_source": _both(SAME),
    "metrics:comparable": _both(SAME),
    "metrics:mode_specific": _both(SAME),
    "metrics:autonomous": _both(SAME),
    "metrics:early_termination": _both(SAME),
    "metrics:reset": _per(
        SAME,
        differs(
            "no reset episodes: resets and their durations stay 0; skip "
            "decisions count forward starts only"
        ),
    ),
    "metrics:automation": _per(
        differs("every intervention is unplanned"),
        differs("a scene check sending the run to a person is planned"),
    ),
    "metrics:turnaround": _per(
        differs("human_reset_ms only when the reset policy gave up"),
        differs("reset_policy_ms is always 0"),
    ),
    "metrics:time_per_valid_episode_ms": _both(SAME),
    "metrics:human_minutes_per_valid_episode": _both(SAME),
    "metrics:scene_decisions_by_human": _both(SAME),
    # levi automatic subcommands (cli.build_parser).
    "cli:doctor": _per(
        SAME,
        differs("the launch check fails without a scene provider that can answer"),
    ),
    "cli:validate": _per(
        SAME,
        differs(
            "a real run is refused without a scene provider that can answer; "
            "--dry-run validates"
        ),
    ),
    "cli:run": _per(
        SAME,
        differs("a dry-run scene that is not ready ends the run in WAIT_HUMAN"),
    ),
    "cli:status": _both(SAME),
    "cli:report": _both(SAME),
}


# --- where capabilities come from ------------------------------------------------------------------


def _transitions() -> set:
    from levi.automatic import state_machine

    return {_transition(a, b) for a, b in state_machine.TRANSITIONS}


def _arbitration() -> set:
    from levi.automatic import reset_manager, state_machine

    return {f"arbitration:plan:{d}" for d in reset_manager.DECISIONS} | {
        f"arbitration:after_reset:{o}" for o in state_machine.RESET_OUTCOMES
    }


def _metrics() -> set:
    from levi.automatic import metrics

    # An empty run reports every key (and must not fail on it).
    return {f"metrics:{key}" for key in metrics.report([])}


def _cli() -> set:
    from levi.automatic import cli

    parser = cli.build_parser()
    commands = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return {f"cli:{name}" for name in commands.choices}


# Append a function here for each new source (design X2 §2: recorder roles
# and manifest fields, API routes, job keys).
SOURCES = [_transitions, _arbitration, _metrics, _cli]


def capabilities(sources=None) -> set:
    found: set = set()
    for source in SOURCES if sources is None else sources:
        found |= set(source())
    return found


# --- the guard --------------------------------------------------------------------------------------


def kind(cell) -> str | None:
    """``same``, ``differs`` or ``n/a``; None for a malformed cell (a
    ``differs`` or ``n/a`` without its sentence is malformed)."""
    if cell == SAME:
        return SAME
    if not isinstance(cell, str):
        return None
    for prefix in ("differs", "n/a"):
        if cell.startswith(prefix + ":") and cell[len(prefix) + 1 :].strip():
            return prefix
    return None


def matrix_problems(matrix=None) -> list:
    """Cells that are not well formed: every mode has one, ``n/a`` and
    ``differs`` say why."""
    problems = []
    for capability, cells in sorted((matrix or MODE_MATRIX).items()):
        if not isinstance(cells, dict) or set(cells) != set(RESET_MODES):
            problems.append(f"{capability}: needs one cell for each of {RESET_MODES}")
            continue
        for mode in RESET_MODES:
            if kind(cells[mode]) is None:
                problems.append(
                    f"{capability} [{mode}]: {cells[mode]!r} is not same, "
                    "differs:<why> or n/a:<reason>"
                )
    return problems


def covered(item: dict) -> tuple[list, list]:
    """``(modes, problems)`` of one collected test (see ``audit``)."""
    problems = []
    param = item.get("reset_mode")
    declared = set()
    for mark in item.get("marks", []):
        modes = mark.get("modes")
        if modes is not None and not isinstance(modes, list | tuple):
            problems.append(f"modes={modes!r} is not a tuple of reset modes")
            continue
        for mode in modes or ():
            if not isinstance(mode, str) or mode not in RESET_MODES:
                problems.append(f"modes={mode!r} is not a reset mode")
                continue
            declared.add(mode)
    if item.get("uses_reset_mode") and param is None:
        problems.append("uses reset_mode, but it is not parametrised")
    if param is not None:
        if param not in RESET_MODES:
            problems.append(f"reset_mode={param!r} is not a reset mode")
            return [], problems
        if declared and param not in declared:
            problems.append(f"runs in {param} but declares modes={sorted(declared)}")
        return [param], problems
    if not declared and not problems:
        problems.append(
            "says no mode: use the reset_mode fixture or modes=(...) on the marker"
        )
    return sorted(declared), problems


def audit(items, *, matrix=None, enumerated=None) -> list:
    """Every way the registry fails (empty: it holds). ``items``: the
    collected tests carrying a ``mode_matrix`` marker, as
    ``{"nodeid", "marks": [{"args": [...], "modes": [...] | None}],
    "reset_mode": <its parameter> | None, "uses_reset_mode": bool}``;
    ``enumerated``: the capabilities found in the code (default: ``SOURCES``).

    (a) every capability the code has is in the matrix (and none the code no
    longer has); (b) every ``same``/``differs`` cell is covered by at least
    one collected test in that mode; (c) every cell is well formed, ``n/a``
    with its reason. A marker naming no capability or an unknown one, a test
    saying no mode, and a test claiming a mode where the cell is ``n/a`` are
    failures too."""
    matrix = MODE_MATRIX if matrix is None else matrix
    enumerated = capabilities() if enumerated is None else set(enumerated)
    problems = list(matrix_problems(matrix))
    problems += [
        f"{c}: in the code but not in MODE_MATRIX (levi/automatic/modes.py)"
        for c in sorted(enumerated - set(matrix))
    ]
    problems += [
        f"{c}: in MODE_MATRIX but no longer in the code"
        for c in sorted(set(matrix) - enumerated)
    ]
    coverage: dict = {}
    for item in items:
        node = item.get("nodeid", "?")
        modes, found = covered(item)
        problems += [f"{node}: {p}" for p in found]
        for mark in item.get("marks", []):
            if not mark.get("args"):
                problems.append(f"{node}: a mode_matrix marker names no capability")
            for capability in mark.get("args") or ():
                if not isinstance(capability, str):
                    problems.append(f"{node}: {capability!r} is not a capability name")
                    continue
                cells = matrix.get(capability)
                if not isinstance(cells, dict):
                    problems.append(f"{node}: unknown capability {capability!r}")
                    continue
                for mode in modes:
                    if kind(cells.get(mode)) == "n/a":
                        problems.append(
                            f"{node}: covers {capability} in {mode}, where it is n/a"
                        )
                    else:
                        coverage.setdefault((capability, mode), []).append(node)
    for capability, cells in sorted(matrix.items()):
        if not isinstance(cells, dict):
            continue
        for mode in RESET_MODES:
            if kind(cells.get(mode)) in (SAME, "differs") and not coverage.get(
                (capability, mode)
            ):
                problems.append(
                    f"{capability} [{mode}]: no collected test covers it in this mode"
                )
    return problems


# --- snapshot and documentation -------------------------------------------------------------------------


def snapshot() -> dict:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "modes": list(RESET_MODES),
        "reset_only_transitions": sorted(
            f"{a}->{b}" for a, b in RESET_ONLY_TRANSITIONS
        ),
        "matrix": {c: dict(cells) for c, cells in sorted(MODE_MATRIX.items())},
    }


def snapshot_text() -> str:
    return json.dumps(snapshot(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


_WORDS = {
    "en": {
        "head": "Capability",
        "same": "same",
        "differs": "differs: ",
        "n/a": "n/a: ",
    },
    "zh": {"head": "能力", "same": "相同", "differs": "不同：", "n/a": "不适用："},
}
# The Chinese documentation's wording of every sentence in MODE_MATRIX (a
# test requires one for each; a missing one would print the English).
ZH = {
    "a dry-run scene that is not ready ends the run in WAIT_HUMAN": (
        "试运行中场景不就绪时，运行停在 WAIT_HUMAN"
    ),
    "a real run is refused without a scene provider that can answer; "
    "--dry-run validates": "没有能作答的场景提供方时拒绝真机运行；--dry-run 可通过校验",
    "a scene check sending the run to a person is planned": (
        "场景核对把运行交给人，属于计划内干预"
    ),
    "as unknown: never skips the reset": "同 unknown：绝不跳过复位",
    "asks a person (scene_reset_required)": "转人工（scene_reset_required）",
    "asks a person (scene_unknown)": "转人工（scene_unknown）",
    "every intervention is unplanned": "所有干预都是计划外",
    "human_reset_ms only when the reset policy gave up": (
        "只有复位策略放弃时才有 human_reset_ms"
    ),
    "no reset episodes: resets and their durations stay 0; skip decisions "
    "count forward starts only": "没有复位片段：复位次数和耗时恒为 0；跳过决策只统计前向开始",
    "no reset policy runs: a scene that is not ready waits for a person": (
        "不运行复位策略：场景不就绪时等人处理"
    ),
    "only this mode runs reset episodes": "只有该模式运行复位片段",
    "only when the reset policy is disabled, out of attempts, or on_unknown is "
    "wait_human (an unplanned intervention)": (
        "仅在复位策略停用、次数用尽或 on_unknown 为 wait_human 时（计划外干预）"
    ),
    "reset_policy_ms is always 0": "reset_policy_ms 恒为 0",
    "runs the reset policy while attempts remain, then asks a person": (
        "次数未用尽时运行复位策略，之后转人工"
    ),
    "runs the reset policy, or asks a person with on_unknown wait_human": (
        "运行复位策略；on_unknown 为 wait_human 时转人工"
    ),
    "the launch check fails without a scene provider that can answer": (
        "没有能作答的场景提供方时，启动检查不通过"
    ),
    "the normal path of a scene that is not ready: a person resets it "
    "(a planned intervention)": "场景不就绪时的正常路径：由人复位（计划内干预）",
}


def sentences() -> set:
    """Every sentence of a ``differs`` or ``n/a`` cell."""
    return {
        cell.split(":", 1)[1].strip()
        for cells in MODE_MATRIX.values()
        for cell in cells.values()
        if kind(cell) in ("differs", "n/a")
    }


def _cell(cell: str, lang: str) -> str:
    words = _WORDS[lang]
    found = kind(cell)
    if found == SAME:
        return words["same"]
    text = cell.split(":", 1)[1].strip()
    if lang == "zh":
        text = ZH.get(text, text)
    return words[found] + text.replace("|", "\\|")


def markdown(lang: str = "en") -> str:
    """The body of the ``aeri-reset-modes`` documentation section, which
    ``levi docs sync`` writes into ``DOCS_FILES`` (``levi/docs.py``)."""
    words = _WORDS[lang]
    rows = [
        f"| {words['head']} | `{SINGLE}` | `{HUMAN}` |",
        "| --- | --- | --- |",
    ]
    for capability, cells in sorted(MODE_MATRIX.items()):
        rows.append(
            f"| `{capability}` | {_cell(cells[SINGLE], lang)} | "
            f"{_cell(cells[HUMAN], lang)} |"
        )
    return "\n".join(rows) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m levi.automatic.modes",
        description="The AERI reset-mode matrix: prints its snapshot; "
        "`levi docs sync` writes its documentation table / AERI 复位模式矩阵："
        "打印快照；文档表由 `levi docs sync` 生成",
    )
    parser.add_argument(
        "--markdown",
        choices=sorted(_WORDS),
        help="print the documentation section / 打印文档生成段",
    )
    args = parser.parse_args(argv)
    sys.stdout.write(markdown(args.markdown) if args.markdown else snapshot_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
