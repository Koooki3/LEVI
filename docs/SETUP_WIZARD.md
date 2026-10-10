# Setup wizard: recipes and status probes

English | [中文](SETUP_WIZARD.zh-CN.md)

LEVI can guide a person through setting up a real-robot session: which
command comes next, what it touches, and whether the step it starts is
already healthy. This page covers the two parts that exist so far: the
**recipe registry**, which ties each step to the machine's own operator
guide, and the **read-only status probes** behind `GET /api/levi/setup/status`.
Neither starts, stops or connects to anything.

## Recipes

A recipe is one excerpt of the machine's operator guide (a Markdown file)
plus what it touches and how an interface may offer it. The recipe file holds
the machine's own paths, addresses and serial numbers, so it lives outside the
repository; point `LEVI_SETUP_RECIPES` at it and `LEVI_SETUP_DOC` at the guide.

```toml
version = 1

[[recipe]]
id = "R-CHK-3"
title = "GPU, process and port snapshot"
risk = 1            # 1 read-only, 2 LEVI/vLLM/recorder service, 3 robot stack or policy server, 4 makes the robot move
ui = "native"       # native | execute | copy | link
requires = []      # ids of recipes that come first
preconditions = ["nothing else is needed"]

[recipe.source]
section = "1"       # the guide's section number
heading = "Order"   # its title, when recorded (a renamed heading is drift)
block = 1           # code block of that section, from 1; absent = the whole section body
pick = [4, 6]       # optional line range inside the block
lines = [58, 60]    # where it was when recorded (reference only)
sha256 = "<64 hex digits of the normalised excerpt>"
# text = "..."      # optional: the recorded excerpt, to show a difference on drift

[recipe.touches]
robot = false
gpu = false
moves = false
commands_robot = false
listens = []        # ports it opens
connects = []       # ports it connects to
```

**Safety rules (the validator refuses a recipe that breaks one, and leaves it
out):**

- Class 3 and class 4 recipes are never `execute` and never `native`: they
  are only offered as `copy` or `link`.
- The one exception is a class 3 policy server (`kind = "policy_server"`) that
  only loads a model: it may be `execute` if `touches.robot`, `moves` and
  `commands_robot` are all false and it carries
  `confirm = { required = true, decision = "<who allowed it, when>" }`.
  Nothing in LEVI runs it yet.
- `moves = true` requires class 4; `commands_robot = true` requires class 3
  or 4.
- A recipe that connects to a robot-side port (5000, 5001, 5100, 7470, 8000)
  is only `copy` or `link`: LEVI never connects there.

**Drift.** The hash covers the excerpt after Unicode NFC, trailing spaces
removed and leading and trailing blank lines removed; any other change, one
character included, is drift. A check finds each excerpt by section number
and block index, never by line number:

| Status | Meaning |
| --- | --- |
| `ok` | the excerpt is unchanged (`shifted` when it now sits on other lines) |
| `drift` | the text or the section heading changed: review it before anyone copies the command (a unified diff is shown when the recipe records `text`) |
| `moved` | the same text is elsewhere (blocks reordered, section renumbered): update the recipe's source |
| `missing` | the section, the block or the picked lines are gone |
| `ambiguous` | the section number appears more than once |
| `doc_error` | the guide cannot be read reliably (a code fence left open) |

```bash
levi setup recipes check --recipes site/setup-recipes.toml --doc setup.md    # exit 0 / 1 (problems or drift) / 2 (unreadable)
levi setup recipes check --json
levi setup recipes excerpt --doc setup.md --section 1 --block 1 --pick 4 6  # hash and lines, to write a recipe
```

Both commands only read the two files. Updating the recipe file after drift
is a person's decision (the guide is maintained separately).

## Status probes

`GET /api/levi/setup/status` (the ordinary local authentication of LEVI's
API: the web UI token; a LEVI Agent credential is refused) answers with one
read-only sample of the machine. Every part has a `state`; a part that cannot
be read says `unknown` (with a `detail`) and never fails the rest.

| Part | Source | Never |
| --- | --- | --- |
| `ports` | the LISTEN rows of `/proc/net/tcp` and `tcp6` for 5000, 5001, 5100, 7470, 7860, 7861, 7880–7882, 8000, 8100 (plus the live service's policy, vLLM and judge ports), the owner from `/proc/<pid>/fd` (same user only; `owner_known: false` otherwise) and its short process name | connects to any port, LEVI's own included |
| `gpu` | `nvidia-smi --query-gpu` and `--query-compute-apps`; each process is named by the port it or a parent listens on, else `other` | creates a CUDA context |
| `gpu_locks` | `/proc/locks` matched to the lock files' device and inode (`LEVI_GPU_LOCK_FILE`, the live service's `gpu.lock_file`): `held` with the holder, `free`, `absent` | takes the lock |
| `live` | the live service's `status.json`: alive, vLLM asleep/awake, the GPU gate, the admission decision, `labelling_paused` | sends a request to vLLM or the live core |
| `host` | `/proc/loadavg`, `/proc/stat` (busy % between two samples), `/proc/meminfo` (memory, swap), `/proc/pressure/{cpu,memory,io}` | |
| `ros` | counts over the last hour in the newest 12 `ros2_control_node_*.log` (the last 256 KiB of each): `comm_violation`, `cartesian_reflex`, `motion_generator`, `reflex_other`, `overrun`, `overrun_warn`, `error` | runs ROS |
| `fr3_health` | the live service's `fr3.health_file`, read as the live page does (`ok`, `red`, `offline`, `missing`); `unknown` when none is configured | |
| `disks` | free space of the product workspace, the live workspace, its rollout roots and the temporary folder, by label | returns a path |
| `recorder` | the diagnostics recorder's `recorder.pid` (checked against the process start time) and `status.json` in `LEVI_FR3_RECORDER_DIR` (or its `data/`): `running`, `stopped`, `unknown` | starts or stops it |
| `guard` | the real-robot quiet guard: robot-side processes (`ros2_control_node`, `franka_server`, `run_robotiq_client`, `policy_server`, `serve_policy`, found by command name, the command line itself is never returned), pressure avg10 above 25 % CPU, 5 % memory, 50 % I/O or swap above 50 %, the live gate (`robot_quiet`), `gpu.quiet_states`, `online.pressure_avg10_max` and the product's running jobs. `quiet` is `true`, `false` or `null` (cannot tell). While the FR3 health file says the arm controller is active, product jobs, swap in use or a strained host raise a `banner` | |

The ROS log folder is `LEVI_ROS_LOG_DIR`, else `ROS_LOG_DIR`, else
`~/.ros/log`. The recorder folder is `LEVI_FR3_RECORDER_DIR` (unset: the
recorder is `unknown`).

**Sampling.** Nothing samples in the background. A request takes a sample;
another request within 2 seconds gets the same one (`cached: true`, `age_s`),
and concurrent requests wait for one sampler instead of starting their own.
The costly parts have longer intervals and caps: a full scan for socket
owners at most every 30 s (owners already known are rechecked cheaply), the
robot process scan every 5 s, the ROS logs every 5 s (a file is re-read only
when it changed), the disks every 10 s; at most 4096 processes and 4096 file
descriptors per process are scanned, and `nvidia-smi` has a 4 s timeout. The
response holds no command line, environment, token or path.
