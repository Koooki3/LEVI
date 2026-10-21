"""Mutation check of the gates of the hosted evaluation run: each gate is removed
(or one safety property turned round) in turn, the one test file that is meant
to guard it runs, and every mutation must make at least one test fail. A
mutation that survives means a gate nobody tests.

    uv run --no-sync python tests/marvel0/mutate_run.py [--check] [--no-baseline] [name ...]

``--check`` only verifies that every mutation applies (its old text occurs
exactly once) and runs nothing; ``--no-baseline`` skips the run of each test
file on the unbroken copy (it is the first thing a sweep does). The mutations are made in a **copy** of
``levi/`` and ``tests/`` in a temporary folder (the checkout itself is never
edited, so nobody else's test run can meet a broken gate), and the tests run
there with that folder first on ``PYTHONPATH``. It starts no process but pytest,
one test file at a time, under ``nice -n 19 taskset -c 20-23``. It refuses to
run while a robot process is alive (the passive ``ps`` check of setup.md): a
sweep is many test runs, and the arm's control loop is not to be disturbed by
it; ``--even-if-robot-runs`` lifts that (one file at a time stays)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="mutate-run-"))
PKG = ROOT / "levi" / "marvel0"
COPIED = (
    "levi",
    "tests",
    "scripts/check_client_prompts.py",
    "docs/MARVEL0_RUN.md",
    "src/components/marvel0/__tests__/marvel0-run.samples.json",
    "src/components/marvel0/__tests__/marvel0-run.answers.json",
    "pyproject.toml",
)
ROBOT = re.compile(
    r"ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)

RUN = "tests/marvel0/test_marvel0_run.py"
SUP = "tests/marvel0/test_marvel0_run_supervisor.py"
LIVE = "tests/marvel0/test_marvel0_run_live.py"
ROUTE = "tests/marvel0/test_marvel0_run_route.py"

# (name, file of levi/marvel0, old text that occurs exactly once, new text, test file)
MUTATIONS = [
    # --- which key in which state -------------------------------------------------
    (
        "keys: s offered at the start gate",
        "run.py",
        '"success": (b"s", ("running", "awaiting_label")),',
        '"success": (b"s", ("at_gate", "running", "awaiting_label")),',
        RUN,
    ),
    (
        "keys: f offered at the start gate",
        "run.py",
        '"failure": (b"f", ("running", "awaiting_label")),',
        '"failure": (b"f", ("at_gate", "running", "awaiting_label")),',
        RUN,
    ),
    (
        "keys: q at the gate is an s",
        "run.py",
        '"end_at_gate": (b"q", ("at_gate",)),',
        '"end_at_gate": (b"s", ("at_gate",)),',
        RUN,
    ),
    (
        "keys: enter offered while running",
        "run.py",
        '"enter": (b"\\r", ("at_gate",)),',
        '"enter": (b"\\r", ("at_gate", "running")),',
        RUN,
    ),
    (
        "keys: discard offered while running",
        "run.py",
        '"discard": (b"d", ("awaiting_label",)),',
        '"discard": (b"d", ("awaiting_label", "running")),',
        RUN,
    ),
    (
        "keys: exit-and-go-home offered at the gate",
        "run.py",
        '"exit_home": (b"q", ("running",)),',
        '"exit_home": (b"q", ("running", "at_gate")),',
        RUN,
    ),
    (
        "keys: a digit is a key",
        "run.py",
        '"discard": (b"d", ("awaiting_label",)),',
        '"discard": (b"1", ("awaiting_label",)),',
        RUN,
    ),
    # --- one key per episode, one Enter per gate, one in flight -----------------------
    (
        "locks: enter sent twice",
        "run.py",
        'if name == "enter" and c.enter_locked:',
        "if False:",
        RUN,
    ),
    (
        "locks: q behind an untaken Enter",
        "run.py",
        'if name == "end_at_gate" and c.enter_locked:',
        "if False:",
        RUN,
    ),
    (
        "locks: a second label in one episode",
        "run.py",
        'if name in (*LABELS, "exit_home") and c.label_locked:',
        "if False:",
        RUN,
    ),
    (
        "locks: exit-and-go-home after a label",
        "run.py",
        'if name in (*LABELS, "exit_home") and c.label_locked:',
        "if name in LABELS and c.label_locked:",
        RUN,
    ),
    (
        "locks: a key while another is in flight",
        "run.py",
        '    if c.keyed:\n        return _entry("key_in_flight", blind)',
        '    if False:\n        return _entry("key_in_flight", blind)',
        RUN,
    ),
    (
        "locks: the in-flight flag never clears",
        "run.py",
        "                self._seq += 1\n                self._keyed = False\n",
        "                self._seq += 1\n",
        SUP,
    ),
    (
        "locks: the enter lock is not recorded",
        "run.py",
        '            if action == "enter":\n                self._enter_ep = no',
        '            if action == "enter":\n                pass',
        SUP,
    ),
    (
        "locks: the label lock is not recorded",
        "run.py",
        "            else:\n                self._label_ep = no",
        "            else:\n                pass",
        SUP,
    ),
    (
        "keys: the byte is written before the sent record",
        "run.py",
        "            mark_sent()\n",
        "            pass\n",
        SUP,
    ),
    # --- the monitor ------------------------------------------------------------------
    (
        "monitor: red does not veto a key",
        "run.py",
        '    if state == "red":\n        return "monitor_red", {}',
        '    if False:\n        return "monitor_red", {}',
        RUN,
    ),
    (
        "monitor: a stale monitor does not block Enter",
        "run.py",
        'code, params = _monitor_block(c, strict=name == "enter")',
        "code, params = _monitor_block(c, strict=False)",
        RUN,
    ),
    (
        "monitor: the stop actions are vetoed too",
        "run.py",
        '    if d.state == "held":\n        out["hold"] = _entry("already_held")',
        '    if c.monitor.state == "red":\n        out["hold"] = _entry("monitor_red")\n    elif d.state == "held":\n        out["hold"] = _entry("already_held")',
        RUN,
    ),
    (
        "monitor: one red write holds",
        "run.py",
        "elif len(self._red) >= RED_WRITES and d.may_move:",
        "elif len(self._red) >= 1 and d.may_move:",
        SUP,
    ),
    (
        "monitor: red holds a client that cannot move",
        "run.py",
        "elif len(self._red) >= RED_WRITES and d.may_move:",
        "elif len(self._red) >= RED_WRITES:",
        SUP,
    ),
    (
        "monitor: every look is a new write",
        "run.py",
        "self._red.add(monitor.stamp)",
        "self._red.add(self.host.now())",
        SUP,
    ),
    (
        "monitor: red writes never counted",
        "run.py",
        "self._red.add(monitor.stamp)",
        "self._red.add(0)",
        SUP,
    ),
    (
        "monitor: green does not reset the count",
        "run.py",
        '            elif monitor.state == "ok":\n                self._red.clear()',
        '            elif monitor.state == "ok":\n                pass',
        SUP,
    ),
    # --- what the log and the session must show for a state ---------------------------
    (
        "derive: the gate needs no keyboard mode",
        "run.py",
        "            and f.icanon is False\n",
        "",
        RUN,
    ),
    (
        "derive: the gate needs no reason text",
        "run.py",
        '            session.get("reason") == GATE_REASON\n',
        "            True\n",
        RUN,
    ),
    (
        "derive: the gate needs no banner",
        "run.py",
        "            and f.log.gate_ep == no\n",
        "",
        RUN,
    ),
    (
        "derive: the step-limit prompt of any episode",
        "run.py",
        "        if f.log.judge_ep == no:",
        "        if f.log.judge_ep is not None:",
        RUN,
    ),
    (
        "derive: a judge line after any outcome",
        "run.py",
        '            if v.ended_outcome == "timeout":',
        "            if True:",
        RUN,
    ),
    (
        "derive: a silent session is not lost",
        "run.py",
        "    if f.session_age_s is None or f.session_age_s > f.stale_after_s:",
        "    if False:",
        RUN,
    ),
    (
        "derive: a run without a keyboard is not lost",
        "run.py",
        "    if not f.keyboard:",
        "    if False:",
        RUN,
    ),
    (
        "derive: a stopped launcher is not held",
        "run.py",
        '    if f.proc == "stopped" or (',
        "    if False or (",
        RUN,
    ),
    (
        "derive: a continued launcher is not lost",
        "run.py",
        '    if held_at is not None and f.proc == "running":',
        "    if False:",
        RUN,
    ),
    (
        "derive: another task is not noticed",
        "run.py",
        '    if trouble:\n        return Derived("lost", trouble, True)',
        '    if False:\n        return Derived("lost", trouble, True)',
        RUN,
    ),
    (
        "derive: another session is not noticed",
        "run.py",
        '    if wanted and session.get("session_id") != wanted:',
        "    if False:",
        RUN,
    ),
    (
        "derive: an unreadable process is not lost",
        "run.py",
        '    if f.proc == "unknown":',
        "    if False:",
        RUN,
    ),
    (
        "derive: no time limit for the gate",
        "run.py",
        "        if (f.started_age_s or 0.0) > f.start_timeout_s:",
        "        if False:",
        RUN,
    ),
    (
        "derive: a page-ended client is a failure",
        "run.py",
        '    if rec.get("killed"):\n        return Derived("done", "ended_by_page")',
        '    if False:\n        return Derived("done", "ended_by_page")',
        RUN,
    ),
    # --- the token --------------------------------------------------------------------
    (
        "token: used twice",
        "run.py",
        "entry = self._open.pop(token, None) if isinstance(token, str) else None",
        "entry = self._open.get(token) if isinstance(token, str) else None",
        RUN,
    ),
    (
        "token: never expires",
        "run.py",
        'for token in [t for t, e in self._open.items() if e["deadline"] < now]:',
        "for token in [t for t, e in self._open.items() if False]:",
        RUN,
    ),
    (
        "token: not bound to the state",
        "run.py",
        '        if entry["bind"] != bind:\n            raise Refuse("challenge_stale")',
        '        if False:\n            raise Refuse("challenge_stale")',
        RUN,
    ),
    (
        "token: not bound to a purpose",
        "run.py",
        '        if entry["purpose"] != purpose:\n            raise Refuse("challenge_invalid")',
        '        if False:\n            raise Refuse("challenge_invalid")',
        RUN,
    ),
    (
        "token: the state sequence is not in the binding",
        "run.py",
        '"state_seq": c.seq,\n        }',
        '"state_seq": 0,\n        }',
        SUP,
    ),
    (
        "token: a finished run keeps its tokens",
        "run.py",
        "        self._child = None\n        self.challenges.void()",
        "        self._child = None",
        SUP,
    ),
    # --- start ---------------------------------------------------------------------------
    (
        "start: no control switch",
        "run.py",
        "        return bool(self.host.control_enabled())",
        "        return True",
        SUP,
    ),
    (
        "start: outside the product",
        "run.py",
        '        if self._profile()["profile"] != "real":\n            raise Refuse("not_product")',
        '        if False:\n            raise Refuse("not_product")',
        SUP,
    ),
    (
        "start: no typed phrase",
        "run.py",
        '        if body.get("confirm") != phrase:',
        "        if False:",
        SUP,
    ),
    (
        "start: no checklist",
        "run.py",
        'attest.get(k) is True for k in ("estop", "scene_reset", "area_clear")',
        "True for k in ()",
        SUP,
    ),
    (
        "start: the form below the z floor",
        "run.py",
        "    if floor is not None and min_z < floor:",
        "    if False:",
        RUN,
    ),
    (
        "start: the floor is not checked on the start itself",
        "run.py",
        '        if problem:\n            raise Refuse("min_z_below_task_floor", **problem)',
        "        if False:\n            pass",
        SUP,
    ),
    (
        "start: the floor is not listed among the checks",
        "run.py",
        'rows.append(check_row("min_z", problem is None, **(problem or {})))',
        "pass",
        SUP,
    ),
    (
        "start: a check that cannot be read passes",
        "run.py",
        '        failing = [r for r in rows if r["level"] in ("refuse", "unknown")]\n        if failing:\n            raise Refuse(\n                "preflight_failed",',
        '        failing = [r for r in rows if r["level"] in ("refuse",)]\n        if failing:\n            raise Refuse(\n                "preflight_failed",',
        SUP,
    ),
    (
        "start: a policy server of another hand",
        "run.py",
        '    if ready and listener.get("ours") is not True:',
        "    if False:",
        SUP,
    ),
    (
        "start: the server serves another model or RTC",
        "run.py",
        '                and bool(served.get("rtc")) == form.rtc',
        "                and True",
        SUP,
    ),
    (
        "start: someone is connected to the policy server",
        "run.py",
        '"no_connection", None if count is None else count == 0, count=count',
        '"no_connection", True, count=count',
        SUP,
    ),
    (
        "start: an evaluation session is active",
        "run.py",
        'check_row("no_session", not active, count=len(active))',
        'check_row("no_session", True, count=len(active))',
        SUP,
    ),
    (
        "start: the arm lock is held by another",
        "run.py",
        'check_row("arm_lock", None if holders is None else not holders)',
        'check_row("arm_lock", True)',
        SUP,
    ),
    (
        "start: the monitor is not green",
        "run.py",
        'check_row("monitor_green", monitor.state == "ok", state=monitor.state)',
        'check_row("monitor_green", True, state=monitor.state)',
        SUP,
    ),
    (
        "start: an automatic run is alive",
        "run.py",
        'return check_row("no_aeri", not live)',
        'return check_row("no_aeri", True)',
        SUP,
    ),
    (
        "start: the rollout folder is not writable",
        "run.py",
        'check_row("rollout_writable", _writable(site.rollout_root))',
        'check_row("rollout_writable", True)',
        SUP,
    ),
    (
        "start: the arm lock is not really taken",
        "run.py",
        "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
        "        pass",
        SUP,
    ),
    (
        "start: no run lock around a write",
        "run.py",
        '                with lock.hold("marvel0-run", action):\n                    yield',
        "                if True:\n                    yield",
        SUP,
    ),
    (
        "start: the client's log is world readable",
        "run.py",
        "                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,\n                0o600,\n            )\n            spec_path",
        "                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,\n                0o644,\n            )\n            spec_path",
        SUP,
    ),
    # --- the environment and the command ------------------------------------------------
    (
        "env: the UI token reaches the client",
        "run.py",
        '        "PYTHONUNBUFFERED": "1",',
        '        "PYTHONUNBUFFERED": "1",\n        "LEVI_UI_TOKEN": environ.get("LEVI_UI_TOKEN", ""),',
        RUN,
    ),
    (
        "env: uv may use the network",
        "run.py",
        '        "UV_OFFLINE": "1",\n',
        "",
        RUN,
    ),
    (
        "env: the output is buffered",
        "run.py",
        '        "PYTHONUNBUFFERED": "1",\n        "UV_OFFLINE"',
        '        "UV_OFFLINE"',
        RUN,
    ),
    (
        "env: env -i is dropped",
        "run.py",
        '        "/usr/bin/env",\n        "-i",\n',
        '        "/usr/bin/env",\n',
        RUN,
    ),
    (
        "env: the caller's environment goes to systemd-run",
        "run.py",
        "return argv, {name: environ[name] for name in BUS_ENV if environ.get(name)}",
        "return argv, dict(environ)",
        RUN,
    ),
    (
        "command: the display switch is dropped",
        "run.py",
        '    return [*argv, "--no-display-images"]',
        "    return list(argv)",
        RUN,
    ),
    (
        "command: any program may be started",
        "run.py",
        '    if not argv or argv[0] != "systemd-run" or "--user" not in argv[1:3]:',
        "    if False:",
        RUN,
    ),
    # --- replay and fingerprint ------------------------------------------------------------
    (
        "replay: another request under the same id",
        "run.py",
        '        if entry.get("fingerprint") != fingerprint:\n            raise Refuse("request_id_used")',
        '        if False:\n            raise Refuse("request_id_used")',
        SUP,
    ),
    (
        "replay: the token is part of the fingerprint",
        "run.py",
        'if k not in ("challenge", "attest")}',
        "if k not in ()}",
        SUP,
    ),
    (
        "replay: a stored answer is not replayed",
        "run.py",
        '            return {**entry["answer"], "replayed": True}',
        "            return None",
        SUP,
    ),
    # --- hold and end ---------------------------------------------------------------------
    (
        "signals: any signal may be sent",
        "run.py",
        "        if sig not in (signal.SIGSTOP, signal.SIGKILL):",
        "        if False:",
        SUP,
    ),
    (
        "signals: a reused pid is signalled",
        "run.py",
        '        elif self.host.identity(pid) != leader.get("identity"):\n            return False\n        try:\n            self.host.killpg',
        "        try:\n            self.host.killpg",
        SUP,
    ),
    (
        "hold: it does not stop the group",
        "run.py",
        "        sent = self._signal(signal.SIGSTOP)",
        "        sent = self._signal(signal.SIGKILL)",
        SUP,
    ),
    (
        "hold: a second hold stops again",
        "run.py",
        '            _, d, _ = self._current()\n            if d.state == "held":',
        "            _, d, _ = self._current()\n            if False:",
        SUP,
    ),
    (
        "hold: offered with no client",
        "run.py",
        '            if d.state not in LIVE:\n                raise Refuse("state_not_allowed", state=d.state)\n            if not self._hold("person"):',
        '            if False:\n                raise Refuse("state_not_allowed", state=d.state)\n            if not self._hold("person"):',
        SUP,
    ),
    (
        "end: no typed phrase",
        "run.py",
        '        if body.get("confirm") != PHRASES["end"]:',
        "        if False:",
        SUP,
    ),
    (
        "end: it does not kill",
        "run.py",
        "        if not self._signal(signal.SIGKILL):\n            return",
        "        if not self._signal(signal.SIGSTOP):\n            return",
        SUP,
    ),
    (
        "end: offered while the client runs",
        "run.py",
        '    if d.state in ("held", "lost", "starting"):\n        out["end"] = _entry()',
        '    if d.state in ("held", "lost", "starting", "running"):\n        out["end"] = _entry()',
        RUN,
    ),
    (
        "keys: exit-and-go-home needs no phrase",
        "run.py",
        '            if action == "exit_home" and body.get("confirm") != PHRASES["exit_home"]:',
        "            if False:",
        SUP,
    ),
    (
        "adopt: a client of an earlier service is not frozen",
        "run.py",
        '            self._hold("host_restarted")',
        "            pass",
        SUP,
    ),
    (
        "adopt: a client without a keyboard keeps the arm lock",
        "run.py",
        "            with contextlib.suppress(Refuse):\n                self._arm_fd = take_arm_lock(self.host.arm_lock_path())",
        "            pass",
        SUP,
    ),
    (
        "end of run: the locks are kept",
        "run.py",
        "        self._release()\n        with contextlib.suppress(Exception):\n            self._prune()",
        "        with contextlib.suppress(Exception):\n            self._prune()",
        SUP,
    ),
    (
        "watcher: a client that fails is not killed",
        "run.py",
        '        if d.state == "failed":\n            self._kill(d.reason)\n            return',
        '        if d.state == "failed":\n            return',
        SUP,
    ),
    (
        "watcher: a second connection is not noticed",
        "run.py",
        "        elif (c.clients or 0) > 1:",
        "        elif False:",
        SUP,
    ),
    # --- the launcher ---------------------------------------------------------------------
    (
        "launcher: runs without a controlling terminal",
        "hostchild.py",
        "    if not _controlling_tty():",
        "    if False:",
        LIVE,
    ),
    (
        "launcher: SIGHUP kills nothing but itself",
        "hostchild.py",
        "    os.killpg(own, signal.SIGKILL)",
        "    os._exit(1)",
        LIVE,
    ),
    (
        "launcher: the client's other group is not killed",
        "hostchild.py",
        "        if other != own:",
        "        if False:",
        LIVE,
    ),
    (
        "launcher: SIGTERM and SIGINT end it",
        "hostchild.py",
        '    for name in ("SIGINT", "SIGTERM", "SIGTSTP", "SIGQUIT"):',
        "    for name in ():",
        LIVE,
    ),
    (
        "launcher: the exit record is world readable",
        "hostchild.py",
        "os.O_EXCL | os.O_NOFOLLOW, 0o600)",
        "os.O_EXCL | os.O_NOFOLLOW, 0o644)",
        LIVE,
    ),
    # --- the routes -----------------------------------------------------------------------
    (
        "api: the stack is not told about a client",
        "run_api.py",
        "\n\nregister()\n",
        "\n",
        ROUTE,
    ),
    (
        "api: a restart's client is not taken over by the probe",
        "run_api.py",
        "    sup = supervisor()\n    sup.adopt()\n    return sup.active() is not None",
        "    sup = supervisor()\n    return sup.active() is not None",
        ROUTE,
    ),
    (
        "api: hold is open to a Bearer credential",
        "run_api.py",
        '@router.post("/run/hold", dependencies=[Depends(person_only)])',
        '@router.post("/run/hold")',
        ROUTE,
    ),
    (
        "api: a key is open to a Bearer credential",
        "run_api.py",
        '@router.post("/run/key", status_code=202, dependencies=[Depends(person_only)])',
        '@router.post("/run/key", status_code=202)',
        ROUTE,
    ),
    (
        "api: start is open to a Bearer credential",
        "run_api.py",
        '@router.post("/run/start", status_code=202, dependencies=[Depends(person_only)])',
        '@router.post("/run/start", status_code=202)',
        ROUTE,
    ),
    (
        "api: a body may carry more than it says",
        "run_api.py",
        'model_config = ConfigDict(extra="forbid", strict=True)',
        'model_config = ConfigDict(extra="ignore", strict=False)',
        ROUTE,
    ),
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
        timeout=300,
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
