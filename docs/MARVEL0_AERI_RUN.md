# The AERI terminal D buttons of the marvel0 page

English first, Chinese after the line below. Backend: `levi/marvel0/aeri_run.py` (the supervisor: start, keys, Stop, End,
adoption), `aeri_state.py` (the state rules and the closed key table), `aeri_screen.py` and `aeri_journal.py` (what
terminal D shows and keeps), `aeri_run_api.py` (the routes), `hostchild.py` (the launcher, AERI mode). The words of
terminal D per code version: `levi/marvel0/aeri_prompts.json`. Tests: `tests/marvel0/test_marvel0_aeri_*.py`, the fake
terminal D `tests/marvel0/fake_aeri.py`, the mutation checks `tests/marvel0/mutate_aeri.py` and
`tests/marvel0/mutate_aeri_run_fixes.py` (the fixes of the two reviews of the first build), `tests/marvel0/mutate_aeri_run_second.py`
(the fixes of the second reviews, 53 mutations) and the drift check
`scripts/check_aeri_prompts.py`. The preparation and readiness of a run are `docs/MARVEL0_AERI_PREP.md`. Design record:
`LEVI/lab/levi-hub/programs/rig-console/reviews/aeri-page-design-20261021.md` (its challenge log wins where the pieces
differ).

## What the buttons do

The page can start **terminal D** (`aeri.py execute`, the real-robot run of AERI's subtask loop) for a run it prepared,
and press the keys a person presses in it. **A click is a key of terminal D, nothing more**: the page types into the
same terminal the same bytes. It never presses a key by itself. Which run, which code, which plan: exactly what
`RUN.json` prints for a terminal; the page builds the argv itself and refuses to start unless it reads **byte for byte**
the printed command. A terminal remains a valid way to start a new run; a run hosted by the page cannot be taken over by
one (the pty's master is the page's).

- **Which AERI code.** The site file's `[aeri]` section pins a worktree (`pin_sha`, a prefix of its HEAD, and a clean
  tree) and a **prompt profile**: terminal D's words and key tables for one code version (`aeri_prompts.json`, copied from
  `console_text.py` and `console.py` by `tests/marvel0/regen_aeri_prompts.py`, never typed). The profile also records the
  sha256 of the four source files it was written from. A worktree whose files differ is **not hosted** (`profile_unknown`):
  the page does not guess what a changed sentence means. The HEAD must also start with the profile's own commit
  (`profile_unknown`, `head_not_profile_commit`): the profile hashes four files, but the journal the page reads is written
  by others too (`orchestrator.py`, `journal.py`, `domain/aeri.py`), and a clean tree at that very commit pins all of the
  worktree at once, which a longer list of hashes would not. Profiles: `harvest-5bc750ca` (the default, the code of the real
  run of 18 Oct), `bfdee2cf` (the pin of AERI.md; it has no startup home, no forward progress line, no reverse `m` hint)
  and `main-297a74c3` (main's code, with the `camera_stale` wait). `scripts/check_aeri_prompts.py --worktree <dir>` says
  **what** changed when a worktree differs.
- **Start** (`POST /aeri/run/start`). A token first (`/aeri/run/challenge`, bound to the run name and the readiness digest,
  30 s, once), then the phrase `start aeri <name>`, the checklist (`estop`, `scene_ok`, `area_clear`, `arm_home`) and a
  fresh `request_id`. Refused when: the control file does not enable AERI hosting (`[aeri] enabled`, off when missing),
  not the product LEVI, a hosted D or the hosted client is alive, the run is not prepared or its files changed, the code
  pin or the profile does not fit, the name is in a registry or another terminal drives it (`terminal_d.in`, an unfinished
  `terminal_d.events`), or **a readiness row that does not pass**: `ok` passes; `warn` passes only for the rows the card lists as warning rows
  (`aeri_ready.WARN_OK`: `stack_ready`, which is `warn` whenever the arm stack is up, because the gripper stack and the
  controller cannot be seen from here); `refuse`, `unknown` and a `warn` of any other row do not, and the card's own
  `ready` must be `true`. **Nor does it start a D that `levi stop` could not list** (`start_failed`, `why`
  `not_listed_by_levi_stop`; `aeri_activity.hosted_visible_to_activity`: at the token and at the start the wiring is
  checked, and after the launch the real list of jobs must name this run, or the D is sent Ctrl+C at its phrase): a D that
  is not in `activity.running_jobs()` is cut short by the hang-up chain at the R6 restart. D runs in `hostchild.py` inside its own
  `systemd-run --user --scope` (`env -i` with an allowlist: no `LEVI_*` but the console language, no Hugging Face token, no
  ROS, no display; the user bus variables are there because D starts its VLM and policy units with `systemd-run --user`).
  D holds the arm lock and the GPU lock itself, after the phrase: **the page never holds the arm lock**.
- **Keys.** Only these bytes are ever written to the keyboard: Enter (`\r`), `y n q`, `o p h`, `s f d`, the phrase line
  `execute <name>`, and Ctrl+C. Never `\x04` (D reads it as `q`), Escape, a digit, a capital, `m`, free text.

  | State | D waits for | Keys offered |
  | --- | --- | --- |
  | `await_phrase` | the line `execute <name>` | `execute` (typed phrase) |
  | `await_phrase_variant` | `execute <name> after policy switch check` (an unfinished switch log) | `execute_variant` only |
  | `await_home_o` / `_p` / `_h` | the startup home: `o` (object handled), `p` (path clear), `h` (authorise: the arm moves, the gripper opens) | the one key, `q`; three clicks, three tokens, three delays |
  | `await_inventory` | the VLM's object list | `y` (freeze as whitelist), `n` (wait for a person), `q`; never Enter |
  | `await_release` | Enter to release round N, asked where D's loop is idle: at `VERIFY_INITIAL`, after the pose check and the confirmed list (before round 1 and after each reverse loop) | `release_round` (the arm moves), `q` |
  | `forward_running` | nothing; `s f d` end the round | `verdict_success/failure/discard`, **once per round** |
  | `closing`, `reverse_running`, `busy`, `services`, `launching` | nothing (`busy` includes D waiting for the VLM at `SCENE_ASSESS` in the reverse loop: no prompt comes there, and an external answer takes up to 540 s) | none (Stop) |
  | `await_resume` | Enter (some waits ask a second time) | `resume_ask` (level B whichever wait it is: which variant a wait is rests on an inference, so the asking Enter has the delay, the gap and the presence check too), `q` |
  | `await_resume_confirm` | `y` (Enter is ignored) | `resume_confirm`, `resume_cancel`, `q` |
  | `stopping`, `stalled`, `fault_locked`, `orphaned`, `unknown`, `ended` | nothing | none (Stop: the one retry in `stopping`, `stalled` and `orphaned` when D has not answered); End in `stalled`, and after the retry when D never answered |

  **Three sources must agree for a second before a key is offered** (`SETTLE_S`): D's journal says it waits for that kind of
  answer, D's screen shows that prompt as its **last line of its own** (lines of other programs, `Homing over ...`,
  `server config:`, `uv` warnings, and lines nobody knows, do not close a prompt; a line D owns does), and the pty is in
  the mode the prompt needs (single keys: not canonical; the phrase: canonical). A sentence that changed, a journal list
  that does not match the screen's object table, a heartbeat that stopped while D lives, an unreadable termios: the state
  is `unknown`, the only button is Stop. A key that just appeared is greyed for `ARM_DELAY_S`, the next one waits
  `KEY_GAP_S` after the last was seen taken; a key that moves the arm needs a tab that reported within `PRESENCE_S` and a
  fresh green health monitor. Two keys in a row sit in different places (`slot`): D flushes the keys typed before it asks
  the second step, so a double click must not be able to walk through both.
- **One key per prompt.** The request names what the page saw (`seen`: run, state sequence, prompt id) and carries the
  key's token; the server **re-reads all sources**, compares, and refuses a stale view (`stale_page`) before writing. The
  intent is written (and synced) before the byte; after the byte, a key that is **not seen taken** (a line of D's screen
  or a note of its journal, 5 s) locks that prompt for good (`key_unconfirmed`): it is **never sent again**. Only Stop
  and reading the screen remain. A crash after the intent loses a key and never doubles one.
- **The ticks are held to on the server** (`attest`, second safety review N6). The window of a key that needs a person's check
  shows one tick per check and the page sends their codes in `attest` (`KeyBody` and `EndBody`; a closed list of words, any other
  word is a 422). `aeri_state.KEY_ATTEST` says which codes each key needs: `execute` the five that terminal D's own start
  warning names (`scene`, `limits`, `checkpoints`, `providers`, `hardware`) and the three of every window of the arm stack
  (`estop_hand`, `hands_clear`, `no_client`); `execute_variant` the four of the unclean policy switch (`switch_units`,
  `switch_ports`, `switch_gpu`, `switch_home`) before those; `end` the three of the stack windows; `release_round` the one
  terminal D asks for the round (`scene_ready`: "when the scene is ready press Enter"); every other key none, and then
  `attest` must be empty. The server refuses (`attest_incomplete`, 400, with `missing` and `unexpected`) unless the codes
  sent are **exactly** those, none twice; the order is the page's. It is looked at after the state and the token (a stale page
  is still `stale_page`) and before anything is written. **This stops a mistake, not another program of the account**: the
  same list can be sent by any program running as this user (the owner's decision: no typed phrase, ticks and delay).
  `tests/marvel0/fixtures/aeri_codes.json` carries the table as `key_attest`.
- **Stop** (`POST /aeri/run/stop`). Ctrl+C: the terminal turns the byte into SIGINT for D's foreground group; D stops
  sending motion, holds the arm where it is, says so, and shuts its services down (VLM and policy units, the GPU lock),
  which takes up to two minutes. **The arm stays where it stopped; it does not go home.** Not in the single flight, not
  waiting for the run lock, not depending on a fresh document. **Once per run** (one retry if D did not acknowledge it
  within 5 s and its handlers exist): a second Ctrl+C during the wrap-up kills the `systemctl` calls D stops its units
  with and leaves the units running (this is tested with real processes). If the pty cannot carry it, SIGINT goes to the
  pid in D's heartbeat when its identity matches, then to the terminal's foreground group. Four rules came out of the
  reviews of the first build, five more out of the second reviews (below them):
  - **A Stop sent from elsewhere counts.** The page reads the record on disk at every look (the out-of-page script
    writes `stop_sent_epoch` there) and takes D's own `interrupt` line (or the journal's `operator_interrupt` note) as the
    acknowledgement **whoever sent the signal**: this page, the script, a terminal's Ctrl+C, `kill`. It never offers Stop
    again into a wrap-up that has begun. A later time in the record (`aeri_page_stop.py --again`) means the retry was
    used too. The page writes the record **merged** into what is on disk (`record.update_record`), never the whole
    in-memory record back.
  - **A retry never goes by the pty again.** If the first Ctrl+C byte did not become a SIGINT for D, the second would not
    either: the retry goes straight to the pid in D's heartbeat (identity checked), then to the foreground group.
  - **Stop does not need the site file or the derivation of the state.** The values a live run reads from the site file
    (`rollout_root`, `runs_root`, `console_lang`, `gpu_lock_file`, the source-data roots) are copied into the supervisor
    and the run record **when the run starts** and used for as long as it lives; a site file or `[aeri]` section edited
    into something invalid in the middle of a run changes nothing (the state does not turn `ended`, Stop and the keys stay).
    And when anything about the derivation of the state fails, Ctrl+C still goes out, from the record in memory alone, to
    D's checked pid (once).
  - **The words follow the phase.** `stop.phase`: `sent`, `unconfirmed` and `retried` mean "sent, not confirmed; the arm
    may still move" (`state.may_move` is `true` for `stopping`/`stalled`/`orphaned` meanwhile); only `acked` means "winding
    down, the arm stays where it is" (`may_move` `false`).
  - **The retry is there in every wrap-up D has not answered** (N1). It was offered only while `stopping`; an adopted D is
    `orphaned` and a silent one turns `stalled`, and there `stop.allowed` was `false` with `stop_spent` for a Ctrl+C that
    never got an answer. Now `aeri_state.RETRY_STATES` (`stopping`, `stalled`, `orphaned`) all offer it once, when the phase
    is `unconfirmed` (5 s after the first signal, D's heartbeat seen, no `interrupt` line). For an adopted D (no keyboard) it
    goes to the identity-checked pid of D's heartbeat; if that cannot be had the retry answers `signal_failed` and is **not**
    used up.
  - **The record says "sent" before the signal, under its lock** (N13), as the script does, and puts the old values back when
    the signal cannot be sent. Both the write and the restore, and what the script wrote since the page looked, are read and
    written under the record's `flock`, which the script takes too (and looks again inside it): a script run in the
    millisecond between the page's signal and its write can no longer double-fire. The lock is tried for 1 s
    (`RECORD_LOCK_WAIT_S`); if someone else holds it longer the signal goes out all the same and the disk is left alone
    (a Ctrl+C never waits). The page's retry also writes `stop_retry_epoch`, the time End counts from.
  - **A live run the page cannot follow is not another core's** (N12). A record written by the first build has no `site`
    snapshot; with a site file that does not read, the run is seen alive, not followed (`unknown`, `no_keyboard`, no keys),
    and Stop is **offered and sent** (`stop.allowed`, to D's checked pid); the document used to say `driven_elsewhere`
    ("other core") while the backend would have sent.
  - **A key does not outlive the switch** (N3). `[aeri] enabled` of the control file is read at every look, not only at the
    start: when it is switched off during a run, every key of the open prompt is `allowed: false` with `why_not`
    `aeri_disabled`, and a press is refused with `aeri_disabled`, whatever token it holds. Stop and End are not keys and stay.
    The document's `enabled` is `false` meanwhile.
- **End** (`POST /aeri/run/end`, phrase `end terminal D`, with the three ticks of `attest`). Only when the wrap-up is stuck
  (`stalled`: Stop was sent, or D locked or completed, and nothing new for 150 s; 600 s while an external VLM answer is
  awaited), **or when D never answered**: the retry was used (`stop.phase` `retried`, no `interrupt` line from D or its
  journal) and `END_AFTER_RETRY_S` = 60 s have passed since it (the age of the last Ctrl+C: the page's retry, or a later time
  the script wrote). The second case exists because a D that goes on printing steps never stalls (a step is progress), so
  with `stalled` alone a D that ignored two signals had no End for ever. Why 60 s: D's handler prints its acknowledgement
  in its main thread within a step or two of a SIGINT (also while it waits for the VLM), so 5 s without one is already late
  (`STOP_ACK_S`) and a minute after the retry, with the arm possibly still moving, is long past a D that is merely slow; and
  the retry comes first, so End is never the first thing offered. Never for a run another core owns. SIGKILL of the launcher's
  group and of the group D is in. The VLM and policy units stay behind.
- **After a restart of the product.** The pty's master closes: the kernel hangs the launcher's terminal up and
  `hostchild.py` sends **SIGINT** to the groups of the client, **of terminal D itself** (from its heartbeat file, which
  `spec["runner_json"]` names; the pid in it counts only while it is the process the file describes and a descendant of
  the client, and D is remembered while it is, so a `uv` that ends on the SIGINT does not hide D) and its own,
  **SIGKILL 3 s later** (user decision 2026-10-21); while D lives after the client ended, the launcher stays until the
  grace has killed it. On a normal restart nothing of this happens (the product refuses to stop while D lives). A D that is
  found alive by a new process (the chain failed) has no keyboard: it gets **one** SIGINT (not when it has already
  answered a Ctrl+C or the record says one was sent), is `orphaned`, and is watched until it ends; the page never freezes
  it (a SIGSTOP would freeze its heartbeat thread) and never takes the arm lock. Rules of the adoption:
  - **Latched only after a read that worked.** A site or record that could not be read this time is read again the next
    time; one failure never hides a live D from `active()` (`levi stop`, the deploy, the arm stack) for the life of the
    process. A record that names no run is a result; one that cannot be read is not. The run is found without the site
    file (the record carries the values the run reads).
  - **A record that cannot be read is a D that may be running** (second safety review N8). `levi stop` already listed it
    (`record-unreadable`), but `aeri_run_api.active()` said "none" and `run_active()` `false`, so E's start, the deploy and the
    arm stack's start and stop went ahead. Now `active()` answers a row `{kind: marvel0-aeri, id: record-unreadable,
    unreadable: true}` for as long as the read fails, which every one of those gates counts as a D (the deploy names it in
    `aeri_run_live`); the document's `start.why_not` is `d_active` with `params.record: "unreadable"`; a start is refused
    (`d_active`, `record: unreadable`). Nothing is signalled on a record nobody can read. A person repairs or removes the
    file: the next look finds the run, or finds none. When the page is down, `aeri_page_stop.py --scope` is the way to D (below).
  - **One failed read of `/proc` does not end a run** (N7 = mechanics N1). The own child is judged by `poll()` alone; a launcher
    found again is gone only when two reads of `/proc` 0.1 s apart both miss (`MISS_RECHECK_S`), or when its pid answers as
    another process (a reused pid). `ended` closes the pty and the hang-up chain would cut a live D.
  - **The owner comes first, and a launcher is alive only on proof** (mechanics N4). A record in `intent` (the owner is between
    writing it and spawning) read by a second core is not marked ended and gets no `terminal D exited` line; a record whose
    launcher has no identity on record proves nothing and is never signalled.
  - **A D that is already gone is over, and says so.** Its `terminal D exited rc=` line is written to `terminal_d.events`
    (`rc=unknown` when nobody knows), because the launcher that would have written it was killed with the chain and
    `wait_request.sh` and `res_monitor.py` wait for it. Whether a terminal still drives a run name is decided by the
    **last** `start:` / `terminal D exited` line of that file, not by either appearing anywhere in it.
  - **The record names its owner** (the core's pid and identity). A second core that shares the folder does not signal,
    drive or end a D that a live core owns: it only watches it (`unknown`, `no_keyboard`, Stop refused with
    `driven_elsewhere`), still counts it as a running job, and takes it over (one SIGINT, as above) when the owner is gone.
- **Records.** `<workspace>/marvel0/aeri/`: `record.json` (new fields `stop_retry_epoch`), `requests/`, `history.jsonl` (time, person, key, byte, state,
  prompt id, round; **no screen text, no path**) and `logs/`. D's screen is `<runs>/<name>/terminal_d.typescript`
  (written by the launcher, 0600), its events `terminal_d.events` (`start:`, `terminal D exited rc=`), for the AERI tools.
  The document never carries a path or the VLM's judgement (`subtask_check`, plans, retries stay in the journal: a person
  who sees them before labelling is no longer blind).

## What the gates are worth, and what nothing here is

- **Nothing here is an emergency stop.** The physical emergency stop and Franka Desk are. Stop is Ctrl+C: it stops the
  program that sends motion; it does not cut the arm's power or brake it.
- **The gates stop mistakes, not another program of the same account.** Anything running as this user can send the same
  requests, write the same pty, edit the same files or signal the same process. The page's keys and the terminal's are
  indistinguishable in AERI's logs; only `history.jsonl` tells them apart.
- The AERI inbox's `resume` still works on a hosted D (it is a command file): the page notices (`inbox_resume`) and cannot
  stop it.
- **First use is staged with the owner present, a hand on the emergency stop.** Stage 1: no motion (home by hand, `q` at
  the object list). Stage 2: one round, no labels. Stage 3: several rounds with `s f d`. Abort on `key_unconfirmed`,
  on `unknown` while the arm may move, on a Stop without an `interrupt` line in 5 s, on a second D or client.
- All the constants (`SETTLE_S`, `ARM_DELAY_S`, `KEY_GAP_S`, `STOP_ACK_S`, `RUNNER_STALE_S`, `STALL_S`, `PRESENCE_S`) are
  guesses until stage 1 measures them (`aeri_state.py`, one place).

## The page is down, D is alive

`python3 -I scripts/aeri_page_stop.py` (from the product checkout; `--workspace` otherwise): one SIGINT to the pid in D's
heartbeat after checking it is the same process (start time and boot id), else to the launcher's group; it writes it in
the record **before** the signal (and puts the old value back if the signal cannot be sent), so that the running page, or one
restarted later, knows a Stop was sent and offers none of its own. The record is written and looked at again under the record's
lock (the page's Stop takes it too), so a page Stop and a script run at the same moment cannot both send. It opens no terminal or socket and imports nothing of LEVI.
A run hosted by the page has **no terminal D to fall back to**: this script and the emergency stop are the ways out.

**When the record cannot be read** the script says so and stops (no name, no heartbeat, and it cannot tell whether a Stop was sent).
`--scope` is the way on, for that case only (it refuses when the record can be read): it looks in the user's own cgroup tree
(`/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/**`) for the scope the page started D in
(`levi-aeri-termd-marvel0-<run>.scope`; more than one is refused), takes the one process in it whose command line is
`python ... aeri.py execute` (not `uv`, whose command line holds the same words) and sends it SIGINT, or else the launcher
(`hostchild.py`) that leads its process group. It runs no program and asks no service; it reads cgroup files and `/proc` of
processes of your own user. It writes nothing: **the page will not know of this Stop and nothing guards against a second one**,
so make sure none was sent. Why this route and not one from the record: the record is exactly what cannot be read; the heartbeat
file is named by it; the scope name is the one thing the page chose that survives it.

**After a hang-up** (the chain's SIGKILL leaves no time for D's 7/7): the VLM and policy units may be left, and a policy
switch log unfinished. The next start then asks for the variant phrase (`execute_variant`), which the page offers only
beside the passive checks of units, ports and logs. The readiness card's leftover-units step stops only units named
`levi-aeri-vlm-*` and `levi-aeri-policy-*` (A2).

## Wiring, and what the lead adds (one line each)

`api.py`: join `aeri_run_api.router` (eight routes under `/api/levi/marvel0/aeri/run`). `service.py`: `aeri_run_api.adopt_on_start()`
next to the stack's start; on shutdown a Stop and a wait for the 7/7 (130 s) before the hang-up chain. `activity.py`:
`aeri_run_api.active_job()` among the running jobs, so `levi stop` refuses while D lives. `run_api.run_active()` (done here)
already includes D, so the arm stack's start and stop and the client's start refuse meanwhile. The control file's
`[aeri] enabled = false` (A2) is the switch; the owner turns it on in person.

## Restarting the product while D lives (the unit's timeout)

The orderly shutdown (`aeri_activity.shutdown_hosted`) sends D the Stop and waits up to **130 s** for its 7/7 wrap-up. The
user unit `levi-product.service` must give it that time: **`TimeoutStopSec` must be at least 150 s** (the wait, the
launcher's 3 s grace and the rest of the service's own shutdown). With the old 15 s, systemd killed the core 15 s into the
wait, the pty closed, the launcher's chain sent a second SIGINT into the wrap-up and SIGKILL 3 s later: D's 7/7 was cut and
the VLM and policy units stayed behind. The sanctioned first step is **`levi stop`, which refuses while D lives** (wait for
the run, or press Stop on the page). A direct `systemctl --user stop` or `restart` with D alive **cuts D's 7/7 wrap-up** unless
the unit has the 150 s; the readiness card has a `warn` row `unit_timeout` that says so (it reads
`systemctl --user show levi-product.service --property=LoadState,TimeoutStopUSec`, cached for a minute; it never refuses). A core
that does not own the run (another live core's) returns at once from the shutdown and says so: it holds no pty of it, so no
hang-up chain follows from it.

## What was run, and what was not

Run, offline: the state rules on the **real recording of 18 Oct** (r08: its journal, screen, service steps and heartbeat),
the key table, the supervisor on a scripted machine (every byte the page writes read back), the launcher and the fake
terminal D under real ptys (a whole run with every key, Stop mid-round, a second Ctrl+C, a D that ignores Stop and is ended,
a heartbeat that stops, a reworded prompt, lines of other programs, a restart that finds D alive, a core that dies: SIGINT,
then SIGKILL after the grace), the mutation checks (each gate removed in turn must fail a test), the drift check on the three
code versions; the **whole recording of 18 Oct replayed** (the journal cut after each of its 97 lines, with the screen D had
printed by then, looked at again up to 560 s later, through the state rules and through the supervisor: never `unknown` while D
waits), the start gate against the real readiness rows of the card (`aeri_world.World`), a Stop from the script, a site file
that breaks in the middle of a run, adoption with a failing read, a dead D and a second core. **Not run: the real terminal D, the real arm, `uv run` of the real AERI under this environment** (does
`UV_OFFLINE=1` find the pyrealsense2 overlay? does the allowlist environment start D?), `systemd-run --scope` for the
launcher on this machine (the rehearsal `tests/marvel0/rehearse_aeri.py` does, with stand-ins), and the zh screen of the pin
`bfdee2cf` (no recording exists). Measured here with real ptys: on a master close the kernel hangs up the session leader
only, so SIGINT reaches D before any SIGHUP.

---

# AERI 终端 D 按键

先英文，后中文（上面一条线之后是英文，下面是中文）。后端：`levi/marvel0/aeri_run.py`（托管：启动、按键、停止、强制结束、重启后接管）、
`aeri_state.py`（状态规则与封闭的按键表）、`aeri_screen.py`、`aeri_journal.py`（终端 D 显示与保存的东西）、`aeri_run_api.py`（路由）、
`hostchild.py`（启动器，AERI 模式）。各代码版本的终端用词：`levi/marvel0/aeri_prompts.json`。测试：`tests/marvel0/test_marvel0_aeri_*.py`，
假终端 D `tests/marvel0/fake_aeri.py`，变异检查 `tests/marvel0/mutate_aeri.py`、`tests/marvel0/mutate_aeri_run_fixes.py`（首版两份审查所要求的修复）与 `tests/marvel0/mutate_aeri_run_second.py`（第二轮审查的修复，53 个变异），漂移检查 `scripts/check_aeri_prompts.py`。运行的准备与就绪
见 `docs/MARVEL0_AERI_PREP.md`。设计记录：`LEVI/lab/levi-hub/programs/rig-console/reviews/aeri-page-design-20261021.md`（两处不一致时以其挑战记录为准）。

## 按键做什么

页面可以为自己准备好的运行启动**终端 D**（`aeri.py execute`，AERI 子任务循环的真机运行），并替人按终端里的键。**一次点击就是终端 D 的一个键，仅此而已**：页面
向同一个终端键入同样的字节，从不自己按键。运行哪一个、哪份代码、哪份计划：就是 `RUN.json` 为终端打印的那一条；页面自己拼命令行，只有与打印的命令
**逐字节相同**才启动。用终端启动新的运行仍然有效；由页面托管的运行不能被终端接管（pty 主端在页面手里）。

- **用哪份 AERI 代码。** 站点文件的 `[aeri]` 段钉住一个工作树（`pin_sha` 是其 HEAD 的前缀，且工作树干净）和一个**提示词档案**：某个代码版本里终端 D 的
  用词与按键表（`aeri_prompts.json`，由 `tests/marvel0/regen_aeri_prompts.py` 从 `console_text.py`、`console.py` 复制，不手敲），并记录它所依据的
  四个源文件的 sha256。文件不同的工作树**不托管**（`profile_unknown`）：页面不猜改了的句子是什么意思。HEAD 还必须以档案自己的提交开头（`profile_unknown`，`head_not_profile_commit`）：档案只对四个文件做哈希，
  而页面读取的日志还有别的代码在写（`orchestrator.py`、`journal.py`、`domain/aeri.py`）；干净的工作树停在那个提交上，就一次钉住了整个工作树，这是更长的哈希清单做不到的。档案：`harvest-5bc750ca`（默认，10-18 真机运行
  用的代码）、`bfdee2cf`（AERI.md 的固定版本，没有启动回位、没有正向进度行、没有逆向 `m` 提示）、`main-297a74c3`（main 的代码，多一个 `camera_stale`
  等待）。工作树不同时，`scripts/check_aeri_prompts.py --worktree <目录>` 会说出**改了什么**。
- **启动**（`POST /aeri/run/start`）。先取令牌（`/aeri/run/challenge`，绑定运行名与就绪摘要，30 秒，一次），再带短语 `start aeri <名>`、清单
  （`estop`、`scene_ok`、`area_clear`、`arm_home`）和新的 `request_id`。以下情况拒绝：控制文件没有开启 AERI 托管（`[aeri] enabled`，缺省关闭）、
  不是产品 LEVI、已有托管的 D 或托管的评测客户端在运行、运行没有准备或其文件被改、代码固定提交或档案不符、运行名已在登记表里或别的终端在驱动
  （`terminal_d.in`、未结束的 `terminal_d.events`）、**任一就绪行不通过**：`ok` 通过；`warn` 只对就绪卡列为警告行的行通过（`aeri_ready.WARN_OK`：`stack_ready`，臂栈在线时它总是 `warn`，因为夹爪栈与控制器从这里看不到）；
  `refuse`、`unknown` 以及其他行的 `warn` 都不通过，且就绪卡自己的 `ready` 必须为 `true`。**`levi stop` 列不出来的 D 也不启动**（`start_failed`，`why` 为 `not_listed_by_levi_stop`；`aeri_activity.hosted_visible_to_activity`：取令牌与启动时检查接线，启动之后真实的作业清单必须能点出这次运行，否则对停在短语处的 D 发 Ctrl+C）：不在 `activity.running_jobs()` 里的 D 会在 R6 重启时被挂断链截断。D 在 `hostchild.py` 里、在自己的
  `systemd-run --user --scope` 中运行（`env -i` 加白名单：除控制台语言外没有 `LEVI_*`，没有 Hugging Face 令牌、ROS、显示；带用户总线变量是因为 D 用
  `systemd-run --user` 启动 VLM 和策略单元）。臂锁与 GPU 锁由 D 在短语之后自己持有：**页面从不持臂锁**。
- **按键。** 只会向键盘写这些字节：Enter（`\r`）、`y n q`、`o p h`、`s f d`、短语行 `execute <名>`、Ctrl+C。永远没有 `\x04`（D 把它当 `q`）、Escape、
  数字、大写字母、`m`、任意文本。状态与按键见上方英文表。

  **三个来源一致满一秒才提供按键**（`SETTLE_S`）：D 的日志说它在等这类回答；D 的屏幕上这个提示是**它自己的最后一行**（别的程序的行，如
  `Homing over ...`、`server config:`、`uv` 警告，以及没人认识的行，都不会让提示作废；D 自己的行会）；pty 的模式是该提示要的（单键：非规范；短语：规范）。
  句子变了、日志里的清单与屏幕上的物体表对不上、D 活着而心跳停了、termios 读不出：状态为 `unknown`，只剩停止。刚出现的键灰显 `ARM_DELAY_S`，下一个键在
  上一个被看到取走后再等 `KEY_GAP_S`；让手臂动的键需要在 `PRESENCE_S` 内报到过的标签页和新鲜的绿色健康监视器。连续两步的键放在不同位置（`slot`）：
  D 在问第二步前会丢掉之前敲的键，双击不能一路走完两步。
- **每个提示至多一个键。** 请求带上页面看到的东西（`seen`：运行、状态序号、提示 id）和该键的令牌；服务器**重新读取所有来源**、比对，不符则在写入前拒绝
  （`stale_page`）。先写下并落盘“要发”的意图，再发字节；发出后若**没有看到被取走**（D 屏幕上的一行或日志里的一条备注，5 秒），该提示永久锁死
  （`key_unconfirmed`），**绝不重发**，只剩停止和看屏幕。意图之后崩溃只会丢键，不会重复。
- **停止**（`POST /aeri/run/stop`）。Ctrl+C：终端把这个字节变成发给 D 前台进程组的 SIGINT；D 停止发动作、把臂停在原处、说明情况，再收尾服务（VLM 与策略
  单元、GPU 锁），最长约两分钟。**手臂停在停下的位置，不会回位。**不入单飞、不等运行锁、不依赖文档新鲜。**一次运行只发一次**（D 在 5 秒内没有确认且已装好
  处理器时允许重试一次）：收尾期间的第二次 Ctrl+C 会杀掉 D 用来停单元的 `systemctl`，单元就留着（用真进程测过）。pty 送不出时，改对 D 心跳里的 pid 发
  SIGINT（身份核对通过），再不行对终端的前台进程组发。首版的两份审查带来四条规则：
  - **别处发的停止也算数。**页面每次查看都读磁盘上的记录（页面之外的停止脚本把 `stop_sent_epoch` 写在那里），并把 D 自己的 `interrupt` 行（或日志的 `operator_interrupt` 备注）当作确认，
    **不论信号是谁发的**：本页面、脚本、终端里的 Ctrl+C、`kill`。收尾已经开始后，页面不会再提供停止。记录里出现更晚的时间（`aeri_page_stop.py --again`）说明重试也用掉了。
    页面写记录是**合并**进磁盘上现有的内容（`record.update_record`），从不把内存里的整份记录写回去。
  - **重试不再走 pty。**如果第一个 Ctrl+C 字节没有变成发给 D 的 SIGINT，第二个也不会：重试直接对 D 心跳里的 pid（核对身份）发 SIGINT，再不行对前台进程组发。
  - **停止不依赖站点文件，也不依赖状态能被推导出来。**活着的运行从站点文件读的值（`rollout_root`、`runs_root`、`console_lang`、`gpu_lock_file`、源数据根目录）在**运行开始时**复制进
    托管器和运行记录，运行期间一直用这份副本；运行中站点文件或 `[aeri]` 段被改成无效，什么都不变（状态不会变成 `ended`，停止和按键都还在）。状态推导的任何环节失败时，Ctrl+C 仍然会发出，
    只凭内存里的记录，发给 D 的核对过的 pid（一次）。
  - **措辞随阶段变化。**`stop.phase`：`sent`、`unconfirmed`、`retried` 表示“已发送、未确认，手臂仍可能在动”（此时 `stopping`/`stalled`/`orphaned` 的 `state.may_move` 为 `true`）；只有 `acked` 才是
    “正在收尾，手臂停在原处”（`may_move` 为 `false`）。
  - **D 没有回应的收尾，在每一种状态里都有那一次重试**（N1）。以前只在 `stopping` 里提供；被接管的 D 是 `orphaned`，一直不吭声的会变成 `stalled`，在那里 `stop.allowed` 为 `false`、
    `why_not` 为 `stop_spent`，一个从未被回应的 Ctrl+C 就再也没有第二次机会。现在 `aeri_state.RETRY_STATES`（`stopping`、`stalled`、`orphaned`）都在阶段为 `unconfirmed`
    （第一次信号后 5 秒、已见过 D 的心跳、没有 `interrupt` 行）时提供一次。被接管的 D 没有键盘，重试发给 D 心跳里核对过身份的 pid；拿不到时重试答 `signal_failed`，**不算用掉**。
  - **记录先写“已发送”，再发信号，且在记录的锁之下**（N13），和脚本一样，信号发不出去就把旧值放回。写、放回，以及页面查看之后脚本又写了什么，都在记录的 `flock` 之下读写，脚本也拿这把锁（并在锁里再看一遍）：
    页面的信号与它的写入之间那一毫秒里跑起来的脚本，不会再重复发信号。锁最多试 1 秒（`RECORD_LOCK_WAIT_S`）；别人占得更久，信号照样发出、磁盘不动（Ctrl+C 从不等待）。页面的重试还会写 `stop_retry_epoch`，强制结束从它算起。
  - **页面无法跟踪的活着的运行，不是别的核心的**（N12）。首版写的记录没有 `site` 快照；站点文件又读不出时，这个运行被看作活着但不被跟踪（`unknown`、`no_keyboard`、没有按键），
    停止**照样提供、照样发出**（`stop.allowed`，发给 D 核对过的 pid）；以前文档却写 `driven_elsewhere`（“别的核心”），而后端其实是会发的。
  - **按键不会比开关活得更久**（N3）。控制文件的 `[aeri] enabled` 每次查看都读，不只在启动时：运行中被关掉时，当前提示的每个键都是 `allowed: false`、`why_not` 为 `aeri_disabled`，
    无论手里是什么令牌，按键都以 `aeri_disabled` 拒绝。停止与强制结束不是键，保留。此时文档的 `enabled` 为 `false`。
- **强制结束**（`POST /aeri/run/end`，短语 `end terminal D`，并带 `attest` 里的三个勾）。只在收尾卡住时（`stalled`：已发停止、或 D 已锁定/完成，且 150 秒没有任何新进展；等外部 VLM 答题时
  放宽到 600 秒），**或 D 始终没有回应时**：重试已用掉（`stop.phase` 为 `retried`，D 的屏幕和日志都没有 `interrupt`），且距最后一次 Ctrl+C（页面的重试，或脚本写下的更晚时间）已过
  `END_AFTER_RETRY_S` = 60 秒。第二种情况的缘由：一个继续打印步骤的 D 永远不会卡住（打印一步就是进展），只靠 `stalled` 的话，忽略了两次信号的 D 就永远没有强制结束。为什么是 60 秒：
  D 的处理器在主线程里、SIGINT 之后一两步内就打印确认（等 VLM 时也一样），所以 5 秒没有确认已经算晚（`STOP_ACK_S`），重试之后一分钟、手臂还可能在动，早已超过只是慢的 D；
  而且重试在先，强制结束从来不是第一个被提供的。别的核心拥有的运行永远不提供。对启动器的组和 D 所在的组发 SIGKILL。VLM 与策略单元会留下。
- **勾选在服务器上也要核对**（`attest`，第二轮安全审查 N6）。需要人逐项确认的键，窗口里一项一个勾，页面把它们的代码放在 `attest` 里发来（`KeyBody` 与 `EndBody`；封闭的词表，别的词一律 422）。
  `aeri_state.KEY_ATTEST` 规定每个键要哪些代码：`execute` 要终端 D 自己的启动警告点名的五项（`scene`、`limits`、`checkpoints`、`providers`、`hardware`）加臂栈每个窗口的三项
  （`estop_hand`、`hands_clear`、`no_client`）；`execute_variant` 在这些之前再加上未干净结束的策略切换的四项（`switch_units`、`switch_ports`、`switch_gpu`、`switch_home`）；`end` 要臂栈窗口的三项；
  `release_round` 要终端 D 为这一轮提的一项（`scene_ready`：“场景就绪后按 Enter”）；其他键一项也不要，此时 `attest` 必须为空。发来的代码与这份表**完全相同**（不多不少、没有重复）才通过，
  否则 `attest_incomplete`（400，带 `missing` 与 `unexpected`）；顺序是页面的事。它在状态和令牌之后（过期的页面仍是 `stale_page`）、写任何东西之前核对。**它防误操作，防不了同一账号的其他程序**：任何以这个用户运行的程序都能发同一份列表
  （用户的决定：不输入短语，用勾选和延时）。`tests/marvel0/fixtures/aeri_codes.json` 里的 `key_attest` 就是这张表。
- **产品重启之后。**pty 主端关闭：内核挂断启动器的终端，`hostchild.py` 对客户端的、**终端 D 自己的**（来自它的心跳文件，由 `spec["runner_json"]` 指定；文件里的 pid 只在它仍是该文件描述的进程、
  且是客户端的后代时才算数；D 在满足时被记住，所以 `uv` 在 SIGINT 上结束也藏不住 D）和启动器自己的进程组发 **SIGINT**，**3 秒后 SIGKILL**（用户 2026-10-21 决定）；客户端结束后 D 还活着时，启动器会留着，
  直到宽限期的 SIGKILL 杀掉它。正常重启不会发生这些（D 活着时产品拒绝停止）。新进程发现 D 还活着（挂断链失败）时，D 没有键盘：它收到**一次** SIGINT（如果它已经回应过 Ctrl+C，
  或记录说已经发过，就不再发），状态为 `orphaned`，只观察到它结束；页面从不冻结它（SIGSTOP 会冻住它的心跳线程），也从不拿臂锁。接管有三条规则：
  - **只在读成功之后才锁定。**这一次读不到的站点或记录，下一次再读；一次失败不会让活着的 D 在整个进程生命期里对 `active()`（`levi stop`、部署、臂栈）不可见。“记录里没有运行”是一个结果，读不出来不是。
    找运行不需要站点文件（记录里带着运行要读的值）。
  - **读不出的记录，就当作可能有 D 在运行**（第二轮安全审查 N8）。`levi stop` 本来就会列出它（`record-unreadable`），但 `aeri_run_api.active()` 答“没有”、`run_active()` 答 `false`，于是 E 的启动、部署、
    臂栈的启停都放行了。现在只要读不出，`active()` 就答一行 `{kind: marvel0-aeri, id: record-unreadable, unreadable: true}`，这些门都当作有 D（部署在 `aeri_run_live` 里点出它）；文档的 `start.why_not` 是
    `d_active`，`params.record` 为 `"unreadable"`；启动被拒（`d_active`，`record: unreadable`）。读不出的记录不会触发任何信号。由人修好或删掉这个文件：下一次查看就找到运行，或找到没有运行。页面挂了时，
    `aeri_page_stop.py --scope` 是通向 D 的路（见下）。
  - **一次 `/proc` 读失败不会结束一个运行**（N7 = 机制审查 N1）。自己的子进程只由 `poll()` 判断；被重新找到的启动器，只有相隔 0.1 秒的两次 `/proc` 读取都落空（`MISS_RECHECK_S`），或它的 pid 已是另一个进程（pid 被复用）才算不在。
    `ended` 会关掉 pty，挂断链会截断一个活着的 D。
  - **先看拥有者，且启动器只凭证据才算活着**（机制审查 N4）。处于 `intent`（拥有者写下它之后、启动之前）的记录，被第二个核心读到时，不会被标成结束，也不会得到 `terminal D exited` 行；记录里启动器没有身份的，什么也证明不了，
    绝不对它发信号。
  - **已经不在的 D 就是结束了，并且要说明。**它的 `terminal D exited rc=` 行写进 `terminal_d.events`（没人知道时写 `rc=unknown`），因为本该写它的启动器已随挂断链被杀，
    而 `wait_request.sh` 与 `res_monitor.py` 在等它。别的终端是否还在驱动某个运行名，由该文件里**最后一条** `start:` / `terminal D exited` 行决定，而不是看它们是否在文件里出现过。
  - **记录写明它的拥有者**（核心的 pid 与身份）。共用该文件夹的第二个核心，不会对有活核心拥有的 D 发信号、驱动或强制结束：它只观察（`unknown`、`no_keyboard`，停止以 `driven_elsewhere` 拒绝），
    仍把它算作运行中的作业，拥有者不在了才接管（如上，一次 SIGINT）。
- **记录。** `<工作区>/marvel0/aeri/`：`record.json`（新字段 `stop_retry_epoch`）、`requests/`、`history.jsonl`（时间、人、键、字节、状态、提示 id、轮次；**没有屏幕文本、没有路径**）、`logs/`。D 的屏幕在
  `<runs>/<名>/terminal_d.typescript`（启动器写，0600），事件在 `terminal_d.events`（`start:`、`terminal D exited rc=`），供 AERI 的工具使用。文档里从不出现路径
  或 VLM 的判断（`subtask_check`、计划、重试留在日志里：先看到它们再打标签的人就不再是盲的）。

## 这些门的分量，以及这里什么都不是

- **这里没有任何东西是急停。**急停是物理急停和 Franka Desk。停止是 Ctrl+C：它让发动作的程序停下，不会给臂断电或刹车。
- **门只防误操作，防不了同一账号的其他程序。**以这个用户身份运行的任何程序都能发同样的请求、写同一个 pty、改同样的文件或给同一个进程发信号。AERI 的日志分不出页面的键
  和终端的键；只有 `history.jsonl` 能区分。
- AERI 收件箱的 `resume` 对托管的 D 仍然有效（它是命令文件）：页面会提示（`inbox_resume`），但拦不住。
- **首次使用分阶段，用户在场，手放急停。**阶段 1：不动（手动回位，在物体清单处按 `q`）。阶段 2：一轮，不打标签。阶段 3：多轮，用 `s f d`。出现 `key_unconfirmed`、
  手臂可能在动时出现 `unknown`、停止后 5 秒内没有 `interrupt` 行、出现第二个 D 或客户端，立即中止。
- 所有常量（`SETTLE_S`、`ARM_DELAY_S`、`KEY_GAP_S`、`STOP_ACK_S`、`RUNNER_STALE_S`、`STALL_S`、`PRESENCE_S`）在阶段 1 实测之前都是猜测值，集中在 `aeri_state.py`。

## 页面挂了，D 还活着

`python3 -I scripts/aeri_page_stop.py`（在产品检出里运行；否则加 `--workspace`）：核对 D 心跳里的 pid 是同一个进程（启动时间和开机 id）后发一次 SIGINT，否则发给启动器的进程组；
它在发信号**之前**把这件事写入记录（信号发不出去则把旧值放回），这样正在运行的页面、或之后重启的页面，都知道已经发过停止，不会自己再提供一个。记录在记录的锁之下写入并再看一遍（页面的停止也拿这把锁），所以页面的停止和脚本同时发生时不会都发。它不开终端或套接字，也不导入 LEVI 的任何东西。页面托管的运行**没有终端 D 可以退回去用**：这个脚本和急停是出路。

**记录读不出来时**，脚本说明情况后停下（没有名字、没有心跳，也不知道停止是否已经发过）。`--scope` 是继续的办法，只用于这种情况（记录能读时它拒绝）：它在用户自己的 cgroup 树
（`/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/**`）里找页面启动 D 的那个 scope（`levi-aeri-termd-marvel0-<运行>.scope`；多于一个就拒绝），取其中命令行为
`python ... aeri.py execute` 的那个进程（不是 `uv`，它的命令行里有同样的词）发 SIGINT，找不到再发给领着进程组的启动器（`hostchild.py`）。它不运行任何程序、不问任何服务，只读你自己用户的进程的 cgroup 文件和 `/proc`。
它什么也不写：**页面不会知道这次停止，也没有任何东西防止第二次**，所以先确认没有发过。为什么走这条路而不是从记录出发：读不出的恰恰是记录；心跳文件是记录指出的；scope 名是页面选定、且在记录读不出时仍然在的唯一的东西。

**挂断之后**（挂断链的 SIGKILL 不给 D 做完 7/7 的时间）：VLM 和策略单元可能留下，策略切换日志可能未结束。下一次启动会要求变体短语（`execute_variant`），页面只在旁边显示单元、端口、日志的被动检查时才提供。
就绪卡的残留单元步骤只停名为 `levi-aeri-vlm-*`、`levi-aeri-policy-*` 的单元（A2）。

## 接线，以及总管要加的（每项一行）

`api.py`：挂 `aeri_run_api.router`（`/api/levi/marvel0/aeri/run` 下八条路由）。`service.py`：在栈的自动启动旁调 `aeri_run_api.adopt_on_start()`；关闭时先发停止并等 7/7（130 秒）再轮到挂断链。
`activity.py`：把 `aeri_run_api.active_job()` 并入运行中的作业，使 D 活着时 `levi stop` 拒绝。`run_api.run_active()`（已在这里改好）已包含 D，所以此时臂栈的启停与客户端的启动都拒绝。
控制文件的 `[aeri] enabled = false`（A2）是总开关，由用户在场时亲手打开。

## D 活着时重启产品（单元的超时）

有序关停（`aeri_activity.shutdown_hosted`）给 D 发停止，最多等 **130 秒**让它跑完 7/7 收尾。用户单元 `levi-product.service` 必须给出这段时间：**`TimeoutStopSec` 至少 150 秒**
（等待、启动器的 3 秒宽限、以及服务自己其余的关停）。原来的 15 秒下，systemd 在等待开始 15 秒后杀掉核心，pty 关闭，启动器的挂断链往收尾中的 D 发第二次 SIGINT、3 秒后 SIGKILL：
D 的 7/7 被截断，VLM 与策略单元留下。被认可的第一步是 **`levi stop`，D 活着时它拒绝**（等运行结束，或在页面上按停止）。D 活着时直接 `systemctl --user stop` 或 `restart`，
除非单元有那 150 秒，否则会**截断 D 的 7/7 收尾**；就绪卡里有一行 `warn` 的 `unit_timeout` 会说出这一点（它读 `systemctl --user show levi-product.service --property=LoadState,TimeoutStopUSec`，缓存一分钟，从不拒绝）。
不拥有该运行的核心（另一个活核心的）在关停时立刻返回并说明原因：它没有这个运行的 pty，所以也不会有挂断链跟着发生。

## 已经运行过的，和没有运行过的

已离线运行：在 **10-18 的真实录屏**（r08：日志、屏幕、服务步骤、心跳）上的状态规则、按键表、脚本化机器上的托管（页面写出的每个字节都读回来核对）、真 pty 下的启动器与假终端 D
（整轮每个键、轮中停止、第二次 Ctrl+C、忽略停止而被强制结束的 D、停掉的心跳、被改写的提示、别的程序的行、发现 D 仍活着的重启、核心死亡：先 SIGINT、宽限后 SIGKILL）、
变异检查（每道门去掉后必须有测试失败）、三个代码版本上的漂移检查；**10-18 整段录屏的回放**（日志在它的 97 行后各截一次，配上当时 D 已打印的屏幕，再往后看至多 560 秒，分别经过状态规则和托管器：D 在等时从不出现 `unknown`）、
对着就绪卡真实行的启动门、脚本发出的停止、运行中途坏掉的站点文件、读取失败的接管、已经不在的 D 和第二个核心。**没有运行：真终端 D、真手臂、在这个环境下 `uv run` 真 AERI**（`UV_OFFLINE=1` 能否找到 pyrealsense2 覆盖层？白名单环境能否启动 D？）、
本机上启动器的 `systemd-run --scope`（演练 `tests/marvel0/rehearse_aeri.py` 用替身做了）、以及固定版本 `bfdee2cf` 的中文屏幕（没有录屏）。真 pty 实测：主端关闭时内核只挂断会话首进程，
所以 SIGINT 先于任何 SIGHUP 到达 D。
