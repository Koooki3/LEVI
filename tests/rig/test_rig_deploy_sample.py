"""``src/components/rig/__tests__/rig-deploy.samples.json``: the committed
answers of the rig's deploy and catalog routes that the frontend tests parse
with the page's own readers. They are what the backend gives on a fully fake rig
(``regen_deploy_sample.py``), so a field the backend renames, drops or adds
fails here, naming the command that rewrites the file, instead of leaving the
page reading a shape that no longer exists."""

import json
import re
import time

import pytest
from regen_deploy_sample import SAMPLE, build
from regen_sample import ROOT
from test_rig_deploy import forbid_real_machine  # noqa: F401 - the guard

REGENERATE = "uv run --no-sync python tests/rig/regen_deploy_sample.py"
PATH_IN_TEXT = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")


def test_the_committed_samples_are_what_the_backend_builds():
    name = SAMPLE.relative_to(ROOT)
    assert SAMPLE.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert SAMPLE.read_text() == build(), (
        f"{name} differs from the answers the backend builds; "
        f"regenerate it with: {REGENERATE}"
    )


def test_the_build_does_not_depend_on_the_machines_time_zone(monkeypatch):
    expected = build()
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert build() == expected
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_samples_name_every_state_the_page_reads():
    samples = json.loads(SAMPLE.read_text())
    assert {
        name: samples[name]["state"]
        for name in (
            "idle",
            "starting",
            "ready",
            "ready_with_client",
            "failed_oom",
            "foreign",
            "ready_cfg",
            "blocked_arm",
            "idle_after_stop",
        )
    } == {
        "idle": "idle",
        "starting": "starting",
        "ready": "ready",
        "ready_with_client": "ready",
        "failed_oom": "failed",
        "foreign": "foreign_listener",
        "ready_cfg": "ready",
        "blocked_arm": "idle",
        "idle_after_stop": "idle",
    }
    assert samples["deployed"]["created"] is True
    assert samples["stopped"]["stopped"] is True
    assert set(samples["refusals"]) == {
        "already_running",
        "gpu_insufficient",
        "port_taken",
        "unverified_not_accepted",
    }


def test_the_samples_hold_no_path_but_the_templates():
    text = json.dumps(json.loads(SAMPLE.read_text()))
    found = {m for m in PATH_IN_TEXT.findall(text)}
    assert all(p.startswith("/path/to/") or p == "/tmp/" for p in found), found


@pytest.mark.parametrize("name", ["starting", "ready", "failed_oom"])
def test_a_deploy_state_has_no_aeri_registry_row(name):
    # (a poll does not read the run registry: see Supervisor._checks)
    rows = json.loads(SAMPLE.read_text())[name]["checks"]
    assert "aeri_run" not in [row["code"] for row in rows]
