"""Mutation check of the gates of the hosted AERI terminal D: each gate is removed
(or one safety property turned round) in turn, the test file that is meant to
guard it runs, and every mutation must make at least one test fail. A mutation
that survives means a gate nobody tests.

    uv run --no-sync python tests/marvel0/mutate_aeri.py [--check] [--no-baseline] [name ...]

``--check`` only verifies that every mutation applies (its old text occurs
exactly once) and runs nothing; ``--no-baseline`` skips the run of each test file
on the unbroken copy (it is the first thing a sweep does). The mutations are made
in a **copy** of ``levi/``, ``tests/`` and ``scripts/`` in a temporary folder (the
checkout itself is never edited, so nobody else's test run can meet a broken
gate), and the tests run there with that folder first on ``PYTHONPATH``. It starts
no process but pytest, one test file at a time, under ``nice -n 19 taskset -c
20-23``. It refuses to run while a robot process is alive (the passive ``ps``
check of setup.md): a sweep is many test runs, and the arm's control loop is not
to be disturbed by it; ``--even-if-robot-runs`` lifts that (one file at a time
stays)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="mutate-aeri-"))
PKG = ROOT / "levi" / "marvel0"
COPIED = (
    "levi",
    "tests",
    "scripts/check_aeri_prompts.py",
    "scripts/aeri_page_stop.py",
    "docs/MARVEL0_AERI_RUN.md",
    "src/components/marvel0/__tests__/marvel0-aeri-run.samples.json",
    "src/components/marvel0/__tests__/marvel0-aeri-run.answers.json",
    "pyproject.toml",
)
ROBOT = re.compile(
    r"ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)

STATE = "tests/marvel0/test_marvel0_aeri_state.py"
SCREEN = "tests/marvel0/test_marvel0_aeri_screen.py"
JOURNAL = "tests/marvel0/test_marvel0_aeri_journal.py"
RUN = "tests/marvel0/test_marvel0_aeri_run.py"
LIVE = "tests/marvel0/test_marvel0_aeri_run_live.py"
ROUTE = "tests/marvel0/test_marvel0_aeri_run_route.py"
SAMPLE = "tests/marvel0/test_marvel0_aeri_sample.py"
HOST = "tests/marvel0/test_marvel0_aeri_hostchild.py"
STOP = "tests/marvel0/test_marvel0_aeri_page_stop.py"
RUN_E = "tests/marvel0/test_marvel0_aeri_run.py"

# (name, file of levi/marvel0, old text that occurs exactly once, new text, test file)
MUTATIONS = [
    # --- which byte, which prompt -------------------------------------------------------------
    (
        "bytes: q is Ctrl-D (D reads it as q, the table must not contain it)",
        "aeri_state.py",
        'KeySpec(\n            "quit",\n            "q",\n            b"q",',
        'KeySpec(\n            "quit",\n            "q",\n            b"\\x04",',
        STATE,
    ),
    (
        "bytes: Stop is Ctrl-D",
        "aeri_state.py",
        'STOP_BYTE = b"\\x03"',
        'STOP_BYTE = b"\\x04"',
        RUN,
    ),
    (
        "bytes: y at the object list also answers a resume",
        "aeri_state.py",
        'KeySpec("inventory_yes", "y", b"y", "B", "none", "first", ("inventory",)),',
        'KeySpec("inventory_yes", "y", b"y", "B", "none", "first", ("inventory", "confirm_resume")),',
        STATE,
    ),
    (
        "bytes: Enter also releases from a wait",
        "aeri_state.py",
        'KeySpec(\n            "release_round", "Enter", ENTER, "B", "arm_moves", "first", ("release",)\n        ),',
        'KeySpec(\n            "release_round", "Enter", ENTER, "B", "arm_moves", "first", ("release", "resume")\n        ),',
        STATE,
    ),
    (
        "bytes: s is offered at a release prompt",
        "aeri_state.py",
        'KeySpec("verdict_success", "s", b"s", "B", "arm_moves", "second", ("round",)),',
        'KeySpec("verdict_success", "s", b"s", "B", "arm_moves", "second", ("round", "release")),',
        STATE,
    ),
    (
        "bytes: the confirm of a resume is a first-slot key (same place as the ask)",
        "aeri_state.py",
        '"resume_confirm", "y", b"y", "B", "may_move", "second", ("confirm_resume",)',
        '"resume_confirm", "y", b"y", "B", "may_move", "first", ("confirm_resume",)',
        SAMPLE,
    ),
    (
        "bytes: the three homing keys are offered together",
        "aeri_state.py",
        'if kind in ("home_o", "home_p", "home_h"):\n        return [kind, "quit"]',
        'if kind in ("home_o", "home_p", "home_h"):\n        return ["home_o", "home_p", "home_h", "quit"]',
        STATE,
    ),
    (
        "bytes: the policy-switch phrase is not forced to its own key",
        "aeri_state.py",
        'elif action == "execute" and d.state == "await_phrase_variant":',
        "elif False:",
        STATE,
    ),
    # --- the sources must agree ------------------------------------------------------------------
    (
        "sources: the pty's mode is not looked at",
        "aeri_state.py",
        'if (f.icanon is True) != (need == "canonical"):',
        "if False:",
        STATE,
    ),
    (
        "sources: an unknown line closes a prompt",
        "aeri_screen.py",
        '    "blank",\n    "unrecognised",\n    "progress",\n)',
        '    "blank",\n    "progress",\n)',
        SCREEN,
    ),
    (
        "sources: a foreign line closes a prompt",
        "aeri_screen.py",
        '    "recent_note",\n    "noise",\n    "blank",',
        '    "recent_note",\n    "blank",',
        SCREEN,
    ),
    (
        "sources: the object table is not compared with the journal's list",
        "aeri_state.py",
        "or sorted(ids) != sorted(j.inventory_ids)",
        "or False",
        STATE,
    ),
    (
        "sources: a heartbeat that stopped is ignored",
        "aeri_state.py",
        "if f.runner_seen and f.runner_age_s is not None and f.runner_age_s > RUNNER_STALE_S:",
        "if False:",
        STATE,
    ),
    (
        "sources: the heartbeat counts as progress",
        "aeri_state.py",
        "return (f.j.seq, f.steps.steps, f.screen.lines - blank)",
        "return (f.j.seq, f.steps.steps, f.screen.lines - blank, f.runner.updated_wall_ns)",
        STATE,
    ),
    (
        "sources: no settle time",
        "aeri_state.py",
        "settled = prompt is not None and (now - sig_since) >= SETTLE_S",
        "settled = prompt is not None",
        STATE,
    ),
    (
        "sources: the key's delay is gone",
        "aeri_state.py",
        "at = d.since + ARM_DELAY_S",
        "at = d.since",
        STATE,
    ),
    (
        "sources: the monitor is not asked",
        "aeri_state.py",
        'elif level in ("B", "C") and c.monitor_state != "ok":',
        "elif False:",
        STATE,
    ),
    (
        "sources: any tab will do",
        "aeri_state.py",
        'elif level in ("B", "C") and not (',
        "elif False and not (",
        STATE,
    ),
    # --- one key per prompt, one verdict per round ---------------------------------------------------
    (
        "locks: a second verdict in one round",
        "aeri_state.py",
        'elif action in VERDICTS and d.progress["verdict_latched"]:',
        "elif False:",
        STATE,
    ),
    (
        "locks: a prompt that was keyed takes another key",
        "aeri_state.py",
        "elif prompt_id in c.keyed or c.pending:",
        "elif False:",
        STATE,
    ),
    (
        "locks: the page's view (seen) is not compared",
        "aeri_run.py",
        'or seen.get("run_id") != f.record.get("run_id")',
        "or False",
        RUN,
    ),
    (
        "locks: the token is not compared",
        "aeri_run.py",
        'if token != entry["token"]:',
        "if False:",
        RUN,
    ),
    (
        "locks: no single flight",
        "aeri_run.py",
        "if not self._guard.acquire(blocking=False):",
        "if False:",
        RUN,
    ),
    (
        "locks: a key not seen taken is forgotten, not marked",
        "aeri_run.py",
        "self._unconfirmed = p.prompt_id",
        "self._unconfirmed = None",
        RUN,
    ),
    (
        "locks: the intent is not written before the byte",
        "aeri_run.py",
        '            mark_sent()\n            self._audit(\n                "key",',
        '            self._audit(\n                "key",',
        RUN,
    ),
    # --- stop and end ---------------------------------------------------------------------------------
    (
        "stop: a second Stop is allowed",
        "aeri_state.py",
        '    stop_open = alive and (\n        (\n            phase == "idle"\n            and not v.winding\n            and v.state not in ("idle", "ended", "fault_locked", "orphaned")\n        )\n        or (phase == "unconfirmed" and v.state == "stopping")\n    )',
        "    stop_open = alive",
        STATE,
    ),
    (
        "stop: refused in the unknown state",
        "aeri_state.py",
        'and v.state not in ("idle", "ended", "fault_locked", "orphaned")',
        'and v.state not in ("idle", "ended", "fault_locked", "orphaned", "unknown")',
        STATE,
    ),
    (
        "stop: offered while winding down",
        "aeri_state.py",
        '            and not v.winding\n            and v.state not in ("idle", "ended", "fault_locked", "orphaned")\n',
        '            and v.state not in ("idle", "ended", "orphaned")\n',
        STATE,
    ),
    (
        "stop: the retry is not limited to a D with handlers",
        "aeri_state.py",
        "if f.runner_seen and f.stop_age_s is not None and f.stop_age_s > STOP_ACK_S:",
        "if f.stop_age_s is not None and f.stop_age_s > STOP_ACK_S:",
        STATE,
    ),
    (
        "stop: waits in the single flight",
        "aeri_run.py",
        'with self._run_lock_if_free("stop"), self._mutex:',
        'with self._operation("stop"), self._mutex:',
        RUN,
    ),
    (
        "end: offered while D can still wrap up",
        "aeri_state.py",
        "ok = d.stalled and d.alive",
        "ok = d.winding and d.alive",
        STATE,
    ),
    (
        "end: no confirmation phrase",
        "aeri_run.py",
        'if body.get("confirm") != st.END_PHRASE:',
        "if False:",
        RUN,
    ),
    # --- the start -----------------------------------------------------------------------------------
    (
        "start: the checklist is not required",
        "aeri_run.py",
        "if not all(attest.get(k) is True for k in st.ATTEST):",
        "if False:",
        RUN,
    ),
    (
        "start: the pin is not compared",
        "aeri_run.py",
        "or not head.strip().startswith(aeri.pin_sha)",
        "or False",
        RUN,
    ),
    (
        "start: the printed command is not compared",
        "aeri_run.py",
        "if not printed.startswith(prefix) or printed[len(prefix) :] != shlex.join(argv):",
        "if False:",
        RUN,
    ),
    (
        "start: the profile is not compared with the worktree",
        "aeri_run.py",
        '        if not ok:\n            raise Refuse("profile_unknown", profile=aeri.profile, detail=detail)',
        '        if False:\n            raise Refuse("profile_unknown", profile=aeri.profile, detail=detail)',
        RUN,
    ),
    (
        "start: the environment passes the UI token",
        "aeri_run.py",
        '        "no_proxy": "localhost,127.0.0.1,::1",',
        '        "no_proxy": "localhost,127.0.0.1,::1",\n        "LEVI_UI_TOKEN": environ.get("LEVI_UI_TOKEN", ""),',
        RUN,
    ),
    (
        "start: the launcher does not hold a keyboard of its own (no echo off)",
        "aeri_run.py",
        "attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)",
        "attrs[3] |= 0",
        RUN,
    ),
    # --- the document -----------------------------------------------------------------------------------
    (
        "document: the VLM's judgement is kept",
        "aeri_journal.py",
        '        "subtask_service_step",\n        "policy_warmup",',
        '        "subtask_service_step",\n        "subtask_check",\n        "policy_warmup",',
        JOURNAL,
    ),
    (
        "document: paths are not cut from the screen tail",
        "aeri_run.py",
        "c.tail = [deploy.page_line(line) for line in self._buf.tail()]",
        "c.tail = list(self._buf.tail())",
        RUN,
    ),
    # --- the other sides of the arm ---------------------------------------------------------------------
    (
        "arm: the client does not see the hosted D",
        "run_api.py",
        "    return aeri_run_api.active() is not None",
        "    return False",
        RUN,
    ),
    (
        "adopt: the second look sends another SIGINT",
        "aeri_run.py",
        '            if rec.get("stop_sent_epoch") is None:\n                self._adopt_stop(f, rec, pid)',
        "            self._adopt_stop(f, rec, pid)",
        RUN,
    ),
    (
        "adopt: the stop is not remembered",
        "aeri_run.py",
        "        self._update(stop_sent_epoch=self.host.now(), adopted_at=self.host.now())",
        "        pass",
        RUN,
    ),
    # --- the launcher ------------------------------------------------------------------------------------
    (
        "hostchild: a hang-up kills at once (no SIGINT)",
        "hostchild.py",
        "os.killpg(group, signal.SIGINT)",
        "os.killpg(group, signal.SIGKILL)",
        HOST,
    ),
    (
        "hostchild: no grace",
        "hostchild.py",
        "signal.setitimer(signal.ITIMER_REAL, _grace_s)",
        "_alarm(signal.SIGALRM, None)",
        HOST,
    ),
    (
        "hostchild: the grace timer's handler is not installed",
        "hostchild.py",
        "        signal.signal(signal.SIGALRM, _alarm)",
        "        pass",
        HOST,
    ),
    (
        "hostchild: the output pty is not drained",
        "hostchild.py",
        "        os.close(output.slave)  # our copy: the client's side is the only one left\n        output.start()",
        "        os.close(output.slave)  # our copy: the client's side is the only one left",
        HOST,
    ),
    (
        "hostchild: its log's error escapes a signal handler",
        "hostchild.py",
        "    except (OSError, ValueError):\n        pass\n\n\ndef _write_exit",
        "    except ValueError:\n        pass\n\n\ndef _write_exit",
        HOST,
    ),
    (
        "hostchild: a full disk stops the client's output",
        "hostchild.py",
        "        except OSError:\n            self.dropped += len(data)  # a full disk never stops the client",
        "        except OSError:\n            raise  # a full disk never stops the client",
        HOST,
    ),
    (
        "hostchild: the out log may be a link",
        "hostchild.py",
        "os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,\n            0o600,\n        )\n        self.dropped = 0",
        "os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,\n            0o600,\n        )\n        self.dropped = 0",
        HOST,
    ),
    # --- the script outside the page -----------------------------------------------------------------------
]


def applies() -> list:
    """The mutations whose old text is not found exactly once."""
    bad = []
    for name, file, old, new, _ in MUTATIONS:
        text = (SOURCE / "levi" / "marvel0" / file).read_text()
        if text.count(old) != 1 or old == new:
            bad.append((name, text.count(old)))
    return bad


def copy_sources():
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".state", "node_modules")
    for name in COPIED:
        source, target = SOURCE / name, ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=skip)
        else:
            shutil.copy2(source, target)


def run_tests(test_file: str) -> tuple:
    """``(exit code, the first FAILED or ERROR line)`` of one test file."""
    env = dict(os.environ)
    env["LEVI_WORKSPACE"] = str(ROOT / ".state")
    env["TMPDIR"] = str(ROOT / "tmp")
    env["PYTHONPATH"] = str(ROOT)
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [
            "nice",
            "-n",
            "19",
            "taskset",
            "-c",
            "20-23",
            sys.executable,
            "-m",
            "pytest",
            test_file,
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            f"--basetemp={ROOT}/pt",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    lines = [x for x in done.stdout.splitlines() if x.startswith(("FAILED", "ERROR"))]
    return done.returncode, (lines[0][:150] if lines else "")


def robot_processes() -> bool:
    """The passive check: any robot-related process in the process list."""
    listing = subprocess.run(
        ["ps", "-eo", "args"], capture_output=True, text=True, check=False
    ).stdout
    return any(ROBOT.search(line) for line in listing.splitlines())


def main(argv) -> int:
    flags = [a for a in argv if a.startswith("--")]
    names = [a for a in argv if not a.startswith("--")]
    bad = applies()
    for name, count in bad:
        print(f"DOES NOT APPLY ({count} matches): {name}")
    if "--check" in flags:
        print(f"{len(MUTATIONS) - len(bad)} of {len(MUTATIONS)} mutations apply")
        shutil.rmtree(ROOT, ignore_errors=True)
        return 1 if bad else 0
    if robot_processes() and "--even-if-robot-runs" not in flags:
        print("a robot process is alive; not running a sweep (see --help text)")
        shutil.rmtree(ROOT, ignore_errors=True)
        return 2
    chosen = [m for m in MUTATIONS if not names or m[0] in names]
    copy_sources()
    survived = []
    try:
        if "--no-baseline" not in flags:
            for test_file in sorted({m[4] for m in chosen}):
                code, _ = run_tests(test_file)
                print(f"baseline {test_file}: pytest exit {code}", flush=True)
        for name, file, old, new, test_file in chosen:
            path = PKG / file
            text = path.read_text()
            if text.count(old) != 1:
                print(f"SKIPPED  {name}: the text occurs {text.count(old)} times")
                survived.append(name + " (not applied)")
                continue
            path.write_text(text.replace(old, new))
            try:
                code, first = run_tests(test_file)
            finally:
                path.write_text(text)
            print(
                f"{'killed  ' if code else 'SURVIVED'} {name}"
                + (f"  <- {first}" if first else ""),
                flush=True,
            )
            if not code:
                survived.append(name)
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{len(chosen) - len(survived)} of {len(chosen)} mutations killed")
    for name in survived:
        print(f"  not killed: {name}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
