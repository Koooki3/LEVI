"""Regenerates ``src/components/rig/__tests__/rig-status.sample.json`` (a busy
rig) and ``rig-status.quiet.sample.json`` (a quiet one): the exact answers of
``GET /api/levi/rig/status`` (``levi.rig.api.collect``) on a fully fake rig,
which the frontend tests load as the server's real shape.

    uv run --no-sync python tests/rig/regen_sample.py

The rig is a temporary folder, a fixed clock, a fake ``systemctl`` and a fake
setup-probes answer; nothing of the real machine is looked at and nothing is
connected to. ``tests/rig/test_rig_sample.py`` fails, naming this command,
when the committed file differs from what this builds."""

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from levi.rig import api
from levi.rig import status as S
from levi.setup import probes

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "src" / "components" / "rig" / "__tests__"
SAMPLE = FOLDER / "rig-status.sample.json"
QUIET_SAMPLE = FOLDER / "rig-status.quiet.sample.json"

NOW = 1_792_395_000.0  # 2026-10-19 07:30:00 UTC
ZONE = "UTC-8"  # POSIX spelling of "eight hours east": sampled_at ends in +08:00
UNITS = "levi-aeri-policy-pi05.service loaded active running LEVI AERI policy server\n"
GUARD = {"state": "ok", "quiet": True, "robot": [], "controller_active": None}
# The setup probes' answer for a fresh sample: its guard and its age (an answer
# that does not say how old it is counts as old).
ANSWER = {"guard": GUARD, "cached": False, "age_s": 0.0}


@contextlib.contextmanager
def fixed_zone(spec=ZONE):
    """``sampled_at`` carries the local offset; fix it for the build."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = spec
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def build(busy=True) -> str:
    """The file's text: with ``busy`` one marker present, one fresh session,
    one active unit, so the verdict is busy; otherwise none of them (idle)."""
    with tempfile.TemporaryDirectory() as tmp, fixed_zone():
        folder = Path(tmp)
        marker = folder / ".arm-controller-active"
        sessions = folder / "rollouts" / ".eval_sessions"
        sessions.mkdir(parents=True)
        if busy:
            marker.write_text("")
            os.utime(marker, (NOW - 95, NOW - 95))
            session = sessions / "pi05_run_a__plates.json"
            body = {"group": "pi05_run_a", "state": "running", "updated_at": NOW - 12}
            session.write_text(json.dumps(body))
            os.utime(session, (NOW - 12, NOW - 12))
        roots = ([str(folder / "rollouts")], None)
        units = UNITS if busy else ""
        with (
            mock.patch.object(api, "_READS", {}),
            mock.patch.object(api, "marker_path", lambda: marker),
            mock.patch.object(api, "rollout_roots", lambda: roots),
            mock.patch.object(S, "run_systemctl", lambda argv, timeout: (0, units)),
            mock.patch.object(probes, "status", lambda: ANSWER),
        ):
            answer = api.collect(NOW)
    return json.dumps(answer, indent=2) + "\n"


if __name__ == "__main__":
    for path, busy in ((SAMPLE, True), (QUIET_SAMPLE, False)):
        path.write_text(build(busy))
        print(f"wrote {path.relative_to(ROOT)}")
