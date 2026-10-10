"""The registration guard of the reset-mode matrix (T-CL-01, design X2 §2):
every capability the code has is in ``MODE_MATRIX``, every ``same`` or
``differs`` cell has a collected test in that mode, every ``n/a`` says why.

It collects ``tests/automatic`` in a child pytest (``--collect-only``) with
this module loaded as a plugin (``-p test_mode_matrix_guard``): its
``pytest_collection_modifyitems`` writes every collected test's
``mode_matrix`` markers and ``reset_mode`` parameter to the file named by
``AERI_MODE_MATRIX_DUMP``; ``modes.audit`` judges them. (Loaded as an
ordinary test module the hook does nothing: pytest takes hooks from
plugins and conftest files only.) The counter-examples check that the guard
fails when it should."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levi.automatic import modes

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DUMP_ENV = "AERI_MODE_MATRIX_DUMP"


def _modes(mark):
    value = mark.kwargs.get("modes")
    if value is None:
        return None
    # A bare string is a mistake (modes=("x",) was meant): passed on as it
    # is, so the guard reports it.
    return list(value) if isinstance(value, list | tuple) else value


def pytest_collection_modifyitems(session, config, items):
    path = os.environ.get(DUMP_ENV)
    if not path:
        return
    found = []
    for item in items:
        marks = [
            {"args": list(mark.args), "modes": _modes(mark)}
            for mark in item.iter_markers("mode_matrix")
        ]
        if not marks:
            continue
        callspec = getattr(item, "callspec", None)
        found.append(
            {
                "nodeid": item.nodeid,
                "marks": marks,
                "reset_mode": callspec.params.get("reset_mode") if callspec else None,
                "uses_reset_mode": "reset_mode" in getattr(item, "fixturenames", ()),
            }
        )
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(found, handle)


@pytest.fixture(scope="module")
def collected(tmp_path_factory):
    dump = tmp_path_factory.mktemp("mode-matrix") / "collected.json"
    pythonpath = os.pathsep.join(
        [str(HERE), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    proc = subprocess.run(  # noqa: PLW1510 - the return code is checked below
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "test_mode_matrix_guard",
            str(HERE),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, DUMP_ENV: str(dump), "PYTHONPATH": pythonpath},
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(dump.read_text())


def test_the_registry_holds(collected):
    problems = modes.audit(collected)
    assert problems == [], "\n".join(problems)
    # Both modes are really collected (the fixture is parametrised).
    seen = {item["reset_mode"] for item in collected if item["reset_mode"]}
    assert seen == set(modes.RESET_MODES)


# --- counter-examples: the guard must fail ----------------------------------------------------------


def item(*capabilities, mode=None, declared=None, uses=None, node="t.py::x"):
    return {
        "nodeid": node,
        "marks": [{"args": list(capabilities), "modes": declared}],
        "reset_mode": mode,
        "uses_reset_mode": mode is not None if uses is None else uses,
    }


MATRIX = {
    "cap:shared": {modes.SINGLE: modes.SAME, modes.HUMAN: modes.SAME},
    "cap:reset": {
        modes.SINGLE: modes.differs("only here"),
        modes.HUMAN: modes.na("no reset policy"),
    },
}
COVERED = [
    item("cap:shared", mode=modes.SINGLE),
    item("cap:shared", mode=modes.HUMAN),
    item("cap:reset", declared=[modes.SINGLE]),
]


def check(items=COVERED, matrix=MATRIX, enumerated=None):
    return modes.audit(
        items,
        matrix=matrix,
        enumerated=set(matrix) if enumerated is None else enumerated,
    )


def test_a_well_formed_toy_registry_passes():
    assert check() == []


def test_a_capability_left_out_of_the_matrix_fails_the_guard():
    found = check(enumerated={*MATRIX, "cap:forgotten"})
    assert found == [
        "cap:forgotten: in the code but not in MODE_MATRIX (levi/automatic/modes.py)"
    ]
    # The real one: a capability the code gains is caught, from the code.
    real = modes.audit([], enumerated=modes.capabilities() | {"metrics:new_key"})
    assert any(p.startswith("metrics:new_key: in the code but not") for p in real)


def test_a_mode_without_a_test_fails_the_guard():
    found = check(items=COVERED[:1] + COVERED[2:])
    assert found == [
        "cap:shared [human_assisted]: no collected test covers it in this mode"
    ]


@pytest.mark.parametrize(
    "cell", ["n/a", "n/a:", "n/a:   ", "differs", "differs:", "maybe", None, 1]
)
def test_a_cell_without_its_reason_fails_the_guard(cell):
    broken = {**MATRIX, "cap:reset": {**MATRIX["cap:reset"], modes.HUMAN: cell}}
    found = check(matrix=broken)
    assert any(p.startswith("cap:reset [human_assisted]: ") for p in found), found
    missing = {**MATRIX, "cap:reset": {modes.SINGLE: modes.SAME}}
    assert any("needs one cell" in p for p in check(matrix=missing))


@pytest.mark.parametrize(
    "bad, expected",
    [
        (item(), "names no capability"),
        (item("cap:typo", mode=modes.SINGLE), "unknown capability 'cap:typo'"),
        (item("cap:shared"), "says no mode"),
        (item("cap:shared", uses=True), "not parametrised"),
        (item("cap:shared", declared=["single"]), "'single' is not a reset mode"),
        (item("cap:shared", declared=modes.SINGLE), "is not a tuple"),
        (
            item("cap:shared", mode=modes.HUMAN, declared=[modes.SINGLE]),
            "declares modes",
        ),
        (item("cap:reset", mode=modes.HUMAN), "where it is n/a"),
        (item(["cap:shared"], mode=modes.SINGLE), "is not a capability name"),
    ],
)
def test_a_wrong_marker_fails_the_guard(bad, expected):
    found = check(items=[*COVERED, bad])
    assert any(expected in p for p in found), found


def test_a_capability_the_code_dropped_fails_the_guard():
    found = check(enumerated={"cap:shared"})
    assert "cap:reset: in MODE_MATRIX but no longer in the code" in found
