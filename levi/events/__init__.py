"""Event intelligence: what a robot's recorded signals say happened, and when.

The modules, in the order data flows through them:

- ``gripper``: open/close crossings of one gripper channel, with hysteresis.
  The one kernel behind ``agent.signals`` (the signal lines an agent reads)
  and ``agent.anchored`` (where an anchored review looks); how the two differ
  -- which range a crossing is measured on, which end is open, whether a
  barely moving gripper counts -- is an explicit argument, not a copy.
- ``motion``: speed and stillness on the recorded timestamps.
- ``contracts``: ``SignalObservation`` and ``EventCandidate``, the records
  the readers produce (``salience`` is an evidence priority, not a
  probability).
- ``signal_profiles``: what each recorded channel means -- role, actor,
  units, frame, which end of a gripper is open -- inferred from
  ``meta/info.json`` with the signal lines' rules, overridable by a
  declaration.
- ``facts``: the signal facts of one episode, per channel and actor.

Pure functions over numpy arrays and pydantic records: no model, no Torch,
no files written. See docs/EVENTS.md.
"""
