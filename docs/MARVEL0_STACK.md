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
- **Checks.** `a/preflight` makes one TCP connection to the arm's FCI port (1337) and asks the gripper's controller
  manager whether a gripper stack runs already. **It is made only when A is positively absent**: no unit of A, A idle, the
  arm's sockets known and quiet (or lingering after a stop or a check of ours, see "The page's own sockets" below);
  anything that cannot be told refuses it. The question to the controller manager is
  `ros2 control list_controllers -c /robotiq/controller_manager`, which has no timeout of its own (with no manager it waits for
  ever and warns "Could not contact service" every 10 s), so it runs under `timeout -s INT -k 3 20` with systemd's
  `RuntimeMaxSec` behind it, and the verdict is read from what it printed, not from how it ended: the warning means no
  gripper stack, rows of `name type state` mean one runs, and a command ended before it could say is *no answer* (reason
  `b.probe_no_answer`, which keeps everything refused until it is asked again), never "absent". It takes about 20 s when
  there is no gripper stack. `c/preflight` runs three read-only checks (QoS, the three gripper controllers, the end-effector frame).
- **The check of B alone.** `b/probe` (the button "check the gripper stack") asks the same question and nothing else: it
  touches no FCI port, so it is allowed with A up as well as with A idle (not while a unit of B exists: then B is known), and
  `act.checks.can_probe_b` says when the button is there. It exists because "no gripper stack runs" is good for 10 minutes
  (`B_PROBE_TTL_S`) and, once A runs, the check before A (its only other source) is refused, so B could not be started any
  more. An answer older than that reads `unknown` with the reason `b.probe_stale` (`b.not_probed`: never asked;
  `b.probe_no_answer`: asked, not answered), never "absent", and the start blocker `b_probe_stale` says what to do. The
  check is also made **without a press**: while a guided start waits for B (again after 5 minutes of an "absent" answer, after 1
  minute of anything else), and when the person presses B's start with no usable answer: that press makes the check, starts
  nothing and answers 409 `b_probe_started` (press again when the ~20 s are over). A stop ends a running check.
- **The page's own sockets.** A socket to the arm outlives A, and the FCI check, by about a minute: the side that closes
  first holds the connection in TIME_WAIT, and `arm_guard` counts it as a control connection (any state, any port it does
  not know). With no unit of A, A then reads `idle` with the reason `a.sockets_draining` (not `foreign_running`, "stop it in
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
- **Marker.** The page sets `.arm-controller-active` (exclusively, remembering its inode and time), A waits until it is
  `marker_wait_s` old (LEVI's own heavy jobs that honour the marker are cleared meanwhile; other programs on this computer are
  not), and the page removes it once A's unit is gone and the arm's sockets are quiet. A marker made by hand is adopted with
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
  - **The steps** (`act.sequence.steps`, `{step, terminal, state}`; the states are `pending`, `running`, `done`, `skipped`,
    `failed`, `cancelled`, `not_reached`): `monitor`, `probe`, `marker`, `wait`, `arm`, `await_a` (A's, as before), then for
    `"c"`: `b_probe` (the check whether a gripper stack runs; automatic, made only when the last one is due), `b` (B's
    start **and** the wait for ready: the gripper calibrates), `c_preflight` (the three checks before C; **one automatic retry**
    when the controller listing got no answer, `controllers_state` `absent` or `unknown`; a check that answered no is not
    asked again), `c` (C's start and the wait for ready), then `done`. `skipped`: nothing was asked of the machine (the part was
    ready already, or the person did it by hand while the sequence waited). `not_reached`: the sequence ended before it.
    `act.sequence` also carries `through` and `pauses` (`{b, c}`, the effective ones: a start through `"a"` reports both
    `true`, because it waits for the person's B and C).
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
marker_wait_s      = 60                          # 45 to 600

[client]
enabled            = false                      # true: the page may host the evaluation client
```

With `enabled = true` the four machine values are required; no machine value is written in this repository, and every
argv comes from this file and nowhere else.

## The units

All four are `systemd-run --user` transient units (the one-shot checks are `--pipe --wait --collect`, with `RuntimeMaxSec` as
the backstop behind the `timeout` inside their command). A, B and C: `Type=exec`, `KillMode=control-group`, `KillSignal=SIGINT`,
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

Not verified on the real machine: the line patterns (they come from the franka_control sources, each with its source line
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
  and nothing was left. That run predates the reviews of 2026-10-21 and is **not repeated since**: the rehearsal now also
  measures the exit status of `systemd-run --wait` after `RuntimeMaxSec`, `timeout -s INT` inside a unit, the SIGINTs a
  child gets under `KillMode=control-group` and the task count of a unit whose stop timed out, and it refuses to run
  (exit 2) while a robot process is on the machine.
- The mutation check (`tests/marvel0/mutate_stack_act.py`, 207 mutations; `--check` says whether every mutation still applies)
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
- **检查。** `a/preflight` 对机械臂的 FCI 端口（1337）做一次 TCP 连接，并询问夹爪的控制器管理器是否已有夹爪栈在运行。**只有 A 被
  确认不存在时才做**：没有 A 的单元、A 是 idle、机械臂的套接字已知且安静（或刚停 A、刚检查之后在排空，见下面“页面自己的套接字”）；
  任何读不出来的情况都拒绝。向控制器管理器提问的命令 `ros2 control list_controllers -c /robotiq/controller_manager` 自己没有超时
  （没有管理器时它永远等下去，每 10 秒警告一次 “Could not contact service”），所以它在 `timeout -s INT -k 3 20` 之下运行，外面
  还有 systemd 的 `RuntimeMaxSec`，结论读的是它打印了什么，不是它怎么结束的：看到警告就是没有夹爪栈，看到 `名字 类型 状态` 的行就是有，
  还没来得及说话就被结束的是“没有回答”（原因 `b.probe_no_answer`，在再次询问之前一切仍被拒绝），绝不当作“没有”。没有夹爪栈时
  这个检查约需 20 秒。`c/preflight` 运行三项只读检查（QoS、三个夹爪控制器、末端坐标系）。
- **只查 B 的检查。** `b/probe`（按键“检查夹爪栈”）问同一个问题，别的什么都不做：不碰 FCI 端口，所以 A 在运行和 A 空闲时都可以做
  （已有 B 的单元时不做：那时 B 已知），`act.checks.can_probe_b` 说明按键何时出现。需要它，是因为“没有夹爪栈在运行”只有 10 分钟有效
  （`B_PROBE_TTL_S`），而 A 一旦在运行，A 之前的检查（它的另一个来源）就被拒绝，B 就再也启动不了。更旧的回答显示为 `unknown`，原因
  `b.probe_stale`（`b.not_probed`：从没问过；`b.probe_no_answer`：问了没有回答），绝不显示“没有”，启动阻塞项 `b_probe_stale` 说明怎么办。
  不按键也会重新检查：引导式启动等 B 的时候（“没有”的回答满 5 分钟后，其他回答满 1 分钟后），以及人在没有可用回答时按 B 的启动：这一下
  只做检查、什么也不启动，回答 409 `b_probe_started`（约 20 秒后再按）。停止会结束正在进行的检查。
- **页面自己的套接字。** 到机械臂的套接字比 A 和 FCI 检查多活约一分钟：先关闭的一方把连接留在 TIME_WAIT，`arm_guard` 把它算作控制连接
  （任何状态、任何它不认识的端口）。没有 A 的单元时，A 显示 `idle`、原因 `a.sockets_draining`（不是 `foreign_running`，“到它运行的终端里停”），
  条件是监视器的文件新鲜、机械臂的套接字*确实*读到是 active，并且在 `DRAIN_S`（90 秒）之内：最后做的是页面自己停 A 且监视器没有显示硬件
  active（`hardware_active` 为 null 或 false），或者是页面自己的 FCI 检查且监视器显示**没有控制器管理器**（`hardware_active` 为 null）。
  这正是真实监视器写的（`fr3_health_monitor.py:115-120`、`:349-359`：没有管理器回答时为 null，只有管理器回答而硬件没激活时才是 false，
  运行时为 true），所以检查之后有管理器回答，就是别人的 A，检查的标记不会盖住它；页面停 A 之后别人起的 A（硬件为 true）是
  `foreign_running`，并且 M 保留。排空状态下可以停监视器；A 的启动仍要等套接字安静（`gate.arm_sockets_clear`）。剩下的是监视器自己的
  延迟：它会把 A 的管理器的最后一次回答保留最多 5 秒（`--service-stale-after`），所以停得比这更快的话，页面在剩下的时间里可能读到
  `foreign_running`（正常的停止要 10-11 秒，到那时回答通常已经过期）。
- **标记文件。** 页面以独占方式创建 `.arm-controller-active`（记下 inode 和时间），A 要等标记满 `marker_wait_s` 秒
  （这段时间里，认这个标记的 LEVI 自己的重任务被清走；本机其他程序不会），A 的单元消失且机械臂的套接字安静后页面删除它。手工创建的标记点一次“认领”；页面没有创建的
  标记从不删除。释放循环（最多 90 秒）只在内存里：页面重启时若发现自己停掉的 A 的记录、而标记仍是自己的，就把释放做完。记录仍写着 `stopping`
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
  - **步骤**（`act.sequence.steps`，`{step, terminal, state}`；状态 `pending`、`running`、`done`、`skipped`、`failed`、`cancelled`、
    `not_reached`）：`monitor`、`probe`、`marker`、`wait`、`arm`、`await_a`（A 的，和以前一样），`"c"` 再加：`b_probe`（检查有没有夹爪栈在运行；自动，
    只在上一次的结果到期时才做）、`b`（B 的启动**以及**等它就绪：夹爪校准）、`c_preflight`（C 之前的三项检查；控制器列表没有回答时
    （`controllers_state` 为 `absent` 或 `unknown`）**自动重试一次**；回答了“不行”的检查不再重问）、`c`（C 的启动和等它就绪），最后 `done`。
    `skipped`：没有向机器要求任何东西（该部分本来就已就绪，或者序列等待时人已经手工做了）。`not_reached`：序列在它之前已经结束。
    `act.sequence` 还带 `through` 和 `pauses`（`{b, c}`，是生效的值：`"a"` 的启动两个都报 `true`，因为它等人去启动 B 和 C）。
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
命令里的 `timeout` 之外还有 `RuntimeMaxSec` 兜底。命令运行在
`levi/marvel0/stack_exec.sh <fr3_env.sh> --` 之后：它先 source 这台机器人自己的环境文件，再用命令替换自己（它的 sha256 固定在
`stack_units.py` 里）。日志在 `<工作区>/marvel0/stack/<a|b|c>/logs/`（目录 0700、文件 0600），每次启动在单元存在之前先写下记录（更新时持记录锁）；
曾经就绪的单元，日志超过 8 MiB 时原地截成最后 512 KiB（systemd 以 `O_APPEND` 追加；C 的 werkzeug 每个请求写一行）。
`ros2 launch` 自己会升级停止信号（5 秒后 SIGTERM，10 秒后 SIGKILL），`SendSIGKILL=no` 拦不住：A 的正常停止约需 10-11 秒，
停止超过 15 秒的单元会被标出。

`ros2 launch` 显式带 `--noninteractive`：标准输入是 `/dev/null` 时它本来就是 launch 的默认值（`ros2launch/command/launch.py:77`），这个参数只是
防止以后改了 `StandardInput=` 时被悄悄翻转。该模式下 launch 即使自己被中断也会把 SIGINT 转发给子进程
（`launch/actions/execute_local.py:448-452`）：在 `KillMode=control-group` 下，launch 的子进程会收到单元的 SIGINT **和** launch 转发的，共两次，
终端里只发一次。`control-group` 是有意保留的（launch 死了以后留下的子进程也靠它停掉）；两次 SIGINT 对这些节点有没有影响留给 S4b，演练脚本会数。

**没有在真机上验证的：** 日志行的匹配规则（来自 franka_control 源码，每条在 `stack_units.py` 里注明了源码行，并有测试去核对这些源码；
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
  停止后仍然存活，`--pipe --wait` 带回了一次性检查的输出，`RuntimeMaxSec` 结束了卡住的一次性单元，最后没有留下任何东西。这次运行
  早于 2026-10-21 的两份审查，**此后没有重跑**：演练脚本现在还会测量 `RuntimeMaxSec` 之后 `systemd-run --wait` 的退出码、单元里 `timeout -s INT` 的行为、
  `KillMode=control-group` 下 launch 式父进程的子进程收到几次 SIGINT、停止超时的单元的任务数；并且机器上有机器人进程时它拒绝运行（退出码 2）。
- 变异检查（`tests/marvel0/mutate_stack_act.py`，207 个变异；`--check` 只检查每个变异是否仍能应用）逐个去掉每道门，包括 2026-10-21 审查新增的
  每一道及其修复；其中经 B 和 C 的引导式启动、竞争、引导式停止新词的 59 个，各自只跑守着它的那一个测试文件。整张表要跑几分钟的 pytest：机械臂控制器运行时不要跑
  （用 `MUTATE_TESTS` 指定一个测试文件做快速运行；测试在 `MUTATE_CPUS` 指定的核上运行，默认 16-19）。
- `test_marvel0_stack_act_walk.py`：在假机器上随机游走（多数步骤是启动流程的下一步，其余是任何事：顺序错乱的停止、检查的错误回答、时钟跳变、
  监视器不写、套接字表或 systemd 读不出来、停止时卡住的单元、页面重启、经 A 或经 B 和 C 的引导式启动（带或不带暂停）），之后页面自己的按键，包括“停止整个栈”，必须能把一切停下来，包括标记。它找的是人
  无法离开的状态，任何一张用例清单都找不到。
- 重新生成前端读取的样本：`uv run --no-sync python tests/marvel0/regen_stack_act_sample.py`
  （`src/components/marvel0/__tests__/marvel0-stack-act.samples.json`）。
