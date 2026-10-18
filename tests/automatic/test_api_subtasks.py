# ruff: noqa: F401, F811
"""The read-only subtask-loop view (``GET runs/{id}/subtasks``): notes with
their detail parsed, plan files checked against their digest, nothing that
writes."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_api_common import client, finished_run, home, roots, write_job

from levi.automatic import api, subtask_view

BASE = "/api/levi/automatic"


def note(seq, code, detail="", record="note"):
    text = detail if isinstance(detail, str) else json.dumps(detail)
    return SimpleNamespace(
        sequence_no=seq,
        record=record,
        emitted_wall_ns=seq * 1_000_000_000,
        note=SimpleNamespace(code=code, detail=text) if record == "note" else None,
    )


def reader(base: Path):
    def read(parts):
        return (base.joinpath(*parts)).read_bytes()

    return read


PLAN = {
    "subtasks": [
        {
            "index": 0,
            "pick": "red cup",
            "place": "tray",
            "done_when": "the red cup is on the tray",
            "instruction": "put the red cup on the tray",
        }
    ]
}


def write_plan(run_dir: Path, name="subtask_plans/r0-forward.json", value=PLAN):
    path = run_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value).encode()
    path.write_bytes(data)
    return name, hashlib.sha256(data).hexdigest()


def test_only_subtask_and_vlm_notes_are_returned_with_detail_parsed(tmp_path):
    events = [
        note(1, "x_other", {"a": 1}),
        note(2, "subtask_inventory", {"items": ["cup"], "frames": []}),
        note(3, "vlm_sleep", {"ok": True, "mem_available_gib": 12.5, "ms": 900}),
        note(4, "subtask_started", record="committed"),
    ]
    got = subtask_view.view(events, tmp_path, reader(tmp_path), reset_mode="x")
    assert [n["code"] for n in got["notes"]] == ["subtask_inventory", "vlm_sleep"]
    assert got["notes"][1]["detail"]["mem_available_gib"] == 12.5
    assert got["notes"][0]["at"] == 2000
    assert got["plans"] == {} and got["truncated"] is False


def test_a_detail_that_does_not_parse_is_flagged_not_guessed(tmp_path):
    events = [note(1, "subtask_started", '{"round": 0, "instruction": "put the')]
    row = subtask_view.view(events, tmp_path, reader(tmp_path), reset_mode=None)[
        "notes"
    ][0]
    assert row["detail"] is None and row["unparsed"] is True


def test_a_plan_file_is_read_only_when_its_digest_matches(tmp_path):
    name, digest = write_plan(tmp_path)
    events = [
        note(1, "subtask_plan", {"round": 0, "loop": "forward", "task": "t", "n": 1,
                                 "plan_file": name, "sha256": digest}),
    ]  # fmt: skip
    got = subtask_view.view(events, tmp_path, reader(tmp_path), reset_mode=None)
    assert got["plans"][digest]["ok"] is True
    assert got["plans"][digest]["subtasks"][0]["instruction"] == (
        "put the red cup on the tray"
    )
    wrong = "b" * 64
    events[0].note.detail = json.dumps({"plan_file": name, "sha256": wrong})
    got = subtask_view.view(events, tmp_path, reader(tmp_path), reset_mode=None)
    assert got["plans"][wrong] == {
        "ok": False,
        "problem": "sha256_mismatch",
        "subtasks": [],
    }


def test_a_plan_file_outside_the_run_folder_is_refused(tmp_path):
    outside = tmp_path / "elsewhere.json"
    outside.write_text(json.dumps(PLAN))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for name in ("../elsewhere.json", str(outside), ""):
        assert subtask_view.plan_parts(run_dir, name) is None, name
    inside = str(run_dir / "plans" / "p.json")
    assert subtask_view.plan_parts(run_dir, inside) == ("plans", "p.json")


def test_a_plan_of_the_wrong_shape_or_missing_is_named(tmp_path):
    name, digest = write_plan(tmp_path, value={"subtasks": [{"index": "0"}]})
    detail = {"plan_file": name, "sha256": digest}
    assert subtask_view.plan_of(reader(tmp_path), tmp_path, detail)["problem"] == (
        "bad_shape"
    )
    detail = {"plan_file": "nope.json", "sha256": digest}
    assert subtask_view.plan_of(reader(tmp_path), tmp_path, detail)["problem"] == (
        "missing"
    )


def test_a_long_detail_is_read_whole_from_its_sidecar(tmp_path):
    whole = {
        "round": 0, "loop": "forward", "index": 0, "attempt": 1,
        "episode_id": "run-1.forward.0001", "value": False,
        "frames": [{"ref": "wrist:check-1", "sha256": "d" * 64},
                   {"ref": "front:check-2", "sha256": "9" * 64}],
    }  # fmt: skip
    name, digest = write_plan(
        tmp_path, "subtasks/000012-subtask_check-detail.json", whole
    )
    short = {"round": 0, "value": False, "file": name, "file_sha256": digest}
    stored = {"d" * 64}
    got = subtask_view.view(
        [note(12, "subtask_check", short)],
        tmp_path,
        reader(tmp_path),
        reset_mode=None,
        stored=lambda sha: sha in stored,
    )
    detail = got["notes"][0]["detail"]
    assert detail["episode_id"] == "run-1.forward.0001" and detail["index"] == 0
    assert detail["frames"] == [
        {"ref": "wrist:check-1", "sha256": "d" * 64, "stored": True},
        {"ref": "front:check-2", "sha256": "9" * 64, "stored": False},
    ]
    # A sidecar that does not match its digest is not used.
    short["file_sha256"] = "0" * 64
    row = subtask_view.view(
        [note(12, "subtask_check", short)], tmp_path, reader(tmp_path), reset_mode=None
    )["notes"][0]
    assert row["sidecar_problem"] == "sha256_mismatch"
    assert "episode_id" not in row["detail"]


def test_done_when_facts_become_a_text():
    assert subtask_view.facts_text([["on", "red_cup_1", "tray_1"]]) == (
        "red_cup_1 on tray_1"
    )
    assert subtask_view.facts_text("already a text") == "already a text"
    assert subtask_view.facts_text([["on", 1, "x"]]) is None


def test_round_and_wake_refused_notes_are_returned(tmp_path):
    events = [
        note(1, "subtask_round_released", {"round": 0, "by": "terminal"}),
        note(2, "subtask_round_abandoned", {"round": 0, "phase": "forward", "why": "stop"}),
        note(3, "vlm_wake_refused", {"reason": "gpu_memory_short", "free_mib": 900}),
    ]  # fmt: skip
    got = subtask_view.view(events, tmp_path, reader(tmp_path), reset_mode=None)
    assert [n["code"] for n in got["notes"]] == [
        "subtask_round_released",
        "subtask_round_abandoned",
        "vlm_wake_refused",
    ]


def test_the_route_is_read_only_and_empty_for_a_reset_policy_run(
    client, tmp_path, roots
):
    run_id = finished_run(client, tmp_path, roots, name="r-subtasks")
    body = client.get(f"{BASE}/runs/{run_id}/subtasks").json()
    assert body["notes"] == [] and body["plans"] == {}
    assert body["reset_mode"] == "single_reset_policy"
    assert client.get(f"{BASE}/runs/no-such-run/subtasks").status_code == 404
    # No write method on the path.
    assert client.post(f"{BASE}/runs/{run_id}/subtasks", json={}).status_code == 405


def test_no_route_sends_a_subtask_or_round_command():
    for route in api.router.routes:
        path = getattr(route, "path", "")
        if "subtask" in path or "round" in path:
            assert getattr(route, "methods", set()) <= {"GET", "HEAD"}, path


def test_a_subtask_loop_plan_has_its_settings_and_no_reset_policy():
    plan = {
        "reset_mode": "subtask_loop",
        "texts": {"forward": "put the cups on the tray", "reset": "x"},
        "deployments": {"forward": {"checkpoint_id": "fwd"}, "reset": {"checkpoint_id": "r"}},
        "run": {
            "episodes": 3, "forward_max_steps": 200, "reset_max_steps": 200,
            "subtask_task": "cups_on_tray", "subtask_max_steps": 200,
            "subtask_max_retries": 5, "max_subtasks_per_loop": None,
            "home_after_loop": True, "vlm_min_free_mem_gib": 28.0,
        },
    }  # fmt: skip
    need = api.requirements_of(plan)
    assert need["subtask_loop"] == {
        "task": "cups_on_tray",
        "rounds": 3,
        "subtask_max_steps": 200,
        "subtask_max_retries": 5,
        "max_subtasks_per_loop": None,
        "home_after_loop": True,
        "vlm_min_free_mem_gib": 28.0,
    }
    assert need["policies"]["reset"] is None
    assert need["task"]["reset_instruction"] is None
    assert need["max_steps"]["reset"] is None
    # A plan that does not state the cap does not claim it is off.
    del plan["run"]["max_subtasks_per_loop"]
    assert "max_subtasks_per_loop" not in api.requirements_of(plan)["subtask_loop"]
    # Other modes are unchanged: no subtask_loop key.
    plan["reset_mode"] = "single_reset_policy"
    assert "subtask_loop" not in api.requirements_of(plan)


def test_every_note_of_the_mode_is_shown():
    from levi.automatic import subtask_loop

    assert set(subtask_loop.NOTES) <= subtask_view.CODES
    assert "subtask_alternation" in subtask_view.CODES
    assert "subtask_service_step" in subtask_view.CODES


@pytest.mark.parametrize("mode", [None, "local", "external", "bad", True, [], {}])
@pytest.mark.parametrize("model", ["external-agent", "local-model"])
def test_snapshot_provider_comes_only_from_valid_frozen_declaration(
    tmp_path, monkeypatch, mode, model
):
    from levi.automatic.journal import PLAN_FILE, plan_digest

    vlm = {"model": model, "served_name": model, "argv": [model], "usage": {}}
    if mode is not None:
        vlm["provider_mode"] = mode
    plan = {"reset_mode": "subtask_loop", "subtask": {"vlm": vlm}}
    path = tmp_path / PLAN_FILE
    path.write_text(json.dumps({"plan": plan, "plan_sha256": plan_digest(plan)}))
    before = path.read_bytes()
    monkeypatch.setattr(api._Run, "runner", lambda self: {"alive": False})
    run = api._Run("fixture-provider", {"reset_mode": "subtask_loop"}, tmp_path)
    result = api.snapshot_of(run)
    expected = (
        {"mode": mode, "source": "frozen_plan"}
        if mode in ("local", "external")
        else {"mode": "unknown", "source": "unavailable"}
    )
    assert result["subtask_provider"] == expected
    assert path.read_bytes() == before
    assert not (tmp_path / "labels").exists()


@pytest.mark.parametrize("subtask", [None, [], {"vlm": []}, {"vlm": None}])
def test_malformed_provider_sections_are_unknown(subtask):
    assert api.subtask_provider_of({"subtask": subtask}) == {
        "mode": "unknown",
        "source": "unavailable",
    }


def test_rejected_plan_does_not_supply_provider_mode(tmp_path, monkeypatch):
    from levi.automatic.journal import PLAN_FILE, JournalError, read_plan

    plan = {
        "reset_mode": "subtask_loop",
        "subtask": {"vlm": {"provider_mode": "external"}},
    }
    path = tmp_path / PLAN_FILE
    path.write_text(json.dumps({"plan": plan, "plan_sha256": "0" * 64}))
    before = path.read_bytes()
    with pytest.raises(JournalError):
        read_plan(tmp_path)
    monkeypatch.setattr(api._Run, "runner", lambda self: {"alive": False})
    run = api._Run("fixture-provider", {"reset_mode": "subtask_loop"}, tmp_path)
    # Preserve _Run's existing handling: a rejected plan is not kept.
    assert run.plan == {}
    assert api.snapshot_of(run)["subtask_provider"] == {
        "mode": "unknown",
        "source": "unavailable",
    }
    assert path.read_bytes() == before
