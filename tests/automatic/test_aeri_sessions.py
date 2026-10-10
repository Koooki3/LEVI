"""The AERI fake client's C1 rollouts and C2 session files are read
correctly by the live service's own readers (``sessions.read_sessions``,
``criteria.check``), which stay unchanged."""

import json
import time

import pytest
from aeri_sessions import LEGACY_STATES, RoleRollouts, RunPair, iso
from live_helpers import template

from levi.live import criteria, sessions


@pytest.fixture
def source(tmp_path_factory):
    return template(tmp_path_factory)


def _read(root, now=None):
    # The reader caches a file by its mtime; two rewrites inside one coarse
    # kernel clock tick share an mtime, so tests that rewrite at once start
    # from an empty cache.
    sessions._CACHE.clear()
    return sessions.read_sessions([root], now=now)


def _one(root, now=None):
    found = _read(root, now=now)
    assert len(found) == 1
    return next(iter(found.values()))


def test_iso_times_with_offset_are_read_as_epoch(tmp_path, source):
    rollouts = RoleRollouts(tmp_path, source)
    now = time.time()
    written = rollouts.session("running", now=now, run_id="r1")
    assert written["updated_at"][-5] in "+-"  # +HHMM, as the real client writes
    session = _one(tmp_path, now=now + 1)
    assert abs(session.updated_at - int(now)) < 1.01
    assert session.started_at == pytest.approx(int(rollouts.started_at), abs=1.01)
    assert session.state == session.raw_state == "running"
    assert not session.crashed and session.active()
    assert session.run_id == "r1"
    assert sessions.parse_time(iso(1_791_600_000)) == 1_791_600_000


def test_stale_iso_session_of_a_dead_process_is_crashed(tmp_path, source):
    rollouts = RoleRollouts(tmp_path, source)
    old = time.time() - 120
    rollouts.session("running", now=old, pid=2**22 + 12345)
    session = _one(tmp_path)
    assert session.crashed and session.state == "crashed"
    assert session.raw_state == "running"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("unattended", "unattended"),
        ("dual_label", "dual_label"),
        ("auto", None),
        (None, None),
    ],
)
def test_levi_mode_is_read(tmp_path, source, mode, expected):
    RoleRollouts(tmp_path, source).session("standby", mode=mode)
    assert _one(tmp_path).label_mode == expected


def test_waiting_reset_since_keeps_the_first_entry_and_clears_after(tmp_path, source):
    rollouts = RoleRollouts(tmp_path, source)
    start = time.time() - 20
    rollouts.session("running", now=start)
    assert _one(tmp_path).waiting_reset_since is None
    rollouts.session("waiting_reset", now=start + 5)
    rollouts.session("waiting_reset", now=start + 9)
    session = _one(tmp_path, now=start + 10)
    assert session.waiting_reset_since == pytest.approx(start + 5, abs=0.001)
    assert session.reset_wait_s is None  # AERI waits have no countdown
    rollouts.session("running", now=start + 11)
    assert _one(tmp_path, now=start + 12).waiting_reset_since is None
    # A new wait starts a new clock.
    rollouts.session("waiting_reset", now=start + 15)
    assert _one(tmp_path, now=start + 16).waiting_reset_since == pytest.approx(
        start + 15, abs=0.001
    )


def test_future_waiting_reset_since_is_not_believed(tmp_path, source):
    rollouts = RoleRollouts(tmp_path, source)
    now = time.time()
    rollouts.session("waiting_reset", now=now, waiting_reset_since=now + 60)
    assert _one(tmp_path, now=now).waiting_reset_since is None


def test_reset_wait_and_epoch_times_still_supported(tmp_path, source):
    rollouts = RoleRollouts(tmp_path, source)
    now = time.time()
    rollouts.session("waiting_reset", now=now, epoch_times=True, reset_wait_s=10)
    raw = json.loads(rollouts.session_path.read_text())
    assert isinstance(raw["updated_at"], float)
    session = _one(tmp_path, now=now + 1)
    assert session.reset_wait_s == 10 and session.updated_at == now


def test_forward_and_reset_are_two_sessions_with_one_active(tmp_path, source):
    pair = RunPair(tmp_path, source)
    pair.write("reset", "running", aeri_state="RESET_ACTIVE", control_epoch=4)
    found = _read(tmp_path)
    by_task = {s.task_folder: s for s in found.values()}
    assert set(by_task) == {"stack_the_plates", "reset_stack_the_plates"}
    forward, reset = by_task["stack_the_plates"], by_task["reset_stack_the_plates"]
    assert (forward.state, reset.state) == ("standby", "running")
    assert reset.active() and not forward.active()
    assert forward.label_mode == "unattended" and forward.levi_enabled is True
    assert reset.levi_enabled is False and reset.label_mode is None
    assert reset.prompt == "Reset: stack the plates of same color together"
    assert forward.run_id == reset.run_id == "r20261010-a"
    # The extra keys reach the file; the reader ignores them.
    raw = json.loads(pair.reset.session_path.read_text())
    assert raw["episode_role"] == "reset"
    assert raw["aeri"] == {
        "run_id": "r20261010-a",
        "state": "RESET_ACTIVE",
        "control_epoch": 4,
    }
    assert "aeri" not in reset.public() and "episode_role" not in reset.public()
    pair.write("forward", "waiting_reset", aeri_state="FORWARD_FINALIZE")
    by_task = {s.task_folder: s for s in _read(tmp_path).values()}
    assert by_task["stack_the_plates"].state == "waiting_reset"
    assert by_task["reset_stack_the_plates"].state == "standby"


def test_only_the_clients_seven_states_can_be_written(tmp_path, source):
    rollouts = RoleRollouts(tmp_path, source)
    for state in LEGACY_STATES:
        rollouts.session(state)
    for state in ("reset_running", "RESET_ACTIVE", "crashed", "unknown"):
        with pytest.raises(ValueError):
            rollouts.session(state)
    # Why: the live reader passes an unknown name through unchanged.
    path = rollouts.session_path
    raw = json.loads(path.read_text())
    path.write_text(json.dumps({**raw, "state": "reset_running"}))
    assert _one(tmp_path).state == "reset_running"


def test_session_files_are_replaced_atomically(tmp_path, source):
    rollouts = RoleRollouts(tmp_path, source)
    rollouts.session("running")
    leftovers = [p.name for p in rollouts.session_path.parent.iterdir()]
    assert leftovers == [rollouts.session_path.name]
    # A hidden temporary file is never read as a session.
    (rollouts.session_path.parent / ".x.json").write_text("{")
    assert len(_read(tmp_path)) == 1


def test_forward_and_reset_rollouts_pass_the_completion_check(tmp_path, source):
    pair = RunPair(tmp_path, source)
    now = time.time() + 1
    for role in (pair.forward, pair.reset):
        demo = role.begin(3)
        assert criteria.check(demo, now=now).state == "pending"
        role.finish(3)
        result = criteria.check(demo, now=now)
        assert result.ok and result.marker and result.outcome == "unlabeled"
        assert criteria.kind_of(demo.name) == "demo"
    pair.reset.write(4)
    target = pair.reset.incomplete(4, reason="orchestrator_crash")
    assert criteria.kind_of(target.name) == "incomplete"
    assert criteria.abort_reason(target) == "orchestrator_crash"
    rejected = pair.forward.write(5, match=False)
    assert criteria.check(rejected, now=now).state == "rejected"
