# ruff: noqa: F401, F811
"""A dry-run campaign end to end through the HTTP interface (T-API-2): two
arms, two segments each, a real dry run per segment, a report at the end;
blinding, peeks, pause and resume, the report routes."""

import json

from campaign_guard import aeri_home_fixture, guard_fixture
from test_api_campaign_common import (
    URL,
    wait_for,
    world,
)

BANNED = ("success", "rate", "agreement", "verdict", "label", "wilson", "p_value")


def at_segment(world, cid, number, kind="place_cards"):
    return wait_for(
        lambda: (
            (s := world.snapshot(cid))["segment"]["no"] == number
            and s["todo"]
            and s["todo"]["kind"] == kind
            and s
        ),
        describe=lambda: world.dump(cid),
    )


def keys_of(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            keys_of(item, found)
    elif isinstance(value, list):
        for item in value:
            keys_of(item, found)
    return found


def run_to_end(world, cid, segments=4, start=1, seen=None):
    for number in range(start, segments + 1):
        snap = at_segment(world, cid, number)
        if seen is not None:
            seen.append(snap)
        answer = world.confirm(cid, snap["todo"], f"cmd-env-{number}")
        assert answer.status_code == 200, answer.text
        assert answer.json()["result"] == "applied"
    return world.wait_state(cid, "REPORTED")


def started(world, **over):
    job = world.make_job()
    answer, plan = world.start(job, execution_mode="dry_run", **over)
    assert answer.status_code == 202, answer.text
    return answer.json()["campaign_id"], plan


def test_a_dry_run_campaign_runs_to_its_report(world):
    cid, plan = started(world)
    seen: list = []
    done = run_to_end(world, cid, seen=seen)
    assert [s["segment"]["no"] for s in seen] == [1, 2, 3, 4]
    todo = seen[0]["todo"]
    assert todo["kind"] == "place_cards" and todo["challenge"]
    assert todo["cards"] == plan["segments"][0]["cards"]
    assert seen[0]["segment"]["total"] == 4
    assert done["blinded"] is False and done["todo"] is None
    assert {a["done"] for a in done["arms"]} == {4}
    assert {a["remaining"] for a in done["arms"]} == {0}
    assert done["safety"] == {"faults": 0, "fused": False}
    report = world.client.get(f"{URL}/{cid}/report")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["basis"] == "autonomous_verdict"
    assert set(body) == {"basis", "files", "analysis", "manifest"}
    names = {f["name"] for f in body["files"]}
    assert {"manifest.json", "figures/f1-success.json", "data/analysis.json"} <= names
    assert {"table", "figure", "figure_spec", "data", "summary", "manifest"} >= {
        f["kind"] for f in body["files"]
    }
    assert body["manifest"]["campaign_sha256"] == plan["campaign_sha256"]
    assert body["analysis"]["schema"]
    # Files: a figure spec is JSON, a figure is SVG, a table is CSV.
    spec = world.client.get(f"{URL}/{cid}/report/files/figures/f1-success.json")
    assert (
        spec.status_code == 200 and spec.headers["content-type"] == "application/json"
    )
    assert json.loads(spec.content)
    svg = world.client.get(f"{URL}/{cid}/report/files/figures/f1-success.svg")
    assert svg.headers["content-type"] == "image/svg+xml"
    csv = world.client.get(f"{URL}/{cid}/report/files/tables/success.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    listed = world.client.get(URL).json()["campaigns"]
    assert [c["id"] for c in listed] == [cid] and listed[0]["state"] == "REPORTED"


def test_a_blinded_campaign_shows_no_rate_anywhere(world, aeri_home):
    cid, _ = started(world)
    shots = []
    for number in range(1, 5):
        snap = at_segment(world, cid, number)
        shots.append(snap)
        shots.append(world.client.get(URL).json())
        assert snap["blinded"] is True
        world.confirm(cid, snap["todo"], f"cmd-env-{number}")
        shots.append(world.client.get(f"{URL}/{cid}").json())
    for shot in shots:
        for key in keys_of(shot):
            assert not any(word in key for word in BANNED), key
    # The arms are shown by their codes, never by their letters.
    assert {a["id"] for a in shots[0]["arms"]} == {"X1", "X2"}
    for path in (aeri_home / "campaigns" / cid).rglob("*"):
        if path.is_file() and "report" not in path.parts:
            text = path.read_bytes()
            assert b"success_rate" not in text and b"wilson" not in text, path
    world.wait_state(cid, "REPORTED")
    # Once the campaign has ended the letters come back.
    assert {a["id"] for a in world.snapshot(cid)["arms"]} == {"A", "B"}


def test_the_report_is_not_served_while_blind(world):
    cid, _ = started(world)
    at_segment(world, cid, 1)
    for url in (f"{URL}/{cid}/report", f"{URL}/{cid}/report/files/manifest.json"):
        answer = world.client.get(url)
        assert answer.status_code == 409
        assert answer.json()["detail"]["code"] == "blinded"
    recompute = world.client.post(f"{URL}/{cid}/report", json={"command_id": "rp-1"})
    assert recompute.status_code == 409


def test_an_unblind_is_a_counted_peek(world, aeri_home):
    cid, _ = started(world)
    at_segment(world, cid, 1)
    wrong = world.client.post(
        f"{URL}/{cid}/unblind", json={"command_id": "ub-0", "confirm": "yes"}
    )
    assert wrong.status_code == 422
    first = world.command(cid, "unblind", "ub-1")
    assert first.status_code == 200 and first.json()["result"] == "applied"
    assert world.command(cid, "unblind", "ub-1").json()["result"] == "repeated"
    after = world.snapshot(cid)
    assert after["blinded"] is False and after["peeks"] == 1
    assert {a["id"] for a in after["arms"]} == {"A", "B"}
    run_to_end(world, cid)
    # A look afterwards is no peek.
    assert world.command(cid, "unblind", "ub-2").json()["result"] == "repeated"
    report = world.client.get(f"{URL}/{cid}/report").json()
    assert report["manifest"]["peeks"] == 1
    assert report["analysis"]["conclusion_level"]["level"] == "exploratory"


def test_a_pause_takes_effect_at_the_segment_boundary(world):
    cid, _ = started(world)
    snap = at_segment(world, cid, 1)
    paused = world.command(cid, "pause", "pz-1")
    assert paused.status_code == 200 and paused.json()["queued"] is True
    assert world.command(cid, "pause", "pz-1").json()["result"] == "repeated"
    world.confirm(cid, snap["todo"], "cmd-env-1")
    held = world.wait_state(cid, "PAUSED")
    assert held["segment"]["no"] == 1 and held["todo"] is None
    assert world.command(cid, "pause", "pz-2").status_code == 409
    resumed = world.command(cid, "resume", "rs-1")
    assert resumed.status_code == 200, resumed.text
    run_to_end(world, cid, start=2)
    assert world.command(cid, "resume", "rs-2").status_code == 409


def test_confirms_are_idempotent_one_time_and_bound_to_the_step(world):
    cid, _ = started(world)
    snap = at_segment(world, cid, 1)
    stale = world.client.post(
        f"{URL}/{cid}/confirm",
        json={"command_id": "c-1", "kind": "env", "challenge": "made-up"},
    )
    assert stale.status_code == 200
    assert stale.json() == {"result": "refused", "code": "stale_sequence"}
    wrong_kind = world.client.post(
        f"{URL}/{cid}/confirm",
        json={
            "command_id": "c-2",
            "kind": "switch_policy",
            "challenge": snap["todo"]["challenge"],
        },
    )
    assert wrong_kind.status_code == 409
    assert wrong_kind.json()["detail"]["code"] == "not_waiting"
    good = world.confirm(cid, snap["todo"], "c-3")
    assert good.json()["result"] == "applied"
    # The same command again, and the same challenge under another command.
    assert world.confirm(cid, snap["todo"], "c-3").json()["result"] == "repeated"
    again = world.confirm(cid, snap["todo"], "c-4")
    # Either nothing is asked any more (409) or, in the moment between the
    # controller taking the answer and withdrawing the question, the used
    # challenge is refused (200); no second answer is ever written.
    if again.status_code == 409:
        assert again.json()["detail"]["code"] == "not_waiting"
    else:
        assert again.json() == {"result": "refused", "code": "stale_sequence"}
    run_to_end(world, cid, start=2)


def test_an_agent_cannot_write_and_a_stranger_cannot_read_a_path(world):
    cid, _ = started(world)
    at_segment(world, cid, 1)
    bearer = {"authorization": "Bearer agent-credential"}
    posts = {
        f"{URL}/{cid}/confirm": {
            "command_id": "x-1",
            "kind": "env",
            "challenge": "c",
        },
        f"{URL}/{cid}/pause": {"command_id": "x-2", "confirm": "pause"},
        f"{URL}/{cid}/resume": {"command_id": "x-3", "confirm": "resume"},
        f"{URL}/{cid}/unblind": {"command_id": "x-4", "confirm": "unblind"},
        f"{URL}/{cid}/attach": {"request_id": "x-5"},
        f"{URL}/{cid}/cards": {"command_id": "x-6", "episode_key": "k", "card": "c1"},
        f"{URL}/{cid}/report": {"command_id": "x-7"},
    }
    for url, body in posts.items():
        answer = world.client.post(url, json=body, headers=bearer)
        assert answer.status_code == 403, url
        assert answer.json()["detail"]["code"] == "person_only", url
        no_token = world.client.post(url, json=body, headers={"x-levi-ui-token": "no"})
        assert no_token.status_code == 401, url
    # Nothing it tried took effect.
    assert world.snapshot(cid)["blinded"] is True
    for bad in ("..", "c-nope", "c%20x", "a" * 80):
        assert world.client.get(f"{URL}/{bad}").status_code in (404, 422)
    assert world.client.get(f"{URL}/c-0000000000").status_code == 404


def test_report_files_stay_below_the_basis_folder(world, aeri_home):
    cid, _ = started(world)
    run_to_end(world, cid)
    base = f"{URL}/{cid}/report/files"
    for name in (
        "../manifest.json",
        "..%2Fplan.json",
        "figures/../../../plan.json",
        "/etc/passwd",
        "figures",
        "nope.csv",
        ".hidden",
    ):
        assert world.client.get(f"{base}/{name}").status_code == 404, name
    root = aeri_home / "campaigns" / cid / "report" / "autonomous_verdict"
    (root / "link.json").symlink_to(aeri_home / "campaigns" / cid / "plan.json")
    assert world.client.get(f"{base}/link.json").status_code == 404
    names = {f["name"] for f in world.client.get(f"{URL}/{cid}/report").json()["files"]}
    assert "link.json" not in names
    missing = world.client.get(f"{URL}/{cid}/report?basis=operator_label")
    assert (
        missing.status_code == 404 and missing.json()["detail"]["code"] == "no_report"
    )
    invalid = world.client.get(f"{URL}/{cid}/report?basis=ground_truth")
    assert invalid.status_code == 422


def test_the_report_can_be_made_again_for_a_basis(world):
    cid, _ = started(world)
    run_to_end(world, cid)
    again = world.client.post(
        f"{URL}/{cid}/report",
        json={"command_id": "rp-1", "basis": "autonomous_verdict"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["result"] == "applied"
    assert again.json()["basis"] == "autonomous_verdict"
    other = world.client.post(
        f"{URL}/{cid}/report", json={"command_id": "rp-2", "basis": "posthoc_verdict"}
    )
    assert other.status_code == 200, other.text
    shown = world.client.get(f"{URL}/{cid}/report?basis=posthoc_verdict")
    assert shown.status_code == 200 and shown.json()["basis"] == "posthoc_verdict"
