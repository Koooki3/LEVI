"""The AERI run of the marvel0 page: terminal D (``aeri.py execute``) hosted by
the core, with the keys a person presses in it as buttons.

What this module may do, and nothing more:

* **Start terminal D** exactly as the prepared run's RUN.json prints it for a
  terminal (the argv is built here and must equal that text byte for byte), in
  ``hostchild.py``'s AERI mode inside a ``systemd-run --user --scope`` of its
  own: a restart of the product does not end it. The core holds the pty's
  master (the keyboard); the launcher drains D's screen into the run's
  ``terminal_d.typescript``; the core only reads that file.
* **Press the keys of ``aeri_state.KEYS``**, one prompt at a time, only after the
  journal, the screen and the pty's mode agree for a second, only for the
  prompt on the page the person looked at (``seen``), only once per prompt and
  never again when it was not seen taken. The bytes come from a closed table.
* **Send Ctrl+C** (Stop): at any time D lives and has not been told; once per
  run (one retry when D did not acknowledge it). Never a signal to anything but
  D's own processes (the launcher's pty, the pid in D's heartbeat when its
  identity matches, the terminal's foreground group), never SIGSTOP (D's
  heartbeat thread would freeze with it), SIGKILL only for ``end``.
* **Read passively**: D's journal, heartbeat, service steps and screen file, the
  pty's modes, the health file, the lock table and the socket tables. No port is
  connected to. The arm lock is **D's**, taken after the phrase: this page never
  holds it.
* **Take over after a restart**: a D that outlived the core has no keyboard; it
  is sent one SIGINT (user decision of 2026-10-21) and watched.

**What the gates are worth.** Every gate here stops a mistake, not another
program of the same account: anything running as this user can send the same
request, write the same pty or signal the same process. The real barriers are
Franka Desk and the physical emergency stop; nothing on the page is an
emergency stop and a click is a key of terminal D, nothing more.

All of the machine is reached through ``Host``, which tests replace whole.
"""

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levi import paths
from levi.live import gpumgr, jsonio
from levi.setup import probes
from levi.workspace import Workspace

from . import aeri_journal as jr
from . import aeri_screen as sc
from . import aeri_state as st
from . import command, deploy, net, record, run, stack
from .aeri_state import Refuse

REL = ("marvel0", "aeri")
HOSTCHILD = "hostchild.py"
UNIT_PREFIX = "levi-aeri-termd-marvel0-"
NAME_RE = re.compile(r"aeri-[0-9]{8}-(?:ext|loc)-r[0-9]{2}")
TAB_RE = re.compile(r"[A-Za-z0-9._:-]{8,64}")
TICK_S = 0.25
LOG_KEEP = 10
HISTORY_MAX_BYTES = 256 * 1024
SCREEN_REPLAY = 256 * 1024  # bytes of the screen file replayed after a restart
SCREEN_STEP = 1 << 20  # bytes of the screen file read per look, at most
KILL_WAIT_S = 3.0
BUS_ENV = run.BUS_ENV
# The command a terminal runs, as new_run.py prints it: the pieces.
UV_WITH = "pyrealsense2==2.55.1.6486"
ENV_ALLOW = ("LEVI_AERI_CONSOLE_LANG",)
EXIT_NO_TTY = run.EXIT_NO_TTY
EXIT_SPAWN = run.EXIT_SPAWN
PHASES = ("intent", "started", "ended")


# --- what the site file says ---------------------------------------------------------------


def settings_of(main_site):
    """The ``[aeri]`` section of the site, or ``Refuse("aeri_disabled")``: a
    site file without it is an old one, and hosting is off for it."""
    section = getattr(main_site, "aeri", None)
    if section is None:
        raise Refuse("aeri_disabled", configured=False)
    return section


def _control_enabled() -> bool:
    """``[aeri] enabled`` of the control file. Anything that stops it being read
    as true (no module, no file, a section that does not parse) is off."""
    try:
        from . import control_site

        loaded = control_site.load()
        section = getattr(loaded, "aeri", None)
        return bool(
            loaded is not None and section is not None and section.enabled is True
        )
    except Exception:  # noqa: BLE001 - unreadable switches are off
        return False


def _snapshot() -> dict:
    from . import aeri_ready

    return aeri_ready.snapshot()


def _e_active() -> bool:
    """Whether the hosted evaluation client (terminal E) is on. Only E: the
    page's own D is asked separately."""
    from . import run_api

    sup = run_api.supervisor()
    sup.adopt()
    return sup.active() is not None


def _git(worktree: str, *args: str, home: str = "") -> str | None:
    """Output of a read-only git command in the AERI worktree, or None."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": home or "/nonexistent",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        done = subprocess.run(
            ["git", "-C", worktree, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _units() -> list:
    from . import status

    part = status.units_part()
    if part.get("state") != "ok":
        return []
    return [str(i.get("unit") or i.get("name")) for i in part.get("items") or []]


def _swap_clean(root) -> bool | None:
    try:
        from levi.automatic import fr3_policy_swap

        return not fr3_policy_swap.pending(root)
    except Exception:  # noqa: BLE001 - unreadable: not known clean
        return None


def scope_launch(unit, hostchild, spec, python, env, environ) -> tuple:
    """``(argv, env of the process)``: a transient scope of its own, then
    ``env -i`` with the allowlist, then the launcher."""
    argv = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "--description=AERI terminal D, hosted by the marvel0 page",
        "--",
        "/usr/bin/env",
        "-i",
        *[f"{name}={value}" for name, value in env.items()],
        python,
        "-I",
        hostchild,
        spec,
    ]
    return argv, {name: environ[name] for name in BUS_ENV if environ.get(name)}


@dataclass
class Host:
    """Everything the run reaches outside Python. Tests build one with fakes (a
    launch without a scope, a clock, a lock folder); ``production_host()`` builds
    the real one."""

    spawn: Callable = run._spawn
    # (unit, hostchild, spec, python, allowlist, caller's environ) -> (argv, env)
    launch: Callable = scope_launch
    # The argv of terminal D from the prepared command's. Tests swap in the
    # fake D; the real one is never run by a test.
    client_argv: Callable = lambda argv: argv
    openpty: Callable = os.openpty
    killpg: Callable = os.killpg
    kill: Callable = os.kill
    getpgid: Callable = os.getpgid
    tcgetattr: Callable = termios.tcgetattr
    tcgetpgrp: Callable = os.tcgetpgrp
    proc_state: Callable = run.proc_state
    identity: Callable = lambda pid: run._identity(pid)
    run_lock_path: Callable = lambda: run._hub() / run.LOCK_FILE
    arm_lock_path: Callable = lambda: Path(command.marvel0_site.ARM_LOCK_FILE)
    locks_path: str = "/proc/locks"
    health_file: Callable = stack._health_file
    net_table: Callable = net.read
    snapshot: Callable = _snapshot
    e_active: Callable = _e_active
    control_enabled: Callable = _control_enabled
    cgroup: Callable = stack._read_self_cgroup
    in_main_checkout: Callable = stack._in_main_checkout
    git: Callable = _git
    units: Callable = _units
    swap_clean: Callable = _swap_clean
    environ: dict = field(default_factory=lambda: os.environ)
    python: str = sys.executable
    hostchild: str = str(Path(__file__).with_name(HOSTCHILD))
    now: Callable = time.time
    monotonic: Callable = time.monotonic
    sleep: Callable = time.sleep
    # Run the watcher in a thread of its own (tests call ``tick`` by hand).
    background: bool = False


def production_host() -> Host:
    return Host(background=True)


def direct_launch(unit, hostchild, spec, python, env, environ) -> tuple:
    """The same launch without a scope (tests): the launcher with the allowlist
    as its environment."""
    return [python, "-I", hostchild, spec], dict(env)


# --- small things --------------------------------------------------------------------------


def child_env(environ, aeri) -> dict:
    """D's whole environment: an allowlist, never a copy of ours. No ``LEVI_*``
    (the UI token) but the console language, no Hugging Face token, no ROS, no
    conda, no display. The bus variables are there because D starts its VLM and
    policy as ``systemd-run --user`` units."""
    home = environ.get("HOME") or ""
    if not home.startswith("/"):
        raise Refuse("start_failed")
    env = {
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "USER": environ.get("USER") or Path(home).name,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
        "no_proxy": "localhost,127.0.0.1,::1",
        "LEVI_AERI_CONSOLE_LANG": aeri.console_lang,
    }
    if aeri.uv_offline:
        env["UV_OFFLINE"] = "1"
    for name in BUS_ENV:
        if environ.get(name):
            env[name] = environ[name]
    return env


def hosted_argv(aeri, name: str, run_json: dict) -> list:
    """The argv of terminal D for a prepared run: the very command RUN.json
    prints for a terminal, as a list (no shell). ``prepared_changed`` when the
    two do not read the same."""
    folder = f"{aeri.runs_root}/{name}"
    argv = [
        "env",
        "-u",
        "VIRTUAL_ENV",
        f"PYTHONPATH={aeri.worktree}",
        "uv",
        "run",
        "--no-sync",
        "--with",
        UV_WITH,
        "python",
        "-B",
        f"{aeri.worktree}/scripts/aeri.py",
        "execute",
        "--config",
        f"{folder}/job.yaml",
        "--binding",
        f"{folder}/binding.json",
        "--expect-plan",
        str(run_json.get("plan_sha256")),
        "--expect-binding",
        str(run_json.get("binding_sha256")),
    ]
    printed = str(run_json.get("execute") or "")
    prefix = f"cd {shlex.quote(aeri.openpi_root)} && "
    if not printed.startswith(prefix) or printed[len(prefix) :] != shlex.join(argv):
        raise Refuse("prepared_changed", what="execute")
    return argv


def _sha256_of(path) -> str | None:
    fd = jr.open_regular(path)
    if fd is None:
        return None
    try:
        digest = hashlib.sha256()
        while True:
            part = os.read(fd, 1 << 20)
            if not part:
                break
            digest.update(part)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def state_dir(avoid, workspace=None, create=True) -> Path:
    """``<workspace state>/marvel0/aeri`` (``0700``, no link in it, not inside
    source data), like the folders of the deploy and the client."""
    base = (workspace or Workspace.at(paths.workspace())).state
    target = base / Path(*REL)
    if record.UNSAFE.search(str(target)):
        raise record.StateDirUnusable("the folder path has characters systemd misreads")
    for root in avoid:
        if target.is_relative_to(Path(root).expanduser().resolve()):
            raise record.StateDirUnusable("the folder would lie inside source data")
    if create:
        base.mkdir(parents=True, exist_ok=True)
    for folder in (
        target.parent,
        target,
        *(target / name for name in record.SUBFOLDERS),
    ):
        record._check_dir(folder, create)
    return target


class Challenges:
    """The 30-second tokens a page asks for before it starts D. Bound to what
    the page saw (the run name, the readiness digest); used once. **It stops
    mistakes only.** Memory only; a restart voids all of them."""

    def __init__(self, clock=time.monotonic, wall=time.time, ttl=st.CHALLENGE_TTL_S):
        self._clock, self._wall, self._ttl = clock, wall, ttl
        self._lock = threading.Lock()
        self._open: dict = {}
        self._closed: dict = {}

    def issue(self, purpose: str, bind: dict) -> dict:
        token = secrets.token_urlsafe(16)
        entry = {
            "purpose": purpose,
            "bind": dict(bind),
            "deadline": self._clock() + self._ttl,
            "expires_at": round(self._wall() + self._ttl, 3),
        }
        with self._lock:
            self._forget()
            self._open[token] = entry
            while len(self._open) > 32:
                self._open.pop(next(iter(self._open)))
        return {
            "challenge": token,
            "expires_at": entry["expires_at"],
            "ttl_s": self._ttl,
        }

    def consume(self, token, purpose: str, bind: dict) -> None:
        with self._lock:
            self._forget()
            entry = self._open.pop(token, None) if isinstance(token, str) else None
            if entry is None:
                closed = self._closed.get(token) if isinstance(token, str) else None
                raise Refuse(f"challenge_{closed}" if closed else "challenge_invalid")
            self._close(token, "used")
        if entry["purpose"] != purpose:
            raise Refuse("challenge_invalid")
        if entry["bind"] != bind:
            raise Refuse("challenge_stale")

    def void(self) -> None:
        with self._lock:
            self._open.clear()

    def _close(self, token: str, how: str) -> None:
        self._closed[token] = how
        while len(self._closed) > 128:
            self._closed.pop(next(iter(self._closed)))

    def _forget(self) -> None:
        now = self._clock()
        for token in [t for t, e in self._open.items() if e["deadline"] < now]:
            del self._open[token]
            self._close(token, "expired")


def http(exc: Refuse):
    """``exc`` as the answer of a route (``errors.fail``)."""
    from .errors import fail

    return fail(exc.status, exc.code, exc.message, **exc.extra)


@dataclass
class Pending:
    """A key that was written and not yet seen taken."""

    action: str
    prompt_id: str
    at: float  # wall
    mono: float
    base: dict


# --- the supervisor ------------------------------------------------------------------------


class Supervisor:
    """The one hosted terminal D of this process: start, state, keys, stop, end
    and log. ``site_loader`` gives the marvel0 site (or raises the 412 answer),
    ``workspace`` is the folder the run record lives in (default: the process's)."""

    def __init__(self, host, site_loader=None, workspace=None):
        if site_loader is None:
            from . import site

            site_loader = site.require
        self.host = host
        self.site_loader = site_loader
        self.workspace = workspace
        self.challenges = Challenges(host.monotonic, host.now)
        self._secret = secrets.token_bytes(16)
        self._guard = threading.Lock()  # one write at a time, in this process
        self._mutex = threading.RLock()  # the fields below
        self._rec: dict | None = None
        self._dir: Path | None = None
        self._child = None
        self._master: int | None = None
        self._adopted = False  # adopt() ran
        self._found = False  # this run was found again after a restart
        self._mem = st.Memory()
        self._seq = 0
        self._seq_sig = None
        self._buf = sc.ScreenBuf()
        self._lex = None
        self._out_off = 0
        self._jr: jr.JournalReader | None = None
        self._steps: jr.StepsReader | None = None
        self._runner_seen = False
        self._keyed: set = set()
        self._pending: Pending | None = None
        self._unconfirmed: str | None = None
        self._last_taken: float | None = None
        self._verdict_round: str | None = None
        self._tabs: dict = {}
        self._cache: tuple | None = None
        self._watcher: threading.Thread | None = None
        self._finished = False
        self._answered = False  # the phrase was sent

    # ---- small things

    def _iso(self) -> str:
        return record.iso(self.host.now())

    def _aeri(self, main_site):
        return settings_of(main_site)

    def _profile_of(self, name):
        return st.PROFILES.get(name)

    def _require_real(self) -> None:
        h = self.host
        if stack.profile(h.cgroup(), h.in_main_checkout())["profile"] != "real":
            raise Refuse("not_product")

    def _folder(self, main_site, create) -> Path:
        avoid = (
            main_site.rollout_root,
            main_site.checkpoints_root,
            main_site.openpi_checkout,
        )
        try:
            return state_dir(avoid, self.workspace, create=create)
        except record.StateDirUnusable:
            raise Refuse("start_failed", why="state_dir") from None

    def _existing_folder(self, main_site) -> Path:
        directory = self._folder(main_site, create=False)
        if not directory.is_dir():
            raise Refuse("state_not_allowed", state="idle")
        self._dir = directory
        return directory

    def _request_id(self, body) -> str:
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not record.REQUEST_ID.fullmatch(
            request_id
        ):
            raise Refuse("request_id_invalid")
        return request_id

    def _audit(self, event: str, **fields) -> None:
        """One line of the history: who (a person, through the product), what and
        when. Never a transcript, a path or D's output."""
        if self._dir is None:
            return
        line = {
            "time": self._iso(),
            "actor": "person",
            "via": "product",
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        with contextlib.suppress(Exception):
            path = self._dir / record.HISTORY
            # created private (append_line would use the umask)
            os.close(
                os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_APPEND
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                )
            )
            jsonio.append_line(path, line, max_bytes=HISTORY_MAX_BYTES, keep=1)

    def _update(self, **fields) -> dict:
        """``fields`` into the run record: in memory first (a safety action never
        waits for the disk), then on disk."""
        with self._mutex:
            self._rec = {**(self._rec or {}), **fields}
            self._cache = None
            if self._dir is not None:
                with contextlib.suppress(OSError, record.RecordCorrupt):
                    record.write_record(self._dir, self._rec)
            return self._rec

    # ---- what is read

    def _paths(self, aeri, name):
        run_dir = Path(aeri.rollout_root) / ".aeri" / "runs" / name
        return {
            "run": run_dir,
            "journal": run_dir / jr.JOURNAL,
            "runner": run_dir / jr.RUNNER,
            "steps": Path(aeri.rollout_root)
            / ".aeri"
            / "units"
            / f"{name}-steps.jsonl",
            "queue": Path(aeri.runs_root) / name / "queue",
            "typescript": Path(aeri.runs_root) / name / "terminal_d.typescript",
            "events": Path(aeri.runs_root) / name / "terminal_d.events",
        }

    def _begin_readers(self, aeri, name, lang, base=0) -> None:
        p = self._paths(aeri, name)
        self._buf = sc.ScreenBuf(base=base)
        self._out_off = base
        profile = st.PROFILES.get((self._rec or {}).get("profile"))
        self._lex = (
            sc.Lexicon(profile.data, sc.prompts()["foreign"], lang)
            if profile is not None
            else None
        )
        self._jr = jr.JournalReader(p["journal"], p["run"])
        self._steps = jr.StepsReader(p["steps"])
        self._runner_seen = False
        self._mem = st.Memory()
        self._seq, self._seq_sig = 0, None
        self._keyed = set()
        self._pending, self._unconfirmed, self._last_taken = None, None, None
        self._verdict_round = None
        self._finished = False
        self._answered = False

    def _feed_screen(self, aeri, name) -> None:
        path = self._paths(aeri, name)["typescript"]
        fd = jr.open_regular(path)
        if fd is None:
            return
        try:
            size = os.fstat(fd).st_size
            if size < self._out_off:  # not the file we followed
                self._buf = sc.ScreenBuf()
                self._out_off = 0
            budget = SCREEN_STEP
            while self._out_off < size and budget > 0:
                chunk = os.pread(fd, min(1 << 16, size - self._out_off), self._out_off)
                if not chunk:
                    break
                self._out_off += len(chunk)
                budget -= len(chunk)
                self._buf.feed(chunk)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _termios(self) -> tuple:
        if self._master is None:
            return None, None
        try:
            lflag = self.host.tcgetattr(self._master)[3]
        except (OSError, termios.error):
            return None, None
        return bool(lflag & termios.ICANON), bool(lflag & termios.ISIG)

    def _proc(self, rec) -> tuple:
        """``(alive, exit)``: whether the launcher lives and, once it is gone,
        how it ended. The launcher is our own child unless we were restarted."""
        h = self.host
        if rec.get("phase") == "ended":
            return False, rec.get("exit")
        child = self._child
        if child is not None:
            code = child.poll()
            if code is not None:
                if code >= 0:
                    return False, {"code": code, "signal": None}
                return False, {"code": None, "signal": -code}
            return h.proc_state(child.pid) is not None, None
        leader = rec.get("leader") or {}
        pid = leader.get("pid")
        if not isinstance(pid, int) or h.identity(pid) != leader.get("identity"):
            return False, None
        return h.proc_state(pid) is not None, None

    def _facts(self, aeri, now) -> st.Facts:
        rec = self._rec
        if rec is None:
            return st.Facts(now=now)
        name = rec["name"]
        alive, ex = self._proc(rec)
        if ex is None and not alive:
            ex = rec.get("exit") or self._exit_file(rec)
        profile = self._profile_of(rec.get("profile"))
        self._feed_screen(aeri, name)
        view = sc.read(self._buf, self._lex) if self._lex else sc.ScreenView()
        j = self._jr.poll() if self._jr else jr.JView()
        steps = self._steps.poll() if self._steps else jr.SView()
        p = self._paths(aeri, name)
        runner = jr.read_runner(p["runner"])
        wall = self.host.now()
        age = jr.runner_age_s(runner, wall)
        if runner.present and runner.state == "running":
            self._runner_seen = True
        pending, oldest = (
            jr.queue_state(p["queue"], wall)
            if rec.get("mode") == "external"
            else (0, None)
        )
        icanon, isig = self._termios()
        began = rec.get("created_epoch")
        sent = rec.get("stop_sent_epoch")
        return st.Facts(
            now=now,
            record=rec,
            profile=profile,
            alive=alive,
            exit=ex,
            keyboard=self._master is not None and not self._found,
            adopted=self._found,
            j=j,
            screen=view,
            runner=runner,
            runner_age_s=age,
            runner_seen=self._runner_seen,
            steps=steps,
            icanon=icanon,
            isig=isig,
            queue_pending=pending,
            queue_oldest_s=oldest,
            stop_written="idle"
            if sent is None
            else ("retried" if rec.get("stop_retried") else "sent"),
            stop_age_s=None if sent is None else max(0.0, now - sent),
            started_age_s=now - began if isinstance(began, (int, float)) else None,
            external=rec.get("mode") == "external",
            verdict_round_id=self._verdict_round,
        )

    def _exit_file(self, rec) -> dict | None:
        name = rec.get("exit_file")
        if self._dir is None or not isinstance(name, str):
            return None
        try:
            raw, _, _ = probes.read_regular(self._dir / "logs" / name, 4096)
            data = json.loads(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        code, sig = data.get("code"), data.get("signal")
        return {
            "code": code if isinstance(code, int) else None,
            "signal": sig if isinstance(sig, int) else None,
        }

    # ---- the state, as the document and the watcher both see it

    def _aeri_or_none(self):
        try:
            return self._aeri(self.site_loader())
        except Exception:  # noqa: BLE001 - no site: nothing can be read
            return None

    def _current(self) -> tuple:
        """``(facts, derived, context)`` read afresh."""
        main_site = self.site_loader()
        aeri = self._aeri(main_site)
        now = self.host.now()
        f = self._facts(aeri, now)
        d = st.derive(f, self._mem)
        self._mem = d.mem
        if f.record and f.record.get("phase") == "started" and not d.alive:
            self._finish(f, aeri)
            f = self._facts(aeri, now)
            d = st.derive(f, self._mem)
            self._mem = d.mem
        return f, d, self._observe(f, d)

    def _monitor(self, now) -> tuple:
        try:
            path, stale_s = self.host.health_file()
        except Exception:  # noqa: BLE001 - no configuration: unknown
            return "unknown", None
        health = stack.read_health(path, min(float(stale_s), run.MONITOR_STALE_S), now)
        return health["state"], health.get("age_s")

    def _presence_age(self, tab=None) -> float | None:
        now = self.host.monotonic()
        if tab is not None:
            at = self._tabs.get(tab)
            return None if at is None else now - at
        ages = [now - at for at in self._tabs.values()]
        return min(ages) if ages else None

    def _observe(self, f: st.Facts, d: st.Derived) -> st.Context:
        """The context for ``f``, and the bookkeeping that follows from what was
        read: the sequence, the evidence that a key was taken."""
        with self._mutex:
            sig = st.seq_signature(d, f.keyboard)
            if sig != self._seq_sig:
                self._seq_sig = sig
                self._seq += 1
                self._cache = None
            self._check_pending(f, d)
            state, age = self._monitor(f.now)
            c = st.Context(
                now=f.now,
                secret=self._secret,
                seq=self._seq,
                enabled=bool(self.host.control_enabled()),
                run_id=(f.record or {}).get("run_id"),
                monitor_state=state,
                monitor_age_s=age,
                presence_age_s=self._presence_age(),
                keyed=frozenset(self._keyed),
                unconfirmed=self._unconfirmed,
                pending=self._pending is not None,
                last_taken_at=self._last_taken,
            )
            return c

    # ---- keys: the evidence that one was taken

    def _baseline(self, f: st.Facts) -> dict:
        j, screen = f.j, f.screen
        return {
            "commits": j.commit_count,
            "seq": j.seq,
            "released": len(j.released),
            "verdicts": len(j.verdicts),
            "confirmed": j.confirmed_seq,
            "rejected": j.rejected_seq,
            "startup": j.startup_requested_seq,
            "lines": screen.lines,
            "bytes": screen.bytes,
            "svc": screen.counts.get("svc", 0),
            "kinds": dict(screen.counts),
            "interrupt": j.interrupt_seq,
        }

    def _taken(self, p: Pending, f: st.Facts) -> bool:
        """Whether the evidence of §4.3 of the design has appeared for the key
        ``p``: a line on D's screen or a note in its journal. Nothing less."""
        j, screen, base, action = f.j, f.screen, p.base, p.action
        kinds = screen.counts

        def more(kind) -> bool:
            return kinds.get(kind, 0) > base["kinds"].get(kind, 0)

        if not f.alive:
            return True
        if action in ("execute", "execute_variant"):
            return screen.counts.get("svc", 0) > base["svc"] or j.lines > 0
        if action == "inventory_yes":
            return j.confirmed_seq > base["confirmed"]
        if action == "inventory_no":
            return j.rejected_seq > base["rejected"]
        if action == "home_o":
            return more("home_p") or j.startup_requested_seq > base["startup"]
        if action == "home_p":
            return more("home_h") or j.startup_requested_seq > base["startup"]
        if action == "home_h":
            return (
                j.startup_requested_seq > base["startup"]
                or j.commit_count > base["commits"]
            )
        if action == "release_round":
            return len(j.released) > base["released"]
        if action in st.VERDICTS:
            return (
                more("verdict_taken")
                or more("verdict_refused")
                or len(j.verdicts) > base["verdicts"]
            )
        if action == "resume_ask":
            return more("confirm_resume") or j.commit_count > base["commits"]
        if action == "resume_confirm":
            return j.commit_count > base["commits"]
        if action == "resume_cancel":
            return more("confirm_cancelled")
        if action == "quit":
            return (
                bool(screen.completed)
                or j.to_state == "COMPLETED"
                or j.commit_count > base["commits"]
            )
        return False

    def _check_pending(self, f: st.Facts, d: st.Derived) -> None:
        p = self._pending
        if p is None:
            return
        if self._taken(p, f):
            self._pending = None
            self._last_taken = f.now
            return
        if self.host.monotonic() - p.mono >= st.KEY_NOT_TAKEN_S:
            self._pending = None
            self._unconfirmed = p.prompt_id
            self._audit("key_unconfirmed", key=p.action, prompt_id=p.prompt_id)

    # ---- the document

    def view(self) -> dict:
        """The ``levi.marvel0.aeri_run.v1`` document."""
        self.adopt()
        h = self.host
        with self._mutex:
            cached = self._cache
            live = self._rec is not None and self._rec.get("phase") in (
                "intent",
                "started",
            )
        ttl = 0.4 if live else 2.0
        if cached is not None and h.monotonic() - cached[0] < ttl:
            return cached[1]
        with self._mutex:
            body = self._document()
            self._cache = (h.monotonic(), body)
            return body

    def _document(self) -> dict:
        main_site = None
        aeri = None
        try:
            main_site = self.site_loader()
            aeri = settings_of(main_site)
        except Refuse:
            pass
        now = self.host.now()
        if aeri is None or self._rec is None:
            f = st.Facts(now=now)
            if self._rec is not None:
                f.record = self._rec
            d = st.derive(f, st.Memory())
            c = st.Context(
                now=now,
                secret=self._secret,
                seq=self._seq,
                enabled=aeri is not None and bool(self.host.control_enabled()),
                presence_age_s=self._presence_age(),
                start_why=self._start_why(aeri is not None),
                locks=self._idle_locks(main_site),
            )
            return st.document(f, d, c)
        f, d, c = self._current()
        c.tail = [deploy.page_line(line) for line in self._buf.tail()]
        c.history = self._history()
        # D's own pid when its heartbeat names it (what `aeri_page_stop.py` signals),
        # else the launcher's
        c.pid = (f.runner.pid if f.runner.present else None) or (
            (f.record or {}).get("leader") or {}
        ).get("pid")
        c.exit = d.state == "ended" and (f.exit or f.record.get("exit")) or None
        c.locks = self._locks(main_site, f, d)
        c.after_exit = (
            (f.record or {}).get("after_exit") if d.state == "ended" else None
        )
        c.start_why = self._start_why(True) if d.state in ("ended", "idle") else None
        return st.document(f, d, c)

    def _start_why(self, configured: bool) -> dict | None:
        if not configured:
            return st.why("aeri_disabled", configured=False)
        try:
            if self.host.e_active():
                return st.why("readiness_failed", first="no_e_run")
        except Exception:  # noqa: BLE001 - a probe that fails is not "free"
            return st.why("readiness_failed", first="no_e_run")
        return None

    def _history_all(self) -> list:
        if self._dir is None:
            return []
        path = self._dir / record.HISTORY
        try:
            raw, _, _ = probes.read_regular(path, 1 << 18)
        except OSError:
            return []
        out = []
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event") in (
                "key",
                "stop",
                "end",
                "start",
            ):
                out.append(row)
        return out

    def _history(self) -> list:
        rows = []
        for row in self._history_all()[-st.HISTORY_KEPT :]:
            rows.append(
                {
                    "time": row.get("time"),
                    "key": row.get("key") or row.get("event"),
                    "byte": row.get("byte"),
                    "state": row.get("state"),
                    "prompt_id": row.get("prompt_id"),
                    "round": row.get("round"),
                }
            )
        return rows

    def _idle_locks(self, main_site) -> dict:
        return {
            "arm": "free",
            "gpu": "free",
            "ports": {"8000": "free", "8100": "free", "8110": "free"},
        }

    def _locks(self, main_site, f: st.Facts, d: st.Derived) -> dict:
        h = self.host
        answered = self._answered or bool(f.j.lines)

        def lock(path) -> str:
            try:
                holders = gpumgr.flock_holders(path, locks=h.locks_path)
            except Exception:  # noqa: BLE001
                return "unknown"
            if holders is None:
                return "unknown"
            if not holders:
                return "free"
            return "held_by_d" if d.alive and answered else "held_elsewhere"

        ports = {}
        try:
            table = h.net_table()
        except Exception:  # noqa: BLE001
            table = None
        for port in (8000, 8100, 8110):
            if table is None or not table.ok:
                ports[str(port)] = "unknown"
            elif not net.listeners(table, port):
                ports[str(port)] = "free"
            else:
                ports[str(port)] = "d_unit" if d.alive and f.steps.steps else "other"
        arm = lock(h.arm_lock_path())
        gpu = lock(main_site.gpu_lock_file)
        return {"arm": arm, "gpu": gpu, "ports": ports}

    def active(self) -> dict | None:
        """The running D as a job (``levi stop`` refuses while it runs), or
        None. Memory only."""
        with self._mutex:
            rec = self._rec
            if rec is None or rec.get("phase") not in ("intent", "started"):
                return None
            began = rec.get("created_epoch") or self.host.now()
            return {
                "kind": "marvel0-aeri",
                "id": rec.get("run_id"),
                "running_seconds": round(self.host.now() - began),
            }

    # ---- one request at a time

    @contextlib.contextmanager
    def _operation(self, action):
        if not self._guard.acquire(blocking=False):
            raise Refuse("busy")
        try:
            lock = stack.StackLock(
                self.host.run_lock_path(), self.host.locks_path, self.host.now
            )
            try:
                with lock.hold("marvel0-aeri", action):
                    yield
            except stack.Refuse as exc:
                raise Refuse("busy") from exc
        finally:
            self._guard.release()

    @contextlib.contextmanager
    def _run_lock_if_free(self, action):
        """The machine-wide run lock when nobody holds it. A stop-direction
        action takes it so another instance sees it, and goes on without it when
        it is busy: a Ctrl+C never waits."""
        lock = stack.StackLock(
            self.host.run_lock_path(), self.host.locks_path, self.host.now
        )
        with contextlib.ExitStack() as held:
            with contextlib.suppress(stack.Refuse):
                held.enter_context(lock.hold("marvel0-aeri", action))
            yield

    def _replayed(self, directory, request_id, fingerprint, locked):
        try:
            entry = record.get_request(directory, request_id)
        except record.RecordCorrupt:
            entry = {"status": "sent", "fingerprint": fingerprint}
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise Refuse("request_id_used")
        if entry.get("status") == "done" and isinstance(entry.get("answer"), dict):
            return {**entry["answer"], "replayed": True}
        if not locked:
            return None
        return {
            "schema": st.RESULT_SCHEMA,
            "replayed": True,
            "request_id": request_id,
            "accepted": False,
            "reason": "request_incomplete",
        }

    def _transaction(self, action, directory, request_id, body, work):
        """Run ``work(mark_sent)`` once for this request, remembering the answer.
        ``mark_sent`` writes (and syncs) that the byte is about to go, so a
        crash after it can never send it again."""
        shown = {
            k: v
            for k, v in body.items()
            if k not in ("challenge", "attest", "token", "seen", "tab_id")
        }
        fingerprint = record.fingerprint(action, shown)
        stored = self._replayed(directory, request_id, fingerprint, locked=False)
        if stored:
            return stored
        with self._operation(action):
            stored = self._replayed(directory, request_id, fingerprint, locked=True)
            if stored:
                return stored
            entry = {"fingerprint": fingerprint, "action": action}
            record.put_request(
                directory,
                request_id,
                {**entry, "status": "accepted", "time": self._iso()},
            )
            sent = []

            def mark_sent():
                record.put_request(
                    directory,
                    request_id,
                    {**entry, "status": "sent", "time": self._iso()},
                    replace=True,
                )
                sent.append(True)

            try:
                answer = work(mark_sent)
            except Refuse as exc:
                if not sent:
                    record.drop_request(directory, request_id)
                self._audit(f"{action}_refused", request_id=request_id, code=exc.code)
                raise
            record.put_request(
                directory,
                request_id,
                {**entry, "status": "done", "answer": answer, "time": self._iso()},
                replace=True,
            )
            self._cache = None
            return answer

    # ---- start

    def _run_json(self, aeri, name) -> dict:
        """RUN.json of a prepared run, checked against the files it names."""
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise Refuse("not_prepared", name=str(name)[:60])
        folder = Path(aeri.runs_root) / name
        try:
            raw, truncated, _ = probes.read_regular(folder / "RUN.json", 1 << 18)
            data = json.loads(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            raise Refuse("not_prepared", name=name) from None
        if truncated or not isinstance(data, dict) or data.get("name") != name:
            raise Refuse("not_prepared", name=name)
        for key in (
            "mode",
            "task",
            "rounds",
            "steps",
            "plan_sha256",
            "binding_sha256",
            "execute",
            "confirm",
        ):
            if key not in data:
                raise Refuse("prepared_changed", what=key)
        if (
            data["mode"] not in ("external", "local")
            or data["confirm"] != f"execute {name}"
        ):
            raise Refuse("prepared_changed", what="confirm")
        files = data.get("files_sha256")
        if isinstance(files, dict):
            for fname in ("job.yaml", "binding.json"):
                want = files.get(fname)
                if want is not None and _sha256_of(folder / fname) != want:
                    raise Refuse("prepared_changed", what=fname)
        elif (
            _sha256_of(folder / "job.yaml") is None
            or _sha256_of(folder / "binding.json") is None
        ):
            raise Refuse("prepared_changed", what="files")
        return data

    def _code_gate(self, aeri) -> None:
        """The AERI code is the pinned commit, clean, and the one the profile
        was written from."""
        h = self.host
        home = h.environ.get("HOME") or ""
        head = h.git(aeri.worktree, "rev-parse", "HEAD", home=home)
        dirty = h.git(aeri.worktree, "status", "--porcelain", home=home)
        if (
            head is None
            or dirty is None
            or not head.strip().startswith(aeri.pin_sha)
            or dirty.strip()
        ):
            raise Refuse("code_pin_mismatch")
        if not st.profile_known(aeri.profile):
            raise Refuse("profile_unknown", profile=str(aeri.profile))
        ok, detail = st.profile_matches(aeri.worktree, aeri.profile)
        if not ok:
            raise Refuse("profile_unknown", profile=aeri.profile, detail=detail)

    def _not_driven_elsewhere(self, aeri, name) -> None:
        """The run name is free in every registry and no other terminal drives
        it."""
        p = self._paths(aeri, name)
        home = Path(self.host.environ.get("HOME") or "/nonexistent")
        if (home / ".levi-aeri" / "runs" / f"{name}.json").exists() or p[
            "run"
        ].exists():
            raise Refuse("run_name_taken", name=name)
        folder = Path(aeri.runs_root) / name
        if (folder / "terminal_d.in").exists():
            raise Refuse("driven_elsewhere", name=name)
        events = folder / "terminal_d.events"
        try:
            text = events.read_text(errors="replace")
        except OSError:
            text = ""
        if "start:" in text and "terminal D exited" not in text:
            raise Refuse("driven_elsewhere", name=name)

    def _start_gate(self, main_site, aeri, name) -> tuple:
        """Everything a start needs, read once: ``(RUN.json, readiness)``."""
        if not self.host.control_enabled():
            raise Refuse("aeri_disabled", configured=True)
        self._require_real()
        with self._mutex:
            rec = self._rec
            if rec is not None and rec.get("phase") in ("intent", "started"):
                raise Refuse("d_active")
        try:
            if self.host.e_active():
                raise Refuse("readiness_failed", first="no_e_run")
        except Refuse:
            raise
        except Exception:  # noqa: BLE001 - an unreadable probe does not rule E out
            raise Refuse("readiness_failed", first="no_e_run") from None
        data = self._run_json(aeri, name)
        hosted_argv(aeri, name, data)  # the printed command reads the same
        self._code_gate(aeri)
        self._not_driven_elsewhere(aeri, name)
        snap = self.host.snapshot()
        bad = [r for r in snap.get("rows", []) if r.get("level") != "ok"]
        if bad or not snap.get("ready"):
            first = bad[0]["code"] if bad else "readiness"
            raise Refuse("readiness_failed", first=first, rows=bad[:6])
        return data, snap

    def challenge(self, body) -> dict:
        """A one-time token for a start of the prepared run ``name`` (the only
        purpose: a key's token is in the document)."""
        self.adopt()
        if body.get("purpose") != "start":
            raise Refuse("challenge_invalid")
        main_site = self.site_loader()
        aeri = settings_of(main_site)
        name = body.get("name")
        _data, snap = self._start_gate(main_site, aeri, name)
        issued = self.challenges.issue(
            "start", {"name": name, "readiness": snap["sha256"]}
        )
        return {
            "schema": st.RESULT_SCHEMA,
            "purpose": "start",
            "name": name,
            "readiness_sha256": snap["sha256"],
            **issued,
        }

    def start(self, body) -> dict:
        """Start D for a prepared run (person only, product LEVI only)."""
        self.adopt()
        request_id = self._request_id(body)
        main_site = self.site_loader()
        aeri = settings_of(main_site)
        name = body.get("name")
        phrase = st.START_PHRASE.format(name=name)
        if body.get("confirm") != phrase:
            raise Refuse("confirm_required", phrase=phrase)
        attest = body.get("attest") or {}
        if not all(attest.get(k) is True for k in st.ATTEST):
            raise Refuse("attest_required")
        directory = self._folder(main_site, create=True)
        self._dir = directory
        return self._transaction(
            "start",
            directory,
            request_id,
            body,
            lambda mark_sent: self._start(main_site, aeri, directory, request_id, body),
        )

    def _start(self, main_site, aeri, directory, request_id, body) -> dict:
        name = body["name"]
        with self._mutex:
            if self._rec is None:
                with contextlib.suppress(record.RecordCorrupt):
                    self._rec = record.read_record(directory)
        data, snap = self._start_gate(main_site, aeri, name)
        self.challenges.consume(
            body.get("challenge"), "start", {"name": name, "readiness": snap["sha256"]}
        )
        return self._launch(main_site, aeri, directory, request_id, name, data)

    def _launch(self, main_site, aeri, directory, request_id, name, data) -> dict:
        h = self.host
        argv = h.client_argv(hosted_argv(aeri, name, data))
        if not os.path.isdir(aeri.openpi_root) or not os.path.isfile(h.hostchild):
            raise Refuse("start_failed", why="folders")
        now = h.now()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        stem = next(
            (
                f"termd-{stamp}{'' if n == 1 else f'-{n}'}"
                for n in range(1, 100)
                if not os.path.lexists(
                    directory / "logs" / f"termd-{stamp}{'' if n == 1 else f'-{n}'}.log"
                )
            ),
            None,
        )
        if stem is None:
            raise Refuse("start_failed", why="log_name")
        run_id = f"{time.strftime('%Y%m%dT%H%M%S', time.localtime(now))}-{secrets.token_hex(3)}"
        log_name, exit_name, spec_name = (
            f"{stem}.log",
            f"{stem}.exit.json",
            f"{stem}.spec.json",
        )
        unit = f"{UNIT_PREFIX}{run_id}"
        p = self._paths(aeri, name)
        rec = {
            "schema": st.RECORD_SCHEMA,
            "run_id": run_id,
            "name": name,
            "phase": "intent",
            "request_id": request_id,
            "created_epoch": now,
            "created_at": self._iso(),
            "mode": data["mode"],
            "task": data["task"],
            "rounds": data["rounds"],
            "steps": data["steps"],
            "profile": aeri.profile,
            "console_lang": aeri.console_lang,
            "plan_sha256": data["plan_sha256"],
            "binding_sha256": data["binding_sha256"],
            "argv_sha256": hashlib.sha256("\0".join(argv).encode()).hexdigest(),
            "unit": unit,
            "runner_json": str(p["runner"]),
            "log_file": log_name,
            "exit_file": exit_name,
            "stop_sent_epoch": None,
            "stop_retried": False,
        }
        master = slave = log_fd = None
        try:
            with self._mutex:
                self._reset_run(aeri, name, rec)
                self._dir = directory
                self._update()
            master, slave = h.openpty()
            self._quiet_pty(master, slave)
            log_fd = os.open(
                directory / "logs" / log_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            spec_path = directory / "logs" / spec_name
            self._write_private(
                spec_path,
                json.dumps(
                    {
                        "mode": "aeri",
                        "argv": argv,
                        "cwd": aeri.openpi_root,
                        "exit_file": str(directory / "logs" / exit_name),
                        "out_log": str(p["typescript"]),
                        "events_file": str(p["events"]),
                        "hangup_grace_s": 3.0,
                    }
                ),
            )
            self._event(p["events"], f"start: {shlex.join(argv)}")
            line, process_env = h.launch(
                unit,
                h.hostchild,
                str(spec_path),
                h.python,
                child_env(h.environ, aeri),
                h.environ,
            )
            child = h.spawn(
                line,
                stdin=slave,
                stdout=log_fd,
                env=process_env,
                cwd=aeri.openpi_root,
            )
        except BaseException as exc:
            for fd in (master, slave, log_fd):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            self._event(p["events"], f"terminal D exited rc={EXIT_SPAWN}")
            self._update(phase="ended", exit={"code": EXIT_SPAWN, "signal": None})
            self._audit("start_failed", request_id=request_id, error=type(exc).__name__)
            if isinstance(exc, Refuse):
                raise
            if isinstance(exc, (OSError, ValueError)):
                raise Refuse("start_failed", why=type(exc).__name__) from None
            raise
        os.close(slave)
        os.close(log_fd)
        who = h.identity(child.pid)
        with self._mutex:
            self._child, self._master = child, master
            self._found = False
            self._update(phase="started", leader={"pid": child.pid, "identity": who})
        self._audit("start", request_id=request_id, run_id=run_id, name=name)
        self._start_watcher()
        return {
            "schema": st.RESULT_SCHEMA,
            "accepted": True,
            "request_id": request_id,
            "run_id": run_id,
            "name": name,
            "state": "launching",
            "state_seq": self._seq,
        }

    def _reset_run(self, aeri, name, rec) -> None:
        self._rec = rec
        self._begin_readers(aeri, name, aeri.console_lang)
        self.challenges.void()
        self._found = False

    @staticmethod
    def _quiet_pty(master, slave) -> None:
        """No echo (the screen is not read from this pty), and the master never
        blocks a write."""
        import fcntl

        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @staticmethod
    def _write_private(path, text) -> None:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "w") as handle:
            handle.write(text)

    @staticmethod
    def _event(path, text) -> None:
        """One line of ``terminal_d.events`` (what ``wait_request.sh`` and the
        resource monitor of the AERI tools wait for)."""
        with contextlib.suppress(OSError):
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            with os.fdopen(fd, "a") as handle:
                handle.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")

    # ---- keys

    def key(self, body) -> dict:
        """Press one key of ``aeri_state.KEYS`` for the prompt the person saw."""
        self.adopt()
        request_id = self._request_id(body)
        action = body.get("action")
        if action not in st.KEYS:
            raise Refuse("state_not_allowed", state="unknown")
        main_site = self.site_loader()
        self._require_real()
        directory = self._existing_folder(main_site)
        return self._transaction(
            "key",
            directory,
            request_id,
            body,
            lambda mark_sent: self._press(request_id, action, body, mark_sent),
        )

    def _press(self, request_id, action, body, mark_sent) -> dict:
        with self._mutex:
            f, d, c = self._current()
            seen = body.get("seen") or {}
            prompt = d.prompt or {}
            if (
                f.record is None
                or seen.get("run_id") != f.record.get("run_id")
                or seen.get("state_seq") != c.seq
                or seen.get("prompt_id") != prompt.get("id")
            ):
                raise Refuse("stale_page")
            entries = st.offer(d, f, c)
            entry = st.find_key(entries, action)
            if entry is None:
                raise Refuse("state_not_allowed", state=d.state)
            if not entry["allowed"]:
                why = entry["why_not"]
                raise Refuse(why["code"], **why["params"])
            token = body.get("token")
            if token != entry["token"]:
                raise Refuse("stale_page")
            spec = st.KEYS[action]
            now = f.now
            if entry["level"] in ("B", "C"):
                tab = body.get("tab_id")
                age = self._presence_age(tab) if isinstance(tab, str) else None
                if age is None or age >= st.PRESENCE_S:
                    raise Refuse("presence_stale")
                due = st.not_before_epoch(entry)
                if due is not None and now + 0.05 < due:
                    raise Refuse("key_too_soon", wait_s=round(due - now, 2))
            wire = spec.wire
            if wire is None:  # the phrase line
                phrase = entry["phrase"]
                if body.get("confirm") != phrase:
                    raise Refuse("confirm_required", phrase=phrase)
                wire = phrase.encode("utf-8") + b"\r"
            if self._master is None or (
                spec.wire is not None and wire not in st.ALL_BYTES
            ):
                raise Refuse("key_failed")
            # Locks first: a key that may not have arrived is never sent again.
            self._keyed.add(prompt["id"])
            if action in st.VERDICTS:
                self._verdict_round = d.round_id
            self._pending = Pending(
                action, prompt["id"], now, self.host.monotonic(), self._baseline(f)
            )
            mark_sent()
            self._audit(
                "key",
                key=action,
                byte=entry["byte"],
                request_id=request_id,
                run_id=f.record.get("run_id"),
                state=d.state,
                prompt_id=prompt["id"],
                round=d.progress["round"],
            )
            try:
                if os.write(self._master, wire) != len(wire):
                    raise OSError("short write")
            except OSError:
                raise Refuse("key_failed") from None
            if spec.wire is None:
                self._answered = True
                self._buf.answered()
            self._cache = None
            return {
                "schema": st.RESULT_SCHEMA,
                "accepted": True,
                "action": action,
                "byte": entry["byte"],
                "state_seq": c.seq,
                "prompt_id": prompt["id"],
                "sent_at": self._iso(),
                "evidence": "pending",
            }

    # ---- stop and end

    def stop(self, body) -> dict:
        """Ctrl+C: at any time D lives and has not been told; not in the single
        flight and not waiting for the run lock."""
        self.adopt()
        request_id = self._request_id(body)
        with self._run_lock_if_free("stop"), self._mutex:
            f, d, _c = self._current()
            if not d.alive:
                raise Refuse("d_gone")
            if not d.stop_open:
                if d.stop_phase != "idle":
                    raise Refuse("stop_spent")
                raise Refuse("state_not_allowed", state=d.state)
            via = self._send_stop(f)
            first = (f.record or {}).get("stop_sent_epoch") is None
            if first:
                self._update(stop_sent_epoch=f.now)
            else:
                self._update(stop_retried=True)
            self._audit(
                "stop",
                request_id=request_id,
                run_id=(f.record or {}).get("run_id"),
                state=d.state,
                route=via,
                byte="Ctrl+C",
                key="stop",
            )
            return {
                "schema": st.RESULT_SCHEMA,
                "accepted": True,
                "phase": "sent" if first else "retried",
                "via": via,
            }

    def _runner_pid(self, f: st.Facts) -> int | None:
        runner = f.runner
        if not runner.present or runner.pid is None or runner.identity is None:
            return None
        who = self.host.identity(runner.pid)
        if who is None:
            return None
        want = {k: str(v) for k, v in runner.identity.items()}
        got = {k: str(v) for k, v in who.items()}
        return (
            runner.pid
            if all(want.get(k) == got.get(k) for k in ("start_ticks", "boot"))
            else None
        )

    def _send_stop(self, f: st.Facts) -> str:
        """Ctrl+C by the pty (ISIG on: the terminal turns it into SIGINT for D's
        foreground group); else SIGINT to the pid in D's heartbeat when its
        identity matches; else to the terminal's foreground group. Never more."""
        h = self.host
        master = self._master
        if master is not None and f.isig is not False and not self._found:
            try:
                if os.write(master, st.STOP_BYTE) == 1:
                    return "pty"
            except OSError:
                pass
        pid = self._runner_pid(f)
        if pid is not None:
            try:
                h.kill(pid, signal.SIGINT)
                return "pid"
            except OSError:
                pass
        if master is not None:
            try:
                h.killpg(h.tcgetpgrp(master), signal.SIGINT)
                return "group"
            except OSError:
                pass
        raise Refuse("signal_failed")

    def end(self, body) -> dict:
        """Force D to end (SIGKILL of its groups): only when its wrap-up is
        stuck. The VLM and policy units stay behind."""
        self.adopt()
        request_id = self._request_id(body)
        if body.get("confirm") != st.END_PHRASE:
            raise Refuse("confirm_required", phrase=st.END_PHRASE)
        main_site = self.site_loader()
        directory = self._existing_folder(main_site)
        return self._transaction(
            "end", directory, request_id, body, lambda mark_sent: self._end(request_id)
        )

    def _end(self, request_id) -> dict:
        with self._mutex:
            f, d, _c = self._current()
            if not st.end_entry(d)["allowed"]:
                raise Refuse("end_not_allowed")
            groups = self._groups(f)
            self._audit(
                "end",
                request_id=request_id,
                run_id=(f.record or {}).get("run_id"),
                key="end",
            )
            for group in groups:
                with contextlib.suppress(OSError):
                    self.host.killpg(group, signal.SIGKILL)
            child = self._child
            if child is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(KILL_WAIT_S)
            self.tick()
            return {
                "schema": st.RESULT_SCHEMA,
                "accepted": True,
                "state": self._current()[1].state,
            }

    def _groups(self, f: st.Facts) -> list:
        """The two groups the launcher's own hang-up handler signals: its own
        (the leader's pid) and the one D is in (``uv`` may put it elsewhere)."""
        leader = ((f.record or {}).get("leader") or {}).get("pid")
        groups = []
        if isinstance(leader, int):
            groups.append(leader)
        pid = self._runner_pid(f)
        if pid is not None:
            with contextlib.suppress(OSError):
                other = self.host.getpgid(pid)
                if other not in groups:
                    groups.append(other)
        return groups

    def presence(self, body) -> None:
        tab = body.get("tab_id")
        if not isinstance(tab, str) or not TAB_RE.fullmatch(tab):
            raise Refuse("request_id_invalid")
        now = self.host.monotonic()
        with self._mutex:
            if body.get("visible"):
                self._tabs[tab] = now
            else:
                self._tabs.pop(tab, None)
            for old in [t for t, at in self._tabs.items() if now - at > 120.0]:
                del self._tabs[old]
            self._cache = None

    # ---- the watcher

    def _start_watcher(self) -> None:
        if not self.host.background:
            return
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._watcher = threading.Thread(
            target=self._watch, name="levi-marvel0-aeri", daemon=True
        )
        self._watcher.start()

    def _watch(self) -> None:
        while True:
            self.host.sleep(TICK_S)
            try:
                if not self.tick():
                    return
            except Exception:  # noqa: BLE001, S112 - one bad look never ends the watcher
                continue

    def tick(self) -> bool:
        """One look at D and what follows from it (the watcher calls this four
        times a second; tests call it by hand). False once the run is over. The
        page does nothing on its own here: it reads and records."""
        with self._mutex:
            rec = self._rec
            if rec is None or rec.get("phase") not in ("intent", "started"):
                return False
            self._drain()
            self._current()
            return (self._rec or {}).get("phase") in ("intent", "started")

    def _drain(self) -> None:
        """Nothing is expected from the keyboard pty's master (the echo is off
        and D's screen is the launcher's other pty): read and drop anything
        there so that it can never fill up and stall a write of D's."""
        if self._master is None:
            return
        with contextlib.suppress(OSError):
            os.read(self._master, 4096)

    def _finish(self, f: st.Facts, aeri) -> None:
        """The launcher is gone: record how it ended, let go of everything, look
        (without touching anything) at what D left."""
        if (self._rec or {}).get("phase") == "ended" or self._finished:
            return
        self._finished = True
        ex = f.exit or {"code": None, "signal": None}
        after = self._after_exit(aeri)
        name = (self._rec or {}).get("name")
        if name:
            events = self._paths(aeri, name)["events"]
            try:
                text = events.read_text(errors="replace")
            except OSError:
                text = ""
            if "terminal D exited" not in text:
                rc = 128 + ex["signal"] if ex.get("signal") else ex.get("code")
                self._event(events, f"terminal D exited rc={rc}")
        self._update(
            phase="ended", exit=ex, ended_epoch=self.host.now(), after_exit=after
        )
        self._audit(
            "ended",
            run_id=(self._rec or {}).get("run_id"),
            code=ex.get("code"),
            signal=ex.get("signal"),
        )
        self._release()
        with contextlib.suppress(Exception):
            self._prune()

    def _after_exit(self, aeri) -> dict:
        """What D left, read only: our units, the ports, the swap logs."""
        h = self.host
        out = {"units": [], "ports": {}, "swap_clean": None}
        with contextlib.suppress(Exception):
            out["units"] = sorted(
                u
                for u in h.units()
                if u.startswith("levi-aeri-") and not u.startswith(UNIT_PREFIX)
            )
        with contextlib.suppress(Exception):
            table = h.net_table()
            for port in (8000, 8100):
                if not table.ok:
                    out["ports"][str(port)] = "unknown"
                else:
                    out["ports"][str(port)] = (
                        "free" if not net.listeners(table, port) else "d_unit"
                    )
        with contextlib.suppress(Exception):
            out["swap_clean"] = h.swap_clean(aeri.rollout_root)
        return out

    def _release(self) -> None:
        fd, self._master = self._master, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._child = None
        self.challenges.void()

    def _prune(self) -> None:
        """Keep the newest ``LOG_KEEP`` launcher logs (with their exit and spec
        files) and the newest request files."""
        if self._dir is None:
            return
        record.prune(self._dir)
        logs = self._dir / "logs"
        names = os.listdir(logs)
        stems = sorted({n.split(".")[0] for n in names if n.startswith("termd-")})
        keep = {
            *stems[-LOG_KEEP:],
            str((self._rec or {}).get("log_file", "")).split(".")[0],
        }
        for name in names:
            stem = name.split(".")[0]
            if stem.startswith("termd-") and stem not in keep:
                with contextlib.suppress(OSError):
                    os.unlink(logs / name)

    # ---- after a restart

    def adopt(self) -> None:
        """Take over the record a restarted service finds. A D that is still
        alive has no keyboard any more: it is sent **one** SIGINT (user decision
        2026-10-21; its pid and identity from its own heartbeat file) and
        watched. One that is gone is over. The arm lock is never taken and no
        SIGSTOP is sent: D holds the lock itself and a frozen D freezes its
        heartbeat thread."""
        with self._mutex:
            if self._adopted:
                return
            self._adopted = True
            try:
                main_site = self.site_loader()
                aeri = settings_of(main_site)
                self._dir = self._folder(main_site, create=False)
                self._rec = record.read_record(self._dir)
            except Exception:  # noqa: BLE001 - nothing readable: no run is assumed
                self._rec = None
                return
            rec = self._rec
            if rec is None or rec.get("phase") not in ("intent", "started"):
                return
            leader = rec.get("leader") or {}
            pid = leader.get("pid")
            alive = (
                rec.get("phase") == "started"
                and isinstance(pid, int)
                and self.host.identity(pid) == leader.get("identity")
            )
            name = rec.get("name")
            if not alive:
                self._update(phase="ended", exit=self._exit_file(rec))
                return
            size = self._screen_size(aeri, name)
            self._begin_readers(
                aeri,
                name,
                rec.get("console_lang") or aeri.console_lang,
                base=max(0, size - SCREEN_REPLAY),
            )
            self._found = True
            self._answered = True
            self._feed_screen(aeri, name)
            f = self._facts(aeri, self.host.now())
            if rec.get("stop_sent_epoch") is None:
                self._adopt_stop(f, rec, pid)
            self._start_watcher()

    def _adopt_stop(self, f: st.Facts, rec: dict, leader_pid: int) -> None:
        """The one SIGINT of an adopted D: to the pid in its heartbeat when that
        identity matches, else to the launcher's group (the launcher's identity
        was just checked; it ignores SIGINT and D is in its group unless ``uv``
        moved it)."""
        via = None
        with contextlib.suppress(Exception):
            pid_d = self._runner_pid(f)
            if pid_d is not None:
                self.host.kill(pid_d, signal.SIGINT)
                via = "pid"
        if via is None:
            with contextlib.suppress(OSError):
                self.host.killpg(leader_pid, signal.SIGINT)
                via = "group"
        self._update(stop_sent_epoch=self.host.now(), adopted_at=self.host.now())
        self._audit(
            "adopted", run_id=rec.get("run_id"), key="stop", byte="SIGINT", route=via
        )

    def _screen_size(self, aeri, name) -> int:
        try:
            return os.lstat(self._paths(aeri, name)["typescript"]).st_size
        except OSError:
            return 0

    # ---- the log

    def log(self, lines) -> dict:
        """The tail of D's screen file, paths cut to their last segment."""
        self.adopt()
        with self._mutex:
            rec = self._rec
            try:
                aeri = settings_of(self.site_loader())
            except Refuse:
                aeri = None
        if rec is None or aeri is None:
            return {"schema": st.LOG_SCHEMA, "lines": [], "bytes": 0}
        path = self._paths(aeri, rec["name"])["typescript"]
        try:
            size = os.lstat(path).st_size
        except OSError:
            size = 0
        return {
            "schema": st.LOG_SCHEMA,
            "lines": deploy.page_lines(path, lines) if size else [],
            "bytes": size,
        }
