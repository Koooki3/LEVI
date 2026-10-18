"""Real signal source + original Orchestrator/Cycle/kernel, CPU fixtures only."""

import json

import numpy as np
import pytest
import test_event_pause_orchestrator_runtime as runtime

from levi.automatic import state_machine as sm
from levi.automatic.adapters.signal_events import Binding, CachedObserver, SignalEvents
from levi.automatic.recorder import EvidenceStore
from levi.events.signal_profiles import ChannelSpec, SignalProfile


@pytest.mark.skipif(runtime.KERNEL is None, reason="needs original resident kernel")
def test_rejected_signal_candidate_resumes_at_same_step_with_fresh_observation(
    tmp_path,
):
    fixture = runtime.OrchestratorRuntimeTests()
    try:
        r = fixture.make(
            goal_script=[{"decision": "rejected"}, {"decision": "confirmed"}]
        )
        original_observe = r.robot.observe

        def pixels():
            r.clock.advance(1)
            obs = original_observe()
            step = r.orch.ctx.last_step if r.orch.ctx else 0
            value = float(
                runtime.CANDIDATE_STEP <= step < runtime.SECOND_CANDIDATE_STEP
            )
            obs["pose"][6] = value
            obs["images_bgr"] = {
                "side": np.full((8, 8, 3), r.robot.observe_calls % 200, np.uint8)
            }
            return obs

        r.robot.observe = pixels
        observer = CachedObserver(
            r.robot, r.clock, step_of=lambda: r.orch.ctx.last_step if r.orch.ctx else 0
        )
        profile = SignalProfile(
            channels=[
                ChannelSpec(
                    role="gripper",
                    feature="action",
                    index=6,
                    kind="commanded",
                    units="normalized",
                    valid_range=(0, 1),
                    open_level="high",
                )
            ]
        )
        store = EvidenceStore(tmp_path / "evidence")
        events = SignalEvents(
            observer,
            profile,
            [Binding("action", 6, ("pose", 6), "commanded")],
            run_id=r.config.run_id,
            clock=r.clock,
            evidence_root=store.folder,
            keep_frame=store.keep_frame,
        )
        r.orch.robot, r.orch.events = observer, events
        original_poll = events.poll
        polled, emitted = [], []

        def poll(stream_id, **kwargs):
            polled.append(
                (
                    kwargs["through_step"],
                    observer.latest[1],
                    observer.latest[-1]["observed_ns"],
                )
            )
            wires = original_poll(stream_id, **kwargs)
            emitted.extend(json.loads(wire) for wire in wires)
            return wires

        events.poll = poll
        assert r.orch.run() == "COMPLETED"
        assert r.robot.step_calls == runtime.SECOND_CANDIDATE_STEP
        same_step = [row for row in polled if row[0] == runtime.CANDIDATE_STEP]
        assert len(same_step) == 2
        assert same_step[1][1] > same_step[0][1]
        assert same_step[1][2] > same_step[0][2]
        assert [event["step"] for event in emitted] == [
            runtime.CANDIDATE_STEP,
            runtime.SECOND_CANDIDATE_STEP,
        ]
        assert len({event["event_id"] for event in emitted}) == 2
        resumed = [
            e
            for e in r.orch.journal.events
            if e.record == "committed" and e.reason == "inference_resumed"
        ]
        assert len(resumed) == 1
        assert r.policy.requests == 2
        assert sm.check_journal(r.orch.journal.events) == []
        assert r.owner.snapshot(now_ns=r.clock.now())["request"] is None
        assert r.orch.fence.current is None
    finally:
        fixture.doCleanups()
