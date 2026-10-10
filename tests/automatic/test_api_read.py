# ruff: noqa: F401, F811
"""The read-only routes of the HTTP interface (T-API-1): capabilities,
policies, jobs, plan, runs, snapshot, events, metrics, evidence, frames."""

import json
import os

import pytest
from test_api_common import (
    REQ,
    client,
    home,
    job_id_of,
    roots,
    write_job,
)

from levi.automatic import api, launch

BASE = "/api/levi/automatic"
RUN_KEYS = {
    "run_id", "state", "seq", "reset_mode", "scene_check", "execution_mode",
    "episodes", "counters", "pending_card", "scene_question", "challenge",
    "runner", "updated_at",
}  # fmt: skip


def finished_run(
    client, tmp_path, roots, name="r-read", strategy="single_reset_policy"
):
    """A dry run that ran to its end (in this thread): returns the run id."""
    path = write_job(roots, tmp_path, name=name, strategy=strategy)
    request = launch.LaunchRequest(
        job_path=str(path),
        execution_mode="dry_run",
        entry="api",
        backend="inprocess",
    )
    found = launch.plan(request)
    launch.launch(
        request,
        plan_sha256=found.plan_sha256,
        launch_token=found.launch_token,
        deadline_s=20,
    )
    return name


def test_capabilities_say_only_a_dry_run_launches(client):
    body = client.get(f"{BASE}/capabilities").json()
    assert body["modes"]["dry_run"] == {"available": True}
    for mode in ("shadow", "assisted", "autonomous"):
        assert body["modes"][mode]["available"] is False
        assert body["modes"][mode]["reason"]
    assert {a["kind"] for a in body["adapters"]} == {"fake", "robot"}
    assert body["reset_modes"] == ["single_reset_policy", "human_assisted"]
    assert body["scene_checks"] == ["provider", "operator_attested"]
    assert set(body["defaults"]) == {"max_steps", "episodes", "reset_wait_s"}


def make_checkpoint(root, name, *, role=None, config="pi05_fr3_all_state"):
    folder = root / name
    (folder / "params").mkdir(parents=True)
    (folder / "assets").mkdir()
    (folder / "norm_stats.json").write_text("{}")
    version = {"config": config}
    if role:
        version["policy_role"] = role
    (folder / "VERSION.json").write_text(json.dumps(version))
    return folder


def test_policies_without_a_root_offer_human_reset_only(client):
    body = client.get(f"{BASE}/policies").json()
    assert body["checkpoints"] == [] and body["reset_available"] is False
    assert body["root_configured"] is False


def test_policies_list_deployable_checkpoints_and_no_reset(
    client, tmp_path, monkeypatch
):
    root = tmp_path / "ckpt"
    root.mkdir()
    make_checkpoint(root, "fwd-a", role="forward")
    (root / "pytorch-only" / "actor").mkdir(parents=True)
    monkeypatch.setenv(api.POLICY_ROOT_ENV, str(root))
    body = client.get(f"{BASE}/policies").json()
    assert [c["id"] for c in body["checkpoints"]] == ["fwd-a"]
    first = body["checkpoints"][0]
    assert first["role"] == "forward" and first["sha256_status"] == "none"
    assert set(first) >= {"id", "name", "role", "config", "sha256_status", "notes"}
    assert body["reset_available"] is False and body["hidden"] == 1
    # No absolute path anywhere in the answer.
    assert str(tmp_path) not in json.dumps(body)


def test_a_stated_reset_checkpoint_makes_reset_available(client, tmp_path, monkeypatch):
    root = tmp_path / "ckpt"
    root.mkdir()
    make_checkpoint(root, "fwd-a", role="forward")
    make_checkpoint(root, "rst-a", role="reset", config="some_reset_config")
    monkeypatch.setenv(api.POLICY_ROOT_ENV, str(root))
    body = client.get(f"{BASE}/policies").json()
    assert body["reset_available"] is True


def test_sha256_status_is_verified_only_for_a_list_naming_weights(
    client, tmp_path, monkeypatch
):
    root = tmp_path / "ckpt"
    root.mkdir()
    folder = make_checkpoint(root, "fwd-h", role="forward")
    (folder / "params" / "w.bin").write_bytes(b"x")
    (folder / "manifest.sha256").write_text(f"{'a' * 64}  params/w.bin\n")
    monkeypatch.setenv(api.POLICY_ROOT_ENV, str(root))
    body = client.get(f"{BASE}/policies").json()
    assert body["checkpoints"][0]["sha256_status"] in ("verified", "recorded")


def test_jobs_listing_hides_paths_and_marks_invalid(client, tmp_path, roots):
    write_job(roots, tmp_path, name="good")
    (roots / "bad.yaml").write_text("not: [a job")
    body = client.get(f"{BASE}/jobs").json()
    rows = {r["name"]: r for r in body["jobs"]}
    assert rows["good.yaml"]["valid"] is True
    assert rows["good.yaml"]["reset_mode"] == "single_reset_policy"
    assert rows["bad.yaml"]["valid"] is False and rows["bad.yaml"]["errors"]
    assert str(tmp_path) not in json.dumps(body)
    assert all(r["id"].startswith("j-") for r in body["jobs"])
    assert set(rows["good.yaml"]) == {
        "id",
        "name",
        "modified",
        "valid",
        "reset_mode",
        "errors",
    }


def test_plan_has_the_contract_shape_and_a_token(client, tmp_path, roots):
    write_job(roots, tmp_path, name="p1")
    job_id = job_id_of(client, "p1")
    plan = client.post(
        f"{BASE}/plan", json={"job_id": job_id, "execution_mode": "dry_run"}
    ).json()
    assert plan["launchable"] is True and plan["refusals"] == []
    assert plan["execution_mode"] == "dry_run" and plan["motion"] is False
    assert plan["roles"] == ["forward", "reset"]
    assert plan["isc"]["id"] == "stack-plates-initial"
    assert plan["launch_token"] and isinstance(plan["token_expires_at"], int)
    assert {c["severity"] for c in plan["checks"]} <= {"info", "warn", "error"}
    assert str(tmp_path) not in json.dumps(plan)


def test_a_real_mode_plan_is_not_launchable(client, tmp_path, roots):
    write_job(roots, tmp_path, name="p2")
    plan = client.post(
        f"{BASE}/plan",
        json={"job_id": job_id_of(client, "p2"), "execution_mode": "assisted"},
    ).json()
    assert plan["launchable"] is False and "E_NO_ROBOT_ADAPTER" in plan["refusals"]


def test_plan_of_an_unknown_job_is_404_and_of_a_bad_one_422(client, tmp_path, roots):
    missing = client.post(f"{BASE}/plan", json={"job_id": "j-0000"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"
    (roots / "bad.yaml").write_text("schema_version: nope\n")
    response = client.post(f"{BASE}/plan", json={"job_id": job_id_of(client, "bad")})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "job_invalid"
    assert response.json()["detail"]["errors"]


def test_a_job_outside_the_roots_cannot_be_named(client, tmp_path):
    response = client.post(f"{BASE}/plan", json={"job_id": str(tmp_path / "x.yaml")})
    assert response.status_code == 404


def test_snapshot_events_metrics_of_a_finished_run(client, tmp_path, roots):
    name = finished_run(client, tmp_path, roots)
    listing = client.get(f"{BASE}/runs").json()["runs"]
    assert [r["run_id"] for r in listing] == [name]
    summary = listing[0]
    assert set(summary) == {
        "run_id", "state", "reset_mode", "execution_mode",
        "episodes_done", "episodes_total", "started_at", "updated_at",
    }  # fmt: skip
    assert summary["state"] == "COMPLETED" and summary["execution_mode"] == "dry_run"
    assert summary["episodes_done"] == summary["episodes_total"] == 2

    snap = client.get(f"{BASE}/runs/{name}").json()
    assert set(snap) == RUN_KEYS
    assert snap["state"] == "COMPLETED" and snap["pending_card"] is None
    assert snap["runner"]["alive"] is False and snap["scene_question"] is None
    assert set(snap["counters"]) == {
        "planned_interventions",
        "unplanned_interventions",
        "faults",
    }
    assert snap["episodes"]["done"] == 2 and snap["episodes"]["total"] == 2
    assert snap["challenge"] and snap["seq"] > 5

    events = client.get(f"{BASE}/runs/{name}/events", params={"limit": 3}).json()
    assert len(events["events"]) == 3
    assert set(events["events"][0]) == {"seq", "at", "kind", "from", "to", "reason"}
    more = client.get(
        f"{BASE}/runs/{name}/events", params={"after": events["next"]}
    ).json()
    assert more["events"] and more["events"][0]["seq"] > events["next"]
    assert str(tmp_path) not in json.dumps([events, more])

    report = client.get(f"{BASE}/runs/{name}/metrics").json()
    assert report["reset_mode"] == "single_reset_policy" and "comparable" in report
    assert "agreement" in report


def test_the_snapshot_challenge_is_stable_while_nothing_moves(client, tmp_path, roots):
    name = finished_run(client, tmp_path, roots, name="r-chal")
    first = client.get(f"{BASE}/runs/{name}").json()["challenge"]
    assert client.get(f"{BASE}/runs/{name}").json()["challenge"] == first


def test_get_routes_write_nothing(client, tmp_path, roots, home):
    name = finished_run(client, tmp_path, roots, name="r-quiet")

    def tree():
        return sorted(
            (str(p), p.stat().st_mtime_ns, p.stat().st_size)
            for base in (tmp_path / "rollouts", home)
            for p in base.rglob("*")
        )

    before = tree()
    for path in ("runs", f"runs/{name}", f"runs/{name}/events", f"runs/{name}/metrics",
                 "jobs", "policies", "capabilities"):  # fmt: skip
        assert client.get(f"{BASE}/{path}").status_code == 200
    assert tree() == before


@pytest.mark.parametrize("run_id", ["nope", "../etc", "a b", "x" * 200])
def test_unknown_or_malformed_run_ids_are_404(client, run_id):
    for path in ("", "/events", "/metrics"):
        assert client.get(f"{BASE}/runs/{run_id}{path}").status_code in (404, 422)


def test_blind_events_hide_why_an_episode_ended(client, tmp_path, roots):
    name = finished_run(client, tmp_path, roots, name="r-blind")
    rows = client.get(f"{BASE}/runs/{name}/events", params={"limit": 500}).json()[
        "events"
    ]
    reasons = {r["reason"] for r in rows}
    assert not reasons & {"goal_verified", "horizon_exhausted", "operator_stop"}
    assert api.HIDDEN in reasons


def test_blind_metrics_withhold_the_run_s_own_verdict_rates(client, tmp_path, roots):
    name = finished_run(client, tmp_path, roots, name="r-metrics")
    report = client.get(f"{BASE}/runs/{name}/metrics").json()
    assert report["autonomous"] is None and report["early_termination"] is None
    assert report["withheld"] == ["autonomous", "early_termination"]


def _evidence_run(client, tmp_path, roots):
    name = finished_run(client, tmp_path, roots, name="r-evidence")
    run_dir = launch.read_record(launch.index_path(name))["run_dir"]
    folder = os.path.join(run_dir, "evidence")
    os.makedirs(os.path.join(folder, "frames"), exist_ok=True)
    return name, run_dir, folder


def test_evidence_and_frames_are_confined(client, tmp_path, roots):
    name, run_dir, folder = _evidence_run(client, tmp_path, roots)
    digest = "ab" * 32
    with open(os.path.join(folder, "frames", f"{digest}.jpg"), "wb") as handle:
        handle.write(b"\xff\xd8\xff-jpeg")
    with open(os.path.join(folder, "q1.json"), "w") as handle:
        json.dump(
            {"schema": "x", "note": f"saved under {run_dir}/evidence", "n": 1}, handle
        )
    frame = client.get(f"{BASE}/runs/{name}/frames/{digest}")
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
    assert frame.content.startswith(b"\xff\xd8\xff")
    record = client.get(f"{BASE}/runs/{name}/evidence/q1")
    assert record.status_code == 200 and record.json()["n"] == 1
    assert "<path>" in record.json()["note"] and run_dir not in record.text


def test_evidence_and_frame_ids_reject_traversal_and_symlinks(client, tmp_path, roots):
    name, _run_dir, folder = _evidence_run(client, tmp_path, roots)
    secret = tmp_path / "secret.json"
    secret.write_text('{"leak": true}')
    os.symlink(secret, os.path.join(folder, "evil.json"))
    digest = "cd" * 32
    os.symlink(secret, os.path.join(folder, "frames", f"{digest}.jpg"))
    assert client.get(f"{BASE}/runs/{name}/evidence/evil").status_code == 404
    assert client.get(f"{BASE}/runs/{name}/frames/{digest}").status_code == 404
    for bad in ("..%2F..%2Fsecret", "a%2Fb", "%2e%2e", "q" * 300):
        assert client.get(f"{BASE}/runs/{name}/evidence/{bad}").status_code == 404
    for bad in ("zz", "AB" * 32, "ab" * 31, f"{digest}.jpg"):
        assert client.get(f"{BASE}/runs/{name}/frames/{bad}").status_code == 404
    # The run folder replaced by a link to elsewhere is no run any more.
    assert client.get(f"{BASE}/runs/{name}/evidence/initial_state").status_code == 404


def test_the_run_index_cannot_point_outside(client, tmp_path, roots, home):
    index = home / launch.RUNS
    index.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / launch.LAUNCH_RECORD).write_text("{}")
    (index / "r-evil.json").write_text(
        json.dumps({"run_id": "r-evil", "run_dir": str(other)})
    )
    assert client.get(f"{BASE}/runs/r-evil").status_code == 404
