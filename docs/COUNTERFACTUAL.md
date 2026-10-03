# Counterfactual data: the action contract

This is the data-contract foundation for a CAST-style relabelling and counterfactual-data line of work (the plan is `levi-hub/PLAN-cast.md` in the maintainer's workspace). **Today it is only a contract and a validator.** LEVI does not generate counterfactual actions, import them, store them or show them in the interface, and there is no API or CLI for it.

The package is `levi.counterfactual`:

| Module | What it holds |
| --- | --- |
| `schema.py` | `ActionContract` (immutable) and a registry. Contracts have an `id` and an integer `version`; `get("fr3-robotiq@1")` pins a version, `get("fr3-robotiq")` takes the newest. An id that is not registered raises `UnknownContract`; an `(id, version)` is never replaced |
| `validation.py` | `validate_action_block(actions, contract_id, metadata)`: deterministic checks of one candidate block, returning a `ValidationResult` |

## The FR3-Robotiq contract (`fr3-robotiq@1`)

Read from the code that makes the training data and trains on it; each fact carries its file and lines in the contract's `source` field, and `tests/test_counterfactual_contract.py` compares the constants with those files.

| Fact | Value |
| --- | --- |
| Embodiment | Franka Research 3 with a Robotiq 2F-85 |
| Dimensions | 7, in the order `x, y, z, rx, ry, rz, gripper` (state and action alike) |
| Block | `H = 10` steps (one second); a client executes 8 and asks again |
| Control | 10 Hz, 0.1 s per step |
| What a step is | the **absolute** end-effector pose of the next frame: `action[t] = state[t+1]` (the last frame repeats its own state) |
| Units | metres; Euler angles in radians |
| Frame | `franka_hand_tcp` |
| Gripper | binary command, `1.0` open, `0.0` closed (the command, not the measured width) |
| What training does | adds pi to `rx` and wraps into [-pi, pi), turns `x, y, z, rx, ry, rz` into deltas against the current state, wraps the three angle deltas, leaves the gripper absolute |

A candidate block is checked in the stored (absolute) space, before that training transform. The contract also notes that there is no camera extrinsic calibration for this embodiment, and that the dataset converters differ in how they cut `rx` (the capture script reports roll on [0, 2 pi); LEVI's conversion unwraps it along time), which is one reason the validator does not constrain the rotation range tightly.

## What the validator checks

`validate_action_block(actions, contract_id, metadata=None)` takes a numpy array and a registered contract id. `metadata` may hold `representation` (`"absolute"` or `"delta"`), `time_scale` and `action_mask`. It never reads a file, opens a pickle or runs external code.

| Check | Fails when |
| --- | --- |
| `contract` | the id or version is not registered |
| `array` | not a numeric numpy array (lists, strings, booleans, objects) |
| `shape` | not `[H, D]` equal to the contract's `(10, 7)` |
| `finite` | any NaN or infinity |
| `gripper_values` | a gripper value other than the contract's `0.0` / `1.0` |
| `representation` | not declared, or different from the contract's (`absolute`) |
| `time_scale` | not a positive finite number. A missing value or a deviation above 5 % from 1.0 is a **warning** |
| `action_mask` | missing, not a 1-D list of flags with one entry per step, or no step valid |
| `value_ranges` | never fails: warns about a position beyond 2 m or an angle beyond 4 pi (millimetres or degrees by mistake) in an absolute block |

`time_scale` is the block's control rate divided by the contract's: 1.0 is exactly 10 Hz and 0.95 is 9.5 Hz (rollouts are recorded at about 9.5 to 9.96 fps). A check that cannot run because an earlier one failed is reported as `skipped`. `passed` is false when any check fails; warnings do not fail.

**What a pass means.** Only the properties in the table hold. It does **not** show that the motion is physically feasible or safe on a robot, or that it carries out any language instruction. `ValidationResult.limits` says so in every result.

```python
import numpy as np
from levi.counterfactual import validate_action_block

result = validate_action_block(
    block,  # float32 [10, 7], absolute poses
    "fr3-robotiq@1",
    {"representation": "absolute", "time_scale": 1.0, "action_mask": [1] * 10},
)
print(result.passed, [c.name for c in result.warnings])
```
