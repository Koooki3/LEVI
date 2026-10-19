# Setup wizard: recipes and status probes

English | [中文](SETUP_WIZARD.zh-CN.md)

LEVI can guide a person through setting up a real-robot session: which
command comes next, what it touches, and whether the step it starts is
already healthy. This page covers the two parts that exist so far: the
**recipe registry**, which ties each step to the machine's own operator
guide, and the **read-only status probes** behind `GET /api/levi/setup/status`.
Neither starts, stops or connects to anything. The read-only **Rig console**
page builds on the probes; it is described at the end of "Status probes".

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
# context_sha256 = "..."  # optional: the whole section body, so changed prose around the block (a new warning) is drift too

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
  only loads a model: it may be `execute` if it carries
  `confirm = { required = true, decision = "<who allowed it, when>" }`.
  Nothing in LEVI runs it yet.
- Every policy server, whatever its `ui`, must write `touches.robot`,
  `moves` and `commands_robot` as `false` itself (left out counts as not
  declared and is refused), connect to nothing and listen on none of 5000,
  5001, 5100 and 7470.
- Any `execute` recipe has `touches.robot = false` and listens on no
  robot-side port (8000 only for the policy server).
- `moves = true` requires class 4; `commands_robot = true` requires class 3
  or 4.
- A recipe that connects to a robot-side port (5000, 5001, 5100, 7470, 8000)
  is only `copy` or `link`: LEVI never connects there.
- A recipe whose `requires` names an unknown or refused recipe, or one left
  out for this reason, is left out too, and so is every recipe on a
  `requires` cycle; the check names each one.

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

The guide is read as UTF-8 (a byte order mark is ignored) and split at
newlines only, so line numbers match an editor's. A section number is the
heading's leading number with at most three digits per part (`2.2`, `10`;
`## 2026 notes` is a title). Only code fences indented by at most three
spaces are blocks: a fence indented further (inside a list, for instance)
is prose and cannot be named by `block`.

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
| `fr3_health` | the live service's `fr3.health_file`, read as the live page does (`ok`, `red`, `offline`, `missing`); `unknown` when none is configured or it is not a regular file | |
| `disks` | free space of the product workspace, the live workspace, its rollout roots and the temporary folder, by label | returns a path |
| `recorder` | the diagnostics recorder's `recorder.pid` (checked against the process start time) and `status.json` in `LEVI_FR3_RECORDER_DIR` (or its `data/`): `running`, `stopped`, `unknown` | starts or stops it |
| `guard` | the real-robot quiet guard: robot-side processes (`ros2_control_node`, `franka_server`, `run_robotiq_client`, `policy_server`, `serve_policy`, found by command name, the command line itself is never returned), pressure avg10 above 25 % CPU, 5 % memory, 50 % I/O or swap above 50 %, the live gate (`robot_quiet`), `gpu.quiet_states`, `online.pressure_avg10_max` and the product's running jobs. `quiet` is `true`, `false` or `null` (cannot tell). While the FR3 health file says the arm controller is active, product jobs, swap in use or a strained host raise a `banner` | |

The ROS log folder is `LEVI_ROS_LOG_DIR`, else `ROS_LOG_DIR`, else
`~/.ros/log`. The recorder folder is `LEVI_FR3_RECORDER_DIR` (unset: the
recorder is `unknown`).

`context` says whether the configuration (the live workspace and its
`live.toml`) could be read; when it could not, the rest still answers from
`/proc` and the settings.

**Nothing can hang a request.** Every file is read only if `lstat` says it
is a regular file, opened with `O_NONBLOCK | O_NOFOLLOW` and checked again
with `fstat`, up to a size cap: a FIFO, device, socket or symbolic link is
`unknown` ("not a regular file") and never read. Each probe runs in its own
thread with a deadline (1.5 s; 2.5 s for the GPU, each `nvidia-smi` query is
killed after 2 s); one that misses it is `unknown` ("timed out"), and while
it is still stuck (a dead network mount, say) later samples answer
`unknown` for it at once instead of starting it again. Reading the
configuration has its own 1.5 s deadline.

**Sampling.** Nothing samples in the background. A request takes a sample;
another request within 2 seconds gets the same one (`cached: true`,
`age_s`). One request samples at a time and no lock is held meanwhile: a
request arriving during a sample waits for it at most 3.5 s, then gets the
previous sample (`stale: true`) or `{"state": "sampling"}`. The costly parts
have longer intervals and caps: a full scan for socket owners at most every
30 s (owners already known are rechecked cheaply, a new listener is looked
up at once), the robot process scan every 5 s, the ROS logs every 5 s (a file
is re-read only when it changed), the disks every 10 s, `nvidia-smi` every
10 s while the arm controller is active (`reused: true`). At most 4096
processes and 4096 file descriptors per process are scanned; a cut scan says
`truncated: true` (ports; and the guard, whose `quiet` is then `null` unless
a robot process was found). Only the last 256 KiB of a ROS log are read
(`truncated: true`: the counts are a floor). The response holds no command
line, environment, token or path; `live.gate.reason` is the live service's
own text, passed on (at most 200 characters).

### Rig console (read-only)

`GET /api/levi/rig/status` (the same local authentication as the route above)
answers one question for the **Rig console** page (`/rig`; it is not a header link: open it from the command palette, `Ctrl+K`, or with the go-to chord `G C`):
is any robot activity to be seen? The document has the schema
`levi.rig.status.v1` and four parts. As above, a part that cannot be read says
`unknown` with a `detail` and never fails the rest.

| Part | Source |
| --- | --- |
| `marker` | the file that setup.md says is touched while the arm controller runs: `lab/levi-hub/.arm-controller-active` of the main checkout (also when LEVI runs from a worktree; if no checkout with a `.git` folder is found, of this checkout). The optional second marker named by `LEVI_HUB_DIR` is not read. Presence and age (`age_s`, from the modification time) come from `lstat` alone and the file is never opened: a regular file is `present`, even if it could not be read; a symbolic link, directory or FIFO is `unknown` (with a `detail`). A file that is not there is `absent` only if the folder it would be in is there (checked with `stat`, so a folder that is a link to nothing is missing too); when that folder does not exist (a wrong checkout, a disk that is not mounted) the part is `unknown` ("the marker folder does not exist"), because a missing folder says nothing about the file. The marker is never cleared or touched |
| `eval_sessions` | the demo client's `<rollout root>/.eval_sessions/*.json` files (group, age, status), under the rollout roots of the live service's configuration. A session that has not ended (its status is not `fault`, `stopped` or `finished`, or it has none) is listed while it was updated within the last hour, an ended one within the last 10 minutes; the age comes from the file's `updated_at`, else its modification time. At most the 50 freshest are listed (a note says so; the part stays `ok`). `unknown` when no root is known, a root cannot be listed or is not there at all, or a recent file cannot be read as a session |
| `units` | our own `levi-aeri-*` systemd user units, from `systemctl --user list-units --type=service,scope --all` (so a policy scope started by hand as `levi-aeri-policy-manual-*` is seen, and so are inactive and failed units), with their active and sub states: running ones first, then those in a state this page does not classify, then the quiet ones, at most 50 in all; `unknown` when `systemctl` is missing, fails or does not answer in time, or a row of ours cannot be read |
| `robot_idle` | the advisory verdict below |

`robot_idle.state` merges those parts with the `guard` part of the setup
probes above:

- **`busy`** if any of these holds:
  - the guard names a robot-side process (`guard.robot` is not empty);
  - the FR3 health file says the arm controller is active
    (`guard.controller_active` is `true`, or any other value that is truthy);
  - the marker is present and younger than 12 h;
  - an evaluation session was updated within the last 60 s, whatever its
    status (an ended session counts too);
  - a `levi-aeri-*` unit is `active`, `activating`, `deactivating` or
    `reloading` (running, starting, stopping or reloading). `inactive` and
    `failed` units add nothing.
- otherwise **`unknown`** if any of these holds:
  - the marker, `eval_sessions` or `units` part is `unknown` (the marker
    also when it is not there and neither is its folder; `eval_sessions` also
    when a configured rollout root is not there at all, an unmounted disk;
    `units` also when a `systemctl` row of ours cannot be read);
  - the marker is present and 12 h old or more: nothing here tells a leftover
    of a crash from a very long run, so **the verdict is never `idle` while
    the marker file exists** (remove it by hand if the arm stack is down);
  - an evaluation session has not ended and was last updated more than 60 s
    ago (such a session is listed for up to 1 h): its client may have crashed
    or hung with the robot still moving;
  - a `levi-aeri-*` unit is in an `ActiveState` other than `active`,
    `activating`, `deactivating`, `reloading`, `inactive` and `failed` (for
    example `maintenance`);
  - `guard.state` is not `ok`;
  - `guard.quiet` is not `true` (that is `false` or `null`) while no robot
    process was found, because then the guard cannot tell "none exists" from
    "could not look";
  - `guard.controller_active` is neither `true`, `false` nor `null` and is
    falsy (for example `0` or an empty string): the file did not say "no";
  - the setup probes handed back an old sample (`stale`, or `age_s` over
    10 s), so an old quiet guard never reads as a quiet robot now. A sample
    whose `age_s` is missing or not a number counts as old.
- otherwise **`idle`**, shown as "no robot activity seen".

The 60 s and 12 h limits are decided on the exact ages; `age_s` is rounded to
0.1 s only in the answer. A session that has not ended and is shown as 60.0 s can
therefore be busy (59.96 s) or unknown (60.04 s), and a marker shown as 43200.0 s
can be younger than 12 h (busy) or 12 h old (unknown). The page, which sees
only the rounded age, can disagree with the verdict within those 0.05 s (a card
may show OK for a session the banner calls unknown). A session that has not
ended stops being listed one hour after its last update, the client's own
"crashed" rule (`eval_session.py`), so its "cannot tell" ends there and the
verdict can return to idle if nothing else shows activity.

`reasons` lists only the sources that are busy or unknown, busy ones first.
Each entry has `source` (`guard`, `marker`, `eval_sessions` or `units`),
`state` (`busy` or `unknown`), an English `text`, a stable `code` and `params`;
the list is empty when the state is `idle`. `text` is the server's own English
sentence, shown as written; it may be reworded. `code` is the stable name
of the case (`source.case`), and `params` is a flat object of strings and
numbers with the values the case needs (names, ages in seconds, rounded to
0.1 s; never a path), so a page can translate the case and fill in the values
itself. `code` and `params` were added without changing the schema string
`levi.rig.status.v1`: an older consumer still finds `source`, `state` and
`text`, and a consumer should treat the two new fields as optional. At most
five sessions of each kind, five units of each kind and five process names
are listed.

| `code` | Source, state | When | `params` |
| --- | --- | --- | --- |
| `marker.present` | `marker`, busy | the marker exists and is younger than 12 h | `age_s` |
| `marker.old` | `marker`, unknown | the marker exists and is 12 h old or more | `age_s` |
| `marker.unreadable` | `marker`, unknown | the part is `unknown` for any reason but a missing folder (a link, directory or FIFO in its place, an error, no known location) | none |
| `marker.no_folder` | `marker`, unknown | the marker is not there and neither is its folder | none |
| `sessions.recent` | `eval_sessions`, busy | a session was updated within the last 60 s | `group`, `age_s`, and `status` when the file has one |
| `sessions.silent` | `eval_sessions`, unknown | a session has not ended and was updated more than 60 s ago | `group`, `age_s`, and `status` when the file has one |
| `sessions.unreadable` | `eval_sessions`, unknown | the part is `unknown` (it can come with `sessions.recent` or `sessions.silent` entries for the sessions that were read) | none |
| `units.active` | `units`, busy | a unit is `active`, `activating`, `deactivating` or `reloading` | `name`, `active`, `sub` |
| `units.state_unknown` | `units`, unknown | a unit is in another state than those and than `inactive` and `failed` | `name`, `active` |
| `units.unreadable` | `units`, unknown | the part is `unknown` | none |
| `guard.robot_process` | `guard`, busy | the guard names a robot-side process | `names`: up to five distinct process names, sorted, joined with `, ` |
| `guard.controller_active` | `guard`, busy | `guard.controller_active` is `true` or another truthy value | none |
| `guard.controller_value` | `guard`, unknown | `guard.controller_active` is falsy and neither `false` nor `null` | none |
| `guard.old_sample` | `guard`, unknown | the setup probes sample is old (rule above) | none |
| `guard.truncated` | `guard`, unknown | no robot process was found, `quiet` is not `true` and the process scan was cut (`truncated`) | none |
| `guard.not_quiet` | `guard`, unknown | no robot process was found, the scan was not cut and `quiet` is `false` | none |
| `guard.unreadable` | `guard`, unknown | `guard.state` is not `ok` (and the sample is not an old one), or no robot process was found and `quiet` is `null` (or missing) without a cut scan | none |

**The verdict is advisory display only and gates nothing.** Nothing in LEVI
reads it to allow, refuse, start or stop anything, and the quiet guard and the
live service's gate work as before. "No robot activity seen" is not "safe": a
run LEVI has no file, process, session or unit to see is not seen. In
particular:

- The verdict does not use the lab's `arm_guard.py` (in `lab/levi-hub/` of the
  maintainer's workspace), which reads the kernel's socket tables for
  connections to the arm's address (the FCI command port, the UDP stream) and
  needs the arm's address from setup.md. A controller that was started without
  the marker file, without a running health monitor and without a process the
  guard recognises by name is not seen.
- An unmounted disk is seen only when the mountpoint folder itself is not
  there. An empty mountpoint folder reads as a root without sessions.
- Of many sessions only the 50 freshest are judged.

**Passive only.** The route never connects to a robot port, starts, stops or
writes anything, and never clears the marker. The only commands behind it are
read-only queries: `nvidia-smi` (through the setup probes) and `systemctl
--user list-units` for `levi-aeri-*` units. The rig parts read no process
directory of their own; the response holds no path. The `guard` part is the
setup probes' own (`probes.status()`, cached together with
`/api/levi/setup/status`), so a request to the rig route takes or reuses that
sample and inherits its sampling, including the process scan and the
socket-owner scan described above. Each rig part reads in its own thread with
a deadline. The results of `units` and `eval_sessions` are reused for 2
seconds; a request arriving while a part is being read waits for that read
(up to the part's deadline) and uses its result, and a part is declared stuck
(`unknown`, answered at once) only after its deadline has passed.

**Terminals A, B and C are not controlled.** The page is read-only. The arm
stack (A), the gripper stack (B) and the robot server (C) are started and
stopped by hand as setup.md describes. The terminal A and C cards show what the
FR3 health file and the kernel's socket table say; the terminal B card shows
`unknown` for the controller state, because the gripper stack has no status
file or port that the page may read.

**The page.** A banner shows the verdict, then one card per component: the
robot stack (Desk mode and FCI, terminal A arm stack, terminal B gripper stack,
terminal C robot server, terminal M health monitor, policy server, demo client)
and the services on this machine (live service, vision-language model, GPU). A
card shows a badge (OK, Fault, Stale, Not started or Unknown), a sentence, the
facts behind it and how that component is started (setup.md, AERI.md). Fault is
the health monitor's red light (the sentences of the cards that mirror the
monitor still say "red light"), a failed policy unit while no listening policy server is
seen (also when the port table cannot be read), or a fault reported by an evaluation session.

*Requests.* The page asks `/api/levi/setup/status` and `/api/levi/rig/status`
every 3 seconds while the tab is visible and not at all while it is hidden. A
request that has not answered within 6.5 seconds is given up and aborted, so one
that never answers cannot hold the polling; after a round in which both fail
the wait doubles, up to 30 seconds. An answer older than 10 seconds is dropped,
not shown as current, when a request fails or the tab becomes visible again;
the age its sample already had when it arrived (`age_s` of the setup status)
counts, so a cached sample that arrived 6 seconds old is dropped 4 seconds
later.

*An old sample.* When the setup status is an old sample (the server flags it
`stale`, or its `age_s` is over 10 seconds), a note says so and every card read
from it shows Stale and says how old the sample is (all cards except the
terminal B gripper card, which has no source, and the demo client card, which
is read from the rig status). What the sample said stays as the card's first
fact ("In that sample") and the readings stay below it, but none of it is the
card's verdict now: a red light in an old sample is not shown as Fault. A card
that had nothing to show stays Unknown. A setup status without `age_s` is not
called old by the page (the verdict above does call it old).

*The reasons.* The banner words each reason itself from its `code` and `params`,
in the page's language, so a Chinese page prints no English sentence for a
reason it knows. The server's English `text` is shown instead, as written,
when the page does not know the `code`, when the `params` lack a value
that the sentence needs, or when the reason has no `code` (an older server).
Reasons are listed busy first, then those that cannot tell, then any others, in
the server's order within each kind, at most 12 (the cap is applied after the
ordering). The note of a card for a marker, evaluation sessions or units part
that could not be read is worded the same way from `marker.unreadable`,
`marker.no_folder`, `sessions.unreadable` or `units.unreadable`; without one of
those codes the part's `detail` is shown as written.

*The banner.* It is downgraded to "cannot tell" when the marker, `eval_sessions`
or `units` part of the same answer is `unknown`, when the answer's schema is
present and is not `levi.rig.status.v1`, or when it says `idle` but still lists
reasons.

*The cards.* The demo client card takes the first of these that holds:
Fault when a session updated within the last 60 seconds reports a fault; OK when
a session that has not ended was updated within the last 60 seconds; Fault when a
session reported a fault earlier (it stays Fault, with the time of that session's
last update, for as long as the server lists the session: 10 minutes after its
last update); Stale when a session that has not ended was last updated more than
60 seconds ago; otherwise Not started (the listed sessions have all ended, or
there are none). It is Unknown when no session could be read, or when the list
may be incomplete and Not started would be the answer. A marker 12 hours old or
older is shown as "may be a leftover of a crash or belong to a very long run".
The health monitor turns the red light on by itself when the arm stack is not
up; when the only reasons are `hardware_interface_missing`,
`hardware_interface=`, `controller_manager_unreachable` or
`robot_state_no_data`, the robot reports no error and the hardware interface is
not reported active, the terminal A card says "not started" and notes that the
red light follows from terminal A not running. With the hardware interface
active, those reasons are a fault of the arm or its link and stay Fault,
whatever the controller flag says. Nothing on the page is an emergency stop or
a safety check, and a green card does not mean it is safe to approach the
robot.
