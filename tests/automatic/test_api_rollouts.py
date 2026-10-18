# ruff: noqa: F401, F811
"""The read-only rollout sets of a run (``GET runs/{id}/rollouts``).

The generator ``levi.automatic.rollouts`` lives on another branch: these
tests put a fake module in its place, so they only check the route and the
projection (``rollout_view``): what leaves, what is withheld, where video
URLs point and that nothing is written."""

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from test_api_common import client, finished_run, home, roots, write_job

from levi.automatic import api, launch, rollout_view

BASE = "/api/levi/automatic"
GENERATOR = "levi.automatic.rollouts"
FWD = "stack__r-rollouts__subtask"
REV = "stack__r-rollouts__reverse"


def attempt(n, episode_id, demo, **over):
    return {
        "attempt": n,
        "episode_id": episode_id,
        "demo": demo,
        "frames": 200,
        "started_at": "2026-10-18T19:02:25+0800",
        "ended_at": "2026-10-18T19:02:52+0800",
        "stop_reason": "subtask_ended",
        "check": {
            "by": "vlm",
            "value": True,
            "file": "evidence/check-0003.json",
            "request": "queue/0003-check",
        },
        "sealed": True,
        "seconds": 26.5,
        **over,
    }


def fake_manifest(run_id):
    fwd = f"{run_id}.forward.0001"
    return {
        "schema": "levi.aeri.rollouts.v1",
        "run_id": run_id,
        "task": "pick the pepper into the white plate",
        "mode": "external",
        "generated_from": {"journal_sha256": "c" * 64, "events": 123},
        "rollouts": [
            {
                "round": 1,
                "round_id": "r1",
                "released_at": "2026-10-18T19:02:00+0800",
                "outcome": "forward_done",
                "sets": {
                    "forward": {
                        "plan_file": "subtasks/000010-plan.json",
                        "outcome": "done",
                        "subtasks": [
                            {
                                "index": 0,
                                "instruction": "put the red pepper into the white plate",
                                "pick": "red_pepper_1",
                                "place": "white_plate_1",
                                "attempts": [
                                    attempt(
                                        1,
                                        fwd,
                                        f"g/{FWD}/demo_0001",
                                        stop_reason="horizon_exhausted",
                                        request_evidence="subtasks/000011-request-evidence.json",
                                    )
                                ],
                            }
                        ],
                    },
                    "reverse": {
                        "plan_file": "subtasks/000020-plan.json",
                        "outcome": "done",
                        "subtasks": [
                            {
                                "index": 0,
                                "instruction": "put the red pepper into the blue plate",
                                "attempts": [
                                    attempt(
                                        1,
                                        f"{run_id}.reset.0001",
                                        f"g/{REV}/demo_0001",
                                        stop_reason="horizon_exhausted",
                                        check={"value": False},
                                    ),
                                    attempt(
                                        2,
                                        f"{run_id}.reset.0002",
                                        None,
                                        incomplete={
                                            "folder": f"g/{REV}/incomplete_demo_0002",
                                            "why": "recording not sealed (incomplete_*)",
                                        },
                                        check={"value": None},
                                    ),
                                ],
                            }
                        ],
                    },
                },
            },
            {
                "round": 2,
                "round_id": "r2",
                "released_at": "2026-10-18T11:10:00Z",
                "outcome": "abandoned",
                "abandoned": {"phase": "reverse", "why": "operator abandoned"},
                "notes": [
                    {
                        "code": "motion_refused",
                        "detail": "see subtasks/000031-request-evidence.json",
                        "at": "2026-10-18T11:11:00Z",
                    }
                ],
                "operator_label": {
                    "value": "discard",
                    "by": "op",
                    "source": "terminal",
                },
                "sets": {"forward": None, "reverse": None},
            },
        ],
        "reset_mode": "subtask_loop",
        "unmatched_demos": [
            {"episode_id": f"{run_id}.forward.0009", "demo": f"g/{FWD}/demo_0009"}
        ],
        "problems": [
            {
                "kind": "no_demo",
                "episode_id": f"{run_id}.forward.0002",
                "detail": "looked in /home/someone/secret/rollouts",
                "request_evidence": "subtasks/000012-request-evidence.json",
            }
        ],
    }


@pytest.fixture
def generator(monkeypatch):
    """A fake ``levi.automatic.rollouts`` whose ``load_or_build`` records its
    arguments and returns ``fake_manifest``."""
    module = ModuleType(GENERATOR)
    module.calls = []
    module.SCHEMA = "levi.aeri.rollouts.v1"

    def load_or_build(run_dir, rollout_root):
        module.calls.append((Path(run_dir), Path(rollout_root)))
        return fake_manifest(Path(run_dir).name)

    module.load_or_build = load_or_build
    monkeypatch.setitem(sys.modules, GENERATOR, module)
    # No catalog in these tests: media lookups find nothing unless a test
    # supplies its own sources.
    monkeypatch.setattr(rollout_view, "default_sources", lambda: ({}, None, None))
    return module


def files_below(folder: Path) -> list:
    return sorted(
        (str(p.relative_to(folder)), p.stat().st_mtime_ns)
        for p in folder.rglob("*")
        if p.is_file()
    )


def test_the_route_returns_the_projected_manifest_and_writes_nothing(
    client, tmp_path, roots, generator
):
    run_id = finished_run(client, tmp_path, roots, name="r-rollouts")
    _entry, run_dir = launch.registered_run(run_id)
    before = files_below(run_dir)
    response = client.get(f"{BASE}/runs/{run_id}/rollouts")
    assert response.status_code == 200, response.text
    body = response.json()
    assert files_below(run_dir) == before
    assert generator.calls == [(run_dir, run_dir.parent.parent.parent)]
    assert body["schema"] == "levi.aeri.rollouts.v1"
    assert body["run_id"] == run_id and body["mode"] == "external"
    assert body["generated_from"] == {"journal_sha256": "c" * 64, "events": 123}
    rollout = body["rollouts"][0]
    # The rollout-set blind rule is off (user 2026-10-18): shown unlabelled.
    assert rollout["round"] == 1 and rollout["outcome"] == "forward_done"
    reverse = rollout["sets"]["reverse"]["subtasks"][0]["attempts"]
    assert [a["attempt"] for a in reverse] == [1, 2]
    assert reverse[1]["demo"] is None
    assert reverse[1]["incomplete"]["why"].startswith("recording not sealed")
    assert reverse[0]["sealed"] is True
    assert reverse[1]["media"] == {"available": False, "reason": "no_demo"}
    assert reverse[0]["media"] == {"available": False, "reason": "no_dataset"}
    # No write method on the path.
    assert client.post(f"{BASE}/runs/{run_id}/rollouts", json={}).status_code == 405


def test_unknown_runs_are_404(client, generator):
    assert client.get(f"{BASE}/runs/no-such-run/rollouts").status_code == 404
    assert client.get(f"{BASE}/runs/..%2Fetc/rollouts").status_code == 404
    assert generator.calls == []


def test_a_check_keeps_only_its_value_and_no_private_reference_leaves(
    client, tmp_path, roots, generator
):
    run_id = finished_run(client, tmp_path, roots, name="r-rollouts")
    body = client.get(f"{BASE}/runs/{run_id}/rollouts").json()
    text = json.dumps(body)
    assert "request-evidence" not in text and "request_evidence" not in text
    assert "queue/0003-check" not in text
    assert "/home/someone" not in text and "<path>" in text
    sets = body["rollouts"][0]["sets"]
    checks = [
        a["check"]
        for s in ("forward", "reverse")
        for a in sets[s]["subtasks"][0]["attempts"]
    ]
    # Shown before the round is labelled (rollout-set blind rule off).
    assert checks == [{"value": True}, {"value": False}, {"value": None}]
    assert body["problems"][0]["kind"] == "no_demo"
    assert body["unmatched_demos"] == [
        {"episode_id": f"{run_id}.forward.0009", "demo": f"g/{FWD}/demo_0009"}
    ]
    assert "evidence/check-0003.json" not in text
    assert body["reset_mode"] == "subtask_loop"
    second = body["rollouts"][1]
    assert second["outcome"] == "abandoned"
    assert second["abandoned"] == {"phase": "reverse", "why": "operator abandoned"}
    assert second["notes"][0]["code"] == "motion_refused"
    assert "<private>" in second["notes"][0]["detail"]
    assert second["operator_label"]["value"] == "discard"
    assert second["sets"] == {"forward": None, "reverse": None}


def test_the_route_shows_forward_checks_reasons_and_outcomes_before_labelling(
    client, tmp_path, roots, generator
):
    """User 2026-10-18: the rollout-set blind rule is not needed; the route
    passes no ``blind`` (events and metrics keep T-CL-14)."""
    run_id = finished_run(client, tmp_path, roots, name="r-rollouts")
    body = client.get(f"{BASE}/runs/{run_id}/rollouts").json()
    first = body["rollouts"][0]
    forward = first["sets"]["forward"]["subtasks"][0]["attempts"][0]
    reverse = first["sets"]["reverse"]["subtasks"][0]["attempts"][0]
    assert forward["check"] == {"value": True}
    assert forward["stop_reason"] == "horizon_exhausted"
    assert first["outcome"] == "forward_done"
    assert first["sets"]["forward"]["outcome"] == "done"
    assert api.HIDDEN not in json.dumps(body)
    assert reverse["check"] == {"value": False}
    assert first["sets"]["reverse"]["outcome"] == "done"
    second = body["rollouts"][1]
    assert second["outcome"] == "abandoned"
    assert second["notes"][0]["code"] == "motion_refused"


def test_the_projection_still_hides_an_unlabelled_round_for_a_blind_caller(
    generator,
):
    got = rollout_view.project(
        fake_manifest("run-x"),
        run_id="run-x",
        blind=lambda e: ".forward." in str(e),
        reasons=api.BLIND_REASONS,
    )
    first = got["rollouts"][0]
    forward = first["sets"]["forward"]["subtasks"][0]["attempts"][0]
    reverse = first["sets"]["reverse"]["subtasks"][0]["attempts"][0]
    assert forward["check"] == {api.HIDDEN: True}
    assert forward["stop_reason"] == api.HIDDEN
    assert first["outcome"] == api.HIDDEN
    assert first["sets"]["forward"]["outcome"] == api.HIDDEN
    assert reverse["check"] == {"value": False}


def test_a_labelled_round_shows_forward_checks_reasons_and_outcomes(generator):
    manifest = fake_manifest("run-x")
    manifest["rollouts"][0]["operator_label"] = {
        "value": "success",
        "by": "op",
        "source": "terminal",
    }
    got = rollout_view.project(
        manifest, run_id="run-x", blind=lambda e: True, reasons=api.BLIND_REASONS
    )
    first = got["rollouts"][0]
    out = first["sets"]["forward"]["subtasks"][0]["attempts"][0]
    assert out["check"] == {"value": True}
    assert out["stop_reason"] == "horizon_exhausted"
    assert first["outcome"] == "forward_done"
    assert first["sets"]["forward"]["outcome"] == "done"
    assert first["operator_label"]["value"] == "success"


def test_an_attempt_label_in_the_manifest_reveals_and_is_passed(generator):
    manifest = fake_manifest("run-x")
    row = manifest["rollouts"][0]["sets"]["forward"]["subtasks"][0]["attempts"][0]
    row["operator_label"] = {"value": "success", "at": "2026-10-18T20:00:00+0800"}
    got = rollout_view.project(
        manifest, run_id="run-x", blind=lambda e: True, reasons=api.BLIND_REASONS
    )
    out = got["rollouts"][0]["sets"]["forward"]["subtasks"][0]["attempts"][0]
    assert out["stop_reason"] == "horizon_exhausted"
    assert out["check"] == {"value": True}
    assert out["operator_label"]["value"] == "success"


def test_a_missing_generator_is_503(client, tmp_path, roots, monkeypatch):
    run_id = finished_run(client, tmp_path, roots, name="r-rollouts")
    monkeypatch.setitem(sys.modules, GENERATOR, None)
    response = client.get(f"{BASE}/runs/{run_id}/rollouts")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "rollouts_unavailable"


def test_a_generator_failure_is_409_without_paths(client, tmp_path, roots, generator):
    run_id = finished_run(client, tmp_path, roots, name="r-rollouts")

    def broken(run_dir, rollout_root):
        raise ValueError(f"bad journal at {run_dir}/state_journal.jsonl")

    generator.load_or_build = broken
    response = client.get(f"{BASE}/runs/{run_id}/rollouts")
    assert response.status_code == 409
    assert str(tmp_path) not in response.text


def test_rollout_root_is_only_the_run_folder_shape(tmp_path):
    run_dir = launch.run_dir_of(tmp_path / "models", "run-1")
    assert rollout_view.rollout_root_of(run_dir) == tmp_path / "models"
    assert rollout_view.rollout_root_of(tmp_path / "elsewhere" / "run-1") is None


@pytest.mark.parametrize(
    "demo",
    [None, "", "/abs/g/f/demo_0001", "g/../demo_0001", "g/f", "g/.aeri/demo_0001",
     "g/f/demo_0001/extra"],
)  # fmt: skip
def test_a_demo_path_of_another_shape_has_no_media(demo):
    assert rollout_view.demo_parts(demo) is None
    media = rollout_view.Media(lambda base, parts, limit: b"", lambda: ({}, None, None))
    assert media.of(demo, "x.forward.0001") == {"available": False, "reason": "no_demo"}


# --- media: datasets the file service already serves --------------------------------


def read_below(base, parts, limit):
    return api._read_below(Path(base), parts, limit)


def capture_and_view(tmp_path, name, episode_id, demo="demo_0001", in_view=True):
    capture = tmp_path / "captures" / name
    (capture / demo).mkdir(parents=True)
    (capture / demo / "metadata.json").write_text(
        json.dumps({"eval": {"aeri": {"episode_id": episode_id}}})
    )
    view = tmp_path / "views" / name
    (view / "meta").mkdir(parents=True)
    (view / "meta" / "info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v2.1",
                "chunks_size": 1000,
                "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/"
                "episode_{episode_index:06d}.mp4",
                "features": {
                    "observation.images.side": {"dtype": "video"},
                    "observation.images.wrist": {"dtype": "video"},
                    "observation.state": {"dtype": "float32"},
                },
            }
        )
    )
    rows = [{"episode_index": 0, "source_demo": "demo_0000"}]
    if in_view:
        rows.append({"episode_index": 3, "source_demo": demo})
    (view / "meta" / "episodes.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    return capture, view


def sources_for(items, views, allowed):
    def capture_root(item):
        path = Path(item["path"])
        if not path.resolve().is_relative_to(allowed.resolve()):
            raise ValueError("outside")
        return path

    def view_root(name):
        if name not in views:
            raise ValueError("view not ready")
        return views[name]

    return lambda: (items, capture_root, view_root)


def test_a_linked_live_dataset_with_the_same_episode_gives_file_service_urls(
    tmp_path,
):
    from levi.live import mirror

    name = mirror.dataset_name("g", FWD)
    capture, view = capture_and_view(tmp_path, name, "run-1.forward.0001")
    items = {
        f"live.{name}": {
            "kind": "raw",
            "path": str(capture),
            "linked": {"kind": "live"},
        },
        "other": {"kind": "raw", "path": str(tmp_path / "captures" / "other")},
    }
    media = rollout_view.Media(
        read_below, sources_for(items, {f"live.{name}": view}, tmp_path / "captures")
    )
    got = media.of(f"g/{FWD}/demo_0001", "run-1.forward.0001")
    assert got["available"] is True
    assert got["dataset"] == f"local/live.{name}"
    assert got["episode_index"] == 3
    assert got["viewer"] == f"/local/live.{name}/episode_3"
    assert [v["camera"] for v in got["videos"]] == [
        "observation.images.side",
        "observation.images.wrist",
    ]
    assert got["videos"][0]["url"] == (
        f"/api/levi/files/live.{name}/videos/chunk-000/"
        "observation.images.side/episode_000003.mp4"
    )


def test_media_needs_the_exact_episode_id_and_a_served_view(tmp_path):
    from levi.live import mirror

    name = mirror.dataset_name("g", FWD)
    capture, view = capture_and_view(tmp_path, name, "run-1.forward.0001")
    items = {f"live.{name}": {"kind": "raw", "path": str(capture), "linked": {}}}
    allowed = tmp_path / "captures"
    # Same folder name, another episode: not this attempt's demo.
    media = rollout_view.Media(
        read_below, sources_for(items, {f"live.{name}": view}, allowed)
    )
    assert media.of(f"g/{FWD}/demo_0001", "run-2.forward.0001") == {
        "available": False,
        "reason": "no_dataset",
    }
    # The view is not ready.
    media = rollout_view.Media(read_below, sources_for(items, {}, allowed))
    assert media.of(f"g/{FWD}/demo_0001", "run-1.forward.0001") == {
        "available": False,
        "reason": "no_browsing_view",
    }
    # A capture outside the allowed roots is never read.
    media = rollout_view.Media(
        read_below, sources_for(items, {f"live.{name}": view}, tmp_path / "views")
    )
    assert media.of(f"g/{FWD}/demo_0001", "run-1.forward.0001")["available"] is False


def test_a_demo_left_out_of_the_view_has_no_video(tmp_path):
    from levi.live import mirror

    name = mirror.dataset_name("g", REV)
    capture, view = capture_and_view(tmp_path, name, "run-1.reset.0001", in_view=False)
    items = {f"live.{name}": {"kind": "raw", "path": str(capture), "linked": {}}}
    media = rollout_view.Media(
        read_below,
        sources_for(items, {f"live.{name}": view}, tmp_path / "captures"),
    )
    assert media.of(f"g/{REV}/demo_0001", "run-1.reset.0001") == {
        "available": False,
        "reason": "not_in_view",
    }


def test_a_symlinked_metadata_file_is_not_followed(tmp_path):
    from levi.live import mirror

    name = mirror.dataset_name("g", FWD)
    capture, view = capture_and_view(tmp_path, name, "run-1.forward.0001")
    real = capture / "demo_0001" / "metadata.json"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(real.read_text())
    real.unlink()
    real.symlink_to(elsewhere)
    items = {f"live.{name}": {"kind": "raw", "path": str(capture), "linked": {}}}
    media = rollout_view.Media(
        read_below,
        sources_for(items, {f"live.{name}": view}, tmp_path / "captures"),
    )
    assert media.of(f"g/{FWD}/demo_0001", "run-1.forward.0001")["available"] is False


def test_the_route_attaches_media_after_scrubbing(
    client, tmp_path, roots, generator, monkeypatch
):
    from levi.live import mirror

    run_id = finished_run(client, tmp_path, roots, name="r-rollouts")
    name = mirror.dataset_name("g", FWD)
    capture, view = capture_and_view(tmp_path, name, f"{run_id}.forward.0001")
    items = {f"live.{name}": {"kind": "raw", "path": str(capture), "linked": {}}}
    monkeypatch.setattr(
        rollout_view,
        "default_sources",
        sources_for(items, {f"live.{name}": view}, tmp_path / "captures"),
    )
    body = client.get(f"{BASE}/runs/{run_id}/rollouts").json()
    got = body["rollouts"][0]["sets"]["forward"]["subtasks"][0]["attempts"][0]
    # The URL survives the path scrub (it is added after it).
    assert got["media"]["videos"][0]["url"].startswith(f"/api/levi/files/live.{name}/")
