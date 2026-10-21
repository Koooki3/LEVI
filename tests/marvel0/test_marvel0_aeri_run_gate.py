"""The start gate of the hosted terminal D (``aeri_run.Supervisor._start_gate``),
tested against the **real readiness rows** of the preparation card
(``aeri_ready.rows`` on a made-up machine, ``aeri_world.World``): the first build
refused every row that was not ``ok`` while the card makes ``stack_ready`` ``warn``
whenever the arm stack is up (the gripper stack and the controller cannot be seen
from here), so no start could ever go through; the test of the gate had replaced
the snapshot with a hand-made one in which every row was ``ok``.

Also here: the code pin (a clean tree at the very commit the profile was written
from) and the check that no other terminal drives the run name (the **last**
``start:`` / ``terminal D exited`` line of ``terminal_d.events`` decides)."""

import dataclasses
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from aeri_machine import HEAD, NAME
from aeri_run_kit import machine_of, refusal
from aeri_world import DEFAULT_PROFILES, World, installed_profiles

from levi.marvel0 import aeri_ready, aeri_run
from levi.marvel0 import aeri_state as st


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    mm = machine_of(tmp_path / "a1", monkeypatch)
    yield mm
    mm.close()


def real_snapshot(tmp_path, world_facts=None, stack=None) -> dict:
    """``aeri_ready.Readiness.snapshot()`` of a made-up machine whose rows are the
    real ones: a prepared run, the arm stack up (or as ``stack`` says)."""
    with pytest.MonkeyPatch.context() as mp:
        installed_profiles(mp, DEFAULT_PROFILES)
        world = World(Path(tempfile.mkdtemp(dir=tmp_path)))
        world.facts["stack"] = stack or World.stack_doc()
        world.facts.update(world_facts or {})
        world.set_live("stopped")
        world.preparer().prepare(world.prepare_body())
        return aeri_ready.Readiness(world.host()).snapshot()


def levels(snap) -> dict:
    return {r["code"]: r["level"] for r in snap["rows"]}


def ask(m):
    m.present()
    return m.sup.challenge({"purpose": "start", "name": NAME})


STACK_UP_ROWS = json.loads(
    (Path(__file__).parent / "fixtures" / "aeri_stack_up_rows.json").read_text()
)


# --- the real rows -------------------------------------------------------------------------


def test_a_machine_that_is_ready_has_the_stack_warning_and_nothing_else(tmp_path):
    snap = real_snapshot(tmp_path)
    assert snap["ready"] is True
    assert {c: v for c, v in levels(snap).items() if v != "ok"} == {
        "stack_ready": "warn"
    }
    assert aeri_ready.WARN_OK == ("stack_ready",)


def test_a_start_goes_through_on_the_rows_the_card_really_gives(m, tmp_path):
    """The review's finding: ``readiness_failed first=stack_ready`` at every
    start, though the page showed the machine ready."""
    m.snap = real_snapshot(tmp_path)
    assert levels(m.snap)["stack_ready"] == "warn"
    challenge = ask(m)
    assert challenge["name"] == NAME and challenge["readiness_sha256"]
    answer = m.start()
    assert answer["accepted"] is True and answer["state"] == "launching"
    assert m.spawned, "terminal D was started"


def test_a_row_that_refuses_in_the_real_card_stops_the_start(m, tmp_path):
    m.snap = real_snapshot(tmp_path, {"gpu": (30000, 32607)})
    assert levels(m.snap)["gpu_free"] == "refuse" and m.snap["ready"] is False
    _status, code, extra = refusal(ask, m)
    assert code == "readiness_failed" and extra["first"] == "gpu_free"
    assert m.spawned == []


def test_a_row_that_is_unknown_in_the_real_card_stops_the_start(m, tmp_path):
    m.snap = real_snapshot(tmp_path, {"gpu": None})
    assert levels(m.snap)["gpu_free"] == "unknown"
    assert refusal(ask, m)[1:2] == ("readiness_failed",)


def test_the_arm_stack_down_stops_the_start(m, tmp_path):
    m.snap = real_snapshot(tmp_path, stack=World.stack_doc(a="stopped"))
    assert levels(m.snap)["stack_ready"] == "refuse"
    _status, code, extra = refusal(ask, m)
    assert code == "readiness_failed" and extra["first"] == "stack_ready"


# --- what the gate lets through ------------------------------------------------------------


def snapshot_with(tmp_path, change=None, **top) -> dict:
    snap = real_snapshot(tmp_path)
    snap = {**snap, "rows": [dict(r) for r in snap["rows"]], **top}
    for code, level in (change or {}).items():
        next(r for r in snap["rows"] if r["code"] == code)["level"] = level
    return snap


@pytest.mark.parametrize("level", ["warn", "refuse", "unknown", "OK", None, 3])
def test_a_warning_passes_only_for_a_row_of_the_warn_list(m, tmp_path, level):
    m.snap = snapshot_with(tmp_path, {"gpu_free": level})
    assert refusal(ask, m)[1:2] == ("readiness_failed",)
    m.snap = snapshot_with(tmp_path, {"stack_ready": level})
    if level == "warn":
        assert ask(m)["name"] == NAME
    else:
        assert refusal(ask, m)[1] == "readiness_failed"


@pytest.mark.parametrize("ready", [False, None, 0, "yes", 1, "true", []])
def test_the_cards_own_verdict_must_be_true_itself(m, tmp_path, ready):
    m.snap = snapshot_with(tmp_path, ready=ready)
    assert refusal(ask, m)[1] == "readiness_failed"


@pytest.mark.parametrize("rows", [None, "ok", {}, [None], ["ok"], [{"code": "x"}]])
def test_rows_that_are_not_rows_do_not_pass(m, tmp_path, rows):
    m.snap = snapshot_with(tmp_path, rows=rows)
    assert refusal(ask, m)[1] == "readiness_failed"


def test_the_gate_uses_the_cards_list_of_warn_ok_rows_not_its_own(
    m, tmp_path, monkeypatch
):
    m.snap = real_snapshot(tmp_path)
    monkeypatch.setattr(aeri_ready, "WARN_OK", ())
    assert refusal(ask, m)[1] == "readiness_failed"
    monkeypatch.setattr(aeri_ready, "WARN_OK", ("stack_ready", "gpu_free"))
    m.snap = snapshot_with(tmp_path, {"gpu_free": "warn"})
    assert ask(m)["name"] == NAME


def test_the_digest_of_the_challenge_and_of_the_start_must_agree(m, tmp_path):
    m.snap = real_snapshot(tmp_path)
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    m.snap = {**m.snap, "sha256": "0" * 64}  # the machine changed in the meantime
    body = {
        "request_id": m.rid(),
        "name": NAME,
        "challenge": c["challenge"],
        "confirm": f"start aeri {NAME}",
        "attest": {k: True for k in st.ATTEST},
    }
    assert refusal(m.sup.start, body)[1] == "challenge_stale"
    assert m.spawned == []


# --- the code pin: the commit the profile was written from --------------------------------------


def test_the_head_must_be_the_commit_the_profile_was_written_from(m):
    """The profile hashes four files; a clean tree at the profile's own commit pins
    all of the worktree (the files that write the journal included). A site file
    that pins another commit with the same four files is not enough."""
    other = "fedcba98" + "0" * 32
    m.git_head = other
    m.site = dataclasses.replace(
        m.site, aeri=dataclasses.replace(m.aeri, pin_sha="fedcba98")
    )
    m.present()
    _status, code, extra = refusal(m.sup.challenge, {"purpose": "start", "name": NAME})
    assert code == "profile_unknown" and extra["detail"] == "head_not_profile_commit"
    assert m.spawned == []
    m.git_head = HEAD  # the profile's commit again
    m.site = dataclasses.replace(
        m.site, aeri=dataclasses.replace(m.aeri, pin_sha=HEAD[:8])
    )
    assert m.sup.challenge({"purpose": "start", "name": NAME})["name"] == NAME


def test_the_head_must_also_start_with_the_sites_pin(m):
    m.site = dataclasses.replace(
        m.site, aeri=dataclasses.replace(m.aeri, pin_sha="abcdef13")
    )
    assert refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1] == (
        "code_pin_mismatch"
    )


@pytest.mark.parametrize(
    ("profile", "head", "ok"),
    [
        ("harvest-5bc750ca", "5bc750cacc12e375d77f5036b4976d560ac682a3", True),
        ("harvest-5bc750ca", "5BC750CACC12E375D77F5036B4976D560AC682A3", True),
        ("harvest-5bc750ca", "5bc750cbcc12e375d77f5036b4976d560ac682a3", False),
        ("harvest-5bc750ca", "5bc750c", False),
        ("harvest-5bc750ca", "", False),
        ("harvest-5bc750ca", None, False),
        ("harvest-5bc750ca", 5, False),
        ("somebody-elses", "5bc750cacc12e375d77f5036b4976d560ac682a3", False),
        (None, "5bc750cacc12e375d77f5036b4976d560ac682a3", False),
        ("bfdee2cf", "bfdee2cf53365d8e0065b3f019e471213fe871fe", True),
        ("main-297a74c3", "297a74c3" + "0" * 32, True),
    ],
)
def test_profile_head_ok(profile, head, ok):
    assert st.profile_head_ok(profile, head) is ok


@pytest.mark.parametrize(
    ("profile", "worktree"),
    [("harvest-5bc750ca", "harvest-validation-fix"), ("bfdee2cf", "aeri-subtask-loop")],
)
def test_the_real_worktrees_are_at_the_commit_of_their_profiles(profile, worktree):
    """The real function on the real repositories (read-only git): the gate is
    only worth what the profile table says about the worktrees."""
    folder = None
    for parent in Path(__file__).resolve().parents:
        if (parent / "lab" / "worktrees" / worktree).is_dir():
            folder = parent / "lab" / "worktrees" / worktree
            break
    if folder is None:
        pytest.skip(f"no {worktree} worktree on this machine")
    done = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip("git cannot read that worktree")
    assert st.profile_head_ok(profile, done.stdout.strip()) is True
    other = {"harvest-5bc750ca": "bfdee2cf", "bfdee2cf": "harvest-5bc750ca"}[profile]
    assert st.profile_head_ok(other, done.stdout.strip()) is False


# --- who drives the run name ---------------------------------------------------------------


EVENTS = [
    ("[10:00:00] start: uv run x\n", True),
    ("[10:00:00] start: uv run x\n[10:05:00] terminal D exited rc=0\n", False),
    (
        "[10:00:00] start: a\n[10:05:00] terminal D exited rc=0\n[10:06:00] start: b\n",
        True,
    ),
    (
        (
            "[10:00:00] start: a\n[10:05:00] terminal D exited rc=130\n"
            "[10:06:00] start: b\n[10:07:00] terminal D exited rc=0\n"
        ),
        False,
    ),
    ("[10:05:00] terminal D exited rc=0\n", False),
    ("", False),
    ("no lines of ours\n", False),
    # a start line whose command text mentions the exit is still a start line
    ("[10:00:00] start: echo terminal D exited\n", True),
    ("[10:00:00] resource monitor pid 77\n[10:00:00] start: x\n", True),
]


@pytest.mark.parametrize(("text", "open_"), EVENTS)
def test_the_last_start_or_exit_line_decides_whether_a_terminal_drives_the_run(
    text, open_
):
    assert aeri_run.events_open(text) is open_


@pytest.mark.parametrize(("text", "open_"), EVENTS)
def test_a_name_driven_by_a_terminal_that_is_still_running_is_refused(m, text, open_):
    (m.runs / NAME / "terminal_d.events").write_text(text)
    m.present()
    if open_:
        assert refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1] == (
            "driven_elsewhere"
        )
    else:
        assert m.sup.challenge({"purpose": "start", "name": NAME})["name"] == NAME


# --- the card's own fixture of the contract (aeri_stack_up_rows.json) --------------------------


def test_the_cards_snapshots_of_the_stack_decide_the_start(m):
    """``aeri_stack_up_rows.json`` is ``aeri_ready.snapshot()`` on the made-up
    machine with the arm stack up, down and not read, written by the card's own
    regeneration: the start goes on for the first and for no other."""
    assert STACK_UP_ROWS["warn_ok"] == list(aeri_ready.WARN_OK)
    snaps = STACK_UP_ROWS["snapshots"]
    m.snap = snaps["stack_up"]
    assert ask(m)["readiness_sha256"] == snaps["stack_up"]["sha256"]
    for name in ("stack_down", "stack_not_read"):
        m.snap = snaps[name]
        assert refusal(ask, m)[1] == "readiness_failed", name


def test_a_warning_passes_for_exactly_the_rows_the_card_names(m):
    """For every row of the card: all ok but that row at ``warn`` starts exactly
    when the card lists the row in ``warn_ok``."""
    base = STACK_UP_ROWS["snapshots"]["stack_up"]
    for code in aeri_ready.ROW_CODES:
        rows = [
            {**r, "level": "warn" if r["code"] == code else "ok"} for r in base["rows"]
        ]
        m.snap = {**base, "rows": rows, "ready": True}
        if code in STACK_UP_ROWS["warn_ok"]:
            assert ask(m)["name"] == NAME, code
        else:
            assert refusal(ask, m)[1] == "readiness_failed", code
