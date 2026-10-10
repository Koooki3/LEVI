"""Event intelligence: what a robot's recorded signals say happened, and when.

The modules, in the order data flows through them:

- ``gripper``: open/close crossings of one gripper channel, with hysteresis.
  The one kernel behind ``agent.signals`` (the signal lines an agent reads)
  and ``agent.anchored`` (where an anchored review looks); how the two differ
  -- which range a crossing is measured on, which end is open, whether a
  barely moving gripper counts -- is an explicit argument, not a copy.
- ``motion``: speed and stillness on the recorded timestamps.

Pure functions over numpy arrays: no model, no Torch, no files.
"""
