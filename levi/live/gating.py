"""The live service's gate, seen from the core.

In timeshare mode the supervisor closes ``live/gate.json`` while the policy
server infers. The worker obeys it; this is the same rule for a *person* who
presses Run or Resume in the interface meanwhile (the core runs the model
call in its own process and knows nothing of the policy). Stdlib only: the
core imports it on every ``runs.execute`` in a live workspace.
"""

from __future__ import annotations

import time

from . import jsonio

GUARDED = frozenset({"runs.execute", "runs.resume"})
# The worker's own principals obey the gate themselves (``worker.stand_down``).
EXEMPT = frozenset({"live-auto", "live-planner"})
STALE_S = 20.0  # no heartbeat for this long: the supervisor is gone, not gating


def check(principal, name: str, live_dir=None):
    """Raise ``Conflict`` when ``name`` would start model requests while the
    gate is closed. A no-op outside a live workspace."""
    if name not in GUARDED:
        return
    if getattr(principal, "auto", False) or principal.id in EXEMPT:
        return
    from . import auto

    live = live_dir or auto.live_dir()
    if not (live / auto.MARKER).is_file():
        return
    gate = jsonio.read(live / "gate.json")
    if not isinstance(gate, dict) or gate.get("open", True):
        return
    if time.time() - float(gate.get("updated_at") or 0) > STALE_S:
        return
    from levi.agent.store import Conflict

    raise Conflict(
        "The robot evaluation is inferring on the GPU "
        f"({gate.get('reason') or gate.get('code') or 'gate closed'}); "
        "the evaluation is inferring, try again shortly"
    )
