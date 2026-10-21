# The arm stack buttons of the marvel_workstation page

English first, Chinese after the line below. Backend: `levi/marvel0/stack_act.py` and `stack_act_api.py`
(on top of the read-only arm stack of `levi/marvel0/stack.py`), `stack_units.py`, `gripper.py`,
`control_site.py`, `stack_exec.sh`. Tests: `tests/marvel0/test_marvel0_stack_act*.py`.
Design record: `LEVI/lab/levi-hub/programs/rig-console/reviews/control-design-20261020.md`.

## What the buttons do

The page can start and stop the four members of the arm stack on the machine that runs the product LEVI:

| Member | Unit (`systemctl --user`) | Starting it | Ready means |
| --- | --- | --- | --- |
| M health monitor | `levi-fr3-health-monitor.service` | `fr3_health_monitor.sh` (read-only) | fresh health file |
| A arm controller | `levi-aeri-stack-marvel0-a.service` | `ros2 launch serl_franka_controllers_ros2 impedance.launch.py` (the arm does not move) | the control node got realtime priority 98, both spawners finished, and the health file shows the hardware active |
| B gripper stack | `levi-aeri-stack-marvel0-b.service` | `ros2 launch franka_gripper_manager robotiq_collect.launch.py` (full open and close, about 2 s) | the driver activated and all three gripper controllers are configured and active |
| C robot server | `levi-aeri-stack-marvel0-c.service` | `python3 -m fr3_robot_server.franka_server_ros2` (the impedance controller turns on after about 3 s, the arm may sink 1-2 mm) | impedance controller started, the server announces its port, a listener is on the port and the health file shows the controller active |

A unit being active proves nothing: A stays active after a reflex, B can half-start, a duplicate A exits 0. A member is
`ready` only when its log lines and the health file (or the socket table) agree. **The first time they agree, the page
latches it** (`ready_at` in the unit's record, so it survives a restart of the page): after that the log's start-up rules
are not read again (C prints "Impedance controller started" again on every `/startimp` and `/jointreset`, and Flask logs a
Traceback for any request that answers 500; neither makes a healthy server `failed`), and a health file that goes stale or a
port that loses its listener reads `unknown` (reason `health_lost`), never `failed` and never "did not become ready". Only a
process of A's or B's launch that dies later (`process has died`) still fails them: B has no other evidence. States: `idle`, `starting`, `ready`,
`failed`, `dead` (the unit vanished although the page started it), `unknown`, `foreign_running` (started elsewhere, or a
unit of that name runs another command: the page never stops those), `stopping`.

Beside each button the page shows why it is not available (`start_blockers`, from the backend, never guessed by the
page), and while a member starts, which ready lines were seen and which are missing.

- **Order.** B needs A **ready** and C needs A and B **ready**: running is not enough (a stack that failed while its
  sockets are still connected is "running" to the passive reads, and the checks before C stay good for 300 s). Blockers
  `a_not_ready` and `gripper_b_not_ready`; a gripper stack started by hand (`foreign_running`) does not unlock C.
- **Checks.** `a/preflight` makes one TCP connection to the arm's FCI port (1337) and asks whether a gripper stack runs
  already, **both at the same time**. **It is made only when A is positively absent**: no unit of A, A idle, the
  arm's sockets known and quiet (or lingering after a stop or a check of ours, see "The page's own sockets" below);
  anything that cannot be told refuses it. The connection is closed with a reset, not a FIN, so that it leaves no socket in
  TIME_WAIT behind (see below). **Whether a gripper stack runs is asked of the ROS graph first**: `ros2 node list --no-daemon
  --spin-time 2.0` under `timeout -s INT -k 2 8` (about 3 s; it starts its own node, waits for discovery and prints the
  fully qualified names, `ros2node/verb/list.py`). A node in the gripper stack's namespace `/robotiq` (the control node is
  `/robotiq/controller_manager`, `robotiq_collect.launch.py`) means one runs, even half-started. **"None" only counts
  when the health monitor's own node `/fr3_health_monitor` is in the list** (a graph that has not been discovered yet is empty
  too) **and the command ended well**; otherwise the graph proved nothing and the slow question is asked:
  `ros2 control list_controllers -c /robotiq/controller_manager`, which has no timeout of its own (with no manager it waits for
  ever and warns "Could not contact service" every 10 s), so it runs under `timeout -s INT -k 3 20` with systemd's
  `RuntimeMaxSec` behind it, and the verdict is read from what it printed, not from how it ended: the warning means no
  gripper stack, rows of `name type state` mean one runs, and a command ended before it could say is *no answer* (reason
  `b.probe_no_answer`, which keeps everything refused until it is asked again), never "absent". That takes about 20 s, and is
  only needed when the monitor is not in the graph. `checks.b_probe` says which way the answer came (`by`: `nodes` or
  `probe`; `graph`: what the graph said) and whether it is **`confirmed`**. **"None" from the graph alone is one answer, and one
  answer is not enough to start B**: a graph that listed the monitor but has not discovered the gripper stack's participants
  yet says the same, and a second gripper stack on the same RS-485 port is a motion risk. It is confirmed by a second answer: the
  answer before it, still good (10 minutes), was "absent" too, or the check makes a second graph check itself (so every check a
  person asks for is two graph checks, about 7 s when nothing fresh goes before it). A second answer that is "present" is final;
  one that proves nothing goes on to the slow question, and the controller manager's own word ("Could not contact service") and B's own stop need no second answer. **A's start still uses the
  first answer** (it needs B known, not confirmed), so the 5 s target before A holds: the guided start asks the check that A
  waits for with `confirm` off (one graph check), and, when the chain goes straight on to B, makes the second graph check
  **during A's start-up** (about 5 s, off the critical path); a start that stops at A or pauses before B makes it while it waits for
  the person. B's start is refused as a press that checks first (the warning `b_probe_unconfirmed`, 409 `b_probe_started`, nothing
  started) while the only answer is the unconfirmed one. `c/preflight` runs three read-only checks (QoS, the three gripper controllers, the
  end-effector frame); the listing of the controllers and the frame check do not depend on each other and **run at the same
  time**.
- **The check of B alone.** `b/probe` (the button "check the gripper stack") asks the same question and nothing else: it
  touches no FCI port, so it is allowed with A up as well as with A idle (not while a unit of B exists: then B is known), and
  `act.checks.can_probe_b` says when the button is there. It exists because "no gripper stack runs" is good for 10 minutes
  (`B_PROBE_TTL_S`) and, once A runs, the check before A (its only other source) is refused, so B could not be started any
  more. An answer older than that reads `unknown` with the reason `b.probe_stale` (`b.not_probed`: never asked;
  `b.probe_no_answer`: asked, not answered), never "absent", and the start blocker `b_probe_stale` says what to do. The
  check is also made **without a press**: at the start of a guided start whose answer A needs or that goes on to B (see
  "Guided start"), while a guided start waits for B (again after 5 minutes of an "absent" answer, after 1
  minute of anything else), and when the person presses B's start with no usable answer: that press makes the check, starts
  nothing and answers 409 `b_probe_started` (press again when the few seconds are over). A stop ends a running check.
  **It is an operation that runs beside the others** (`SIDE_OPS`): beside the FCI check, and the start of A does not wait for
  its end (`operation_running` is not raised for A by it; it does for B, C and the gripper test). Two checks of B never run at
  once.
- **The page's own sockets.** A socket to the arm outlives A by about a minute: the side that closes
  first holds the connection in TIME_WAIT, and `arm_guard` counts it as a control connection (any state, any port it does
  not know; 1337 is not a port it knows). **The FCI check no longer leaves one**: it closes with a reset (`SO_LINGER` 0),
  which skips TIME_WAIT (checked against the kernel's table in `test_marvel0_stack_act_fast_processes.py`, with a plain close as
  the control, which does leave one). Before, the check's own socket kept A from starting for a minute after it, which the
  old marker wait of 45-60 s happened to cover. What the arm's own FCI server makes of a reset rather than a close has not
  been seen. With no unit of A, A reads `idle` with the reason `a.sockets_draining` (not `foreign_running`, "stop it in
  the terminal it runs in") when the monitor's file is fresh, the arm's sockets are *known* to be active, and, within
  `DRAIN_S` (90 s): the page's own stop of A was the last thing done and the monitor does not show the hardware active
  (`hardware_active` null or false), or its own FCI check was, and the monitor shows **no controller manager**
  (`hardware_active` null). That is what the real monitor writes (`fr3_health_monitor.py:115-120` and `:349-359`: null when
  no manager answers, false only while a manager answers with the hardware not active, true while it runs), so a manager that
  answers after the check is somebody's A and the check's mark never covers it, and an A of somebody else that comes up
  after the page's stop (hardware true) is `foreign_running` and keeps M. The monitor can be stopped in the draining state;
  A's start still waits for quiet sockets (`gate.arm_sockets_clear`). What is left is the monitor's own delay: it keeps
  the last answer of A's manager for up to 5 s (`--service-stale-after`), so after a stop that was quicker than that the page
  may read `foreign_running` for the rest of it (a stop takes 10-11 s, and the answer is usually old by then).
- **Marker.** The page sets `.arm-controller-active` (exclusively, remembering its inode and time) **right after M**, so that
  its age runs while the checks are made. A starts when the marker is `marker_wait_s` old (control file: 2 to 600 s, default 3: the
  time a gate needs to see it) **and no heavy job of this computer really runs**; the page removes it once A's unit is gone
  and the arm's sockets are quiet. It used to wait a fixed 45-60 s whether a job ran or not. The jobs are those of the heavy-job gate
  (`levi-hub/heavy_gate.py`: pytest and bun runs; other programs on this computer do not honour the marker and are not looked
  at), read from **two sources whose union decides** (`heavy_jobs_running`, as `heavy_gate.py wait-clear` also uses its log and the
  scopes systemd lists). The first is the gate's log `heavy-gate.jsonl` in the hub: a job is a scope launched in the last 6
  hours with no terminal record whose scope was confirmed empty (a `queued` after an `interrupted`, or a `not_empty`, change
  nothing). The second is systemd: every loaded unit of the gate's naming (`heavy-gate-<kind>-<pid>-<n>-<hex>.scope`), whoever
  launched it and wherever its gate logged. A gate run with another `HEAVY_GATE_LOG` or `LEVI_HUB_DIR` still honours the fixed
  marker but never appears in the page's log; a job started more than 6 hours ago, or logged outside the last 4 MB that are
  read, would be missed by the log alone and A could start while it was still exiting. A job counts only **while its scope still
  holds a process** (`TasksCurrent` > 0; an inactive or failed scope that still has processes counts, as the gate counts it as
  not cleared). Both sources are answered by one `systemctl --user show --property=Id,LoadState,ActiveState,TasksCurrent
  'heavy-gate-*' <the log's candidates>` (about 7 ms; systemd 255 expands the glob itself). The stale scopes the gate leaves
  behind (loaded, active, `TasksCurrent=0`: 18 on this machine on 21 Oct, 9 of them probe scopes) are not jobs.
  **What cannot be told is never "no job"**: a log that is missing or unreadable, a systemd that does not answer (or a glob it
  did not expand), a scope without a task count, a job that ran without a scope, more than 64 candidates all read `unknown`, and then the fixed
  wait of old applies (at least 45 s; the document shows `heavy.state` `unknown` and the reason). A job that is seen
  decides over one that cannot be told. Two looks in a row must find no job (as `heavy_gate.py wait-clear` wants). A job
  yields within about 30-40 s of the marker (the gate stops its own scope); while one runs the end of the wait is not known
  (`remaining_s` null) and the document says how many (`act.sequence.wait`, `act.marker.heavy`); **after
  `HEAVY_WAIT_CAP_S` (90 s) the start ends** with `heavy_job_still_running` (the count in `reason_params`), nothing started, the
  marker given back. The reason for the wait is CPU jitter on the 1 kHz control loop (`setup.md` 2.2), not the arm's safety: the gates
  stop mistakes only. A start that ends before A (FCI closed, no answer about B, that cap, a cancel) gives back the marker it made
  (not one that was there before; not once A was started). A marker made by hand is adopted with
  one request; a marker the page did not make is never removed. The release loop (up to 90 s) lives in memory: a page that
  starts and finds the record of an A it stopped, with a marker that is still ours, finishes the release. That includes a
  record that still says `stopping` when A's unit is gone (the page died during the stop): it is closed as `stopped` first. A
  unit that is still there, or that systemd cannot be asked about, leaves the record and the marker as they are (the document
  names them; the marker button releases it once A is gone and the sockets are quiet).
- **Gripper test.** Two buttons, open (position 0.0) and close (0.7929, `max_effort 0.0`), through B's controller only;
  C is not needed. One `ros2 action send_goal` in a one-shot unit under `timeout -s INT -k 5 20`: SIGINT, not
  `timeout`'s default SIGTERM, because only SIGINT makes `send_goal` cancel its goal. The result (`reached_goal`,
  `stalled`) is kept as `gripper_test.last`; one that ran out of time says `timeout` when the cancel was seen, and
  `timeout_killed` ("the gripper may still be moving: keep hands out of the jaws") when a goal was sent and no cancel was
  seen; any end that is not a plain 0 or `timeout`'s 124 and printed no result (255, which is what `systemd-run --wait`
  is expected to give for a unit whose `timeout` had to SIGKILL its program, a traceback's 1, ...) is also
  `timeout_killed`. Stopping B ends a running test (its unit is stopped: SIGINT) and the result says `cancelled`; B is
  stopped only once that unit is gone (at most `GRIPPER_END_WAIT_S`, 10 s, then anyway), because `send_goal`'s SIGINT handler
  needs B's action server to answer the cancel, and a stop that lands before systemd has created the unit waits for it. Gates: B ready, no
  hosted run (or one that cannot be told), no client on C, the AERI arm lock free, hands clear ticked, one operation at a time.
- **Guided start ("start whole stack").** One request, `POST .../stack/sequence/start`, body (closed; an unknown field or a
  value of the wrong type is a 422): `request_id`, `confirm` (`start arm stack`, which the page's confirm window sends for
  the person), `attest`, and the options `through` (`"a"`, the default, or `"c"`), `pause_before_b` and `pause_before_c`
  (booleans, default `false`; they only matter for `"c"`). **Through `"a"`** (the body of old) runs M, the checks, the marker,
  the wait and A, then waits for the person to start B and then C with their own confirmations. **Through `"c"`** goes on by
  itself: B, then C, each by the single start's own code and gates (`_do_start`: every gate is asked again, under the
  machine-wide lock, at every step), with the confirmations the request carried. It lives in memory only and is never resumed
  after a restart of the service.
  - **The confirmations.** `attest` is one flat dictionary; the request is refused as a whole (400 `attest_missing`, naming
    the keys, nothing begun or set) when a stage the sequence will start itself lacks one: A `desk_done`, `estop_hand`;
    B `hands_clear`; C `estop_hand`, `hands_clear`, `no_client`. One tick serves every stage that asks for it. A part
    that is ready or starting when the button is pressed, and a part the request pauses before, asks for nothing; an
    unknown key is `attest_invalid`. Each unit's record keeps the keys of its own stage. `act.attest` lists the keys per
    stage; the hazard sentences are `act.terminals.{a,b,c}.hazard`. The gates stop mistakes only: the ticks are clicks that
    are recorded.
  - **The steps** (`act.sequence.steps`, `{step, terminal, state, at, took_s}`; the states are `pending`, `running`, `done`, `skipped`,
    `failed`, `cancelled`, `not_reached`): `monitor`, `marker`, `probe`, `wait`, `arm`, `await_a` (A's: the marker is set first and
    the check whether a gripper stack runs begins with it, as an operation of its own; `probe` is the FCI check and the wait for that
    answer; `wait` is what is left of the marker's age, the heavy jobs and the arm's sockets), then for
    `"c"`: `b_probe` (the check whether a gripper stack runs; made here only when the answer is stale or unconfirmed and was not
    refreshed during A's start-up, which a start that goes on to B does: the second graph check B needs), `b` (B's
    start **and** the wait for ready: the gripper calibrates), `c_preflight` (the three checks before C; **one automatic retry**
    when the controller listing got no answer, `controllers_state` `absent` or `unknown`; a check that answered no is not
    asked again), `c` (C's start and the wait for ready), then `done`. `skipped`: nothing was asked of the machine (the part was
    ready already, or the person did it by hand while the sequence waited). `not_reached`: the sequence ended before it.
    `act.sequence` also carries `through` and `pauses` (`{b, c}`, the effective ones: a start through `"a"` reports both
    `true`, because it waits for the person's B and C). **Where the time goes:** each step has `at` (when it last began, epoch s)
    and `took_s` (seconds it has taken in all; null for a step that never ran); the sequence has `took_s` (their sum, when it
    ended) and `elapsed_s` (while it runs). While the start is before A, `act.sequence.wait` says what it waits for:
    `{age_s, min_s, configured_s, remaining_s, heavy: {state, count, kinds}, sockets, holds, cap_s, waited_s}`, `holds` being
    `marker_young`, `heavy_jobs_running`, `heavy_jobs_unknown`, `arm_sockets` or `marker_absent`; `remaining_s` is null while a
    heavy job runs.
  - **A pause** (`pause_before_b`, `pause_before_c`): the sequence waits (`waiting_confirm`, `next` `b` or `c`) for the person's
    own start of that part, with a window and confirmations of their own; when it is ready the chain goes on by itself if the
    next stage is not paused. The ticks of the click are not good for ever: A must be started within `SEQUENCE_ARM_WINDOW_S`
    (300 s) plus the marker's wait of the click, B within `SEQUENCE_A_READY_S` + `SEQUENCE_B_READY_S` (300 s + 300 s) after
    that, C within `SEQUENCE_C_CHECK_S` + `SEQUENCE_C_READY_S` (240 s + 240 s) after that. A chain that is late for a stage
    ends (`cancelled`, `expired`) and starts nothing on the old ticks; the person starts that part themselves. A step that
    takes longer than `STEP_TIMEOUT_S` (600 s) ends it (`step_timeout`).
  - **It ends at the first step that fails**, with the reason in `act.sequence.reason`: the code of the gate that refused (the
    same codes as the single start's `start_blockers`), `b_not_seen` (the check of B got no answer), `b_failed`, `b_dead`
    (B did not become ready, or vanished), `c_preflight_failed`, `c_failed`, `c_dead`, `a_lost`, `b_lost` (a part before this
    one is not ready any more; a part that merely cannot be vouched for for a while is waited for), `attest_missing` (a part
    that was up at the request is gone and nothing was ticked for starting it). **What is already up stays up**: the document
    says what is up; the person fixes the cause and presses the button again, which **skips what is ready** (A that is up is
    not started, its checks, the marker and the wait are `skipped`; B and C that are up are passed) and answers 200
    `already_started` when A, B and C are all ready. The sequence never starts B without A ready or C without A and B ready.
  - **Cancel and stop win.** `sequence/cancel` and "stop whole stack" end it and it advances no more; nothing that already
    runs is stopped by either. Because the sequence reads the machine without the machine-wide lock and then starts something
    under it, every start, touch and check it makes **looks again, inside the lock, whether it is still the running
    sequence** (`Act._seq_locked`): a stop or a cancel that was answered in between is not undone (found by the review of the
    stop-all package for A; it holds for the monitor, the FCI check, the marker, the check of B, B, the checks before C and C).
    A stop of A while it waits for A to become ready ends it (`a_lost`): that step has no time window of its own and would
    otherwise hold `sequence_active` for up to 10 minutes.
- **Guided stop ("stop whole stack").** One request, `POST .../stack/sequence/stop` (body `request_id`, `confirm`, `attest`; the
  phrase is `stop whole stack`, which the page's confirm window sends for the person; `attest` ticks nothing and unknown keys are
  `attest_invalid`), stops C, then A, then B, and leaves M running (the marker's release already follows A's stop). Each step is
  the **single stop of that unit, by the same code and the same gates** (`stop_c_first`, `not_ours`, `unit_foreign`,
  `record_corrupt`, `systemd_unreadable`, `run_active`, the client rule of C): a unit with nothing of ours to stop is skipped, one
  that has is stopped, and the next step waits until the unit is gone. After a stop the question is only whether the unit is
  gone, not what the rest of the evidence says: the monitor's last health file and the arm's sockets outlast A by seconds and
  would read as a part that is not ours. It never bypasses a gate, starts nothing and sends no kill. The override phrase of C's
  own stop stays a typed single-stop feature and is **not** part of the sequence.
  It is refused up front (409, nothing is stopped) while a hosted run is known to be active (`run_active`), while a client is
  connected to C or the socket table cannot be read (`stop_all_client_connected`), when systemd or a record cannot be read
  (`systemd_unreadable`, `record_corrupt`) and when this instance is not the product; with no unit of ours it answers 200
  `already_stopped`. `act.checks.can_stop_all` and `act.stop_all_blockers` (rows `{code, level, text, params}`; `nothing_to_stop`
  and `sequence_active` are rows too) say the same beforehand. **The way out does not depend on the way in (RIG-04):** a run
  probe that raises or is missing, a stale health file, a check or the gripper test that is running and a missing control file do
  not block it.
  A stop that fails ends the sequence at that step, with the single stop's code as `act.sequence.reason` (`stop_failed`: the
  unit did not stop on SIGINT in its time; `not_ours`, `unit_foreign`: the page did not start it; `stop_left_processes`;
  `stop_c_first`; `run_active`; `stop_all_client_connected` when the client came after the press; `step_timeout`: the unit's
  stop time plus 90 s; `stop_c_unknown`: C is not the page's and whether it runs cannot be told, so "nothing to stop" would be
  a guess and A is not stopped under a C that may be up, while a stop of A or B alone goes ahead with the warning), the steps
  after it read `not_reached`, and nothing after it is touched. The person presses again: what is
  stopped is skipped (`skipped`), so a press can be repeated. `sequence/cancel` ends it (a stop already sent stays sent; nothing
  further is stopped). It ends a guided start that runs or waits (`superseded`), also when nothing was running, so that the start
  does not go on to start what was just stopped. Like the guided start it lives in memory only: a restart of the service ends it,
  and the next press skips what is already stopped.
  `act.sequence` carries `kind` (`start`, `stop`, null when idle) and `steps`; for a stop they are `{step: stop_c|stop_a|stop_b,
  terminal, state: pending|running|done|skipped|failed|cancelled|not_reached}`. A page words the end of a stop from them
  (`done`: stopped by this press; `skipped`: there was nothing of the page's), not from a fixed sentence. **While a guided
  stop is live nothing is started, set or tested** (`sequence_stopping`, a required blocker on the starts of A, B and C, on
  the gripper test's blockers and on `act.marker.touch_blockers`; the single stops stay open, the way out does not wait).
  A stop that was asked for and is carried by nobody (the page restarted in the middle of it, or the stop failed) leaves the
  notice `notice.stop_unfinished` (`params.unit`), built from the unit's record: the unit is still there, the person decides.
  **A stop of C or A is what the single stops are:** C's leaves
  the arm under impedance control until A is stopped, and A's closes the connection to the arm; neither locks the arm and neither
  is an emergency stop (the unit notes `c.stop_arm_still_held` and `a.stop_fci_dropped` say so, per unit, once it is so). Desk's
  Lock and Deactivate FCI, and the physical emergency stop, stay a person's.
- **Stop order:** client, C, A, B (optional), M last (the guided stop does the first four in one request). The page names only the unit it recorded, after `systemctl show`
  reports that very `Id` and command, and sends the unit's own stop signal (SIGINT, like Ctrl+C). It never kills.
  Stopping C leaves the impedance controller holding the arm until A stops; once C is stopped the document says so
  (`stop_note`: "the arm is still under impedance control"), and A's note ("the connection to the arm closed") comes only
  when A's unit is gone and the arm's sockets are quiet, never in the answer to the press. A stop with a client connected on
  C's port needs the extra phrase `stop robot server with client connected`. Stops work without the control file.
- **The way out does not wait.** A stop is not refused because a check, the gripper test or another stop is running: it
  ends the checks (their result is dropped, code `superseded`), stopping B also ends a running gripper test, and stops of
  different units run side by side (ordered by "stop C first"; the same stop twice is one). A run probe that cannot answer
  (nothing registered, or it raised) refuses a *start* but never a stop; a run known to be active still refuses a stop, and a
  client connected to C still needs the override phrase. A C that is the page's own unit and up keeps `stop_c_first` for A
  and B whatever the evidence says (a health file gone stale after M was restarted: `c.health_lost`; the control file gone:
  `c.no_specs`). Only a C the page cannot see (its socket table or its record unreadable) carries a warning
  (`stop_c_unknown`), not a refusal: C's own stop is refused for such a record, so a refusal would lock A and B for good.
- **A stop that failed.** `SendSIGKILL=no`: a unit that did not stop on SIGINT within 30 s is `failed` with its processes
  still running, still connected to the arm. Pressing stop again does **not** reset it while the control group still has
  processes (`TasksCurrent`, or the group's process list): it sends SIGINT once more (a second Ctrl+C; never a kill),
  keeps the unit named, writes the record as `stop_failed` and answers `stop_left_processes`; once they are gone the same
  press acknowledges it. What cannot be told counts as "processes left": no task count, a group that cannot be read and a main
  pid that is 0 or missing (a failed unit shows 0 with processes still in its group); only a unit with no control group at all
  has nothing left. If the arm must be freed meanwhile, the physical emergency stop is the way.

Each start needs: the control file switching the stack on, the exact phrase in `confirm` (`start arm stack`, `start gripper
stack`, `start robot server`; the page's confirm window sends it for the person once the boxes are ticked, a script sends it
itself; the only phrase a person still types is the override of a stop of C with a client connected, and the
deploy page's), the confirmations ticked (A: Desk done, hand on the e-stop; B: hands clear; C: hand on the e-stop,
hands clear, no client running), a fresh `request_id` (a repeated request is answered, never done twice), the
machine-wide stack lock, and every blocker clear. A request id is answered again from memory **and from the folder of
answered requests (`<workspace>/marvel0/stack/replay/`, the newest 64) after a restart of the page**, so a browser that
resends a gripper request never moves the gripper twice; the same id with another body is `request_id_used`.

**What the machine-wide lock covers.** The lock is held for the decision and the launch (the blockers, `systemd-run`, the
record), not for the slow part that follows (the stop's wait, a check, the gripper test): those run in a worker that is
single-flight inside the product, and a lock held that long would make a stop wait for a check, which the way out must
not do. Only one instance, the product LEVI, can act at all; the lock keeps a second instance's request from interleaving
with a decision.

**The product's workspace.** Writes act only in the product LEVI whose workspace is the main checkout's `.state` (design
record 2.12). A product unit started with another `LEVI_WORKSPACE` would write its records where no later instance looks,
so it only shows the stack (`gate.product`).

## How long the start takes, and every delay that was taken out

The owner asked (21 Oct) for the time before A to go from about 45 s to under 5 s, and for every similar deliberate delay of
the chain to be compressed. What the chain adds to what the machine takes, old and new:

| Delay | Old | New |
| --- | --- | --- |
| Marker wait before A | fixed `marker_wait_s`: 45-60 s (file floor 45, default 60) | marker age >= `marker_wait_s` (floor 2, default **3 s**) and no heavy job really running; 90 s cap; unknown: 45 s |
| Check whether a gripper stack runs (A's start needs the answer: rule R) | `list_controllers`, about 21 s when no stack runs, **before** the marker | ROS graph, about 3.4 s (estimated, see below), **beside** the marker's age and the FCI check; the 20 s question only when the graph proves nothing; the second graph check B needs is made **during A's start-up** |
| FCI check | 1 s, before the marker; its socket (TIME_WAIT, 60 s) kept A from starting for a minute | 1 s, beside the marker's age; reset close, no socket left |
| Ticker | one look every 2.0 s, one step per look (ten steps: 20 s and more of waiting) | a look 0.25 s after the last one **until A has a unit, then 1.0 s** (the 1 kHz loop is live), at once after any change (step moved, operation ended), woken by operations |
| Page's cached read | 2 s after any change | dropped at each change (`on_change` -> `stack_api.invalidate`); 2 s kept for idle reads of several pages |
| Checks before C | listing (1.2 s) then frame (5 s) one after the other | at the same time: 5 s |
| Refresh of an old answer about B (5-10 min old) between A and B | a check of its own there (20 s, now 3 s) | made during A's start-up, for a start that goes on to B |

Kept, because they are what the machine takes: the FCI check (1 s, off the critical path now), A ready about 5 s, B about 2 s
(the gripper calibrates), the end-effector frame check about 5 s, C about 3 s. Not changed here: the health monitor writes the
health file twice a second (`--hz 2`, `stack.py` starts it), so each "ready" that waits for the file is seen up to 0.5 s late;
the retry of the listing before C (a hiccup costs the listing's 20 s bound); the validity windows (`FCI_TTL_S`,
`B_PROBE_TTL_S`, `C_PREFLIGHT_TTL_S`: they say how long an answer is good, nothing waits for them); the 2 s polls of the
marker release after a stop (not the start chain). **The expected timeline** with the timings above (the timing test
`test_marvel0_stack_act_timeline.py` runs the real ticker on a clock it moves): A starts about 3.4 s after the press (the
check of the graph), is ready about 5 s later, B starts within one look and is ready 2 s later, the checks before C take
5 s, C starts and is ready 3 s later: about **20 s from the press to C ready** (about 100 s before; 18.9 s with 0.25 s looks
throughout, which cost some 29 fork/execs a second while the control loop ran). Before A the ticker adds at most 0.25 s (it sees a stage
ready at its next look); once A has a unit the pause is 1 s, so A, B and C are each seen up to 1 s late (C ready at 19.9 s and the
sequence done at 20.8 s in the timing test: A starts at 3.4 s, is ready at 8.5 s, B starts at 8.8 s, C at 16.8 s). **The control file of the machine must say
`marker_wait_s = 3` (or nothing)**: a file that says 45 keeps A back 45 s all the same.

**Cost of the looks.** One look is one read of the stack: six `systemctl show` and some small reads, about 40 ms on the machine
(measured 21 Oct, read-only), plus about 7 ms for the heavy jobs (one `systemctl show` of the gate's glob and the log's
candidate scopes, the log parsed only when it changes). **Before A has a unit** one look per 0.25 s plus the looks that follow a change
is about 3-4 per second, a tenth of one core (nothing is running yet that the looks could disturb). **Once A has a unit the control loop
is live and the pause is 1 s** (`SEQUENCE_TICK_ARM_S`): in the timing test 1.5 looks per second from A's start to the end (48 looks in
all, 93 with 0.25 s throughout), about 9 fork/execs a second instead of some 29 (the alternative, one `systemctl show` for all four
units, would have kept 12). The looks
that follow a change and the wake-up when an operation ends stay, so a finished check is acted on at once; what the slow pause
costs is at most one pause per stage that came up between two looks. The cores the control loop runs on are not reserved by any rule (this kernel is not real-time: it is a
few short processes per look, not a thread of its own, and only while a guided start or stop runs; waiting for the person looks
every 2 s). It has not been measured against the 1 kHz loop.

## What the gates are worth, and what nothing here is

**The gates stop mistakes. They do not stop another program of the same account**: anything that runs as this user can
send the same requests, forge the page's headers, edit the control file or the health file. The confirmations are clicks
that are recorded, not proofs. The real barriers are Franka Desk and the physical emergency stop. **Nothing on this page is
an emergency stop**: the stop buttons end a program, they do not stop the arm. Agent sessions never call the start or stop
routes (a rule, not a mechanism, on a shared account).

Writes act only inside the product LEVI (`levi-product.service` from the main checkout); a worktree or temporary instance
shows the stack and refuses every write (`not_product`).

## The control file

Optional TOML, `lab/marvel0/control.toml` in the main checkout, or the file named by `LEVI_MARVEL0_CONTROL`. **Missing means
off**: every start and the gripper test answer 403 `control_disabled`, the views say `enabled: false`, the rest of the page
works. The switches default to off; the person who owns the rig turns them on, with the owner present. The file must belong
to the user and not be writable by others.

```toml
schema = "levi.marvel0.control.v1"

[stack]
enabled            = false                      # true: the page may start and stop A, B, C and run the gripper test
franka_control     = "/path/to/franka_control"   # the checkout with env/fr3_env.sh, scripts/, robot_server/, tools/
robot_ip           = "10.42.0.157"               # the arm's address (Desk and FCI)
com_port           = "/dev/serial/by-id/usb-FTDI_USB_TO_RS-485_XXXXXXXX-if00-port0"
server_port        = 5000                        # the robot server's loopback port (not 5001, 5100, 7470, 8000)
reset_joint_target = [-0.8063, -0.0623, 0.9130, -2.4036, 0.0580, 2.3011, 1.5844]
marker_wait_s      = 3                           # 2 to 600: the least age of the marker before A starts

[client]
enabled            = false                      # true: the page may host the evaluation client
```

With `enabled = true` the four machine values are required; no machine value is written in this repository, and every
argv comes from this file and nowhere else.

## The units

All four are `systemd-run --user` transient units (the one-shot checks are `--pipe --wait --collect`, with `RuntimeMaxSec` as
the backstop behind the `timeout` inside their command; the check of the graph is `levi-aeri-stack-marvel0-oneshot-probe-b-nodes-<second>-<6 hex>`: the name is unique per call, not per second, so
two checks of one kind in the same second, such as the early check of B and a click on the FCI check, do not meet in `systemd-run`). A, B and C: `Type=exec`, `KillMode=control-group`, `KillSignal=SIGINT`,
`SendSIGKILL=no`, `TimeoutStopSec=30`, `Restart=no`, `StandardInput=null`, `ManagedOOMPreference=avoid`; A also
`LimitRTPRIO=99` and `LimitMEMLOCK=infinity`, B `LimitRTPRIO=99`; no `Nice`, CPU affinity or quota; no `--collect` (a
failed unit stays listed until the page resets it); the manager's ROS, conda and venv variables are unset and
`PYTHONUNBUFFERED=1`, `RCUTILS_COLORIZED_OUTPUT=0` are set. The command runs behind `levi/marvel0/stack_exec.sh <fr3_env.sh> --`,
which sources the rig's environment file and replaces itself with the command (its sha256 is pinned in `stack_units.py`).
Logs: `<workspace>/marvel0/stack/<a|b|c>/logs/` (folders 0700, files 0600), with a record of each start written before the
unit exists (and updated under the record's lock); the log of a unit that has been ready is cut in place to its last
512 KiB once it passes 8 MiB (systemd appends with `O_APPEND`; C's werkzeug writes a line per request). `ros2 launch` escalates a stop itself (SIGTERM after 5 s, SIGKILL after 10 s), which `SendSIGKILL=no` cannot
prevent: a normal stop of A takes about 10-11 s, and a unit still stopping after 15 s is flagged.

`ros2 launch` gets `--noninteractive`, written out: with a standard input that is `/dev/null` it is launch's default anyway
(`ros2launch/command/launch.py:77`), so the flag only keeps a changed `StandardInput=` from flipping it. In that mode
launch sends SIGINT on to its children even when it was interrupted itself (`launch/actions/execute_local.py:448-452`):
under `KillMode=control-group` a child of launch gets the unit's SIGINT **and** launch's, two where a terminal sends one.
`control-group` is kept on purpose (it also stops children that outlive a launch that died); whether two SIGINTs matter
for these nodes is for S4b, and the rehearsal counts them.

Not verified on the real machine: **the time of `ros2 node list --no-daemon --spin-time 2.0` in a unit** (about 3.4 s is an estimate:
the wrapper sourcing the rig's environment, python and rclpy starting, 2 s of discovery; if it is slower A waits for it, because A
needs B known), **that discovery of the monitor's node within 2 s means a gripper stack's nodes would have been seen too** (the
canary is a proof of discovery, not of completeness), **what the arm's FCI server makes of a reset instead of a close**, the real
timings of the chain, that **the second graph check (a python/rclpy process of a few seconds of CPU) while A starts up
does not disturb A's start-up or the control loop** (the early check of old ran before A existed), and the load of the looks on the 1 kHz loop. Verified on this machine without starting anything:
the format of `systemctl --user show` for several scopes and for the stale scopes (`heavy_jobs_running` ran against the real log and
systemd: `clear`, 18 stale scopes ignored, 7 ms per `show` of the glob), the kernel's table after a reset close. Not verified on the real machine either: the line patterns (they come from the franka_control sources, each with its source line
in `stack_units.py`, and a test greps those sources; no real unit log has been read), the format of `ros2 control
list_controllers` (read from `ros2controlcli/verb/list_controllers.py:34-36`, the source the installed egg-link runs;
never run), A, B and C in a unit at all, FIFO 98 and `mlockall` inside a unit, the double SIGINT, the 10-11 s stop,
`SendSIGKILL=no` against a real hang (what `TasksCurrent` shows for a failed unit that keeps processes), the exit status and
output `systemd-run --wait` gives after `RuntimeMaxSec` ended a command and its status (255, read from systemd's `run.c`)
when `timeout -k` had to kill its program, C's reaction to SIGINT, the real gripper result and the cancel on SIGINT, the FCI
probe, the health monitor's writes in the states above (read from its source, never measured), and a restart of the
product with the units running. B has no live evidence
after it is ready (no health source): a gripper driver that dies without a `process has died` line stays `ready`.

## What was run

- L1, a fake machine (`stack_act_machine.py`): every gate alone, the order of stops, the marker, the checks, the gripper
  test, the sequence, the guided stop (`test_marvel0_stack_act_stopall.py`; what it holds back and what it says when it
  cannot finish: `test_marvel0_stack_act_stopping.py`), the guided start through B and C (`test_marvel0_stack_act_chain.py`:
  the confirmations per stage, the pauses, every gate of B's and C's single start alone at its step, a failure at each step
  and a press again that skips what is ready, the retry of the listing, the windows, cancel at every step, "stop whole
  stack" taking its place, a restart; `test_marvel0_stack_act_race.py`: a stop or a cancel answered just before each start,
  touch or check of the sequence), the document. L2, real child processes of stand-in programs (`fake_stack/`; the guided
  stop in `test_marvel0_stack_act_stopall_processes.py`, the whole chain to C ready, a B that never becomes ready, a listing
  that needs the retry and a press again in `test_marvel0_stack_act_chain_processes.py`) behind a runner that maps
  `systemd-run`/`systemctl` to `Popen`: slow, never ready, crash, ignore SIGINT, duplicate and missing lines, the
  wrapper. L3, `tests/marvel0/rehearse_stack.py`, once on the development machine with the arm off: real `systemd --user`
  units named `rehearsal`, stand-in commands: the properties above were accepted, the program inside saw `rtprio 99` and
  an unlimited memory lock, the control group held only the stand-in, a SIGINT stop ended it, a child unit survived the
  stop of the unit that started it, `--pipe --wait` returned a one-shot's output, `RuntimeMaxSec` ended a hung one-shot,
  and nothing was left. It was **run again on 2026-10-21 (23:01, arm off, no robot process): 63 checks passed, none failed, nothing left**,
  after two things in the rehearsal itself were brought up to date (it ran as a worktree, so the product-only rule of the design record
  2.12 refused it; and it had no run probe). It now also measures the exit status of `systemd-run --wait` after `RuntimeMaxSec`
  (1, after 4.2 s; the output before the kill comes back), `timeout -s INT` inside a unit (the program that cancels: 124; the one that
  ignores SIGINT: 255 after 4.0 s), the SIGINTs a child gets under `KillMode=control-group` (2) and the task count of a unit whose stop timed out
  (`TasksCurrent` 1, a second SIGINT ignored by that program), refuses to run (exit 2) while a robot process is on the machine, and, for the fast
  start, runs the check of the graph through a real one-shot behind `timeout` (stand-in output) and `_scopes_tasks` on a real scope that
  holds a process, on one that is not there and on the same one after it ended.
- The fast start: `test_marvel0_stack_act_heavy.py` (the real `heavy_jobs_running` on log files in the gate's own shape and the real
  `systemctl show` outputs, among them the capture of `show 'heavy-gate-*'` with the machine's 18 stale scopes
  (`fixtures/systemctl_show_heavy_gate_glob.txt`), the second source for a job of another hub or older than the log window,
  unreadable logs, a log longer than the tail read, the document and the gates behind it), `test_marvel0_stack_act_fast.py` (the graph check and its fallback from the real `ros2 node list` format, the check of B
  as an operation beside others, the order, the marker given back, the checks before C side by side, the ticker, the page's
  cache, the step times, the control file's floor), `test_marvel0_stack_act_timeline.py` (the real ticker and threads on a
  clock the test moves, `sim_time.py`: A under 5 s, the whole chain, the pause once A has a unit, a heavy job that holds A back until it ends or the cap, the
  fixed wait when it cannot be told), `test_marvel0_sim_time.py` (the clock behind the timing tests: a thread that goes on after waiting for another goes on at the moment the other ended or blocked), `test_marvel0_stack_act_fast_review.py` (the unit name of a one-shot, a person's operation that holds the
  start back without the lock, the FCI answer a stop drops, and B started only on a confirmed absence: the first answer, the second answer during A's
  start-up, present as the final word, the manager's word, a stale first answer) and `test_marvel0_stack_act_fast_processes.py` (real processes: the stand-in of the graph
  check, two checks side by side, the whole chain on the real ticker, and the kernel's table after the FCI check).
- The mutation check (`tests/marvel0/mutate_stack_act.py`, 304 mutations, 97 of them for the fast start; `--check` says whether every mutation still applies)
  removes each gate in turn, including every gate added by the reviews of 2026-10-21 and their fixes; the 59 of the guided
  start through B and C, of the race and of the guided stop's new words each run against the one test file that guards them.
  A run of the whole list takes a few minutes of pytest: not while the arm controller is active (`MUTATE_TESTS` names one test
  file for a quick run; the tests run on cores `MUTATE_CPUS`, default 16-19).
- `test_marvel0_stack_act_walk.py`: random walks over the fake machine (most steps the next step of the bring-up, the rest
  anything: stops out of order, wrong answers of the checks, clock jumps, a silent monitor, an unreadable socket table or
  systemd, a unit that hangs on its stop, a restart of the page, a guided start through A or through B and C with or without
  pauses), after which the page's own buttons, "stop whole stack" among them, must be able to bring everything down, the
  marker included. It looks for a state the person cannot leave, which no list of cases does.
- Regenerate the sample the front end reads: `uv run --no-sync python tests/marvel0/regen_stack_act_sample.py`
  (`src/components/marvel0/__tests__/marvel0-stack-act.samples.json`).

---

# 机械臂栈按键

上文是英文，下文是中文。后端：`levi/marvel0/stack_act.py`、`stack_act_api.py`（建立在只读的 `levi/marvel0/stack.py` 之上）、
`stack_units.py`、`gripper.py`、`control_site.py`、`stack_exec.sh`。测试：`tests/marvel0/test_marvel0_stack_act*.py`。
设计记录：`LEVI/lab/levi-hub/programs/rig-console/reviews/control-design-20261020.md`。

## 按键做什么

页面可以启动和停止运行产品 LEVI 的这台机器上机械臂栈的四个成员：

| 成员 | 单元（`systemctl --user`） | 启动的是 | 就绪的含义 |
| --- | --- | --- | --- |
| M 健康监视器 | `levi-fr3-health-monitor.service` | `fr3_health_monitor.sh`（只读） | 健康文件是新的 |
| A 机械臂控制器 | `levi-aeri-stack-marvel0-a.service` | `ros2 launch serl_franka_controllers_ros2 impedance.launch.py`（机械臂不动） | 控制节点拿到实时优先级 98、两个 spawner 都结束、健康文件显示硬件已激活 |
| B 夹爪栈 | `levi-aeri-stack-marvel0-b.service` | `ros2 launch franka_gripper_manager robotiq_collect.launch.py`（全开全闭，约 2 秒） | 驱动已激活，三个夹爪控制器都已配置并处于 active |
| C 机器人服务器 | `levi-aeri-stack-marvel0-c.service` | `python3 -m fr3_robot_server.franka_server_ros2`（约 3 秒后阻抗控制器打开，机械臂可能下沉 1-2 mm） | 阻抗控制器已启动、服务器报出端口、端口上有监听、健康文件显示控制器 active |

单元处于 active 说明不了什么：反射之后 A 仍然 active，B 可能只启动了一半，重复的 A 以退出码 0 结束。只有日志里的就绪行和
健康文件（或套接字表）互相吻合，成员才算 `ready`。**第一次吻合时页面把它锁存下来**（单元记录里的 `ready_at`，所以页面重启后仍在）：
此后不再读日志的启动期规则（C 每次 `/startimp`、`/jointreset` 都会再打印一遍“Impedance controller started”，Flask 对返回 500 的
请求会记一段 Traceback，这些都不能让健康的服务器变成 `failed`）；健康文件过期或端口没有监听时显示 `unknown`（原因 `health_lost`），
绝不显示 `failed`，也绝不说“没有按时就绪”。只有 A、B 的 launch 里某个进程之后死掉（`process has died`）仍会让它们失败：B 没有别的
证据。状态词：`idle`、`starting`、`ready`、`failed`、`dead`（页面启动的单元不见了）、
`unknown`、`foreign_running`（在别处启动，或同名单元运行着别的命令：页面从不去停它们）、`stopping`。

每个按键旁边，页面显示它不可用的原因（`start_blockers`，由后端给出，页面不自己猜）；成员启动期间还显示已经看到哪几行就绪日志、
还缺哪几行。

- **顺序。** B 需要 A **就绪**，C 需要 A 和 B **就绪**：“在运行”不够（A 失败了但套接字还连着时，被动读取仍认为它在运行；C 之前的检查
  还要 300 秒有效）。阻塞码 `a_not_ready` 和 `gripper_b_not_ready`；手工启动的夹爪栈（`foreign_running`）不能解锁 C。
- **检查。** `a/preflight` 对机械臂的 FCI 端口（1337）做一次 TCP 连接，并询问是否已有夹爪栈在运行，**两件事同时做**。**只有 A 被
  确认不存在时才做**：没有 A 的单元、A 是 idle、机械臂的套接字已知且安静（或刚停 A、刚检查之后在排空，见下面“页面自己的套接字”）；
  任何读不出来的情况都拒绝。这个连接用复位（reset）而不是 FIN 关闭，所以不会留下 TIME_WAIT 套接字（见下）。**是否有夹爪栈在运行，先问 ROS
  图**：`ros2 node list --no-daemon --spin-time 2.0`，在 `timeout -s INT -k 2 8` 之下（约 3 秒；它自己起一个节点、等 2 秒发现、
  打印完整节点名，`ros2node/verb/list.py`）。夹爪栈的命名空间 `/robotiq` 里有任何节点（控制节点是 `/robotiq/controller_manager`，
  `robotiq_collect.launch.py`），就是有夹爪栈，哪怕只启动了一半。**“没有”只有在列表里有健康监视器自己的节点 `/fr3_health_monitor`
  （图还没发现完时列表也是空的）并且命令正常结束时才算数**；否则图什么也证明不了，再问慢的那一个：
  `ros2 control list_controllers -c /robotiq/controller_manager`，它自己没有超时（没有管理器时永远等下去，每 10 秒警告一次
  “Could not contact service”），所以在 `timeout -s INT -k 3 20` 之下运行，外面还有 systemd 的 `RuntimeMaxSec`，结论读的是它打印了什么，
  不是它怎么结束的：看到警告就是没有夹爪栈，看到 `名字 类型 状态` 的行就是有，还没来得及说话就被结束的是“没有回答”（原因
  `b.probe_no_answer`，在再次询问之前一切仍被拒绝），绝不当作“没有”。这要约 20 秒，只在监视器不在图里时才需要。`checks.b_probe` 说明答案
  怎么来的（`by`：`nodes` 或 `probe`；`graph`：图说了什么），以及是否 **`confirmed`**。**只由图给出的“没有”只是一次回答，一次回答不足以启动 B**：图里有监视器、
但还没发现夹爪栈的参与者时，说的也是“没有”，而同一个 RS-485 串口上的第二个夹爪栈会带来运动风险。要第二次回答来确认：它之前仍有效（10 分钟）的回答也是“没有”，
或者这次检查自己再做一次图检查（所以人要求的每次检查都是两次图检查，前面没有新鲜回答时约 7 秒）。第二次回答是“有”就是最终结果；什么也证明不了就去问慢的那一个；控制器管理器自己的话
（“Could not contact service”）和 B 自己的停止不需要第二次回答。**A 的启动仍用第一次回答**（它要 B 已知，不要求已确认），所以 A 之前的 5 秒目标不变：引导式启动让 A 等的那次检查
关掉 `confirm`（只做一次图检查），启动要直接接着去 B 时，**在 A 启动期间**（约 5 秒，不在关键路径上）做第二次图检查；停在 A 或在 B 之前暂停的启动，在等人的时候做。
只要唯一的回答是未确认的，B 的启动就只按“先检查”处理（警告 `b_probe_unconfirmed`，409 `b_probe_started`，什么也不启动）。`c/preflight` 运行三项只读检查（QoS、三个夹爪控制器、末端坐标系）；控制器列表和
  坐标系检查互不依赖，**同时运行**。
- **只查 B 的检查。** `b/probe`（按键“检查夹爪栈”）问同一个问题，别的什么都不做：不碰 FCI 端口，所以 A 在运行和 A 空闲时都可以做
  （已有 B 的单元时不做：那时 B 已知），`act.checks.can_probe_b` 说明按键何时出现。需要它，是因为“没有夹爪栈在运行”只有 10 分钟有效
  （`B_PROBE_TTL_S`），而 A 一旦在运行，A 之前的检查（它的另一个来源）就被拒绝，B 就再也启动不了。更旧的回答显示为 `unknown`，原因
  `b.probe_stale`（`b.not_probed`：从没问过；`b.probe_no_answer`：问了没有回答），绝不显示“没有”，启动阻塞项 `b_probe_stale` 说明怎么办。
  不按键也会重新检查：引导式启动一开始（A 需要这个答案，或启动要接着去 B，见“引导式启动”）、引导式启动等 B 的时候（“没有”的回答满 5 分钟后，
  其他回答满 1 分钟后），以及人在没有可用回答时按 B 的启动：这一下只做检查、什么也不启动，回答 409 `b_probe_started`（几秒钟后再按）。
  停止会结束正在进行的检查。**它是与其他操作并行的操作**（`SIDE_OPS`）：可与 FCI 检查同时进行，A 的启动不等它结束（它不会让 A 报
  `operation_running`；B、C 和夹爪测试仍会）。两次 B 检查不会同时进行。
- **页面自己的套接字。** 到机械臂的套接字比 A 多活约一分钟：先关闭的一方把连接留在 TIME_WAIT，`arm_guard` 把它算作控制连接
  （任何状态、任何它不认识的端口；1337 不是它认识的端口）。**FCI 检查现在不再留下套接字**：它用复位关闭（`SO_LINGER` 0），跳过
  TIME_WAIT（`test_marvel0_stack_act_fast_processes.py` 对着内核的表验证，对照组是会留下套接字的普通关闭）。以前检查自己的套接字会让 A 在检查之后
  一分钟内启动不了，被旧的 45-60 秒标记等待碰巧盖住。机械臂自己的 FCI 服务器怎么对待复位而不是关闭，没有见过。没有 A 的单元时，A 显示 `idle`、原因 `a.sockets_draining`（不是 `foreign_running`，“到它运行的终端里停”），
  条件是监视器的文件新鲜、机械臂的套接字*确实*读到是 active，并且在 `DRAIN_S`（90 秒）之内：最后做的是页面自己停 A 且监视器没有显示硬件
  active（`hardware_active` 为 null 或 false），或者是页面自己的 FCI 检查且监视器显示**没有控制器管理器**（`hardware_active` 为 null）。
  这正是真实监视器写的（`fr3_health_monitor.py:115-120`、`:349-359`：没有管理器回答时为 null，只有管理器回答而硬件没激活时才是 false，
  运行时为 true），所以检查之后有管理器回答，就是别人的 A，检查的标记不会盖住它；页面停 A 之后别人起的 A（硬件为 true）是
  `foreign_running`，并且 M 保留。排空状态下可以停监视器；A 的启动仍要等套接字安静（`gate.arm_sockets_clear`）。剩下的是监视器自己的
  延迟：它会把 A 的管理器的最后一次回答保留最多 5 秒（`--service-stale-after`），所以停得比这更快的话，页面在剩下的时间里可能读到
  `foreign_running`（正常的停止要 10-11 秒，到那时回答通常已经过期）。
- **标记文件。** 页面以独占方式创建 `.arm-controller-active`（记下 inode 和时间），**紧跟在 M 之后**，让它的年龄在做检查的同时走。A 要等标记满
  `marker_wait_s` 秒（控制文件：2 到 600 秒，默认 3：闸门看到它所需的时间）**并且本机没有重任务真的在运行**；A 的单元消失且机械臂的套接字安静后页面删除它。
  以前不管有没有任务都固定等 45-60 秒。这里的任务是重任务闸门（`levi-hub/heavy_gate.py`：pytest 和 bun 运行；本机其他程序不认标记，不去看它们）
  的任务，从**两个来源读出，以两者的并集为准**（`heavy_jobs_running`；`heavy_gate.py wait-clear` 同样用它的日志加 systemd 列出的 scope）。第一个是
  hub 里闸门的日志 `heavy-gate.jsonl`：最近 6 小时内启动的 scope，没有“已确认 scope 为空”的结束记录（`interrupted` 之后的 `queued`、
  `not_empty` 都不改变这一点）。第二个是 systemd：每个已加载、符合闸门命名（`heavy-gate-<种类>-<pid>-<序号>-<十六进制>.scope`）的单元，不论是谁启动、
  闸门把日志写在哪里。用别的 `HEAVY_GATE_LOG` 或 `LEVI_HUB_DIR` 运行的闸门同样遵守固定的标记，却不会出现在页面读的日志里；6 小时前启动的任务、或日志中不在
  读取的最后 4 MB 里的任务，只看日志会漏掉，A 就可能在它还在退出时启动。任务**只在它的 scope 里还有进程时**才算（`TasksCurrent` > 0；
  inactive 或 failed 但仍有进程的 scope 也算，闸门同样把它当作未清空）。两个来源由同一次 `systemctl --user show --property=Id,LoadState,ActiveState,TasksCurrent
  'heavy-gate-*' <日志里的候选>` 回答（约 7 毫秒；systemd 255 自己展开通配符）。闸门留下的陈旧 scope（已加载、active、`TasksCurrent=0`：
  2026-10-21 本机有 18 个，其中 9 个是探测 scope）不算任务。**读不出来的绝不当作“没有任务”**：
  日志缺失或读不了、systemd 不回答（或没有展开通配符）、scope 没有任务计数、没有 scope 的任务、候选超过 64 个，都是 `unknown`，这时用旧的固定等待（至少 45 秒；文档里
  `heavy.state` 为 `unknown` 并给出原因）。看到了在运行的任务，就以它为准，即使另一个读不出来。要连续两次看到没有任务（`heavy_gate.py wait-clear` 也这样要求）。
  任务会在标记设置后约 30-40 秒内让出（闸门停自己的 scope）；有任务在运行时等待何时结束是未知的（`remaining_s` 为 null），文档说明有几个
  （`act.sequence.wait`、`act.marker.heavy`）；**满 `HEAVY_WAIT_CAP_S`（90 秒）启动就结束**，原因 `heavy_job_still_running`（个数在
  `reason_params`），什么也没有启动，标记归还。等待的原因是 1 kHz 控制回路上的 CPU 抖动（`setup.md` 2.2），不是机械臂的安全：这些门只防误操作。在 A 之前就结束的启动（FCI 关闭、没有 B 的答案、这个上限、取消）会把自己设置的标记还回去（之前就在的
  标记不动；A 已经启动则不动）。手工创建的标记点一次“认领”；页面没有创建的标记从不删除。释放循环（最多 90 秒）只在内存里：页面重启时若发现自己停掉的 A 的记录、而标记仍是自己的，就把释放做完。记录仍写着 `stopping`
  而 A 的单元已经不在（停止期间页面死了）也算：先把它写成 `stopped`。单元还在、或 systemd 问不出来，记录和标记都原样不动（文档会点名；
  A 消失、套接字安静之后，标记按键可以释放它）。
- **夹爪测试。** 两个按键：开（位置 0.0）和关（0.7929，`max_effort 0.0`），只经 B 的控制器，不需要 C。在一次性单元里执行一次
  `ros2 action send_goal`，命令在 `timeout -s INT -k 5 20` 之下：用 SIGINT 而不是 `timeout` 默认的 SIGTERM，因为只有 SIGINT 会让
  `send_goal` 取消目标。结果（`reached_goal`、`stalled`）保存为 `gripper_test.last`；超时且看到了取消，结果是 `timeout`；发出了目标而没有看到
  取消，结果是 `timeout_killed`（“夹爪可能仍在动：爪口里不要伸手”）；凡是没有打印结果、结束方式又不是干净的 0 或 `timeout` 的 124 的，
  同样是 `timeout_killed`（255，即 `systemd-run --wait` 对 `timeout` 不得不 SIGKILL 其程序的单元预期给出的状态；或 traceback 的 1，等等）。
  停止 B 会结束正在进行的测试（停止它的一次性单元：SIGINT），结果写 `cancelled`；要等那个单元消失以后才停 B（最多 `GRIPPER_END_WAIT_S`，
  10 秒，之后照停），因为 `send_goal` 的 SIGINT 处理需要 B 的 action server 回答取消；停止若落在 systemd 创建该单元之前，就等它出现。门：B 就绪、没有托管的运行（或读不出来）、C 上没有客户端、AERI 机械臂锁空闲、已勾选爪口清空、同一时刻只做一件事。
- **引导式启动（“启动整个栈”）。** 一次请求 `POST .../stack/sequence/start`，请求体（封闭的：多出的字段、类型不对的值都是 422）：
  `request_id`、`confirm`（`start arm stack`，由页面的确认弹窗替人发送）、`attest`，以及选项 `through`（`"a"`，默认；或 `"c"`）、
  `pause_before_b`、`pause_before_c`（布尔，默认 `false`，只对 `"c"` 有意义）。**`through` 为 `"a"`**（旧请求体）依次完成 M、检查、标记、等待和 A，
  然后停下，等人用各自的确认依次启动 B 和 C。**`through` 为 `"c"`** 自己接着往下走：B，然后 C，每一个都经单独启动自己的代码和门
  （`_do_start`：每一步都在整机级的锁里把所有门再问一遍），用的是请求带来的确认。它只存在于内存里，服务重启后不会恢复。
  - **确认项。** `attest` 是一个扁平的字典；序列自己要启动的某一段缺了确认项，整个请求被拒绝（400 `attest_missing`，点名缺哪些键，
    什么都没开始、没设置）：A 要 `desk_done`、`estop_hand`；B 要 `hands_clear`；C 要 `estop_hand`、`hands_clear`、`no_client`。
    一个勾选满足所有要它的阶段。按键时已经就绪或正在启动的部分、请求选择在它之前暂停的部分，都不要确认项；未知的键是 `attest_invalid`。
    每个单元的记录只保存它自己那一段的键。`act.attest` 按阶段列出这些键；危险提示是 `act.terminals.{a,b,c}.hazard`。这些门只防误操作：
    勾选是被记录下来的点击。
  - **步骤**（`act.sequence.steps`，`{step, terminal, state, at, took_s}`；状态 `pending`、`running`、`done`、`skipped`、`failed`、`cancelled`、
    `not_reached`）：`monitor`、`marker`、`probe`、`wait`、`arm`、`await_a`（A 的：先设标记，查夹爪栈的检查随标记一起作为独立的操作开始；`probe` 是 FCI 检查和等那个答案；
    `wait` 是标记年龄、重任务和机械臂套接字还剩的部分），`"c"` 再加：`b_probe`（检查有没有夹爪栈在运行；这里只在答案过期或未确认、且没有在 A 启动期间刷新时才做，
    接着去 B 的启动会在 A 启动期间刷新：B 需要的第二次图检查）、`b`（B 的启动**以及**等它就绪：夹爪校准）、`c_preflight`（C 之前的三项检查；控制器列表没有回答时
    （`controllers_state` 为 `absent` 或 `unknown`）**自动重试一次**；回答了“不行”的检查不再重问）、`c`（C 的启动和等它就绪），最后 `done`。
    `skipped`：没有向机器要求任何东西（该部分本来就已就绪，或者序列等待时人已经手工做了）。`not_reached`：序列在它之前已经结束。
    `act.sequence` 还带 `through` 和 `pauses`（`{b, c}`，是生效的值：`"a"` 的启动两个都报 `true`，因为它等人去启动 B 和 C）。**时间花在哪里：**
    每一步有 `at`（最近一次开始的时刻，epoch 秒）和 `took_s`（这一步一共用了多少秒；从没跑过的为 null）；序列有 `took_s`（各步之和，结束时）和 `elapsed_s`（运行时）。
    启动还在 A 之前时，`act.sequence.wait` 说明它在等什么：`{age_s, min_s, configured_s, remaining_s, heavy: {state, count, kinds}, sockets, holds, cap_s, waited_s}`，
    `holds` 取值 `marker_young`、`heavy_jobs_running`、`heavy_jobs_unknown`、`arm_sockets`、`marker_absent`；有重任务在运行时 `remaining_s` 为 null。
  - **暂停**（`pause_before_b`、`pause_before_c`）：序列等待（`waiting_confirm`，`next` 为 `b` 或 `c`）人自己启动该部分，用人自己的窗口和确认；
    它就绪之后，如果下一段没有暂停，链自己接着走。按键时的勾选不是永远有效的：A 必须在 `SEQUENCE_ARM_WINDOW_S`（300 秒）加标记等待时间之内启动，
    B 在那之后的 `SEQUENCE_A_READY_S` + `SEQUENCE_B_READY_S`（300 + 300 秒）之内，C 在再之后的 `SEQUENCE_C_CHECK_S` + `SEQUENCE_C_READY_S`
    （240 + 240 秒）之内。某一段来得太晚，序列结束（`cancelled`，`expired`），不会用旧的勾选去启动；人自己去启动那一部分。
    某一步超过 `STEP_TIMEOUT_S`（600 秒）也会结束它（`step_timeout`）。
  - **在第一个失败的步骤结束**，原因在 `act.sequence.reason`：拒绝的那道门的代码（和单独启动的 `start_blockers` 同样的代码）、`b_not_seen`
    （B 的检查没有回答）、`b_failed`、`b_dead`（B 没有就绪，或不见了）、`c_preflight_failed`、`c_failed`、`c_dead`、`a_lost`、`b_lost`（这一段之前的部分不再就绪；
    只是暂时说不清的部分会等）、`attest_missing`（按键时已经在运行的部分后来不见了，而没有为启动它勾选过）。**已经起来的保持原样**：文档说明什么在运行；
    人排除原因后再按一次，**已经就绪的会被跳过**（A 已在运行就不再启动，它的检查、标记、等待记为 `skipped`；已就绪的 B、C 直接通过），
    A、B、C 都就绪时回答 200 `already_started`。序列从不在 A 没就绪时启动 B，也不在 A、B 没都就绪时启动 C。
  - **取消和停止优先。** `sequence/cancel` 和“停止整个栈”会结束它，它不再往前走；两者都不会停掉已经在运行的东西。序列读机器时不持整机级的锁，
    随后才在锁里启动东西，所以它做的每一次启动、设置和检查都会**在锁里再看一遍自己是不是还在运行的那个序列**（`Act._seq_locked`）：
    中间已经回答了的停止或取消不会被撤销（停止整个栈那一包的审查在 A 上发现了这一点；监视器、FCI 检查、标记、B 的检查、B、C 之前的检查和 C 都一样）。
    它等 A 就绪的时候有人停 A，序列随之结束（`a_lost`）：那一步没有自己的时间窗，否则会让 `sequence_active` 最多挂 10 分钟。
- **引导式停止（“停止整个栈”）。** 一次请求 `POST .../stack/sequence/stop`（请求体 `request_id`、`confirm`、`attest`；短语是
  `stop whole stack`，由页面的确认弹窗替人发送；`attest` 不需要勾选，未知的键返回 `attest_invalid`），依次停 C、A、B，M 继续运行
  （标记的释放本来就跟在 A 的停止之后）。每一步就是**该单元的单独停止，同样的代码、同样的门**（`stop_c_first`、`not_ours`、`unit_foreign`、
  `record_corrupt`、`systemd_unreadable`、`run_active`、C 的客户端规则）：没有我们的东西要停的单元直接跳过，有的就停，下一步要等该单元消失以后才开始。
  停止之后只问单元是否已经消失，不再看其他证据：监视器最后一份健康文件和机械臂的套接字会比 A 多存在几秒，会被读成“不是我们的”。
  它不绕过任何一道门，不启动任何东西，不发任何强杀。C 单独停止时的覆盖短语仍是需要手输的单独停止功能，**不属于**这个序列。
  下面这些情况在一开始就被拒绝（409，什么都不停）：已知有托管的运行在进行（`run_active`）、C 上有客户端连接或套接字表读不出来
  （`stop_all_client_connected`）、systemd 或某条记录读不出来（`systemd_unreadable`、`record_corrupt`）、本实例不是产品；没有我们的单元时回答 200
  `already_stopped`。`act.checks.can_stop_all` 和 `act.stop_all_blockers`（行为 `{code, level, text, params}`；`nothing_to_stop`、`sequence_active` 也是行）
  事先说明同样的事。**出路不依赖入口（RIG-04）：** 运行探针抛异常或没注册、健康文件过期、正在进行的检查或夹爪测试、控制文件不见了，
  都不会阻止它。
  某一步停止失败，序列就在该步结束，`act.sequence.reason` 是该单独停止的代码（`stop_failed`：单元在时限内没有响应 SIGINT；`not_ours`、`unit_foreign`：
  不是页面启动的；`stop_left_processes`；`stop_c_first`；`run_active`；按键之后才来的客户端是 `stop_all_client_connected`；`step_timeout`：该单元的停止时限加 90 秒；
  `stop_c_unknown`：C 不是页面的而且说不清它是否在运行，“没有东西要停”只是猜测，所以不在可能还在运行的 C 之下停 A；单独停 A 或 B 带着警告照做），
  它之后的步骤记为 `not_reached`，不会碰。再按一次：已经停掉的被跳过（`skipped`），所以这个按键可以重复按。`sequence/cancel` 结束它（已经发出的停止不会撤回，
  之后不再停任何东西）。它会结束正在运行或等待中的引导式启动（`superseded`），没有东西在运行时也一样，免得那个启动接着去启动刚停掉的东西。和引导式启动一样，它只存在于
  内存里：服务重启就结束了，下一次按键跳过已经停掉的。
  `act.sequence` 带 `kind`（`start`、`stop`，空闲时为 null）和 `steps`；停止序列的是 `{step: stop_c|stop_a|stop_b, terminal, state: pending|running|done|skipped|failed|cancelled|not_reached}`。
  页面按它们来写停止的结果（`done`：本次按键停掉的；`skipped`：本来就没有页面的东西），不用一句固定的话。**引导式停止在进行时，什么都不启动、不设置、不测试**
  （`sequence_stopping`：A、B、C 启动、夹爪测试的阻塞项和 `act.marker.touch_blockers` 里的必选项；单独的停止仍然开放，出路不等人）。
  停止被要求过却没有人在执行（页面在中途重启了，或者停止失败）时，留下通知 `notice.stop_unfinished`（`params.unit`），由该单元的记录得出：单元还在，由人决定。
  **停 C 和停 A 就是单独停止的样子：** 停 C 之后阻抗控制器仍然握住机械臂，直到 A 停止；停 A 则断开与机械臂的连接；两者都不会锁住机械臂，
  也都不是急停（单元的说明 `c.stop_arm_still_held` 和 `a.stop_fci_dropped` 在事实成立之后逐个单元写明）。Desk 里的 Lock 和 Deactivate FCI，以及物理急停，仍归人。
- **停止顺序：** 客户端、C、A、B（可选）、最后 M（引导式停止在一次请求里做前四项）。页面只认自己记录的单元，且要 `systemctl show` 报出的 `Id` 和命令与记录完全一致，
  然后发出该单元自己的停止信号（SIGINT，相当于 Ctrl+C）。它从不强杀。停止 C 之后阻抗控制器仍然握住机械臂，直到 A 停止；
  C 真的停了之后文档里才写明“机械臂仍在阻抗控制下”（`stop_note`），A 的说明（“与机械臂的连接已断开”）只在 A 的单元消失且机械臂的
  套接字安静之后才出现，不会出现在按键的回答里。C 的端口上有客户端连接时，停止需要额外短语 `stop robot server with client connected`。
  停止不依赖控制文件。
- **出路不等人。** 停止不会因为某个检查、夹爪测试或另一个停止正在进行而被拒绝：它会结束这些检查（结果丢弃，代码 `superseded`），
  停止 B 还会结束正在进行的夹爪测试，不同单元的停止并行进行（顺序由“先停 C”保证；同一个停止按两次只算一次）。运行探针没有回答
  （没有注册，或它抛了异常）会拒绝*启动*，但从不拒绝停止；已知有运行在进行时仍拒绝停止，C 上有客户端连接仍需要覆盖短语。C 是页面自己的单元
  且在运行时，无论证据怎么说，停 A 和 B 都保持 `stop_c_first`（M 重启后健康文件过期：`c.health_lost`；控制文件不见了：`c.no_specs`）。只有页面
  看不到的 C（套接字表或它的记录读不出来）只带一条警告（`stop_c_unknown`），不拒绝：这样的记录让 C 自己的停止也被拒绝，若再拒绝 A 和 B，
  就永远锁死了。
- **停止失败之后。** `SendSIGKILL=no`：30 秒内没有响应 SIGINT 的单元是 `failed`，它的进程还在跑，还连着机械臂。再按一次停止，只要控制组里
  还有进程（`TasksCurrent`，或该组的进程列表）就**不会**重置它：页面再发一次 SIGINT（相当于第二次 Ctrl+C，绝不强杀），继续点名这个单元，
  把记录写成 `stop_failed`，回答 `stop_left_processes`；进程没有了以后，同样的按键才会确认它。读不出来的，一律当作“还有进程”：
  没有任务计数、读不了的控制组、值为 0 或缺失的主进程号（失败的单元显示 0，组里却可能还有进程）；只有完全没有控制组的单元才算没有剩余。这期间如果必须放开机械臂，办法是物理急停。

每次启动需要：控制文件打开了栈的开关、`confirm` 里一字不差的确认短语（`start arm stack`、`start gripper stack`、`start robot server`；勾选之后由页面的确认弹窗
替人发送，脚本自己发；人还需要手输的只有 C 有客户端连接时停止的覆盖短语，以及部署页的那一个）、
勾选的确认项（A：Desk 已做完、手在急停上；B：爪口已清空；C：手在急停上、区域已清空、没有评测客户端在运行）、新的
`request_id`（重复的请求只会得到回答，不会做两次）、整机级的栈锁，以及所有阻塞原因都已消除。请求 id 的回答既在内存里，也保存在已回答请求的文件夹里
（`<工作区>/marvel0/stack/replay/`，保留最新 64 个），所以页面重启之后浏览器重发夹爪请求也不会让夹爪动两次；同一个 id 配不同的内容返回 `request_id_used`。

**整机级的锁管到哪里。** 锁在决定和启动期间持有（阻塞原因、`systemd-run`、记录），不在随后的慢动作期间持有（停止的等待、检查、夹爪测试）：
这些在产品内部单飞的工作线程里运行，锁若持有那么久，停止就要等检查，而出路不能这样。能动手的只有一个实例，即产品 LEVI；锁防止第二个实例的
请求插进一次决定中间。

**产品的工作区。** 写操作只在工作区是主检出 `.state` 的产品 LEVI 中生效（设计记录 2.12）。用别的 `LEVI_WORKSPACE` 启动的产品单元会把记录
写到以后没有实例去看的地方，所以它只显示栈（`gate.product`）。

## 启动要多久，以及拿掉的每一处延迟

负责人（10 月 21 日）要求：A 之前的时间从约 45 秒压到 5 秒以内，链上其他类似的刻意延迟也要尽量压缩。链在机器自己用的时间之外加了什么，旧与新：

| 延迟 | 旧 | 新 |
| --- | --- | --- |
| A 之前的标记等待 | 固定 `marker_wait_s`：45-60 秒（文件下限 45，默认 60） | 标记年龄 >= `marker_wait_s`（下限 2，默认 **3 秒**）且没有重任务真的在运行；上限 90 秒；读不出来：45 秒 |
| 查有没有夹爪栈（A 的启动需要这个答案：规则 R） | `list_controllers`，没有夹爪栈时约 21 秒，在标记**之前** | ROS 图，约 3.4 秒（估计值，见下），与标记年龄、FCI 检查**同时**；只有图什么也证明不了时才问那个 20 秒的问题；B 需要的第二次图检查**在 A 启动期间**做 |
| FCI 检查 | 1 秒，在标记之前；它的套接字（TIME_WAIT，60 秒）让 A 在一分钟内启动不了 | 1 秒，与标记年龄同时；复位关闭，不留套接字 |
| 定时器 | 每 2.0 秒看一次，每次只走一步（十步就是 20 秒以上的空等） | 上一次之后 0.25 秒再看，**A 有了单元之后改为 1.0 秒**（1 kHz 回路在运行），任何变化（步骤前进、操作结束）后立刻再看，操作结束时被唤醒 |
| 页面缓存的读 | 变化之后还要 2 秒 | 每次变化就丢弃（`on_change` -> `stack_api.invalidate`）；空闲读仍缓存 2 秒，给多个打开的页面共用 |
| C 之前的检查 | 先列表（1.2 秒）再坐标系（5 秒） | 同时：5 秒 |
| 关于 B 的旧答案（5-10 分钟）在 A 和 B 之间的刷新 | 在那里单独做一次检查（20 秒，现在 3 秒） | 接着去 B 的启动在 A 启动期间就做 |

保留的，因为那是机器本身要用的时间：FCI 检查（1 秒，现在不在关键路径上）、A 就绪约 5 秒、B 约 2 秒（夹爪校准）、末端坐标系检查约 5 秒、C 约 3 秒。这里没有改的：
健康监视器每秒写两次健康文件（`--hz 2`，由 `stack.py` 启动它），所以每个等这个文件的“就绪”最多晚 0.5 秒才被看到；C 之前列表的重试（一次异常要付列表的 20 秒上限）；
各种有效期（`FCI_TTL_S`、`B_PROBE_TTL_S`、`C_PREFLIGHT_TTL_S`：它们说一个答案管多久，没有任何东西在等它们）；停止之后每 2 秒一次的标记释放（不属于启动链）。**预期的时间线**，
用上面的时间（时间测试 `test_marvel0_stack_act_timeline.py` 在一个由测试拨动的时钟上跑真实的定时器）：按键后约 3.4 秒启动 A（图检查的时间），约 5 秒后就绪，B 在一次查看之内启动、2 秒后就绪，
C 之前的检查 5 秒，C 启动、3 秒后就绪：**从按键到 C 就绪约 20 秒**（以前约 100 秒；每次都 0.25 秒一看是 18.9 秒，但那在控制回路运行时每秒要 fork 约 29 次）。A 之前定时器最多多加 0.25 秒（它在下一次查看时才看到阶段就绪）；A 有了单元之后间隔是 1 秒，所以 A、B、C 各最多晚 1 秒被看到（时间测试里 C 就绪在 19.9 秒、序列结束在 20.8 秒：A 在 3.4 秒启动、8.5 秒就绪，B 在 8.8 秒启动，C 在 16.8 秒启动）。**本机的控制文件必须写
`marker_wait_s = 3`（或者不写）**：写着 45 的文件照样让 A 等 45 秒。

**查看的代价。** 一次查看是一次读栈：六次 `systemctl show` 加一些小的读取，本机上约 40 毫秒（10 月 21 日只读测得），重任务另加约 7 毫秒（对闸门的通配符和日志里的候选 scope 只做一次 `systemctl show`，日志只在变化时才重新解析）。
**A 有单元之前**每 0.25 秒一次加上变化之后的那几次，大约每秒 3-4 次，约占一个核的十分之一（那时还没有东西会被这些查看干扰）。**A 有了单元之后控制回路在运行，间隔改为 1 秒**
（`SEQUENCE_TICK_ARM_S`）：时间测试里从 A 启动到结束每秒 1.5 次（共 48 次查看，每次都 0.25 秒一看是 93 次），约每秒 9 次 fork/exec，而不是约 29 次（另一个办法是四个单元合成一次 `systemctl show`，那样仍有 12 次）。变化之后的那几次查看
和操作结束时的唤醒保留，所以做完的检查会立刻被处理；慢间隔的代价是每个恰好在两次查看之间就绪的阶段最多晚一个间隔。没有任何规则保证它不落在控制回路的核上（这个内核不是实时的）：它是每次查看几个短进程，不是自己的线程，
而且只在引导式启动或停止运行时才有；等人确认时每 2 秒看一次。没有对着 1 kHz 回路测过。

## 这些门的分量，以及这里什么都不是

**这些门只防误操作，防不了同一账号下的其他程序**：任何以这个用户运行的程序都能发出同样的请求、伪造页面的请求头、改写控制文件或
健康文件。勾选项是被记录下来的点击，不是证明。真正的屏障是 Franka Desk 和物理急停。**页面上没有任何东西是急停**：停止按键结束的是
一个程序，不会让机械臂停下。agent 会话从不调用启动和停止的路由（在共享账号上这是规则，不是机制）。

写操作只在产品 LEVI（主检出里的 `levi-product.service`）中生效；工作树或临时实例只显示栈，所有写操作都以 `not_product` 拒绝。

## 控制文件

可选的 TOML 文件：主检出的 `lab/marvel0/control.toml`，或环境变量 `LEVI_MARVEL0_CONTROL` 指定的文件。**缺失就是关闭**：所有启动和夹爪测试
返回 403 `control_disabled`，视图显示 `enabled: false`，页面的其余部分照常工作。开关默认关闭，由机器人所有者在场时打开。文件必须属于当前
用户，且其他人不可写。模板见上文英文部分；`enabled = true` 时四个机器值必须填写。仓库里不写任何机器值，所有 argv 只来自这个文件。

## 单元

四个都是 `systemd-run --user` 的临时单元。A、B、C：`Type=exec`、`KillMode=control-group`、`KillSignal=SIGINT`、`SendSIGKILL=no`、
`TimeoutStopSec=30`、`Restart=no`、`StandardInput=null`、`ManagedOOMPreference=avoid`；A 另有 `LimitRTPRIO=99` 和
`LimitMEMLOCK=infinity`，B 有 `LimitRTPRIO=99`；不设 `Nice`、CPU 亲和性和配额；不加 `--collect`（失败的单元保留在列表里，直到页面重置它）；
管理器环境里的 ROS、conda 和虚拟环境变量被清除，并设置 `PYTHONUNBUFFERED=1`、`RCUTILS_COLORIZED_OUTPUT=0`。一次性检查是 `--pipe --wait --collect`，
命令里的 `timeout` 之外还有 `RuntimeMaxSec` 兜底（查 ROS 图的是 `levi-aeri-stack-marvel0-oneshot-probe-b-nodes-<秒>-<6 位十六进制>`：名字每次调用都不同、不是每秒一个，所以同一秒里同种的两个检查，比如 B 的提前检查和一次 FCI 检查的点击，不会在 `systemd-run` 里撞名）。命令运行在
`levi/marvel0/stack_exec.sh <fr3_env.sh> --` 之后：它先 source 这台机器人自己的环境文件，再用命令替换自己（它的 sha256 固定在
`stack_units.py` 里）。日志在 `<工作区>/marvel0/stack/<a|b|c>/logs/`（目录 0700、文件 0600），每次启动在单元存在之前先写下记录（更新时持记录锁）；
曾经就绪的单元，日志超过 8 MiB 时原地截成最后 512 KiB（systemd 以 `O_APPEND` 追加；C 的 werkzeug 每个请求写一行）。
`ros2 launch` 自己会升级停止信号（5 秒后 SIGTERM，10 秒后 SIGKILL），`SendSIGKILL=no` 拦不住：A 的正常停止约需 10-11 秒，
停止超过 15 秒的单元会被标出。

`ros2 launch` 显式带 `--noninteractive`：标准输入是 `/dev/null` 时它本来就是 launch 的默认值（`ros2launch/command/launch.py:77`），这个参数只是
防止以后改了 `StandardInput=` 时被悄悄翻转。该模式下 launch 即使自己被中断也会把 SIGINT 转发给子进程
（`launch/actions/execute_local.py:448-452`）：在 `KillMode=control-group` 下，launch 的子进程会收到单元的 SIGINT **和** launch 转发的，共两次，
终端里只发一次。`control-group` 是有意保留的（launch 死了以后留下的子进程也靠它停掉）；两次 SIGINT 对这些节点有没有影响留给 S4b，演练脚本会数。

**没有在真机上验证的：** **单元里 `ros2 node list --no-daemon --spin-time 2.0` 要多久**（约 3.4 秒是估计：包装脚本 source 机器人环境、python 和 rclpy 启动、
2 秒发现；更慢的话 A 要等它，因为 A 需要 B 已知）、**2 秒内发现了监视器的节点，是否就说明夹爪栈的节点也会被看到**（监视器节点证明发现已经运行，不证明发现完整）、
**机械臂自己的 FCI 服务器怎么对待复位而不是关闭**、整条链的真实用时，**A 启动期间做第二次图检查（一个要用几秒 CPU 的 python/rclpy 进程）是否干扰 A 的启动和控制回路**（以前的提前检查在 A 出现之前就做完了），以及查看对 1 kHz 回路的负担。在本机上不启动任何东西就验证了的：多个 scope 以及陈旧 scope 的
`systemctl --user show` 输出格式（`heavy_jobs_running` 对着真实日志和 systemd 跑过：`clear`，忽略 18 个陈旧 scope，通配符的一次 `show` 约 7 毫秒）、复位关闭之后内核的表。同样没有在真机上验证的：日志行的匹配规则（来自 franka_control 源码，每条在 `stack_units.py` 里注明了源码行，并有测试去核对这些源码；
没有读过任何真实单元的日志）、`ros2 control list_controllers` 的输出格式（读自已安装 egg-link 所指的
`ros2controlcli/verb/list_controllers.py:34-36`，从未运行过）、A/B/C 在单元里的真实运行、单元内的 FIFO 98 与 `mlockall`、双 SIGINT、10-11 秒的停止、
`SendSIGKILL=no` 遇到真实卡死时的表现（失败单元仍有进程时 `TasksCurrent` 显示什么）、`RuntimeMaxSec` 结束命令之后 `systemd-run --wait` 给出的
退出码和输出（以及 `timeout -k` 不得不杀掉其程序时的状态：255，读自 systemd 的 `run.c`）、C 对 SIGINT 的反应、夹爪测试的真实结果和 SIGINT 取消、
FCI 探针、上述各状态下健康监视器的真实写法（读自它的源码，没有测过），以及单元运行时重启产品。B 就绪之后没有实时证据
（没有健康来源）：夹爪驱动悄悄死掉而没有 `process has died` 行时，B 仍显示 `ready`。

## 已经运行过的

- L1，假机器（`stack_act_machine.py`）：每道门单独、停止顺序、标记、检查、夹爪测试、引导序列、引导式停止（`test_marvel0_stack_act_stopall.py`；它拦住什么、
  做不完时说什么：`test_marvel0_stack_act_stopping.py`）、经 B 和 C 的引导式启动（`test_marvel0_stack_act_chain.py`：各阶段的确认项、暂停、B 和 C 单独启动的每道门在它的步骤上单独检验、
  每一步的失败和再按一次跳过已就绪的、列表重试、时间窗、每一步的取消、“停止整个栈”取代它、重启；`test_marvel0_stack_act_race.py`：序列每一次启动、设置、检查之前恰好回答了停止或取消）、文档。
  L2，用真实子进程运行替身程序
  （`fake_stack/`；引导式停止在 `test_marvel0_stack_act_stopall_processes.py`；整条链到 C 就绪、一个永不就绪的 B、需要重试的列表和再按一次在 `test_marvel0_stack_act_chain_processes.py`），把 `systemd-run` 和 `systemctl` 映射到 `Popen`：慢、永不就绪、崩溃、忽略 SIGINT、重复和缺失的行、包装脚本。
  L3，`tests/marvel0/rehearse_stack.py`，在开发机上、机械臂关闭时运行一次：名字带 `rehearsal` 的真实 `systemd --user` 单元，命令是替身：
  上述属性都被接受，单元内的程序看到 `rtprio 99` 和无限的内存锁，控制组里只有替身，SIGINT 停止结束了它，子单元在启动它的单元
  停止后仍然存活，`--pipe --wait` 带回了一次性检查的输出，`RuntimeMaxSec` 结束了卡住的一次性单元，最后没有留下任何东西。**2026-10-21 23:01（机械臂关闭、没有机器人进程）又
  跑了一次：63 项检查全部通过，没有留下任何东西**；跑之前演练脚本自己有两处要更新（它在工作树里运行，设计记录 2.12 的“只有产品能动手”规则拒绝了它；它也没有运行探针）。它现在还会测量
  `RuntimeMaxSec` 之后 `systemd-run --wait` 的退出码（1，4.2 秒后；被杀之前的输出带回来了）、单元里 `timeout -s INT` 的行为（会取消的程序：124；忽略 SIGINT 的程序：4.0 秒后 255）、
  `KillMode=control-group` 下 launch 式父进程的子进程收到几次 SIGINT（2 次）、停止超时的单元的任务数（`TasksCurrent` 为 1，该程序忽略第二次 SIGINT）；机器上有机器人进程时它拒绝运行（退出码 2）；
  对快速启动，它通过真实的一次性单元（外面套 `timeout`，输出是替身的）运行图检查，并对真实的 scope 运行 `_scopes_tasks`：有进程的、不存在的、以及同一个结束之后的。
- 快速启动：`test_marvel0_stack_act_heavy.py`（真实的 `heavy_jobs_running`，对着闸门自己格式的日志文件和真实的 `systemctl show` 输出，其中有 `show 'heavy-gate-*'` 的实测输出（本机的 18 个陈旧 scope，`fixtures/systemctl_show_heavy_gate_glob.txt`）：别的 hub 的或超出日志窗口的任务由第二个来源看到、读不了的日志、比尾部读取更长的日志、文档以及其后的门）、
  `test_marvel0_stack_act_fast.py`（图检查及其回退，用真实的 `ros2 node list` 格式；作为并行操作的 B 检查、顺序、归还标记、C 之前的检查同时进行、定时器、页面缓存、各步用时、控制文件的下限）、
  `test_marvel0_stack_act_timeline.py`（真实定时器和线程跑在测试拨动的时钟上，`sim_time.py`：A 在 5 秒内、整条链、A 有单元之后的间隔、重任务把 A 拦住直到它结束或到上限、读不出来时的固定等待）、
  `test_marvel0_sim_time.py`（计时测试背后的时钟：等过别的线程之后继续的线程，在被等的线程结束或阻塞的那一刻继续）、`test_marvel0_stack_act_fast_review.py`（一次性检查的单元名、人自己的操作让启动等待而不碰锁、停止丢弃的 FCI 答案，以及 B 只在已确认的“没有”之上启动：第一次回答、A 启动期间的第二次回答、“有”为最终结果、管理器的话、过期的第一次回答）、
  `test_marvel0_stack_act_fast_processes.py`（真实进程：图检查的替身、同时进行的两项检查、真实定时器上的整条链、FCI 检查之后内核的表）。
- 变异检查（`tests/marvel0/mutate_stack_act.py`，304 个变异，其中快速启动的 97 个；`--check` 只检查每个变异是否仍能应用）逐个去掉每道门，包括 2026-10-21 审查新增的
  每一道及其修复；其中经 B 和 C 的引导式启动、竞争、引导式停止新词的 59 个，各自只跑守着它的那一个测试文件。整张表要跑几分钟的 pytest：机械臂控制器运行时不要跑
  （用 `MUTATE_TESTS` 指定一个测试文件做快速运行；测试在 `MUTATE_CPUS` 指定的核上运行，默认 16-19）。
- `test_marvel0_stack_act_walk.py`：在假机器上随机游走（多数步骤是启动流程的下一步，其余是任何事：顺序错乱的停止、检查的错误回答、时钟跳变、
  监视器不写、套接字表或 systemd 读不出来、停止时卡住的单元、页面重启、经 A 或经 B 和 C 的引导式启动（带或不带暂停）），之后页面自己的按键，包括“停止整个栈”，必须能把一切停下来，包括标记。它找的是人
  无法离开的状态，任何一张用例清单都找不到。
- 重新生成前端读取的样本：`uv run --no-sync python tests/marvel0/regen_stack_act_sample.py`
  （`src/components/marvel0/__tests__/marvel0-stack-act.samples.json`）。
