# The evaluation run buttons of the marvel_workstation page

English first, Chinese after the line below. Backend: `levi/marvel0/run.py` (state rules, keys, supervisor),
`run_api.py` (routes), `live_frame.py` (the live camera view), `hostchild.py` (the launcher). Tests: `tests/marvel0/test_marvel0_run*.py`, the fake client
`tests/marvel0/fake_client.py` and the drift check `scripts/check_client_prompts.py`. Design record:
`LEVI/lab/levi-hub/programs/rig-console/reviews/control-design-20261020.md` (section 2.10-2.12, 5.3) and the client-hosting
review next to it, whose adversarial part wins where the pieces differ.

## What the buttons do

The page can start the evaluation client (terminal E) that a person starts in a terminal today, press the keys that
person presses, freeze it and end it. It hosts **`--levi-mode off`, the manual start gate, and nothing else**: not the
unattended mode, not the dual-label mode, no automatic next episode, no automatic label, no step-by-step. The command is
the one the command card renders (`command.render` is the only producer of the client's argv); the page adds
`--no-display-images` and nothing else.

- **Start** (`POST /run/start`). The page asks for a token first (`POST /run/challenge`, purpose `start`, with the form):
  the answer carries every start check and the command's warnings; a token is only issued when all checks pass. The start
  then needs the token, the phrase `start client <rollout group>` (`confirm`), the checklist (`attest`: scene reset, area
  clear, a hand on the physical e-stop) and a fresh `request_id`. **On the page, one press of "Start the client" does all of
  it** (owner request 2026-11-01: the three tick boxes and the confirm window are gone): the press stands for the three
  statements, and the line under the button says so ("Pressing Start states that a hand is on the physical e-stop, the scene
  is reset and the area around the arm is clear", next to the e-stop warning), the page sends the phrase and
  `attest: {estop, scene_reset, area_clear}` all `true` itself, and the backend still requires and checks both exactly as for
  a script, which sends them itself. The button is off while a request is on its way or wherever the backend says the client
  cannot be started (the reason is written beside it); a press again after an unanswered request is the same request
  (`request_id` idempotency). The voice run and the instruction change still start the client with the ticks of their own
  windows; nothing starts it without a press or one of those two. The client runs through `hostchild.py` (a launcher that gives it
  a keyboard, a pty) inside its own `systemd-run --user --scope` (`env -i` with the allowlist in front of the launcher; `systemd-run` itself gets
  only the user bus variables), so a restart of the product does not signal it. The core keeps the pty's master. Output goes to a private log (0600) in the workspace.
- **Keys.** Only these five bytes are ever written to the client's keyboard: Enter, `s`, `f`, `d`, `q`. Which of them
  the page offers depends on the state the client is in, read from its session file, its log and the pty:

  | State | The client | Keys offered |
  | --- | --- | --- |
  | `starting` | connecting, warming up the cameras | none (hold, end) |
  | `at_gate` | waits at the start gate | **Enter** (the arm goes home without any limit check, then the policy runs; once per gate), **q** (leave; no motion) |
  | `homing` | the arm goes home | none (hold) |
  | `running` | the policy runs | **s** success, **f** failure (one label per episode), **q** "exit and go home" (blind; its own phrase `exit and go home`, sent by its confirm window, which has tick boxes: hand on the e-stop, area clear) |
  | `awaiting_label` | the step limit is reached, the client waits | **s**, **f**, **d** discard (one label) |
  | `finishing` | saving the episode, closing | none (hold) |
  | `held`, `lost` | frozen, or not supervisable | none (end; hold in `lost`) |
  | `done`, `failed` | over | none (start a new run) |

  **There is no `s`, `p`, `i` or digit at the start gate.** There `s` and `p` choose *another task and start it*
  (`TASK_KEYS`): on 2026-10-20 a stray `s` ran the screws task twice. A label is sent once per episode: a second click while
  the client still saves the episode would wait in the keyboard and become the gate's `s`; so every key after the first is
  refused until the episode number changes. Enter is sent once per gate and never again; `q` is refused behind an Enter
  that has not been taken (the running loop reads that `q` as "quit"). A key is a request with its own token (30 s,
  bound to the run, the session, the episode number and the state; used once) and a `request_id`; one byte, one request at
  a time; the byte is written only after "sent" was synced, so a crash loses a key and never doubles it.
- **Hold** (`POST /run/hold`). SIGSTOP to the client's whole process group: no new target is sent, the arm stays at the
  last one. One click, no phrase, no token, always available while a client exists, idempotent, never waits for a lock.
  **Nothing resumes it**: afterwards the only action is end. (A resumed step would send one stale action, and a hold at
  the step-limit prompt would turn into a blind homing.)
- **End** (`POST /run/end`, phrase `end client run`, sent by the page's confirm window). `q` at the gate (a key); for a held, lost or still starting client
  SIGKILL of its group. Never SIGTERM + SIGCONT. A killed episode leaves its folder without `stopped_at` (the client's
  off mode does not tidy it): the page says so after an end; look at it before using the data.
- **Holds on its own** (it never sends anything else on its own): the health monitor shows red on **two distinct writes**
  while the arm may move; the session file stops updating for 10 s; the session file shows another task (a key reached the
  gate); a second connection is accepted by the policy server; a client is found alive after the service restarted
  (it has no keyboard any more). A monitor that is not fresh and green only **warns and blocks the next Enter**; red also
  blocks every key that makes the arm move. A client that does not reach the gate within 180 s is ended (nothing has
  moved before the gate).
- **Start checks (all passive).** Hosted runs enabled in the control file (`[client] enabled`, `LEVI_MARVEL0_CONTROL`, off
  when missing); the product LEVI; no client running; the policy server **started by this page** is ready, for the form's
  model and RTC setting; nobody is connected to it; no evaluation session is active (an ended one, or the one left by this
  page's own killed client, is not); the arm lock is free (the page then holds it for the whole run: a deploy and an AERI
  run refuse meanwhile); the health monitor is fresh and green; no automatic evaluation run is alive; the rollout folder is
  writable; and the z floor: `--min-z` below the client's floor for the **screws (0.075) and plates (0.05325) tasks is
  refused** (the client lets an explicit `--min-z` beat its task table, risk RIG-08); other tasks only get the warning.
  A robot server that is not listening is a hint, not a refusal.
- **Records.** `<workspace>/marvel0/run/`: `record.json`, `requests/` (answers kept for replay), `history.jsonl` (time,
  person, key name, request id, episode number; no transcript, no path) and `logs/` (the client's output, 0600). The page
  shows the last lines of the log without paths (`GET /run/log`).

## The live view

Below the Run card (step 4) the page shows two pictures side by side: the third-person (side) camera on the left and
the wrist camera on the right (one column on a narrow screen). They are the newest frames of the episode the client is
recording. **While a client runs it owns both RealSense cameras, so LEVI opens none of them for this view** (between runs the page can open them itself for a preview, see below). While an episode runs the client writes
`side_camera_raw.avi` and `wrist_camera_raw.avi` (MJPG, one frame per executed action, about 10 a second) into the
episode folder `<rollout root>/<group>/<task folder>/demo_NNNN/`; at the episode's end it turns them into mp4, deletes the
raw files and creates `.complete`. Each MJPG frame is a complete JPEG, so `GET /run/frame?camera=side|wrist`
(`live_frame.py`, `Supervisor.frame`) reads the tail of the growing file (at most the last 1 MiB) and returns the last
complete JPEG (`image/jpeg`, `Cache-Control: no-store`, `X-Levi-Frame-Age-Ms` and `X-Levi-Frame-Episode`). The folder is
the one without `.complete` whose raw file was written most recently, among all task folders of this run's group, and only
files written since the run began count; the rollout root and the group folder are resolved, and a link below the group
folder (task folder, episode folder, raw file) is refused. The route never adopts a client after a restart of the service
(only `GET /run` does), so it answers `no_run` until the page's status read has.

- **It reads files only.** No camera is opened, no port is connected to, nothing is sent to the client, the control loop
  is not touched. The route is a read like `/run/log`; it answers `404` with `no_run` (no active run of this page),
  `no_episode` (no episode is recording) or `no_frame` (no complete frame yet), and `422` for any other camera.
- **Limits.** There is no picture between episodes: the client records frames only while an episode runs, and the page
  says so. The source is about 10 frames a second and the page asks about 10 times a second (4 until 2026-11-02, which looked jerky) (one request at a time per
  camera, only while the run is active and the tab is visible), so a picture can be a few tenths of a second old; its age
  and episode folder are written under it. The last picture stays for 3 seconds when the next one is missing. The client's
  video writer (OpenCV over FFmpeg) puts its output on disk in blocks of 256 KiB, so the file grows a few frames at a
  time: the picture moves on in steps, about every 0.3-0.9 s at this machine's frame sizes, and can be that far behind
  the camera; the age shown is that of the last block written, and the first picture of an episode appears only after
  the first block (until then the page says the episode has no picture yet). Measured with cv2 4.11 in `openpi/.venv` by
  the review of 2026-11-01.
- The proxy of the page (`src/utils/backendProxy.ts`) passes the two `X-Levi-Frame-*` headers on, besides the content
  type and the cache policy it already passed.

### The camera preview between runs

When no client of the page is on, the live view card has an **Open camera preview** button. It asks LEVI to open the two
cameras itself, so both pictures show between runs (and before a start); **Stop** (the same button) lets them go. Showing
pictures while a client waits at its start gate is not part of this: the client owns the cameras then.

- **The worker.** `POST /preview/start` starts `integrations/camera_preview/worker.py` (`levi/marvel0/preview.py`) as a
  process group of its own, recorded in `levi/children.py` (kind `camera-preview`; the service stops it with its other
  children). `pyrealsense2` is not in LEVI's environment, so it is run the way the AERI terminal D gets it:
  `nice -n 19 uv run --no-sync --with pyrealsense2==2.55.1.6486 python -B worker.py --out <dir> --camera side=<serial> --camera wrist=<serial> ...`
  from the openpi checkout (whose environment has numpy and OpenCV), `UV_OFFLINE=1`, with the hosted client's allowlisted
  environment (no `LEVI_*`, no token, no `UV_CACHE_DIR`: uv's cache is where the hosted client's is). The serials are the
  site file's (`[cameras] view` is the "side" picture, `wrist` the wrist one), so swapping a camera there is all it takes.
  It opens the two colour streams at 640x480, 15 frames a second, encodes at most 15 JPEGs a second per camera and writes
  `side.jpg` and `wrist.jpg` below `<workspace state>/marvel0/camera_preview/` (a temporary name, then a rename, so a reader
  never sees half a picture). `GET /preview/frame?camera=side|wrist` returns the newest one (`image/jpeg`, `no-store`,
  `X-Levi-Frame-Age-Ms`), or `404` with `preview_off` or `no_frame`. `GET /preview` is the state: `off`, `starting`, `on`,
  `stopping`, `refused` (with the reason) or `failed` (with the reason), the age of each picture and the limits.
- **When it is refused** (`409`, the reason in the answer, the card shows it beside the disabled button; the first that
  applies is shown): this LEVI is not the product (`not_product`: only `levi-product.service` in the main checkout may
  open the real cameras, the rule of the hosted runs, so a worktree or temporary instance cannot hold the cameras where the
  product's hand-back cannot see it); the page's hosted evaluation run is active (`run_active`); the page's AERI run is
  active (`aeri_active`); a client is connected to the policy port (`client_connected`, the deploy status's
  `listener.established`, as the voice input's gate reads it); the arm lock is held (`arm_in_use`); or any of these could
  not be read (`state_unreadable`: unknown counts as in use, so does a deploy status whose state is `unknown` or whose
  socket table could not be read, which the deploy reports as 0 connections, and a hosted-run record that cannot be read
  or whose launcher is gone but was never settled, both of which the run supervisor itself takes for "no run" after a restart
  whose first read failed). Also `client_starting` (a client is being launched, also one whose
  launch began while the blockers were being read) and `preview_stopping`. The one refusal that is not a `409` is
  `preview_unavailable` (`503`: the worker file, the openpi checkout, the client environment or the state folder is
  missing). The two writes are a person's, like the other write routes (`person_only`, the page's own origin through the
  proxy).
- **Handed back before a client starts.** Every path that starts a client stops the preview first and waits (up to 6 s)
  until the worker's process has exited, which is when the cameras are free: the hosted evaluation start
  (`run.Supervisor._start`, which the change-of-instruction restart calls again as `/run/start`) and the AERI terminal D
  start (`aeri_run.Supervisor._start`). LEVI closes the worker's input pipe first (the clean path), then sends SIGTERM, and
  sends SIGKILL only after 5 s; a hand-back that needed SIGKILL is written to LEVI's log, shown as `killed` in
  `GET /preview` until the next preview, and followed by half a second of waiting before the client is launched. No preview
  can start until that launch has answered: a hand-back counts, so a preview start that was reading the blockers while a
  client was launched is refused (`client_starting`). If the worker does not exit in time the start is refused with
  `camera_preview_busy` and nothing is launched, instead of a client that fails on a busy camera; the card then tells the
  person to wait until the live view says the cameras are free. A start refused earlier (a wrong token) leaves the preview
  alone. The worker writes the time of every camera's `pipeline.stop()` and of its whole exit to its log
  (`worker.log` below the preview folder), so each run leaves a measurement.
- **It stops by itself** 20 s after the last picture was asked for (the page was closed), 10 minutes after it started (so
  it never holds the cameras against a client someone starts from a terminal for long), when LEVI stops, and when it sees a
  client while it starts or runs: a hosted run, an AERI run, the arm lock or a policy connection (the first look is about
  1 s after the start, then every second). A state that cannot be read in two separate readings in a row stops it too, with
  its own note (`state_unreadable`, "LEVI could not tell whether a client is starting"): the second look drops the deploy
  status's 2 s cache first, so one bad snapshot never counts twice. While the watcher thread runs, a read of `GET /preview`
  only notices an exit; the stops (which take seconds, and a kill more) are the watcher's. A worker whose input
  pipe closes (LEVI died) lets the cameras go about 2 s later (measured 2026-11-02 with both D405s, from the close of the
  pipe to the exit of the process). The Copy button of the command card (step 3) stops a preview that holds the cameras,
  because a command pasted into a terminal is a client LEVI does not start.
- **When it fails** it says why and does not retry: `cameras_busy` (another program holds a camera), `cameras_not_found`
  (a serial of the site file is not connected), `worker_unavailable` (no `uv`, no overlay in uv's
  offline cache, or the worker's imports fail), `no_frames` (the cameras opened and sent nothing,
  or stopped sending), `worker_failed`. The worker's own exit codes (10, 11, 13) reach the page as they are: the worker
  leaves with `os._exit` and reads its input pipe without the buffered reader's lock, because it once aborted (exit code -6)
  at exit on every failure and every stop by signal.
- **Not covered.** A client started from a terminal is seen when it takes the arm lock (the AERI terminal D,
  `levi.automatic.fr3_run`, takes it before it loads modules or connects, so in `policy_swap` mode tens of seconds before
  it opens a camera) or when it connects to the policy port. A client that takes no lock (the openpi client run by hand)
  is seen only at its policy connection, which comes before its cameras are opened by about a second or two. From the
  moment it is seen the preview needs at most 1 s (the look) + 2 s (the deploy status is cached for 2 s) before the stop
  starts, then about 2 s for the worker to exit (up to 6 s if it has to be killed): about 5 s in the usual case, up to
  about 9 s in the worst. A lock-free client can therefore still find a camera busy and fail before any motion; its own
  error and the 10-minute limit are what remain. The preview is not stopped by merely selecting and copying the command
  by hand.

## What the gates are worth, and what nothing here is

**The gates stop mistakes. They do not stop another program of the same account**: anything that runs as this user can send
the same requests (ask for a token, then use it), forge the page's headers, write to the client's keyboard or signal its
process. The tokens, phrases and checklist are clicks that are recorded, not proofs. **Nothing is typed any more**: every
stop of the page, and the start of the arm stack, the policy server and the deploy, is one confirm window with its hazard
sentences and tick boxes, and the page sends the phrase itself; the backend still checks it. Two actions are one press with
no window: starting the hosted client (the press states the three checks the boxes used to ask; the phrase and the
checklist are still sent and checked), and stopping the policy server while no client is connected to it (the page still
sends the phrase, and the server still checks it and still refuses while a client is connected). The two overrides that cut
something that is in use stay typed fields: stopping the robot server C with a client connected, and stopping the policy
server with a client connected. The real barriers are Franka Desk and
the **physical emergency stop; keep a hand on it**. **Nothing on this page is an emergency stop**: hold freezes the client,
it does not stop the arm, which keeps the last target it was sent; success, failure, discard, exit and Enter send the arm
home without any limit check. Agent sessions never call the `/run` or `/stack` write routes (a rule, not a mechanism, on a
shared account). Writes act only inside the product LEVI (`levi-product.service` from the main checkout); another instance
shows the run and refuses (`not_product`). If the core ends, the pty's master closes and the kernel hangs the client up.

Not hosted, by decision: the client in the unattended or dual mode (dual needs `levi-live`, which is not running), the next
episode starting by itself, labels by anything but the person, the step-by-step mode (hold is its substitute) and
anything typed into the client. A browser that closes does not stop the client: the current episode runs to its step
limit and waits; hold and end stay available.

## Wiring, and what is still for the owners of other files

Wired: the routes are in `levi/marvel0/api.py` (and listed in `tests/marvel0/test_marvel0_routes_mounted.py`, which also checks
that every write refuses a Bearer credential); `run_api.py` registers `run_api.run_active` with
`stack_act.register_run_probe` when it is imported, so the arm stack's start and stop refuse while a client runs, and a
client that outlived a restart is taken over (frozen) by that very question, before anyone opens the run card.

Not wired, for whoever owns those files: `run_api.active_job()` gives the running client as a job row (`kind marvel0-client`)
for `levi stop` (`levi/activity.py`) and for the policy deploy's stop (`levi/marvel0/deploy.py` should refuse while a client
runs and not honour its override phrase); `run_api.adopt_on_start()` could be called from the service's lifespan
(`levi/service.py`, next to `stack_api.start_auto()`); nothing calls either today.

## Control rate

The command card has a **Control rate** (`--control-hz`): exactly 5, 10, 20, 25 or 50 Hz, default 10. At 10 Hz no flag is
written and the command is byte for byte the one the card made before (`tests/marvel0/fixtures/command_golden_10hz.json` was
taken from main before the option was written). Any other value is refused with `control_hz_unsupported` (a string, a float,
a boolean is `invalid_type`); the hosted run starts the very command the form renders, so the rate reaches it unchanged.
Code: `levi/marvel0/command.py`; tests: `tests/marvel0/test_marvel0_command_hz*.py`, `src/components/marvel0/__tests__/control-logic.test.ts`,
`command-panel-hz.test.tsx`.

**What scales, and why** (client files are in `openpi/examples/fr3_local/`, read only):

| Flag or value | Rule | Evidence |
| --- | --- | --- |
| `--max-step-m`, `--max-step-rad` | the site's value up to 10 Hz; above it divided by `rate / 10` (20 Hz: 0.05 m and 0.175 rad, 25 Hz: 0.04 and 0.14, 50 Hz: 0.02 and 0.07). The speed they allow stays 1 m/s and 3.5 rad/s; at 5 Hz it is 0.5 m/s and 1.75 rad/s | `run_robotiq_client.py:704` `raw_step` is the distance of the commanded pose from the **measured** pose, `:728` and `:730` compare it with the caps, on every tick (loop period `:1506`); a rejected chunk is dropped, re-queried and costs one step of the budget (`:1585`). So a per-step cap is a speed cap: at 50 Hz a 0.1 m cap would allow 5 m/s |
| `--rtc-execute-horizon`, when the field is empty | the site's default up to 10 Hz; above it the largest horizon that leaves `ceil(3 * rate / 10)` beats of the 10 actions free (20 Hz: 4, 25 Hz: 2, 50 Hz: 1); an explicit value is kept | `run_robotiq_client.py:305` "execute_horizon + d must stay <= 10"; the RTC delay `d` is a latency in ticks, `rtc_chunker.py:206` `ceil(latency_s / period_s) + 1` with `period_s = 1 / control_hz` (`run_robotiq_client.py:904`); the 3 beats of this page are the 0.3 s of 10 Hz (server round trip 187 ms, `README_ROBOTIQ_ZH.md:236`) |
| the RTC margin warning | beats of the chosen rate: 2 (5 Hz), 3, 6, 8, 15 (50 Hz); at 50 Hz not even a horizon of 1 fits (`rtc_never_fits`: a 10-action chunk lasts 200 ms, shorter than the latency) | same |
| the default note | gets ` hz=<rate>` for another rate | the note is stamped into the eval log; the episodes are filed in the same rollout group unless one is typed (`metadata.json` of each episode holds the nominal rate, `run_robotiq_client_collect_data.py:147`, and the measured one) |

**What stays, and why.** `--max-timesteps` stays in steps, and the site's cap (3000 steps) is not changed: the number of
actions a task needs does not depend on the rate (the client's own budget, 400 steps at 10 Hz, `run_robotiq_client.py:230-238`).
The form shows the seconds (`steps / rate`; at least, because each inference of about 187 ms stalls the loop, which does not
catch up, `:1641`) and warns (`episode_short`) when, above 10 Hz, they are under 30 s, about the 90th percentile of the
slower task's demos at 10 Hz (28.7 s, `:230-231`). `--home-duration-s` is already seconds (`:941`: `int(home_duration_s * control_hz)`
steps of `1 / control_hz` each). `--post-step-verify-delay` is not passed: the client sets 0.0 (`:222`; the 0.2 s default
of `RobotEnv` capped the loop at 4.8 Hz, `README_ROBOTIQ_ZH.md:215-231`), and a step costs about 8 ms of HTTP, which fits the 20 ms
of 50 Hz. `--min-z`, `--max-xy`, the angle ranges, the gripper settings (`fr3_env.py:383`, a 0.75 s throttle in seconds) are
lengths, angles or times and do not depend on the rate. `--video-fps` is 0, the measured median rate.

**Warnings** (never refusals), for a rate other than 10 Hz: `control_hz_not_trained` (the policy was trained on 10 Hz data:
`meta/info.json` of `lerobot_fr3_filtered_robotiq_v3` says `"fps": 10`, `action_horizon=10` in `src/openpi/training/config.py:1145`,
`run_robotiq_client.py:294`; the client's README says raising the rate "plays the training trajectories faster, leaves the
action distribution and triggers reflexes more easily", `README_ROBOTIQ_ZH.md:233`); `control_hz_untested` (the first use
changes the arm's speed profile and is untested on this rig: the owner present with a hand on the physical e-stop; the rate field's note
repeats it); `step_cap_tight` (the scaled caps are below the 99th percentile of the 10 Hz training steps); `episode_short`.

**Measured, read only** (2026-10-21, every 6th episode of `lerobot_fr3_filtered_robotiq_v3`: 250 of 1497, 30336 rows): the
distance between a training action and its state, the quantity the client's per-step check uses: median 0.0098 m, 90th
percentile 0.0227, 99th 0.0384; rotation (largest of the three wrapped angle differences) 0.0164, 0.0445, 0.0883 rad. Rows above the
scaled caps: 20 Hz 0.11 % (m) and 0 % (rad), 25 Hz 0.77 % and 0.06 %, 50 Hz **14.32 %** and 2.59 %. A real rollout adds the
tracking lag of the arm, so the share is higher than this: at 50 Hz expect many rejected chunks, which is the interlock
doing its job, not a fault. The constants `TRAIN_STEP_P99_*` in `command.py` are these 99th percentiles.

**Not verified.** No rate other than 10 Hz has been run on the arm, and the client was not run at all: what the arm does at
20, 25 and 50 Hz (whether the controller follows 50 targets a second, whether the loop keeps 20 ms next to the camera and state
reads, how often the scaled caps reject), the real inference latency in beats at each rate, and whether the policy behaves
at all outside 10 Hz are unknown. The flag spelling and the lines this section stands on are checked by
`scripts/check_client_prompts.py` (12 strings, `control_hz_*`, `step_cap_*`, `loop_period`, `home_steps_by_seconds`, `rtc_period`,
`rtc_delay_ticks`, `post_step_verify_zero`, `trained_at_10_fps`). The default rollout group is the same at every rate.

## What was run, and what was not

Run on the development machine, with the arm off, never against a robot port, never the real client: the unit tests of the
state rules (every state, text the client words differently, a client that is gone), what is offered, the token, the
environment and command; the supervisor on a scripted client (start and every refusal, keys and the bytes that reach the
keyboard, hold, end, the watcher's own holds, take-over after a restart, replay); the launcher and the fake client under a
real pty (a whole run, a stray `s`, two label clicks during a slow save, a hold and an end, a client whose core died);
mutation checks (the code broken on purpose, the named tests fail); and the drift check against the client's source.
The five test files hold 260 tests (96 + 81 + 25 + 50 + 8, the 25 with real processes take about 25 s); the mutation sweep
`tests/marvel0/mutate_run.py` (it refuses to run while a robot process is alive; `--check` only validates the list) breaks
the code 98 ways, in a scratch copy, one test file per breakage: the first sweep let 7
through (a pid check after a restart, the launcher's own kill of the group, the exit record's mode, the token list after a
run, the same monitor write counted twice, the arm lock row, the z floor row), each got a test and is now caught, so all 98 are;
`scripts/check_client_prompts.py` found all 49 strings (and 12 more for the control rate, 61 in all) in place on 2026-10-21.
Regenerate what the front end reads with `uv run --no-sync python tests/marvel0/regen_run_sample.py`
(`marvel0-run.samples.json`: `GET /run` in every state; `marvel0-run.answers.json`: the other answers and the refusals).

**Not verified on the real machine:** the camera preview's remaining parts. Measured on 2026-11-02 with both D405s (a smoke test of the worker, run by hand, not by a test): both cameras opened through `uv run --no-sync --with pyrealsense2`, JPEGs written, the process exited about 2 s after its input pipe closed, and the devices could be opened again at once. Still unmeasured: the exit time when a stop has to rely on SIGTERM alone (LEVI's own stop closes the pipe first and then sends SIGTERM at once: the pipe-close path measured above, with a signal on top; SIGTERM alone is what `children.stop_owned` and a reclaim send; and the measured worker file was the one from before the stop timings were written to `worker.log`, so the first run of the current file on the real cameras is the first to record them), a busy D405's answer to `pipeline.start`, and a D405 killed in the middle of `pipeline.stop()`. The tests run the real `worker.py` over stand-in libraries and assert exit code 0 well before the SIGKILL point, but not the librealsense timing. `systemd-run --user --scope` itself (that the pid is the launcher's, that the group
survives a restart of the product, the scope's stop behaviour); the real client under the launcher (`uv` offline with the
environment allowlist, the `uv` chain's signal handling, SIGHUP reaching `python`, `--no-display-images` without a display);
SIGSTOP for minutes (cameras, the policy server's websocket, the controller holding the last target); the real timing of
the saving, homing and the 1 s session heartbeat (the page's thresholds are guesses until measured); the real monitor's
write rate for the two-writes rule; the real client's words (checked only against the source files:
`python3 scripts/check_client_prompts.py` before each day's first run); how a hung-up pty looks to the real client
(Linux answers EIO to a read; the fake takes the worse reading and goes on). The first real use goes in the order of the
review: start to the gate and `q` (no motion), one episode with Enter and `s`, `f`, hold and end, `d` at the step-limit prompt,
with the owner at the arm and a hand on the emergency stop.

---

# 评测运行按键

上文是英文，下文是中文。后端：`levi/marvel0/run.py`（状态规则、按键、监管者）、`run_api.py`（路由）、`live_frame.py`（实时相机画面）、`hostchild.py`（启动器）。
测试：`tests/marvel0/test_marvel0_run*.py`，假客户端 `tests/marvel0/fake_client.py`，漂移检查 `scripts/check_client_prompts.py`。
设计记录：`LEVI/lab/levi-hub/programs/rig-console/reviews/control-design-20261020.md`（2.10-2.12 与 5.3）以及旁边的客户端托管复审，
两者不一致时以复审中的对抗部分为准。

## 按键做什么

页面可以启动人今天在终端里启动的评测客户端（终端 E），按下人会按的键，冻结它，结束它。它托管的**只有 `--levi-mode off`
和手动开始闸门**：不托管无人值守模式，不托管双标签模式，没有自动开始下一个片段，没有自动标签，没有逐步确认。命令就是命令卡片渲染出来的那一条
（客户端的 argv 只由 `command.render` 产生）；页面只多加 `--no-display-images`，别的都不加。

- **开始**（`POST /run/start`）。页面先要一个令牌（`POST /run/challenge`，用途 `start`，带上表单）：回答里有全部开始检查和命令的警告；
  只有全部检查通过才会发令牌。开始还需要：令牌、短语 `start client <rollout group>`（`confirm`）、清单（`attest`：场景已复位、区域已清空、手在物理急停上）
  和新的 `request_id`。**在页面上，点一下“启动客户端”就全做了**（用户 2026-11-01 的要求：三个勾选框和确认弹窗已去掉）：这一下代表那三句话，按钮下面的一行字写明
  （“按下‘启动客户端’即表示：手已放在实体急停上，场景已复位，手臂周围的区域已清空”，旁边是急停警告）；短语和 `attest: {estop, scene_reset, area_clear}`（全为 `true`）由页面自己发送，
  后端和对脚本一样仍然要求并核对这两样，脚本则自己发。请求在途时，或后端说现在不能启动时，按钮是灰的（原因写在旁边）；没有收到回答之后再点一次是同一个请求（`request_id` 幂等）。
  语音直跑和换指令仍然用它们自己弹窗里的勾选启动客户端；没有点击，也没有这两者，就不会启动客户端。客户端通过 `hostchild.py`（给它一个键盘，即伪终端的启动器）运行在它自己的 `systemd-run --user --scope` 里（启动器前面是带白名单的 `env -i`；`systemd-run` 自己只拿到用户总线变量），所以产品重启不会
  给它发信号。核心保留伪终端的主端。输出写入工作区里的私有日志（0600）。
- **按键。** 写给客户端键盘的字节只有这五个：回车、`s`、`f`、`d`、`q`。页面提供哪些，取决于客户端所处的状态，状态由它的会话文件、日志和
  伪终端读出：

  | 状态 | 客户端 | 提供的键 |
  | --- | --- | --- |
  | `starting` | 正在连接、预热相机 | 无（保持、结束） |
  | `at_gate` | 停在开始闸门 | **回车**（机械臂不经任何限位检查地回位，然后运行策略；每个闸门一次）、**q**（离开；不动） |
  | `homing` | 机械臂回位 | 无（保持） |
  | `running` | 策略在运行 | **s** 成功、**f** 失败（每个片段一个标签）、**q** “退出并回位”（盲走；有自己的短语 `exit and go home`，由它的确认弹窗发送，弹窗里有勾选项：手在急停上、区域已清空） |
  | `awaiting_label` | 到达步数上限，客户端在等标签 | **s**、**f**、**d** 作废（一个标签） |
  | `finishing` | 正在保存片段、正在关闭 | 无（保持） |
  | `held`、`lost` | 已冻结，或无法监管 | 无（结束；`lost` 时还可保持） |
  | `done`、`failed` | 已结束 | 无（开始新的一次运行） |

  **开始闸门处没有 `s`、`p`、`i` 和数字。** 在那里 `s` 和 `p` 的意思是“选另一个任务并直接开始”（`TASK_KEYS`）：2026-10-20 一次误按的 `s` 把螺丝任务
  跑了两次。标签每个片段只发一次：客户端还在保存片段时的第二次点击会留在键盘缓冲里，变成下一个闸门的 `s`；所以第一个键之后的所有键都被拒绝，
  直到片段号变化。回车每个闸门只发一次，不再发第二次；回车还没被客户端取走时，`q` 也被拒绝（运行循环会把那个 `q` 读成“退出”）。每个键都是一次带自己令牌的请求
  （30 秒，绑定运行、会话、片段号和状态，只能用一次）加 `request_id`；一次一个字节、一次一个请求；字节在“已发送”记录落盘之后才写入，所以崩溃只会丢掉
  一个键，不会重复一个键。
- **保持**（`POST /run/hold`）。对客户端的整个进程组发 SIGSTOP：不再发出新目标，机械臂停在最后一个目标上。一次点击，不要短语，不要令牌，只要客户端存在就始终可用，
  可重复，从不等锁。**没有任何东西会让它恢复**：之后唯一的动作是结束。（恢复会多发一个过期动作，在步数上限提示处的保持恢复后还会变成盲走回位。）
- **结束**（`POST /run/end`，短语 `end client run`，由页面的确认弹窗发送）。闸门处是 `q`（一个键）；已冻结、失联或仍在启动的客户端则对其进程组发 SIGKILL。绝不用 SIGTERM 加
  SIGCONT。被杀死的片段留下没有 `stopped_at` 的文件夹（客户端的 off 模式不会整理它）：结束后页面会提示；用这份数据之前先看一眼。
- **自动保持**（页面自己不会发别的东西）：健康监视器在**两次不同的写入**里都显示红灯，且机械臂可能在动；会话文件 10 秒不更新；会话文件显示换了任务（有键到了
  闸门）；策略服务器接受了第二个连接；服务重启后发现客户端还活着（它已经没有键盘）。监视器不新鲜或不绿时**只警告并拦住下一次回车**；红灯还会拦住所有会让机械臂动的键。
  180 秒内没到闸门的客户端会被结束（闸门之前什么都没有动过）。
- **开始检查（全部被动）。** 控制文件里打开了托管（`[client] enabled`，`LEVI_MARVEL0_CONTROL`，缺失即关闭）；是产品 LEVI；没有客户端在运行；**由本页启动的**策略服务器
  已就绪，且模型和 RTC 设置与表单一致；没有人连在它上面；没有活动的评测会话（已结束的、或本页自己被杀死的客户端留下的那一个不算）；手臂锁空闲（页面随后在整个运行期间持有它：
  部署和 AERI 运行因此被拒绝）；健康监视器新鲜且为绿；没有自动评测运行；采集目录可写；以及 z 下限：**螺丝（0.075）和盘子（0.05325）任务的 `--min-z` 低于客户端的下限会被拒绝**
  （客户端让显式的 `--min-z` 压过它的任务表，风险 RIG-08）；其他任务只给警告。机器人服务器没有在监听只是提示，不拒绝。
- **记录。** `<工作区>/marvel0/run/`：`record.json`、`requests/`（保留回答以供重放）、`history.jsonl`（时间、人、按键名、请求号、片段号；没有记录文本，没有路径）和
  `logs/`（客户端的输出，0600）。页面显示日志的末尾几行，路径已去掉（`GET /run/log`）。

## 实时画面

运行卡片（第 4 步）下面并排显示两张画面：左边是第三视角（侧面）相机，右边是腕部相机（窄屏时排成一列）。它们是客户端正在录制的片段的最新帧。
**客户端运行时两台 RealSense 相机都归它使用，所以这个画面不会由 LEVI 打开相机**（两次运行之间页面可以自己打开相机做预览，见下）。片段运行时，客户端把 `side_camera_raw.avi` 和 `wrist_camera_raw.avi`
（MJPG，每执行一个动作一帧，约每秒 10 帧）写进片段文件夹 `<采集根目录>/<组>/<任务文件夹>/demo_NNNN/`；片段结束时它把这些文件转成 mp4，
删除原始文件，并创建 `.complete`。MJPG 的每一帧都是一张完整的 JPEG，所以 `GET /run/frame?camera=side|wrist`（`live_frame.py`、`Supervisor.frame`）
读正在增长的文件的尾部（最多最后 1 MiB），返回最后一张完整的 JPEG（`image/jpeg`，`Cache-Control: no-store`，另有 `X-Levi-Frame-Age-Ms` 和 `X-Levi-Frame-Episode`）。
选中的文件夹是本次运行所在组的所有任务文件夹里、没有 `.complete` 且原始文件最近写过的那一个，并且只算运行开始之后写过的文件；采集根目录和组文件夹本身按 realpath 解析，组文件夹以下（任务文件夹、片段文件夹、原始文件）任何一层是链接都会被拒绝。
这个路由不会在服务重启后接管客户端（只有 `GET /run` 会），所以在页面的状态读取接管之前它回答 `no_run`。

- **只读文件。** 不打开相机，不连接任何端口，不给客户端发送任何东西，不碰控制回路。这个路由和 `/run/log` 一样是读取；没有画面时返回 `404`，
  原因为 `no_run`（本页没有活动的运行）、`no_episode`（没有正在录制的片段）或 `no_frame`（还没有完整的一帧）；其他相机名返回 `422`。
- **限制。** 片段之间没有画面：客户端只在片段运行期间记录画面，页面会这样说明。源头约每秒 10 帧，页面约每秒请求 10 次（2026-11-02 前是 4 次，看起来一卡一卡）（每台相机同时只有一个请求，
  且只在运行进行中、标签页可见时请求），所以画面可能晚零点几秒；画面下方写着它的年龄和片段文件夹。下一张缺失时，上一张保留 3 秒。
  客户端的视频写入器（OpenCV 经 FFmpeg）按 256 KiB 一块写盘，文件一次长几帧：画面是一跳一跳更新的，按这台机器的帧大小约每 0.3–0.9 秒一跳，
  也可能比相机落后这么久；显示的年龄是最后写入那一块的时间，每个片段的第一张画面要等第一块写盘后才出现（之前页面会说片段还没有画面）。
  数据来自 2026-11-01 审查时用 `openpi/.venv` 里的 cv2 4.11 实测。
- 页面的代理（`src/utils/backendProxy.ts`）除了原来就转发的内容类型和缓存策略，现在也转发这两个 `X-Levi-Frame-*` 响应头。

### 两次运行之间的相机预览

本页没有客户端在运行时，实时画面卡片上有一个**打开相机预览**按钮。它让 LEVI 自己打开两个相机，于是两次运行之间（以及启动之前）也能看到两张画面；**停止**（同一个按钮）放开相机。
客户端停在开始闸门时显示画面不在这次范围内：那时相机归客户端。

- **工作进程。** `POST /preview/start` 启动 `integrations/camera_preview/worker.py`（`levi/marvel0/preview.py`），它是自己的进程组，登记在 `levi/children.py`
  （类别 `camera-preview`；服务停止时和其他子进程一起被停掉）。LEVI 的环境里没有 `pyrealsense2`，所以按 AERI 终端 D 取得它的办法运行：
  在 openpi 检出目录下执行 `nice -n 19 uv run --no-sync --with pyrealsense2==2.55.1.6486 python -B worker.py --out <目录> --camera side=<序列号> --camera wrist=<序列号> ...`
  （该环境里有 numpy 和 OpenCV），`UV_OFFLINE=1`，环境是托管客户端用的白名单（没有 `LEVI_*`、没有令牌、不设 `UV_CACHE_DIR`：uv 的缓存和托管客户端用的是同一处）。
  序列号取自站点文件（`[cameras] view` 是 “side” 画面，`wrist` 是腕部画面），换相机只需改那里。它以 640x480、每秒 15 帧打开两路彩色流，每台相机每秒最多编码 15 张 JPEG，
  写到 `<工作区状态目录>/marvel0/camera_preview/` 下的 `side.jpg` 和 `wrist.jpg`（先写临时名再改名，读取方不会看到半张图）。
  `GET /preview/frame?camera=side|wrist` 返回最新的一张（`image/jpeg`、`no-store`、`X-Levi-Frame-Age-Ms`），没有时返回 `404`，原因为 `preview_off` 或 `no_frame`。
  `GET /preview` 是状态：`off`、`starting`、`on`、`stopping`、`refused`（带原因）或 `failed`（带原因），以及每张画面的年龄和各项时间限制。
- **什么时候拒绝**（`409`，回答里带原因，卡片把原因写在被禁用的按钮旁边；显示的是第一个适用的）：这个 LEVI 不是产品版（`not_product`：只有主检出目录里的
  `levi-product.service` 可以打开真实相机，和托管运行的规则相同，这样工作树或临时实例就不会在产品的交还看不到的地方占着相机）；本页托管的评测运行在进行（`run_active`）；
  本页的 AERI 运行在进行（`aeri_active`）；有客户端连着策略端口（`client_connected`，读部署状态里的 `listener.established`，和语音输入的闸门读法相同）；
  机械臂锁被占用（`arm_in_use`）；或者以上任何一项读不到（`state_unreadable`：未知按“在用”算；部署状态的 state 为 `unknown`、或套接字表读不到
  （部署此时报告 0 个连接）、或托管运行的记录读不到、或其启动器已不在却从未被结算（重启后第一次读取失败时，运行监督器自己把这两种都当作“没有运行”），都算读不到）。另有 `client_starting`（正在启动客户端，包括启动发生在读取阻挡项期间的情况）
  和 `preview_stopping`。唯一不是 `409` 的拒绝是 `preview_unavailable`（`503`：缺工作进程文件、openpi 检出目录、客户端环境或状态目录）。
  两个写操作和其他写路由一样只属于人（`person_only`，经代理要求来自本页自己的来源）。
- **客户端启动前先交还相机。** 每条启动客户端的路径都先停止预览，并等待（最多 6 秒）工作进程退出——退出才算相机空闲：托管评测的启动
  （`run.Supervisor._start`；更换指令后的重启会再次调用 `/run/start`，同样经过这里）和 AERI 终端 D 的启动（`aeri_run.Supervisor._start`）。
  LEVI 先关闭工作进程的输入管道（干净的路径），再发 SIGTERM，满 5 秒仍未退出才发 SIGKILL；用了 SIGKILL 的交还会写进 LEVI 的日志，在 `GET /preview` 里显示为 `killed`
  （直到下一次预览），并在启动客户端前再等半秒。这次启动答复之前，不能开始新的预览：交还会被计数，所以读取阻挡项期间发生了客户端启动的预览启动会被拒绝（`client_starting`）。
  工作进程没能及时退出时，启动被拒绝，原因 `camera_preview_busy`，什么也不启动，而不是启动一个会因相机被占用而失败的客户端；卡片此时请人等实时画面那里显示相机已空闲。
  更早就被拒绝的启动（比如令牌不对）不动预览。工作进程把每台相机 `pipeline.stop()` 的耗时和整个退出的耗时写进它的日志（预览目录下的 `worker.log`），每次运行都留下一次测量。
- **它会自己停止：** 最后一次请求画面之后 20 秒（页面关了）、启动之后 10 分钟（这样它不会长时间占着相机，妨碍有人在终端里启动的客户端）、LEVI 停止时，
  以及在启动或运行期间看到客户端时：托管运行、AERI 运行、机械臂锁或策略连接（第一次查看在启动后约 1 秒，之后每秒一次）。连续两次独立读取都读不到状态也会停止，并有自己的提示（`state_unreadable`，“读不到是否有客户端在启动”）：第二次查看先丢弃部署状态 2 秒的缓存，所以一份坏的快照不会被算两次。
  监视线程在运行时，`GET /preview` 的读取只负责发现进程已退出；停止（要几秒，杀掉则更久）由监视线程来做。
  工作进程的输入管道一关闭（LEVI 死了），约 2 秒后放开相机（2026-11-02 用两台 D405 实测，从管道关闭到进程退出）。命令卡片（第 3 步）的“复制”按钮会停止正占用相机的预览，
  因为粘贴到终端里的命令是一个 LEVI 不负责启动的客户端。
- **失败时** 说明原因，并且不重试：`cameras_busy`（别的程序占着某个相机）、`cameras_not_found`（站点文件里的某个序列号没有连接）、
  `worker_unavailable`（没有 `uv`、uv 离线缓存里没有覆盖层，或工作进程导入失败）、`no_frames`（相机打开了但没有送来画面，或者不再送）、`worker_failed`。
  工作进程自己的退出码（10、11、13）原样传到页面：它用 `os._exit` 退出，并且不经缓冲读取器的锁读输入管道，因为它曾在每次失败和每次被信号停止时在退出处中止（退出码 -6）。
- **没有覆盖的。** 在终端里启动的客户端，在它取得机械臂锁（AERI 终端 D，`levi.automatic.fr3_run`，在加载模块和连接之前就取锁，所以在 `policy_swap` 模式下比它打开相机早几十秒）
  或连上策略端口时被看到。不取锁的客户端（手动运行的 openpi 客户端）只在连上策略端口时才被看到，这比它打开相机早一两秒。从被看到起，预览最多再用 1 秒（查看间隔）
  + 2 秒（部署状态缓存 2 秒）才开始停止，然后工作进程约 2 秒退出（要杀掉则最多 6 秒）：通常约 5 秒，最坏约 9 秒。所以不取锁的客户端仍可能遇到相机被占用，并在任何动作之前失败；
  保护它的是它自己的报错和 10 分钟的上限。只是用鼠标选中并手动复制命令，不会停止预览。

## 这些门的分量，以及这里什么都不是

**这些门只防误操作，防不了同一账号下的其他程序**：任何以这个用户运行的程序都能发出同样的请求（先要令牌再用令牌）、伪造页面的请求头、直接写客户端的键盘或给它的进程发信号。
令牌、短语和清单是被记录下来的点击，不是证明。**现在不需要再输入任何东西**：页面上的其他停止，以及机械臂栈、策略服务器和部署的启动，都是一个带危险提示和勾选项的确认弹窗，
短语由页面自己发送，后端仍然核对它。有两个操作是一次点击、不弹窗：启动托管客户端（这一下代表原来勾选框问的三项；短语和清单仍然发送并被核对），以及策略服务器上没有客户端连着时的停止（页面仍自己发送短语，后端仍然核对，有客户端连着时后端仍然拒绝）。仍然要手输的只有两个会切断正在使用的东西的覆盖：C 上有客户端连接时停止机器人服务器，以及有客户端连接时停止策略服务器。真正的屏障是 Franka Desk 和**物理急停；手要放在上面**。**页面上没有任何东西是急停**：保持只冻结客户端，不会让机械臂停下，
机械臂保持最后收到的目标；成功、失败、作废、退出和回车都会让机械臂不受任何限位检查地回位。agent 会话从不调用 `/run` 和 `/stack` 的写路由（在共享账号上这是规则，不是机制）。
写操作只在产品 LEVI（主检出里的 `levi-product.service`）中生效；其他实例只显示运行状态并拒绝写操作（`not_product`）。核心结束时伪终端的主端关闭，内核会让客户端挂断。

按决定不托管：无人值守或双标签模式的客户端（双标签需要 `levi-live`，它现在没有运行）、自己开始的下一个片段、由人以外的任何东西打的标签、逐步确认模式（保持是它的替代）、
任何输入到客户端里的文字。浏览器关闭不会让客户端停下：当前片段会跑到步数上限然后等待；保持和结束仍然可用。

## 已经接上的，以及仍交给其他文件所有者的

已接上：路由在 `levi/marvel0/api.py` 里（并列在 `tests/marvel0/test_marvel0_routes_mounted.py` 中，该测试还检查每个写路由都拒绝 Bearer 凭据）；
`run_api.py` 在被导入时把 `run_api.run_active` 注册给 `stack_act.register_run_probe`，所以有客户端在运行时，机械臂栈的启动和停止会被拒绝；
服务重启后仍活着的客户端，也会在这个询问发生时被接管（冻结），不必等有人打开运行卡片。

没有接上，交给拥有那些文件的人：`run_api.active_job()` 把正在运行的客户端给成一行作业（`kind marvel0-client`），供 `levi stop`（`levi/activity.py`）和策略部署的停止使用
（`levi/marvel0/deploy.py` 应在有客户端运行时拒绝停止，且不再接受覆盖短语）；`run_api.adopt_on_start()` 可以在服务的 lifespan 里调用
（`levi/service.py`，紧挨 `stack_api.start_auto()`）；目前没有任何地方调用这两个函数。

## 控制频率

命令卡片有一个**控制频率**（`--control-hz`）：只能是 5、10、20、25 或 50 Hz，默认 10。10 Hz 时不写这个参数，命令与此前卡片生成的逐字节相同
（`tests/marvel0/fixtures/command_golden_10hz.json` 在写这个选项之前从 main 取得）。其他值一律以 `control_hz_unsupported` 拒绝（字符串、浮点数、布尔值是 `invalid_type`）；
托管的运行启动的就是表单渲染出的那条命令，所以频率原样到达客户端。代码：`levi/marvel0/command.py`；测试：`tests/marvel0/test_marvel0_command_hz*.py`、
`src/components/marvel0/__tests__/control-logic.test.ts`、`command-panel-hz.test.tsx`。

**哪些随频率变，为什么**（客户端文件在 `openpi/examples/fr3_local/`，只读）：

| 参数或数值 | 规则 | 依据 |
| --- | --- | --- |
| `--max-step-m`、`--max-step-rad` | 10 Hz 及以下用站点文件的值；高于 10 Hz 除以 `频率 / 10`（20 Hz：0.05 米和 0.175 弧度，25 Hz：0.04 和 0.14，50 Hz：0.02 和 0.07）。上限对应的速度保持 1 米/秒和 3.5 弧度/秒；5 Hz 时是 0.5 米/秒和 1.75 弧度/秒 | `run_robotiq_client.py:704` 的 `raw_step` 是命令位姿与**实测**位姿的距离，`:728`、`:730` 在每一拍拿它与上限比较（循环周期 `:1506`）；被拒绝的动作块会丢掉、重新请求，并用掉预算里的一步（`:1585`）。所以每步上限就是速度上限：50 Hz 时 0.1 米的上限允许 5 米/秒 |
| `--rtc-execute-horizon`（留空时） | 10 Hz 及以下用站点的默认值；高于 10 Hz 取能给 10 个动作里留出 `ceil(3 * 频率 / 10)` 拍空位的最大值（20 Hz：4，25 Hz：2，50 Hz：1）；显式填的值不改 | `run_robotiq_client.py:305`“execute_horizon + d must stay <= 10”；RTC 延迟 `d` 是以拍计的延迟，`rtc_chunker.py:206` 为 `ceil(latency_s / period_s) + 1`，`period_s = 1 / control_hz`（`run_robotiq_client.py:904`）；本页的 3 拍就是 10 Hz 下的 0.3 秒（服务器往返 187 毫秒，`README_ROBOTIQ_ZH.md:236`） |
| RTC 余量警告 | 按所选频率的拍数：2（5 Hz）、3、6、8、15（50 Hz）；50 Hz 时连执行步数取 1 也装不下（`rtc_never_fits`：10 个动作的块只持续 200 毫秒，比延迟还短） | 同上 |
| 默认备注 | 其他频率时加上 ` hz=<频率>` | 备注写进评测日志；不另填数据分组时，各频率的片段会放在同一个分组里（每个片段的 `metadata.json` 记有标称频率，`run_robotiq_client_collect_data.py:147`，也记有实测频率） |

**哪些不变，为什么。** `--max-timesteps` 仍以步为单位，站点的上限（3000 步）也不改：任务需要多少个动作与频率无关（客户端自己的预算是 10 Hz 下 400 步，`run_robotiq_client.py:230-238`）。
表单会显示秒数（`步数 / 频率`；是“至少”，因为每次约 187 毫秒的推理会让循环停顿，而循环不会补回，`:1641`），并且在高于 10 Hz 且不足 30 秒时警告（`episode_short`），
30 秒约等于较慢任务示范在 10 Hz 下的第 90 百分位（28.7 秒，`:230-231`）。`--home-duration-s` 本来就是秒（`:941`：`int(home_duration_s * control_hz)` 步，每步 `1 / control_hz` 秒）。
`--post-step-verify-delay` 不传：客户端自己设为 0.0（`:222`；`RobotEnv` 默认的 0.2 秒会把循环锁在 4.8 Hz，`README_ROBOTIQ_ZH.md:215-231`），一步约 8 毫秒的 HTTP，装得进 50 Hz 的 20 毫秒。
`--min-z`、`--max-xy`、角度范围、夹爪设置（`fr3_env.py:383`，0.75 秒的节流，单位是秒）是长度、角度或时间，与频率无关。`--video-fps` 是 0，即用实测中位帧率。

**警告**（都不是拒绝），频率不是 10 Hz 时：`control_hz_not_trained`（策略是用 10 Hz 的数据训练的：`lerobot_fr3_filtered_robotiq_v3` 的 `meta/info.json` 写着 `"fps": 10`，
`src/openpi/training/config.py:1145` 的 `action_horizon=10`，`run_robotiq_client.py:294`；客户端 README 说调高频率“等于加速播放训练轨迹，会走出动作分布，也更容易触发 reflex”，`README_ROBOTIQ_ZH.md:233`）；
`control_hz_untested`（第一次使用会改变机械臂的速度曲线，在这台设备上没有试过：机主在场，手放在实体急停上；频率字段旁的说明里再说一遍）；`step_cap_tight`（缩放后的每步上限低于 10 Hz 训练步长的第 99 百分位）；`episode_short`。

**实测，只读**（2026-10-21，`lerobot_fr3_filtered_robotiq_v3` 每 6 个片段取 1 个：1497 个里的 250 个，30336 行）：训练动作与其对应状态的距离，也就是客户端每步检查用的量：中位数 0.0098 米，
第 90 百分位 0.0227，第 99 百分位 0.0384；旋转（三个包裹后角度差里最大的一个）0.0164、0.0445、0.0883 弧度。超过缩放后上限的行：20 Hz 为 0.11 %（米）和 0 %（弧度），25 Hz 为 0.77 % 和 0.06 %，
50 Hz 为 **14.32 %** 和 2.59 %。真实运行还要加上机械臂的跟踪滞后，所以占比会更高：50 Hz 时预计会有很多动作块被拒绝，这是联锁在起作用，不是故障。
`command.py` 里的常量 `TRAIN_STEP_P99_*` 就是这两个第 99 百分位。

**没有验证的。** 10 Hz 以外的频率从未在机械臂上运行过，客户端本身也没有运行：机械臂在 20、25、50 Hz 下会怎样（控制器能不能跟上每秒 50 个目标，循环能不能在相机和状态读取之外
守住 20 毫秒，缩放后的上限多久拒绝一次）、每个频率下真实的推理延迟拍数、以及策略在 10 Hz 之外到底表现如何，都不知道。参数拼写和本节依据的那些行由 `scripts/check_client_prompts.py` 检查
（12 条字符串：`control_hz_*`、`step_cap_*`、`loop_period`、`home_steps_by_seconds`、`rtc_period`、`rtc_delay_ticks`、`post_step_verify_zero`、`trained_at_10_fps`）。默认的数据分组在各个频率下相同。

## 已经运行过的，和没有运行过的

在开发机上、机械臂关闭时运行，从未连接任何机器人端口，从未运行真正的客户端：状态规则的单元测试（每个状态、客户端换了说法的文字、已经消失的客户端）、提供哪些动作、令牌、环境和命令；
脚本化客户端上的监管者（开始及每一种拒绝、按键以及到达键盘的字节、保持、结束、监视器的自动保持、重启后的接管、重放）；真实伪终端下的启动器和假客户端（完整的一次运行、误按的 `s`、
慢速保存期间的两次标签点击、保持与结束、核心已死的客户端）；变异检查（故意改坏代码，指定的测试必须失败）；以及对客户端源码的漂移检查。五个测试文件共 260 个测试（96 + 81 + 25 + 50 + 8，其中 25 个用真实进程，约 25 秒）；变异检查脚本 `tests/marvel0/mutate_run.py`（有机器人进程在运行时拒绝运行；`--check` 只校验清单）在临时副本里用 98 种方式改坏代码，每种只跑一个测试文件：第一轮有 7 种没被抓到（重启后的进程号检查、启动器自己对进程组的 SIGKILL、退出记录的权限、运行结束后的令牌、同一次监视器写入被数两次、手臂锁检查行、z 下限检查行），每种都补了测试，现在 98 种全部被抓到；
`scripts/check_client_prompts.py` 在 2026-10-21 找到了全部 49 条字符串（加上控制频率的 12 条，共 61 条）。重新生成前端读取的文件：`uv run --no-sync python tests/marvel0/regen_run_sample.py`
（`marvel0-run.samples.json`：各个状态下的 `GET /run`；`marvel0-run.answers.json`：其他路由的回答和各种拒绝）。

**没有在真机上验证的：** 相机预览的其余部分。2026-11-02 用两台 D405 做过一次工作进程的冒烟测试（手动运行，不是自动测试）：两台相机经 `uv run --no-sync --with pyrealsense2` 打开，JPEG 写出，输入管道关闭后进程约 2 秒退出，设备随即可以再次打开。仍然没有测过：只靠 SIGTERM 的停止的退出耗时（LEVI 自己的停止先关闭管道、随即再发 SIGTERM，即上面测过的管道关闭路径再加一个信号；只发 SIGTERM 的是 `children.stop_owned` 和回收；而且测过的工作进程文件是把停止耗时写进 `worker.log` 之前的那一份，所以当前这份文件在真实相机上的第一次运行才会第一次记下它们）、被占用的 D405 对 `pipeline.start` 怎么回答、以及 D405 在 `pipeline.stop()` 中途被杀。测试用替身库运行真正的 `worker.py`，断言退出码为 0 且远早于 SIGKILL 的时间点，但不含 librealsense 的耗时。`systemd-run --user --scope` 本身（pid 是否就是启动器的、进程组是否在产品重启后存活、scope 的停止行为）；启动器下的真实客户端（环境白名单下 `uv` 的离线运行、
`uv` 链上的信号处理、SIGHUP 能否到达 `python`、没有显示器时的 `--no-display-images`）；SIGSTOP 持续几分钟的后果（相机、策略服务器的 websocket、控制器保持最后目标）；
保存、回位的真实耗时和 1 秒的会话心跳（页面的阈值在测量之前只是猜测）；“两次写入”规则所依赖的真实监视器写入频率；真实客户端的措辞
（只对照过源文件：每天第一次运行前执行 `python3 scripts/check_client_prompts.py`）；挂断的伪终端在真实客户端看来是什么样（Linux 对读取返回 EIO；假客户端取更坏的读法，继续运行）。
第一次真实使用按复审的顺序：先开始到闸门再按 `q`（不动），再一个片段（回车和 `s`），再 `f`，再保持与结束，再在步数上限提示处按 `d`，全程所有者在机械臂旁、手放在急停上。
