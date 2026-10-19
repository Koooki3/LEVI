# Setup wizard: recipes and status probes

English | [中文](SETUP_WIZARD.zh-CN.md)

LEVI can guide a person through setting up a real-robot session: which
command comes next, what it touches, and whether the step it starts is
already healthy. This page covers the two parts that exist so far: the
**recipe registry**, which ties each step to the machine's own operator
guide, and the **read-only status probes** behind `GET /api/levi/setup/status`.
Neither starts, stops or connects to anything. The **Rig console** page builds
on the probes; it is described at the end of "Status probes": the status route
is read-only, and the page's policy server panel starts and stops one policy
server unit and nothing else.

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

**Terminals A, B and C are not controlled.** The status route is read-only (the
policy server panel at the end of this page is the one place the rig page starts
anything, and only its own policy server unit). The arm stack (A), the gripper
stack (B) and the robot server (C) are started and stopped by hand as setup.md
describes. The terminal A and C cards show what the
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
or `units` part of the same answer is `unknown` (the note says a part could not
be read), when the answer's schema is present and is not `levi.rig.status.v1`
(the note says the format is not known), or when it says `idle` but still lists
reasons (the note says the answer lists reasons and so is not trusted; when a
part was also unread, the note about the unread part is the one shown). The
reasons stay listed in every case.

*The leftover-marker hint.* When the verdict is `busy`, every reason is the
arm-controller marker being there (code `marker.present`) and has been there for
at least 10 minutes (its `age_s`; a marker whose age the page cannot read gets no
hint), and the `fr3_health` part of the machine status is `missing`, or is
`offline` with an age (the health file stopped updating: the health monitor is
not running) in a sample that is not old, one extra line under the reason says:
if the arm stack is down, this is probably a marker left over from the last
session; remove it as the step "remove the marker after terminal A has exited" in
setup.md says. The 10 minutes are there because setup.md has the operator touch
the marker, wait 60 seconds and only then start the arm stack: for that time the
marker is there, the monitor is not running and no robot process exists, and the
hint would tell the operator to remove the marker they just placed. The line is
not shown when any other reason is listed (a robot process, an evaluation unit or
session, a marker that is 12 hours old or older or cannot be read), when the
health file is `ok` or red, when it is `offline` only because it cannot be read
(no age; the monitor may be running), when the status sample is old, or when the
machine status has no readable health part. The page only says so; nothing on it
removes the marker.

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
`hardware_interface=`, `controller_manager_unreachable`, `robot_state_no_data`
or `waiting_for_first_robot_state` (the monitor runs but no robot state has
reached it yet), the robot reports no error and the hardware interface is
not reported active, the terminal A card says "not started" and notes that the
red light follows from terminal A not running. A reason is told by its exact
name: `waiting_for_first_robot_state_extra` is another reason and keeps the
light red. With the hardware interface active, those reasons are a fault of the
arm or its link and stay Fault, whatever the controller flag says. When the
health file has stopped updating (Stale), the reasons it last wrote are listed
on the terminal M card only: the Desk and terminal A cards keep the server's
sentence about why it stopped and say "Same reasons as the Terminal M card."
instead of repeating them. With the red light the reasons stay on all three
cards. Nothing on the page is an emergency stop or a safety check, and a green
card does not mean it is safe to approach the robot.

### Rig console: policy server panel and client command panel

Besides `GET /api/levi/rig/status`, the rig page has two panels that prepare a
real-robot session, and routes behind them. The **policy server panel** starts,
shows and stops **one** policy server: what a person otherwise types by hand in
the policy server terminal (the openpi serving script with a checkpoint, a share
of the GPU memory and, optionally, RTC). The **client command panel** builds the
command for the demo client terminal (task instruction, steps, episodes,
`--min-z`, RTC horizon, rollout group, note), checked against the site's limits
and shown as **text to copy**.

On the page (`/rig`) they are the two cards of the last section, **Policy server
and demo client**, under the read-only status cards: the **Policy server** card
and the **Demo client command** card. Nothing else on the page does anything.

They use the same local authentication as the other `/api/levi` routes (the web
UI token; an agent's Bearer credential is refused), and both need the **rig site
file** first. The two writes (deploy and stop) are a person's only.

| Route | What it does |
| --- | --- |
| `GET /api/levi/rig/models` | the checkpoints the panel may deploy, each with its serving config, whether it takes a CFG scale, whether it is restricted to the known task instructions, the label `unverified`, its default rollout group, and `skipped`: why each other folder is not offered |
| `GET /api/levi/rig/site` | the site file without any path: connection, serials, home pose, limits, caps, the fixed client flags, GPU fractions, unit settings and the folders' last names |
| `POST /api/levi/rig/command/preview` | the demo client command for a form, validated and rendered as text to copy. Nothing is executed, started, connected to or written |
| `GET /api/levi/rig/deploy` | the policy server panel's state (`idle`, `starting`, `ready`, `failed`, `dead`, `unknown`, `foreign_listener` or `stopping`), derived afresh from the unit's state, the kernel's socket table, the unit's log and the deploy record; the checks a deploy would meet; the GPU memory the server holds |
| `POST /api/levi/rig/deploy` | starts one policy server as a transient `systemd --user` unit (person only) |
| `POST /api/levi/rig/deploy/stop` | stops the unit this panel started (person only) |
| `GET /api/levi/rig/deploy/log` | the tail of that unit's log, printable, with every path cut to its last name (`?lines=`, 1 to 200, default 80) |

#### What the panels never do

The panel starts and stops **only the policy server unit it created**
(`levi-aeri-policy-manual-rig-<model>`). It never starts the arm controller
(terminal A), the gripper stack (terminal B), the robot server (terminal C), the
health monitor or the demo client; those stay in real terminals as setup.md
describes, and the client command is only text. **Nothing on the page is an
emergency stop**, no control is labelled one, and a hand must stay on the
physical e-stop for every session. The Policy server card says: "This card only
starts and stops the policy server unit it created. It does not start the arm
stack, the gripper stack, the robot server or the demo client, and it is not an
emergency stop. Keep a hand on the physical e-stop for every session." (The
answer of `GET /api/levi/rig/deploy` carries a shorter `estop.text` to the same
effect; the page shows its own sentence.) The stop buttons are labelled **Stop
policy server**, never just "Stop".

It adds a second notice for the case the demo client cannot handle: the client
never homes or recovers the arm after its policy server fails, so if the server
fails or disappears while a session may be running, keep a hand on the e-stop.
The notice is shown when the state is `failed`, `dead`, `stopping`,
`foreign_listener` or `unknown` and the arm controller's marker file is present
or unknown, or an evaluation session has not ended or cannot be read (the page
cannot ask the robot whether it is homed). A stop that cut a connected client
off (forced with the extra phrase) is remembered in the deploy record, and the
notice then stays also in the `idle` state after it, until the marker file is
gone and no session is open; a reload of the page does not lose it.

#### The rig site file (`LEVI_RIG_SITE`)

The values that belong to one machine (the openpi checkout and its python, the
checkpoints folder, the rollout root, the lock files, the policy port, both
camera serials, the home pose, the safety limits, the GPU fractions, the CPU
settings of the policy server unit) are in a TOML file, never in the repository.
`LEVI_RIG_SITE` names it (`~` and `~/...` mean the home folder of the user LEVI
runs as; `~name` is never expanded, because it would reach another user's
folder, and is reported as not absolute; the path must be absolute); without
that setting it is `lab/rig/site.toml` of the main checkout
(also when LEVI runs from a worktree; `lab/` is git-ignored). The page reads it
and never writes it. It must be a regular file of the user LEVI runs as, not
writable by group or others (`chmod 644`), and at most 64 KiB; a link, folder or
FIFO is refused. LEVI reads it again only when its identity, modification or
change time, size, mode or owner changes. A file the parser cannot take (text
that is not TOML, nesting deeper than its stack, an integer longer than Python
converts) is `not_toml`, never an error of the route.

Without the file, every route above answers `412` with `code: site_missing`,
`looked_at` (the path it looked at; the one path that appears in an answer apart
from the command to copy) and `template`: a complete file with `/path/to/...`
placeholders to start from. A file that breaks the schema answers `412` with
`code: site_invalid` and `errors`, a list of `{field, code}` that never repeats a
value. The schema is closed: an unknown key is an error, and so is a missing one
(only `server.port` has a default, 8000). Codes: `missing`, `unknown_key`,
`type`, `range`, `not_absolute`, `not_normalized`, `unsafe_chars` (a path or name
with `$`, `%`, whitespace or a control character) and `inconsistent` (see the
rules after the table); for the file itself (`field: "file"`) `not_regular`,
`not_owner`, `writable`, `too_large`, `unreadable` and `not_toml`. Nothing on the
file system is checked when the file is read: whether a path exists is a check
of the deploy.

While the file is invalid, **every** route answers `412`, also the state of the
policy server card and its Stop: the page cannot stop a server it started. The
usual cause after an edit is `file: writable` (an editor saved the file with
mode 0664 under umask 002): run `chmod 644` on it. To stop the server before
that, in a terminal: `systemctl --user list-units 'levi-aeri-policy-*'` names the
unit (`levi-aeri-policy-manual-rig-<model>`), then `systemctl --user stop
<unit>`. The invalid-file notice on the page says the same.

Example values below are the ones of the template that ships with LEVI; every
path in it is a `/path/to/...` placeholder, so write this machine's own.

| Section and key | Meaning | Example |
| --- | --- | --- |
| (top) `schema` | the format | `"levi.rig.site.v1"` |
| `[paths]` `openpi_checkout` | the openpi checkout: the unit's working directory and the folder the client command starts in | `/path/to/openpi` |
| `python` | the checkout's own python, run directly (not through `uv run`) | `/path/to/openpi/.venv/bin/python` |
| `checkpoints_root` | the folder the model list reads | `/path/to/openpi/checkpoints` |
| `rollout_root` | the one folder the demo client writes episodes into (`--rollout-root`); no form can name another | `/path/to/rollouts` |
| `gpu_lock_file` | the physical GPU lock file (`.gpu.lock` of the ledger folder) | `/path/to/levi-hub/.gpu.lock` |
| `[live]` `unit` | the systemd user unit that owns the GPU lock while the live service's vision model server exists | `"levi-live.service"` |
| `[ros]` `path_prefixes` | `LD_LIBRARY_PATH` entries under these folders are removed from the policy server unit | `["/path/to/ros", "/path/to/franka_control"]` |
| `[server]` `port` | the policy port, used by the unit, the socket checks and the client (default 8000; 1024 to 65535, and never 5000, 5001, 5100 or 7470, which belong to the robot side and to LEVI: the client would be pointed at them) | `8000` |
| `remote_host`, `robot_server_url` | where the client finds the policy server and the robot server | `"127.0.0.1"`, `"http://127.0.0.1:5000/"` |
| `[cameras]` `view`, `wrist` | the two D405 serials (6 to 16 digits, different), always written into the client command | `"262622274256"`, `"262622275490"` |
| `[home]` `pose`, `duration_s` | the six-number home pose and how long the client takes to go there (1 to 30 seconds: a shorter time would make a large move fast) | `[0.481902, -0.011145, 0.235951, 3.119139, 0.008288, -0.883962]`, `3.0` |
| `[safety]` `min_z_floor`, `min_z_default` | the lowest `--min-z` (the form can only raise it) and its default; both above 0 and below the home height. The template's `0.025` is below the table height the client measured (0.04658 m; the client's own floors are 0.075 for the screws instruction and 0.05325 for the plates one), so the default form carries the `min_z_below_task_table` warning; whether to keep it, or to set `min_z_default` to the client's floor and keep `0.025` only as the floor, is the rig owner's decision | `0.025`, `0.025` |
| `max_xy`, `max_step_m`, `max_step_rad`, `rx_range`, `ry_range`, `rz_range` | the client's motion limits (a range is `[low, high]`); the file is refused when `max_xy` is not above 0 and at most 1.0, `max_step_m` above 0 and at most 0.2, or `max_step_rad` above 0 and at most 0.5 (the client's defaults are 0.85, 0.10 and 0.35) | `0.85`, `0.10`, `0.35`, `[1.50, 4.03]`, `[-0.74, 0.96]`, `[-2.44, 0.77]` |
| `binarize_gripper`, `require_matching_server`, `allow_unknown_prompt` | the client's three switches | `true`, `true`, `false` |
| `[caps]` `max_steps_default`, `max_steps_cap`, `eval_num_default`, `eval_num_cap` | steps per episode and number of episodes: default and the cap a form cannot pass | `500`, `3000`, `5`, `50` |
| `rtc_horizon_default`, `rtc_horizon_max`, `open_loop_default`, `open_loop_max` | the RTC execute horizon and, with RTC off, the open-loop horizon (each 1 to 10) | `6`, `8`, `6`, `10` |
| `prompt_max_chars`, `note_max_chars` | longest task instruction (at most 300) and note (at most 200) | `300`, `200` |
| `cfg_scale_default`, `cfg_scale_max` | the advantage-CFG guidance scale of a CFG model | `1.0`, `1.5` |
| `[gpu]` `fraction_presets` | the memory shares the panel offers: one to four values, 0.05 to 0.45 (about 15 GB, the owner's limit), ascending | `[0.22, 0.30, 0.45]` |
| `margin_mib`, `lock_hold_max_s` | free memory kept on top of the share (at least 1024); the longest the GPU lock is held while the server loads | `1024`, `600` |
| `[unit]` `nice`, `cpu_affinity`, `cpu_quota_percent` | CPU settings of the policy server unit: nice 0 to 19, a `taskset` core list (choose cores the arm controller does not use; the template's `8-15` is a guess for a host with no CPU isolation, to be confirmed by the rig owner before the file is installed), a quota in percent (0 = none) | `10`, `"8-15"`, `0` |
| `stop_timeout_s`, `start_timeout_s` | how long a stop may take before systemd kills the unit (10 to 600); how long a start may stay unfinished before it is `failed` (30 to 1800) | `90`, `600` |
| `[catalog]` `known_configs`, `cfg_configs` | the serving configs the panel accepts, and those of them that take a CFG scale | `["pi05_fr3_all_state", "pi05_fr3_all_state_cfg"]`, `["pi05_fr3_all_state_cfg"]` |
| `restricted_prefixes`, `verified`, `excluded` | model families that take only the client's two task instructions; checkpoints with a recorded real-GPU serving check; checkpoints never offered | `["pi05_fr3_sp"]`, `["pi05_fr3_all_step49999"]`, `["reset_r1_step20000"]` |
| `[prompts]` `default`, `known` | the default task instruction and the suggestions the form offers for the other models (1 to 32 entries) | `"pick the eggplant in the blue plate"` and four more |
| `[source]` `hash_files`, `manifest` | files of the openpi checkout whose sha256 is recorded at deploy (relative paths); a manifest file that must agree with them, `""` for none | five files under `scripts/` and `src/openpi/`, `""` |

Rules between keys (`inconsistent`): the two camera serials differ; `min_z_floor`
and `min_z_default` are below the home height and the default is not below the
floor; each default is not above its cap (steps, episodes, RTC horizon,
open-loop horizon, CFG scale); `cfg_configs` is a subset of `known_configs`.

**The arm lock is not a site value.** The lock an AERI run holds while it uses
the arm is the fixed file `/tmp/levi-aeri-fr3-arm.lock` of
`levi/automatic/fr3_run.py` (a test compares the two literals). It was a key of
the file once; one wrong letter in it would have switched the "the arm is in
use" check off without an error, so a key `arm_lock_file` is now an
`unknown_key`.

**The restricted instructions are not a site value either.** The client of a
two-task specialist (`pi05_fr3_sp...`) refuses every task instruction but the
two of its `TASKS` before it moves: `pick screws of same size into the empty slot
of screw box` and `stack the plates of same color together`. LEVI keeps a copy
(the client file lies in the openpi checkout, outside this repository), so the
page offers and accepts only those two for such a model, whatever `[prompts]
known` lists; `known` is the suggestion list for the other models.

#### The model list

A checkpoint is offered only when it is a JAX checkpoint with `params/` that the
policy discovery calls deployable, its role is `forward`, its serving config is
in `known_configs`, the site does not exclude it, its folder name is plain
(letters, digits, `.`, `_`, `-`) and at most 71 characters (the default rollout
group `<name>_rtc_demo` must fit the 80 the command form accepts: a longer name
would be offered with a default form that is refused), and a **top-level**
`norm_stats.json` is a regular file. The last rule matters: the policy server
reads only the norm stats shipped at the top of the checkpoint, and policy
discovery would also accept a file under `assets/`. Each folder that is left out
says why in `skipped`, the first rule that fails: `unsafe_name`,
`name_too_long`, `excluded_by_site`, `not_deployable`, `not_jax`,
`role_not_forward`, `config_unknown` or `no_toplevel_norm_stats`. The
time-reversed reset checkpoint is the usual example: it is not a forward policy
and ships its norm stats only under `assets/`, so it stays out (the site file
lists it under `excluded` besides). Root-level problems of the folder (for
example it is missing) are in `problems`.

Only the `cfg_configs` take `--cfg-guidance-scale`. The family `pi05_fr3_sp` (and
any `restricted_prefixes`) takes only the client's two task instructions. An option not
in `verified` carries the label `unverified`: no real-GPU serving check is
recorded for it, and a deploy of it needs `accept_unverified` as well as the
typed phrase. The folder is read once in 30 seconds.

#### The policy server panel

**On the page.** The Policy server card shows the state badge, a sentence for
the state, the facts of the deploy (model, serving config, RTC, fraction, unit,
connected clients, GPU memory), what the log says (the readiness lines above,
each marked as seen, not yet or not read), the GPU memory the server holds and the notice
that live annotation cannot start, the checks a deploy would meet, and the log of
the unit (the last 80 lines; opened by itself when the state is Failed or
Exited). The form has a model dropdown (a model without a recorded real-GPU check
is marked `unverified`), an RTC switch (on by default), a VRAM fraction dropdown
with the site's presets and, for each, the free memory it needs and whether it is
free now, and a guidance scale field for a CFG model (empty uses the default).
Checkpoints that are not offered are folded under "n checkpoint(s) not offered"
with the reason. **Deploy** opens a dialog with the model, config, RTC, fraction
and memory, the reminder that nothing on the page is an emergency stop, for an
unverified model a box to accept it, and the confirmation phrase to type; the
dialog's Deploy button stays off until the phrase is exact. The dialog's intro
also says: "Do not press Deploy while a server is being started by hand in a
terminal; the page can only see it once it listens." The Deploy button of the
card is off, with the reason beside it, while the state is not `idle`,
`failed` or `dead`, while a check blocks, while the chosen fraction needs more
memory than is free, and while the state cannot be read. **Stop policy server** appears only
when the server says the unit can be stopped; its dialog asks for the stop
phrase, and for the extra phrase too when a client is connected, and says how
long the stop may take (`stop_timeout_s` + 30 seconds). For a server started by
hand there is no Stop, only the instruction to press Ctrl+C in its terminal. The
page reads the state every 3 seconds while the tab is visible (not while it is
hidden; the wait doubles up to 30 seconds after a failure, and an answer older
than 10 seconds is not shown as current). A write that got no answer keeps its
request id, so pressing the same button again repeats the same request, which is
never carried out twice.

**What one deploy does.** `POST /api/levi/rig/deploy` takes `request_id`,
`model_id`, `rtc`, `fraction` (one of the site's presets), `cfg_guidance_scale`
(leave it null for a model without CFG; for a CFG model null means the site's
default, and the flag is always passed), `accept_unverified` and `confirm`, which
must be the phrase `deploy <model>`. The checks run in this order and the first
that fails refuses, with its code, before anything is started:

1. the request id, the phrase, the site file;
2. the deploy folder is usable (below);
3. a request sent again is answered from the stored answer (below);
4. one operation at a time (`busy`, 423, for the second);
5. the form (model in the list, fraction a preset, CFG scale in range, the
   unverified model accepted);
6. the checkpoint: its real path stays inside `checkpoints_root`, `params/` is a
   folder and a top-level `norm_stats.json` a regular file (`checkpoint_invalid`);
   the python is executable and the serving script exists
   (`runtime_files_missing`);
7. the source files: the sha256 of every file in `[source] hash_files` is read
   (`source_file_missing`); with a manifest, any difference refuses
   (`source_changed`); without one the hashes are only recorded in the deploy
   record and the panel notes `source_hash_unmanifested`;
8. the socket tables can be read (`net_table_unreadable`: never taken for a free
   port); nothing listens on the policy port, on IPv4 or IPv6 (`port_taken`; a
   server started by hand is never touched; the page's own running unit gives
   `already_running`); no connection to the port is established
   (`client_connected`);
9. no `levi-aeri-policy-*` unit exists **in any state** (`policy_unit_exists`;
   `activating` counts as a reservation; the page's own unit gives
   `already_running`; systemd unreadable gives `systemd_unreadable`), and no
   earlier start is unsettled (`start_unconfirmed`, 409): a start whose
   `systemd-run` did not answer in time, or whose core stopped before it ran,
   leaves the record at `intent`, and its unit may still appear. Until the
   record is 30 seconds old (the moment the state turns from `starting` to
   `failed`) another deploy, of whichever model, would put a second server on
   the port, so it is refused; the GPU lock of that start is kept meanwhile;
10. the arm lock is not held (`arm_lock_held`; read from the kernel's lock table,
    the file is never opened, so AERI's own lock attempt cannot be disturbed;
    unknowable gives `arm_lock_unknown`) and no standalone AERI run is
    registered alive (`aeri_run_live`; an unreadable registry refuses as
    `aeri_registry_unreadable`). The run registry is read when Deploy is
    pressed and again in the final checks (step 15), **not** on a status poll:
    it needs LEVI's key and every run's journal, which a page that polls every
    3 seconds should not read. The state's checks list therefore has no row for
    it; the arm lock row is what a poll shows (a run holds that lock while it
    uses the arm). LEVI's registry skips an entry whose signature does not
    verify, so with an unreadable key no run is seen at all, and the arm lock
    is the check that still holds;
11. the GPU lock: nobody holds it (the page will take it for the start), or only
    the live service's vision model server does (it keeps the lock while it
    exists, asleep included; the page leaves it alone and the memory check
    decides), or else `gpu_lock_foreign`;
12. the user manager's environment can be read (`env_unreadable`);
13. the free GPU memory (`nvidia-smi`, aggregate used and total only, exactly one
    line, else `gpu_unreadable`) is at least `ceil(fraction x total) +
    margin_mib` (`gpu_insufficient`; the numbers are below);
14. the **deploy record** is written (phase `intent`) and fsynced **before**
    `systemd-run` runs;
15. final checks, immediately before the start and under the GPU lock when the
    page took it: the socket table, the unit list, the arm lock, the run
    registry and the free memory are read again; any change refuses and the record is marked
    `aborted`;
16. the log file is created and `systemd-run --user` starts the unit; the record
    becomes `started`. If `systemd-run` fails, nothing runs and the answer is
    `start_failed` (502); if it does not answer in time the unit may have
    started: the record stays at `intent`, the request is kept so that sending it
    again starts nothing, and the state follows the unit.

**The memory rule.** On the 32607 MiB card with the 1024 MiB margin:

| Fraction | Reserved by the server | Free memory required |
| --- | --- | --- |
| 0.22 | 7174 MiB | **8198 MiB** |
| 0.30 | 9783 MiB | **10807 MiB** |
| 0.45 | 14674 MiB | **15698 MiB** |

The refusal names all of it ("Fraction 0.30 of the 32607 MiB card needs 10807 MiB
free (9783 + 1024); 5100 MiB are free. Put the live vLLM to sleep or stop it
(/live), then try again."). The fractions 0.30 and 0.45 need the live service's
vision model asleep or stopped. The panel lists the same numbers for each preset
before a deploy is tried.

The GPU lock is held only from step 15 until the server is resident (its state
leaves `starting`, three unreadable reads in a row, or `lock_hold_max_s`),
never for the server's lifetime. While the server runs, the panel shows how much
GPU memory it holds (`ceil(fraction x total)` of the recorded deploy, the share
it reserves, not a measurement; a question mark for a server it did not start)
and that live annotation cannot start.

**The unit.** `levi-aeri-policy-manual-rig-<slug>`, where the slug is the model id
in lower case with `_` as `-` (an id that does not reduce to that, or is longer
than 36 characters, is cut and carries 8 characters of a hash of the whole id).
It is a `levi-aeri-policy-*` unit, so the status route's units part and the
live service see it like any policy server of AERI. The unit runs, from the
openpi checkout, the checkout's python directly (not `uv run`):
`scripts/serve_policy_rtc.py` with RTC on, `scripts/serve_policy.py` with RTC
off, `--port` and (for a CFG model, always, with the default or given scale)
`--cfg-guidance-scale` before `policy:checkpoint`, then `--policy.config` and
`--policy.dir` with the absolute checkpoint folder. The policy server terminal
has no ROS and no conda, so neither has the unit: `AMENT_*`, `ROS_*`, `RMW_*`,
`COLCON_*`, `CMAKE_PREFIX_PATH`, `CONDA_*`, `VIRTUAL_ENV`, `PYTHONPATH` and
`PYTHONHOME` are unset, `LD_LIBRARY_PATH` loses the entries under the `[ros]`
prefixes, and `XLA_PYTHON_CLIENT_MEM_FRACTION` (the chosen fraction),
`PYTHONUNBUFFERED=1` and `no_proxy=localhost,127.0.0.1,::1` are set. It stops
with `KillSignal=SIGINT` (the Ctrl+C of the terminal) and `TimeoutStopSec` from
the site file, has no restart and no stop command, runs with the site's `Nice`,
`CPUAffinity` and optional `CPUQuota`, and writes stdout and stderr to its log.
The unit is started with `--collect`, so systemd forgets it as soon as it ends
and no exit status is kept: a failure is read from the log, and a crash that
logged nothing shows as `exited`.

**States.** Every `GET /api/levi/rig/deploy` derives the state afresh from the
unit's state (`systemctl --user`), the listener (the kernel's socket table) and
the deploy record; the answer is reused for 2 seconds and never fails because
one source cannot be read.

| State (badge on the page) | Meaning |
| --- | --- |
| `idle` (Idle) | no policy server of this panel, nothing listens on the port |
| `starting` (Starting) | the unit exists (or `systemd-run` is in flight, up to 30 seconds) and the log does not yet show everything it should; the readiness flags show how far it is |
| `ready` (Ready) | the unit is running, a socket listening on the port belongs to the unit, and the log shows every readiness line |
| `failed` (Failed) | a failure kind is shown: `xla_oom`, `argparse`, `address_in_use`, `traceback`, `rtc_not_ready` or `cfg_not_confirmed` (the server listens without its RTC or guidance-scale lines for 5 seconds), `start_timeout` (running longer than `start_timeout_s` without being ready), `exited` (gone before it listened), `start_failed`, or `intent_only` (the record was written and no unit started). A unit that still runs can be stopped; a failed deploy never blocks the next one |
| `dead` (Exited) | the server had logged that it was listening and then exited, stopped from outside the panel or crashed |
| `unknown` (Unknown) | the socket tables, systemd or the deploy record could not be read (`net_unreadable`, `systemd_unreadable`, `record_unreadable`), or the facts fit no state (`unclassified`). Nothing is assumed free or stopped |
| `foreign_listener` (Started by hand) | something that is not the panel's unit listens on the port: `hand_started` (a server started by hand), `listener_not_ours`, or `owner_unknown` (the unit's processes could not be read, so the listener is treated as not ours). The panel never touches it: press Ctrl+C in the terminal that runs it |
| `stopping` (Stopping) | a stop was requested and the unit has not gone yet, or the unit is shutting down without a stop from the panel (`stopped_elsewhere`). A unit that is still up with no stop going on (the core stopped between the mark and the stop command) can be stopped again |

A deploy is offered (`can_deploy`) in `idle`, `failed` and `dead` only when the
model-independent checks all pass: state folder, socket tables, port free, no
client, no policy unit, arm lock, GPU lock, GPU readable and room for
the smallest fraction (each is a row of `checks` with a level `ok`, `refuse` or
`unknown`; the run registry is read by the deploy itself, see step 10); the model
and fraction are checked again by the deploy itself. A stop is offered
(`can_stop`) when the state is `starting`, `ready`, a `failed` unit that still
runs, or `stopping` with the unit still up and no stop of this process going on. The answer also holds the readiness flags, the `failure`, the
`gpu` numbers, the `vram` need of each preset, `stop_blockers` (`client_connected`,
`foreign_listener`, `net_unreadable`), `live_annotation`, `estop`, `notes`,
`confirm` (the typed phrases) and `operation` (the deploy or stop in progress).
A failed or dead record stays visible until the next deploy replaces it.

Who owns a listening socket is decided from the processes of the panel's **own**
unit only (the unit's control group, then the socket links of those processes);
no one else's process directory is read.

**Readiness is passive.** The page never connects to the policy port, whatever
the state. A server is `ready` when its own log has said so and the port shows in
the socket table: `Using norm stats shipped with the checkpoint:` naming this
checkpoint's top-level `norm_stats.json`; for an RTC deploy `RTC enabled: ...`
and `RTC warm-up: plain N ms, rtc N ms` (a skipped warm-up does not count); for
a CFG model `Advantage-CFG policy, guidance scale w=...`; and `server listening
on 0.0.0.0:<port>`. **A server that did not log RTC readiness for an RTC deploy
is a failed deploy.** The first and last 256 KiB of the log are read, because a
long run's connection lines push the startup lines out of the tail. A message of
the log that signals running out of GPU memory (`RESOURCE_EXHAUSTED`, or an
`XlaRuntimeError` naming OOM or memory) is `xla_oom`; XLA's allocator warning "ran
out of memory trying to allocate ... this is not a failure" is not, and neither is
an out-of-memory error or a traceback of one request on a server that is already
`ready`. A command line error is `argparse`, `Address already in use`
`address_in_use`; a traceback before the server was ready is `traceback`. If
the wording of a library changes, a healthy server may stay `starting` and then
fail with `start_timeout`: the panel shows the log tail so a person can judge.

**Stopping.** `POST /api/levi/rig/deploy/stop` takes `request_id`, `confirm`
(`stop policy server`) and, only to force a stop under a connected client,
`client_override` (`stop with client connected`; any other non-empty value is
refused). It stops only the unit recorded at deploy time:

1. `systemctl --user show` must report that very `Id` and exactly the recorded
   command (`ExecStart`), else `unit_mismatch` and nothing is stopped; a unit
   that is already gone answers 200 with `already: true`;
2. a listener on the port that the unit does not own is
   `foreign_listener_not_stoppable` (so is a server started by hand when the
   panel has no record: press Ctrl+C in its terminal); with no record and no
   listener, `nothing_to_stop`;
3. **a stop never goes on while a client is connected**: a socket in state
   ESTABLISHED that the server accepted (the port is its local port) or that
   a client on this machine holds to the port on a loopback address refuses
   with `client_connected`. An outgoing connection of this machine to port
   8000 of some other host is not a client. The client does not home or
   recover the arm when its policy server goes, so cutting the server under it
   can leave the arm mid-motion. The check is made
   twice, 0.3 seconds apart, the second after the record is marked `stopping`
   and just before the stop command; if a connection shows only then, the record
   goes back to its earlier phase and the stop is refused. Only the typed
   `client_override` goes on, and the answer then carries a warning to keep a
   hand on the e-stop;
4. `systemctl --user stop <unit>`: the server gets SIGINT and, after
   `TimeoutStopSec`, systemd ends the unit's own processes. LEVI sends no signal
   itself and never stops anything by process id, port or name pattern;
5. the route waits up to `stop_timeout_s` + 30 seconds for the unit to be gone and
   the port free, then answers `idle` (`stopped: true`) or, if it is still
   going, `stopping`. A unit that is gone while the port is still held, or still
   deactivating after a second stop request, is `stop_incomplete`: run
   `systemctl --user stop <unit>` yourself.

A short-lived connection to the port (a health probe from another tool) also
blocks a stop until it ends.

**Confirmations.** A write is refused (`confirm_required`, 400) unless the typed
phrase matches exactly, naming the action and the model:

| Action | Phrase |
| --- | --- |
| deploy | `deploy <model id>` |
| stop | `stop policy server` |
| stop under a connected client (`client_override`) | `stop with client connected` |

**Requests.** Every write has a `request_id` (8 to 80 characters of `A-Z a-z 0-9
. _ : -`). The same id with the same body (the phrase is not compared) returns
the stored answer with `replayed: true` and never acts twice, also after LEVI
restarts; the same id with another body is `request_id_used` (409). A request
that was accepted but whose answer was never stored (LEVI stopped in between) is
answered `state: unknown`, `request_incomplete`, and nothing is started again. A
refusal is not stored: the same id may be sent again once its cause is gone.

**Refusals** (`detail` is `{code, message, ...}`; a malformed body is FastAPI's
own 422):

| Status | Codes |
| --- | --- |
| 400 | `request_id_invalid`, `confirm_required` |
| 403, 401 | `person_only` (a Bearer credential), `ui_token` (the web UI token is set and missing or wrong) |
| 412 | `site_missing`, `site_invalid`, `state_dir_unusable`, `checkpoint_invalid`, `runtime_files_missing`, `source_file_missing` |
| 422 | `model_unknown`, `fraction_not_allowed`, `cfg_scale_not_allowed`, `cfg_scale_out_of_range` |
| 423 | `busy` (another deploy or stop is running) |
| 409 | `unverified_not_accepted`, `request_id_used`, `source_changed`, `port_taken`, `already_running`, `client_connected`, `policy_unit_exists`, `start_unconfirmed`, `arm_lock_held`, `aeri_run_live`, `gpu_lock_foreign`, `gpu_insufficient`, `nothing_to_stop`, `foreign_listener_not_stoppable`, `unit_mismatch` |
| 503 | a source that cannot be read, so nothing is assumed: `net_table_unreadable`, `systemd_unreadable`, `arm_lock_unknown`, `aeri_registry_unreadable`, `env_unreadable`, `gpu_unreadable` |
| 502 | `start_failed`, `stop_failed`, `stop_incomplete` |

**Records and logs.** They are in a private folder of the product workspace,
`outputs/LEVI/workbench/rig/deploy/` (mode 0700, files 0600), never under a
rollout root or in source data; the folder is refused when it is a link or would
lie inside the rollout root, the checkpoints folder or the openpi checkout.
`record.json` is the one deploy the panel knows: unit name, request id, model,
config, RTC, fraction, CFG scale, the exact `systemd-run` and server command, log
file name, phase (`intent`, `started`, `start_failed`, `aborted`, `stopping`,
`stopped`), times, the memory numbers of the check, how the GPU lock was used,
the source hashes and, after a stop that cut a connected client off,
`stopped_with_client`. `requests/` holds one file per accepted request (the 200 newest
are kept). `logs/<unit>-<UTC time>.log` is one log per deploy, created by LEVI and
appended to by the unit (the 10 newest are kept, pruned at the next deploy; a long
run's log grows until then). `history.jsonl` is the audit, one line per accepted,
refused, started, stopped or lock-released event, with no path, token or key (256
KiB, one rotated copy). A route answers a log by its file name only; no route
takes a path.

#### The client command panel

On the page, the **Demo client command** card has the form (model, task
instruction with the site's known instructions as suggestions, or the two of the
model for a restricted one, maximum steps per episode, eval number, lowest
height, RTC and its horizon, rollout group, note; steps, eval number, lowest
height and the task instruction start at the site's defaults; an empty group
uses the default shown as its placeholder, and an empty note a default note that
names the model, RTC and the horizon), the list **Fixed by the
site file**, and a **Make the command** button. The command appears with a
**Copy** button and the sentence that the page has not run it; if the form
changes after the command was made, the command is marked stale and must be made
again. Below it the card says whether the policy server of the Policy server card
fits the command (same model and RTC mode, still starting, not ready, another
model, deployed without RTC, or started by hand and so unknown), and lists the
warnings under "Check before you run it".

`POST /api/levi/rig/command/preview` takes a closed form: `model_id` and `prompt`
(the task instruction), and optionally `max_steps`, `eval_num` (episodes),
`min_z`, `rtc`, `rtc_horizon`, `rollout_group` and `note`. A field outside the
form (`levi_mode`, `max_xy`, `allow_unknown_prompt`, ...) is refused; types are
strict. Defaults come from the site file: steps and episodes, `min_z_default`,
RTC on with the site's horizon (with RTC off, `rtc_horizon` is the open-loop
horizon), the rollout group `<model>_rtc_demo` (`<model>_demo` without RTC) and
the note `rig page: <model> rtc=on horizon=<n>`.

Bounds (a refusal is `422` with `{code, field, message}`): `max_steps` and
`eval_num` are 1 to the site's cap (`max_steps_out_of_range`,
`eval_num_out_of_range`); the horizon is 1 to the RTC maximum, or to the
open-loop maximum with RTC off (`rtc_horizon_out_of_range`); `min_z` is a finite
number above 0 (`min_z_not_positive`), not below the site floor
(`min_z_below_floor`) and below the home height (`min_z_above_home`); the
rollout group is a slug of letters, digits, `.`, `_`, `-`, at most 80, starting
with a letter or digit and without `..` (`rollout_group_invalid`).

The **task instruction** is one argv element and never passes through a shell. It
is refused when it is empty, longer than 300 characters (or the site's lower cap),
holds a quote, backslash, `$`, backtick or `!`, a control character (any
whitespace but a plain space too), or starts with a dash (`prompt_invalid` with a
`reason`: `empty`, `too_long`, `forbidden_char`, `control_char`, `leading_dash`);
a restricted model takes only the client's two instructions
(`prompt_unknown_for_model`). The note follows the same rules with its own cap
(`note_invalid`).

**Fixed flags are never form fields.** Everything that guards the arm comes from
the site file and no form can change it: the camera serials (both always
written), the policy server and robot server addresses, the home pose and its
duration, `--max-xy`, `--max-step-m`, `--max-step-rad`, the three angle ranges,
`--binarize-gripper`, `--require-matching-server`, `--allow-unknown-prompt` (shown
as `--no-allow-unknown-prompt` when the site says false) and the rollout root.
The unattended mode is never offered: `--levi-mode=off` is a constant, because
`on` starts without a person at the start gate. `--new` and `--no-step-by-step`
are constants too. `--min-z` is always written, explicitly.

**The command is text to copy and is never executed.** The answer
(`levi.rig.command.v1`) holds `argv`, `env`, `cwd` and `command`, a text of the
form `cd <openpi checkout> && env -u VIRTUAL_ENV no_proxy=... uv run --no-sync
--with pyrealsense2==2.55.1.6486 python examples/fr3_local/run_robotiq_client_collect_data.py ...`
with one group of flags per line and every token shell-quoted, to paste into the
demo client terminal. It is the form AERI uses (the openpi `.venv` has no
`pyrealsense2`). Every flag is one `--flag=value` token, except that the numbers
of `--home-pose` and the three ranges follow the flag as separate tokens (the
client's parser takes them that way) and a switch is `--flag` or `--no-flag`.
The command carries the openpi checkout and the rollout root as absolute paths
because a shell does not expand `~` after `=`; they are the only paths an answer
holds besides `looked_at`. Before it is returned the command is checked against
the schema again (known flags only, none twice, the right count of numbers, the
form's values valid again); a command that differs from what the site and the
form allow is never shown (`500 command_self_check`). The answer also holds:

- `server_requirements`: the model and config, and whether the policy server
  must have been deployed with RTC (a client with `--rtc` needs the RTC server);
- `warnings` (never refusals): `unverified_model`; `rtc_margin_low` when the
  execute horizon plus about 3 beats of latency passes the 10 actions of a chunk
  (horizon above 7); `min_z_below_task_table` when `min_z` is below the floor the
  client measured for the task (0.075 for the screws instruction, 0.05325 for the
  plates instruction and for every other one): an explicit `--min-z` beats the
  client's own table, so a lower value lowers the floor of that task;
- `hands`: keep a hand on the physical e-stop; this page never starts the arm
  stack or this client.

#### Limits

- **Not an emergency stop.** See above. The arm stack and the demo client stay in
  terminals; a hand stays on the physical e-stop.
- **One policy server on the port.** The port table, the policy unit list and the
  arm lock are machine-wide and are what really enforce it. Two LEVI cores (the
  product and a test worktree) have separate deploy folders, so their operation
  locks do not exclude each other.
- **Races that a passive check cannot close.** A client can connect between the
  second socket read and the SIGINT of a stop; a server started by hand can bind
  the port between the final check and the unit's own bind (the unit's bind then
  fails and the log says `address_in_use`). A server that is still loading in a
  terminal (python started, nothing listening yet) is invisible to every check,
  so the Deploy dialog says: "Do not press Deploy while a server is being
  started by hand in a terminal; the page can only see it once it listens."
- **Status route.** While the panel's unit runs, `GET /api/levi/rig/status`
  reports the robot as busy, as it does for any running `levi-aeri-*` unit.
- **The GPU lock** is used as described above and only then. The panel reads no
  per-process GPU data.
- **What only the machine can show.** The `systemd-run` properties, the user
  manager's real environment, the timing of the GPU memory and the exact log text
  of a server on this machine need one supervised run by a person; tests use
  fakes only and connect to no port. The first real deploy is done with the rig
  owner present and the robot idle (no client, no arm stack), after the owner has
  confirmed `cpu_affinity`; a property systemd rejects shows up as `start_failed`
  with systemd-run's own words, and the verified list goes into the site file.
- **The frontend samples.** `rig-deploy.samples.json` (parsed by the page's own
  readers in a test) is built from the backend on a fake machine;
  `tests/rig/test_rig_deploy_sample.py` fails when it differs, naming the
  command that rewrites it, `uv run --no-sync python
  tests/rig/regen_deploy_sample.py`.
