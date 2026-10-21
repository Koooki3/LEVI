"""``src/components/marvel0/__tests__/marvel0-command-hz.sample.json``: what the
backend works out for each control rate, which the frontend test parses and
works out again with the page's own code. ``regen_command_hz_sample.py`` rewrites it."""

import json

from regen_command_hz_sample import SAMPLE, build
from regen_sample import ROOT

REGENERATE = "uv run --no-sync python tests/marvel0/regen_command_hz_sample.py"


def test_the_committed_sample_is_what_the_backend_builds():
    name = SAMPLE.relative_to(ROOT)
    assert SAMPLE.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert SAMPLE.read_text() == build(), (
        f"{name} differs from what the backend works out for the control rates; "
        f"regenerate it with: {REGENERATE}"
    )


def test_the_sample_covers_every_rate_with_rtc_on_and_the_edges():
    doc = json.loads(SAMPLE.read_text())
    assert doc["site_control"]["options"] == [5, 10, 20, 25, 50]
    rates = {c["effective"]["hz"] for c in doc["cases"] if c["effective"]["rtc"]}
    assert rates == {5, 10, 20, 25, 50}
    by_name = {c["name"]: c for c in doc["cases"]}
    assert by_name["default_20hz"]["effective"]["horizon"] == 4
    assert by_name["default_10hz"]["flags"] == [
        "--max-step-m=0.1",
        "--max-step-rad=0.35",
        "--rtc-execute-horizon=6",
    ]
    assert by_name["open_loop_50hz"]["control"]["rtc"] is None
    assert "rtc_never_fits" in by_name["default_50hz"]["warnings"]
    assert by_name["wide_horizon_20hz"]["control"]["rtc"]["margin"] == -4
