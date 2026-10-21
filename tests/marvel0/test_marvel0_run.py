"""``levi/marvel0/run.py``: the hosted evaluation run, without a process.

The machine is fake: a temporary site, openpi checkout and rollout folder, a
fake socket table, lock table, health file and policy-server view, and a
**scripted client**: the test writes the session file and the log lines the
client would, and the supervisor's pty is a real one whose client end the test
holds, so it can read every byte the page ever sends to the keyboard. No
process is started and nothing is signalled (``killpg`` is recorded). The
real-process tests (hostchild and the fake client under a pty) are in
``test_marvel0_run_live.py`` and use the same ``Machine``.

The file has three parts: the pure pieces (log reading, state rules, what is
offered, the one-time token, the child's environment and argv), then the
supervisor's start and refusals, then its keys, hold, end and watcher.
"""

import contextlib
import json
import os
import signal
import sys
import time
import tty
from pathlib import Path

import pytest
from conftest import scaled
from site_sample import make_openpi_tree, make_site
from test_marvel0_deploy import HEADER, LOOPBACK, SFT, FakeCatalog

from levi.marvel0 import command, models, net, record, run
from levi.marvel0 import site as marvel0_site
from levi.workspace import Workspace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
FAKE_CLIENT = HERE / "fake_client.py"
PROMPT = "pick the eggplant in the blue plate"
SCREWS = "pick screws of same size into the empty slot of screw box"
PLATES = "stack the plates of same color together"
GROUP = f"{SFT}_rtc_demo"
TASK = "pick_the_eggplant_in_the_blue_plate"
PRODUCT_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/levi-product.service\n"
)
ATTEST = {"estop": True, "scene_reset": True, "area_clear": True}
T0 = 1_792_000_000.0
SP = "pi05_fr3_sp_step100"


def model(model_id=SFT, **kw):
    base = {
        "id": model_id,
        "config": "pi05_fr3_all_state",
        "cfg_capable": False,
        "restricted": False,
        "verified": True,
        "step": None,
        "version": None,
        "variant": None,
        "size_bytes": 1,
        "warnings": (),
    }
    return models.Model(**{**base, **kw})


MODEL = model()
SP_MODEL = model(SP, restricted=True)


# --- the machine ---------------------------------------------------------------------


class Clock:
    """Wall and monotonic time moved by hand (fake mode)."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return T0 + self.t

    def monotonic(self):
        return 1000.0 + self.t

    def sleep(self, seconds):
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


class FakeChild:
    """What ``Host.spawn`` returns in fake mode: a launcher that the test
    controls. It keeps the client's end of the pty (``keyboard``) so the
    test reads every byte the page sent."""

    def __init__(self, argv, stdin, stdout, env, cwd):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.pid = 4_000_000 + os.getpid() % 1000
        self.keyboard = os.dup(stdin)
        self.returncode = None
        tty.setcbreak(self.keyboard)  # the client's RawKeys: ICANON off, ISIG on

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def exits(self, code=0):
        self.returncode = code

    def typed(self) -> bytes:
        """Everything written to the keyboard since the last call. Enter is
        read as ``\r`` whichever of the two the tty delivers (cbreak keeps
        ICRNL, which turns the CR the page writes into LF; the client accepts
        both, RC:1241)."""
        out = b""
        os.set_blocking(self.keyboard, False)
        while True:
            try:
                chunk = os.read(self.keyboard, 64)
            except (BlockingIOError, OSError):
                return out.replace(b"\n", b"\r")
            if not chunk:
                return out.replace(b"\n", b"\r")
            out += chunk

    def close(self):
        try:
            os.close(self.keyboard)
        except OSError:
            pass


class Machine:
    """One fake machine and the ``Supervisor`` on it. ``live`` runs the real
    launcher and the fake client under a real pty (with a launch that has no
    scope); otherwise the client is scripted by the test."""

    def __init__(self, tmp_path, monkeypatch, live=False, **site_override):
        self.tmp = Path(tmp_path)
        self.live = live
        monkeypatch.setattr(marvel0_site, "ARM_LOCK_FILE", str(self.tmp / "arm.lock"))
        self.site = make_site(tmp_path, **site_override)
        make_openpi_tree(tmp_path)
        Path(self.site.rollout_root).mkdir(parents=True, exist_ok=True)
        self.proc = self.tmp / "proc"
        (self.proc / "net").mkdir(parents=True)
        self.hub = self.tmp / "hub"
        self.hub.mkdir()
        self.health_path = self.tmp / "franka" / "run" / "fr3_health.json"
        self.health_path.parent.mkdir(parents=True)
        self.workspace = Workspace.at(self.tmp / "ws")
        self.clock = Clock()
        self.children = []
        self.spawned = []
        self.signals = []
        self.sockets = []
        self.profile = PRODUCT_CGROUP
        self.enabled = True
        self.deploy = self.ready_view()
        self.aeri = []
        self.environ = {
            "HOME": "/home/tester",
            "USER": "tester",
            "PATH": "/usr/bin",
            "LEVI_UI_TOKEN": "secret-ui-token",
            "HF_TOKEN": "secret-hf-token",
            "ROS_DISTRO": "jazzy",
            "CONDA_PREFIX": "/opt/conda",
            "LD_LIBRARY_PATH": "/opt/ros/lib",
            "DISPLAY": ":1",
        }
        self.write_net()
        self.host = run.Host(
            spawn=self.spawn,
            launch=run.direct_launch,
            client_argv=self.client_argv,
            killpg=self.killpg,
            identity=self.identity,
            proc_state=self.proc_state,
            run_lock_path=lambda: self.hub / run.LOCK_FILE,
            arm_lock_path=lambda: self.tmp / "arm.lock",
            locks_path="/proc/locks",  # (the locks are real flocks on temporary files)
            health_file=lambda: (str(self.health_path), 3.0),
            net_table=lambda: net.read(self.proc / "net"),
            aeri_runs=lambda: list(self.aeri),
            deploy_view=lambda: self.deploy,
            control_enabled=lambda: self.enabled,
            cgroup=lambda: self.profile,
            in_main_checkout=lambda: True,
            environ=self.environ,
            now=time.time if live else self.clock.now,
            monotonic=time.monotonic if live else self.clock.monotonic,
            sleep=time.sleep if live else self.clock.sleep,
        )
        self.sup = run.Supervisor(
            self.host,
            site_loader=lambda: self.site,
            catalog=FakeCatalog(MODEL, SP_MODEL),
            workspace=self.workspace,
        )
        self.n = 0
        self.health()

    # ---- hooks of the host

    def spawn(self, argv, *, stdin, stdout, env, cwd):
        self.spawned.append({"argv": argv, "env": dict(env), "cwd": cwd})
        if self.live:
            import subprocess

            child = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=stdout,
                stderr=stdout,
                env=env,
                cwd=cwd,
                close_fds=True,
                start_new_session=True,
            )
        else:
            child = FakeChild(argv, stdin, stdout, env, cwd)
        self.children.append(child)
        return child

    def client_argv(self, argv):
        """The fake profile: the fake client with the flags the renderer made
        (never the real client)."""
        return [
            sys.executable,
            str(FAKE_CLIENT),
            *argv[len(command.PREFIX) :],
            "--no-display-images",
        ]

    def killpg(self, pid, sig):
        self.signals.append((pid, sig))
        if self.live:
            os.killpg(pid, sig)
        elif sig == signal.SIGKILL:
            self.child.exits(-9)

    def identity(self, pid):
        if self.live:
            return run._identity(pid)
        alive = self.children and self.children[-1].returncode is None
        return {"start_ticks": "1", "boot": "b"} if alive else None

    def proc_state(self, pid):
        if self.live:
            return run.proc_state(pid)
        stopped = any(s == signal.SIGSTOP for _, s in self.signals)
        return "T" if stopped else "S"

    # ---- the fake kernel and files

    @property
    def child(self):
        return self.children[-1]

    def write_net(self, clients=0, port=8000):
        rows = []
        for n in range(clients):
            local = f"{LOOPBACK}:{port:04X}"
            remote = f"{LOOPBACK}:{50000 + n:04X}"
            rows.append(
                f"   {n}: {local} {remote} 01 00000000:00000000 00:00000000 "
                f"00000000  1000        0 {900 + n} 1 0 100 0 0 10 0\n"
            )
        (self.proc / "net" / "tcp").write_text(HEADER + "".join(rows))
        (self.proc / "net" / "tcp6").write_text(HEADER)

    def health(self, red=False, age=0.0):
        """A health file written ``age`` seconds ago."""
        now = self.host.now() if hasattr(self, "host") else T0
        self.health_path.write_text(
            json.dumps(
                {
                    "updated_at_epoch": now - age,
                    "red_light": red,
                    "hardware_active": True,
                    "controller_active": True,
                    "robot_mode_name": "Move",
                    "monitor_pid": 1234,
                }
            )
        )

    @staticmethod
    def ready_view(model=SFT, rtc=True, established=0, state="ready", ours=True):
        return {
            "state": state,
            "deploy": {"model_id": model, "rtc": rtc},
            "listener": {"ours": ours, "established": established, "present": True},
        }

    # ---- the run folder

    @property
    def folder(self) -> Path:
        return run.state_dir((), self.workspace, create=False)

    def record(self):
        return record.read_record(self.folder)

    def log_path(self) -> Path:
        return self.folder / "logs" / self.record()["log_file"]

    def history(self) -> list:
        path = self.folder / record.HISTORY
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    # ---- requests

    def rid(self) -> str:
        self.n += 1
        return f"req-{self.n:06d}-test"

    def form(self, **kw) -> dict:
        return {
            "model_id": SFT,
            "prompt": PROMPT,
            "max_steps": 30,
            "eval_num": 3,
            "min_z": 0.05,
            "rtc": True,
            **kw,
        }

    def start(self, **form):
        f = self.form(**form)
        c = self.sup.challenge({"purpose": "start", "form": f})
        return self.sup.start(
            {
                "request_id": self.rid(),
                "form": f,
                "challenge": c["challenge"],
                "confirm": c["confirm"],
                "attest": dict(ATTEST),
            }
        )

    def press(self, action, **kw):
        purpose = run.PURPOSES[action]
        c = self.sup.challenge({"purpose": purpose})
        body = {
            "request_id": self.rid(),
            "action": action,
            "challenge": c["challenge"],
            "confirm": "",
            **kw,
        }
        return self.sup.key(body)

    def hold(self):
        return self.sup.hold({"request_id": self.rid()})

    def end(self, **kw):
        return self.sup.end(
            {"request_id": self.rid(), "confirm": run.PHRASES["end"], **kw}
        )

    # ---- looking

    def view(self) -> dict:
        self.sup._cache = None
        return self.sup.view()

    def look(self) -> dict:
        """One watcher step, then the document."""
        self.sup.tick()
        return self.view()

    def state(self) -> str:
        return self.look()["state"]

    def wait_state(self, state, timeout=20.0, **until):
        deadline = time.monotonic() + scaled(timeout)
        while True:
            self.health()
            view = self.look()
            if view["state"] == state and all(
                _dig(view, k) == v for k, v in until.items()
            ):
                return view
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"state {view['state']}/{view['reason']['code']}, wanted {state}"
                )
            time.sleep(0.05)

    # ---- the fake client (live mode)

    def fake(self, **cfg):
        """Tune the fake client for the next start (``fake_client.py``)."""
        (Path(self.site.rollout_root) / ".fake_client.json").write_text(json.dumps(cfg))

    def arm(self) -> list:
        """What the fake arm did, from ``fake_arm.jsonl``."""
        path = Path(self.site.rollout_root) / "fake_arm.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def client(self) -> dict:
        """What the fake client wrote about itself at start."""
        return json.loads(
            (Path(self.site.rollout_root) / "fake_start.json").read_text()
        )

    def sessions(self) -> list:
        folder = Path(self.site.rollout_root) / ".eval_sessions"
        return sorted(p.name for p in folder.glob("*.json")) if folder.is_dir() else []

    # ---- the scripted client (fake mode)

    def session_file(self) -> Path:
        rec = self.record()
        return Path(rec["rollout_root"]) / ".eval_sessions" / rec["session_file"]

    def set_session(self, state, reason="", no=1, step=0, counted=0, age=0.0, **extra):
        rec = self.record()
        stamp = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(self.host.now() - age)
        )
        data = {
            "schema": "levi.eval.session.v1",
            "session_id": rec.get("session_id") or "sess-1",
            "state": state,
            "reason": reason,
            "group": rec["group"],
            "task_folder": rec["task_folder"],
            "updated_at": stamp,
            "episode": {
                "no": no,
                "target": 3,
                "counted": counted,
                "demo_index": no,
                "step": step,
                "max_steps": 30,
            },
            "fr3": {"ok": True, "hardware_active": True},
            **extra,
        }
        path = self.session_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        self.session_id = data["session_id"]

    def say(self, *lines):
        with open(self.log_path(), "a") as handle:
            handle.write("\n".join(lines) + "\n")

    def gate(self, no=1):
        """The client reaches the start gate for episode ``no``."""
        self.set_session("standby", run.GATE_REASON, no=no)
        self.say(f'\n=== ready | ep {no} | 0/3 | "{PROMPT}" ===', "  [Enter] start")

    def typed(self) -> bytes:
        return self.child.typed()

    def close(self):
        for child in self.children:
            if hasattr(child, "close"):
                child.close()
        sup = self.sup
        child = sup._child
        if self.live and child is not None and child.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(3)
        for name in ("_master", "_arm_fd"):
            fd = getattr(sup, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(sup, name, None)


def _dig(view, dotted):
    for part in dotted.split("."):
        view = view[part]
    return view


@pytest.fixture(name="machine")
def make_machine(tmp_path, monkeypatch):
    m = Machine(tmp_path, monkeypatch)
    yield m
    m.close()


def refusal(call, *args, **kw):
    """The ``run.Refuse`` a call raises, as ``(status, code, extra)``."""
    with pytest.raises(run.Refuse) as caught:
        call(*args, **kw)
    exc = caught.value
    return exc.status, exc.code, exc.extra


# --- the client's log ------------------------------------------------------------------


def scan(*chunks) -> run.LogView:
    reader = run.LogScan()
    for chunk in chunks:
        reader.feed(chunk if isinstance(chunk, bytes) else chunk.encode())
    return reader.view


def test_the_log_reader_finds_the_lines_the_state_rules_read():
    view = scan(
        "Connected. Server metadata: {}\n",
        '\n=== ready | ep 2 | 1/3 | "pick" ===\n  min_z 0.05\n',
        "Homing over 30 steps -> [0.4]  (rotating 0.0 deg)\nHome reached.\n",
        "\nEpisode 2 ended: timeout  (30 steps, 3.1 s)\n",
        "  hit the 30-step limit. Judge it:\n  [s] success   [f] fail   [d] discard\n",
        "Closing env.\n",
    )
    assert view.gate_ep == 2
    assert (view.ended_ep, view.ended_outcome) == (2, "timeout")
    assert view.judge_ep == 2
    assert (view.home_over, view.home_reached) == (1, 1)
    assert view.closing is True


def test_a_line_split_across_reads_is_read_whole():
    view = scan("=== ready | e", 'p 3 | 0/5 | "x" ===\n')
    assert view.gate_ep == 3
    # A line that never ends is not read as a line, and the carry stays small.
    reader = run.LogScan()
    reader.feed(b"x" * 100_000)
    assert reader.view.lines == 0 and len(reader._carry) <= 4096


def test_ansi_colour_does_not_hide_a_line():
    view = scan('\x1b[1;33m=== ready | ep 4 | 0/5 | "x" ===\x1b[0m\n')
    assert view.gate_ep == 4


def test_the_step_limit_prompt_is_tied_to_a_timeout_only():
    # The prompt follows "Episode N ended: timeout"; after any other outcome a
    # stray line of that text is not a prompt for that episode.
    assert (
        scan(
            "Episode 1 ended: success  (5 steps, 1.0 s)\n  hit the 30-step limit. Judge it:\n"
        ).judge_ep
        is None
    )
    assert scan("  hit the 30-step limit. Judge it:\n").judge_ep is None


def test_a_prompt_the_client_words_differently_is_not_matched():
    view = scan(
        "\n=== READY | episode 2 ===\n",
        "Episode 2 finished: timeout\n",
        "  step budget used up. Judge:\n",
    )
    assert (view.gate_ep, view.ended_ep, view.judge_ep) == (None, None, None)


def test_the_reader_notes_the_failure_lines():
    view = scan(
        "  (no tty: choosing 'q')\n",
        "REFUSING TO START: busy\n",
        "Traceback (most recent call last):\n",
        "Interrupt received, stopping after this step.\n",
    )
    assert view.no_tty and view.refused and view.traceback and view.interrupted


# --- the state rules ---------------------------------------------------------------------

REC = {
    "run_id": "r1",
    "phase": "started",
    "created_epoch": T0 - 100,
    "group": GROUP,
    "task_folder": TASK,
    "session_id": "s1",
}
GATE = run.GATE_REASON


def sess(state="running", reason="", no=1, sid="s1", group=GROUP, task=TASK, step=3):
    return {
        "session_id": sid,
        "state": state,
        "reason": reason,
        "group": group,
        "task_folder": task,
        "episode": {"no": no, "step": step, "max_steps": 30},
    }


def facts(rec=REC, session=None, proc="running", log=None, age=0.5, **kw):
    kw.setdefault("icanon", False)
    kw.setdefault("started_age_s", 100.0)
    return run.Facts(
        now=T0,
        record=rec,
        proc=proc,
        session=session,
        session_age_s=None if session is None else age,
        log=log or run.LogView(),
        **kw,
    )


def verdict(f):
    d = run.derive(f)
    return d.state, d.reason


GATE_LOG = run.LogView(gate_ep=1)
JUDGE_LOG = run.LogView(gate_ep=1, ended_ep=1, ended_outcome="timeout", judge_ep=1)

DERIVE_ROWS = [
    # name, facts, (state, reason)
    ("no run", facts(None, proc="gone"), ("idle", "no_run")),
    (
        "a start being checked",
        facts(None, proc="gone", preflighting=True),
        ("preflight", "checking"),
    ),
    (
        "a start being checked after an ended run",
        facts(
            {**REC, "phase": "ended", "exit": {"code": 0}},
            proc="gone",
            preflighting=True,
        ),
        ("preflight", "checking"),
    ),
    (
        "recorded, not launched yet",
        facts({**REC, "phase": "intent", "created_epoch": T0 - 5}, proc="gone"),
        ("starting", "spawning"),
    ),
    (
        "recorded, never launched",
        facts({**REC, "phase": "intent", "created_epoch": T0 - 60}, proc="gone"),
        ("failed", "intent_only"),
    ),
    (
        "launched, no session file",
        facts(session=None, started_age_s=20.0),
        ("starting", "waiting_for_session"),
    ),
    (
        "launched, no session file in time",
        facts(session=None, started_age_s=200.0),
        ("failed", "no_gate_in_time"),
    ),
    (
        "the start gate",
        facts(session=sess("standby", GATE), log=GATE_LOG),
        ("at_gate", "waiting_at_gate"),
    ),
    (
        "the gate but the keyboard is still canonical",
        facts(session=sess("standby", GATE), log=GATE_LOG, icanon=True),
        ("starting", "gate_not_confirmed"),
    ),
    (
        "the gate but the keyboard mode is unreadable",
        facts(session=sess("standby", GATE), log=GATE_LOG, icanon=None),
        ("starting", "gate_not_confirmed"),
    ),
    (
        "the gate but no banner in the log (text drift)",
        facts(session=sess("standby", GATE), log=run.LogView()),
        ("starting", "gate_not_confirmed"),
    ),
    (
        "the gate banner of another episode",
        facts(session=sess("standby", GATE, no=2), log=GATE_LOG),
        ("starting", "gate_not_confirmed"),
    ),
    (
        "standby with another reason (text drift)",
        facts(session=sess("standby", "waiting at the gate"), log=GATE_LOG),
        ("starting", "gate_not_confirmed"),
    ),
    (
        "standby while connecting",
        facts(session=sess("standby", "session started"), log=run.LogView()),
        ("starting", "gate_not_confirmed"),
    ),
    ("homing", facts(session=sess("homing"), log=GATE_LOG), ("homing", "homing")),
    ("running", facts(session=sess("running"), log=GATE_LOG), ("running", "running")),
    (
        "the step-limit prompt",
        facts(session=sess("running", step=30), log=JUDGE_LOG),
        ("awaiting_label", "judge_prompt"),
    ),
    (
        "a prompt of an earlier episode",
        facts(session=sess("running", no=2), log=JUDGE_LOG),
        ("running", "running"),
    ),
    (
        "a timeout whose prompt is not seen (text drift)",
        facts(
            session=sess("running", step=30),
            log=run.LogView(gate_ep=1, ended_ep=1, ended_outcome="timeout"),
        ),
        ("finishing", "episode_closing"),
    ),
    (
        "a labelled episode closing",
        facts(
            session=sess("running", step=12),
            log=run.LogView(gate_ep=1, ended_ep=1, ended_outcome="success"),
        ),
        ("finishing", "episode_closing"),
    ),
    (
        "the client closing",
        facts(session=sess("running"), log=run.LogView(gate_ep=1, closing=True)),
        ("finishing", "closing_env"),
    ),
    (
        "the session ended",
        facts(session=sess("stopped", "user_quit")),
        ("finishing", "session_ended"),
    ),
    (
        "q sent at the gate",
        facts(
            {**REC, "end_requested": True}, session=sess("standby", GATE), log=GATE_LOG
        ),
        ("finishing", "end_requested"),
    ),
    (
        "a silent session",
        facts(session=sess("running"), log=GATE_LOG, age=11.0),
        ("lost", "heartbeat_stale"),
    ),
    (
        "a frozen client",
        facts(proc="stopped", session=sess("running"), log=GATE_LOG),
        ("held", "held_elsewhere"),
    ),
    (
        "frozen by a person",
        facts(
            {**REC, "held_at": T0 - 30, "held_by": "person"},
            proc="stopped",
            session=sess("running"),
            log=GATE_LOG,
        ),
        ("held", "held_person"),
    ),
    (
        "frozen by the monitor rule",
        facts(
            {**REC, "held_at": T0 - 30, "held_by": "monitor_red"},
            proc="stopped",
            session=sess("running"),
        ),
        ("held", "held_monitor_red"),
    ),
    (
        "a hold just sent, the kernel not yet",
        facts(
            {**REC, "held_at": T0 - 0.5, "held_by": "person"},
            proc="running",
            session=sess("running"),
        ),
        ("held", "held_person"),
    ),
    (
        "continued by something else",
        facts(
            {**REC, "held_at": T0 - 30, "held_by": "person"},
            proc="running",
            session=sess("running"),
            log=GATE_LOG,
        ),
        ("lost", "resumed_elsewhere"),
    ),
    (
        "no keyboard (the service restarted)",
        facts(session=sess("running"), keyboard=False),
        ("lost", "host_restarted"),
    ),
    (
        "the process cannot be read",
        facts(proc="unknown", session=sess("running")),
        ("lost", "process_unreadable"),
    ),
    (
        "the task was switched (old file)",
        facts(session=sess("stopped", "task_changed")),
        ("lost", "task_changed"),
    ),
    (
        "another task in the file",
        facts(session=sess("running", task="pick_screws")),
        ("lost", "task_changed"),
    ),
    (
        "another group in the file",
        facts(session=sess("running", group="other")),
        ("lost", "task_changed"),
    ),
    (
        "another session in the file",
        facts(session=sess("running", sid="s2")),
        ("lost", "session_changed"),
    ),
    (
        "the dual gate state",
        facts(session=sess("waiting_reset")),
        ("lost", "unexpected_state"),
    ),
    (
        "a state nobody knows",
        facts(session=sess("sleeping")),
        ("lost", "unexpected_state"),
    ),
]


@pytest.mark.parametrize(
    "name, f, expected", DERIVE_ROWS, ids=[r[0] for r in DERIVE_ROWS]
)
def test_derive_rows(name, f, expected):
    assert verdict(f) == expected


def ended(code=0, sig=None, session=None, **rec):
    return facts(
        {**REC, **rec},
        session=session,
        proc="gone",
        exit={"code": code, "signal": sig},
    )


ENDED_ROWS = [
    (
        "q at the gate",
        ended(0, session=sess("stopped", "user_quit")),
        ("done", "user_quit"),
    ),
    ("q sent by the page", ended(0, end_requested=True), ("done", "user_quit")),
    (
        "target reached",
        ended(0, session=sess("finished", "target reached")),
        ("done", "target_reached"),
    ),
    ("ended by the page", ended(None, 9, killed=True), ("done", "ended_by_page")),
    (
        "an exception",
        ended(1, session=sess("fault", "exception")),
        ("failed", "exit_code"),
    ),
    (
        "a clean exit after a fault",
        ended(0, session=sess("fault", "exception")),
        ("failed", "exception"),
    ),
    (
        "interrupted",
        ended(0, session=sess("stopped", "interrupted")),
        ("failed", "interrupted"),
    ),
    ("an exit nobody explains", ended(0), ("failed", "exit_unexplained")),
    ("an exit code", ended(2), ("failed", "exit_code")),
    ("killed from outside", ended(None, 9), ("failed", "killed_from_outside")),
    ("hung up", ended(None, signal.SIGHUP), ("failed", "hangup")),
    ("no controlling terminal", ended(run.EXIT_NO_TTY), ("failed", "no_tty")),
    ("not started", ended(run.EXIT_SPAWN), ("failed", "spawn_failed")),
    (
        "killed for no gate",
        ended(None, 9, kill_reason="no_gate_in_time"),
        ("failed", "no_gate_in_time"),
    ),
    (
        "gone, nobody saw how",
        facts(REC, proc="gone"),
        ("failed", "host_restarted"),
    ),
    (
        "recorded as ended",
        facts(
            {**REC, "phase": "ended", "exit": {"code": 0, "signal": None}},
            session=sess("stopped", "user_quit"),
            proc="gone",
        ),
        ("done", "user_quit"),
    ),
]


@pytest.mark.parametrize(
    "name, f, expected", ENDED_ROWS, ids=[r[0] for r in ENDED_ROWS]
)
def test_derive_rows_of_a_client_that_is_gone(name, f, expected):
    assert verdict(f) == expected


def test_every_state_has_a_row_and_every_reason_a_text():
    seen = {r[2][0] for r in DERIVE_ROWS + ENDED_ROWS}
    assert seen == set(run.STATES) - {"held"} | {"held"}
    reasons = {r[2][1] for r in DERIVE_ROWS + ENDED_ROWS}
    assert reasons <= set(run.REASONS), reasons - set(run.REASONS)
    for key in (
        "held_person",
        "held_monitor_red",
        "held_heartbeat_stale",
        "held_task_changed",
        "held_extra_connection",
        "held_host_restarted",
        "held_resumed_elsewhere",
        "held_elsewhere",
        "held_session_changed",
        "held_unexpected_state",
    ):
        assert key in run.REASONS


def test_a_client_that_ended_never_holds_the_arm_in_the_state():
    for _, f, _ in ENDED_ROWS:
        assert run.derive(f).may_move is False


# --- what is offered ---------------------------------------------------------------------


def offered(d: run.Derived, **ctx) -> dict:
    c = run.Context(
        monitor=run.Monitor("ok"), preflight=[run.check_row("product", True)], **ctx
    )
    acts = run.actions(d, c)
    return {name: e["allowed"] for name, e in acts.items()}


def allowed_in(state, reason="x", **ctx):
    return sorted(k for k, v in offered(run.Derived(state, reason), **ctx).items() if v)


def test_what_each_state_offers():
    assert allowed_in("idle") == ["start"]
    assert allowed_in("done") == ["start"]
    assert allowed_in("failed") == ["start"]
    assert allowed_in("preflight") == []
    assert allowed_in("starting") == ["end", "hold"]
    assert allowed_in("at_gate") == ["end_at_gate", "enter", "hold"]
    assert allowed_in("homing") == ["hold"]
    assert allowed_in("running") == ["exit_home", "failure", "hold", "success"]
    assert allowed_in("awaiting_label") == ["discard", "failure", "hold", "success"]
    assert allowed_in("finishing") == ["hold"]
    assert allowed_in("held") == ["end"]
    assert allowed_in("lost") == ["end", "hold"]


def test_no_state_offers_s_p_i_or_a_digit_at_the_start_gate():
    """The incident of 2026-10-20: at the start gate ``s`` and ``p`` choose and
    start another task. The gate offers Enter and q, nothing else, ever."""
    for locks in ({}, {"enter_locked": True}, {"keyed": True}, {"label_locked": True}):
        for monitor in ("ok", "red", "offline", "missing", "unknown"):
            c = run.Context(monitor=run.Monitor(monitor), **locks)
            acts = run.actions(run.Derived("at_gate", "x"), c)
            bytes_ = {
                run.KEY_RULES[name][0]
                for name, e in acts.items()
                if name in run.KEY_RULES and e["allowed"]
            }
            assert bytes_ <= {b"\r", b"q"}, (locks, monitor)
    assert run.KEY_BYTES == {b"\r", b"s", b"f", b"d", b"q"}
    assert all(
        not (b"0" <= b <= b"9") and b not in (b"p", b"i", b"\x03")
        for b in run.KEY_BYTES
    )


def test_enter_is_only_ever_offered_at_the_gate():
    for state in run.STATES:
        c = run.Context(monitor=run.Monitor("ok"))
        acts = run.actions(run.Derived(state, "x"), c)
        assert acts["enter"]["allowed"] == (state == "at_gate")
        assert acts["success"]["allowed"] == (state in ("running", "awaiting_label"))
        assert acts["discard"]["allowed"] == (state == "awaiting_label")
        assert acts["exit_home"]["allowed"] == (state == "running")


def test_one_label_per_episode_and_one_enter_per_gate():
    assert allowed_in("running", label_locked=True) == ["hold"]
    assert allowed_in("awaiting_label", label_locked=True) == ["hold"]
    assert allowed_in("at_gate", enter_locked=True) == ["hold"]
    assert allowed_in("running", keyed=True) == ["hold"]
    why = run.actions(
        run.Derived("at_gate", "x"),
        run.Context(monitor=run.Monitor("ok"), enter_locked=True),
    )
    # q behind an Enter that was not taken is the running loop's "quit":
    # start, home, quit, home.
    assert why["end_at_gate"]["why_not"]["code"] == "enter_locked"


def test_the_monitor_vetoes_motion_keys_and_never_a_stop_action():
    def acts(state, monitor):
        c = run.Context(monitor=run.Monitor(monitor))
        return {
            k: v["allowed"] for k, v in run.actions(run.Derived(state, "x"), c).items()
        }

    # red: nothing that makes the arm move; hold and end stay
    red = acts("running", "red")
    assert not (red["success"] or red["failure"] or red["exit_home"]) and red["hold"]
    assert (
        acts("at_gate", "red")["enter"] is False
        and acts("at_gate", "red")["end_at_gate"]
    )
    # not fresh and green: only Enter is blocked; a label can still end the episode
    for state in ("offline", "missing", "unknown"):
        assert acts("at_gate", state)["enter"] is False
        assert acts("running", state)["success"] is True
    assert acts("held", "red")["end"] and acts("lost", "offline")["hold"]


def test_start_is_offered_only_when_every_check_passes():
    def start(state="idle", **kw):
        c = run.Context(monitor=run.Monitor("ok"), **kw)
        return run.actions(run.Derived(state, "x"), c)["start"]

    ok = [run.check_row("product", True)]
    assert start(preflight=ok)["allowed"] is True
    assert start(preflight=[run.check_row("arm_lock", False)])["why_not"] == {
        "code": "preflight_failed",
        "params": {"first": "arm_lock"},
        "text": "A start check fails: arm_lock.",
    }
    assert start(preflight=[run.check_row("no_session", None)])["allowed"] is False
    # a check that cannot be told refuses; a warning does not
    assert start(preflight=[run.check_row("robot_server", False)])["allowed"] is True
    assert start(enabled=False)["why_not"]["code"] == "not_enabled"
    assert start(profile="fake")["why_not"]["code"] == "not_product"
    for live in run.LIVE:
        assert start(live)["why_not"]["code"] == "run_live"


def test_hold_is_offered_in_every_live_state_that_is_not_held_and_end_in_three():
    c = run.Context(monitor=run.Monitor("red"), keyed=True, label_locked=True)
    for state in run.STATES:
        acts = run.actions(run.Derived(state, "x"), c)
        assert acts["hold"]["allowed"] == (state in run.LIVE and state != "held")
        assert acts["end"]["allowed"] == (state in ("held", "lost", "starting"))
    assert (
        run.actions(run.Derived("held", "x"), c)["hold"]["why_not"]["code"]
        == "already_held"
    )


def test_blind_marks_the_actions_that_send_the_arm_home():
    c = run.Context(monitor=run.Monitor("ok"))
    acts = run.actions(run.Derived("running", "x"), c)
    assert {k for k, v in acts.items() if v["blind"]} == {
        "enter",
        "success",
        "failure",
        "discard",
        "exit_home",
    }


def test_the_document_has_every_action_and_never_a_path():
    f = facts(session=sess("standby", GATE), log=GATE_LOG)
    d = run.derive(f)
    doc = run.document(f, d, run.Context(monitor=run.Monitor("ok", 0.3, T0)))
    assert list(doc["actions"]) == list(run.ACTIONS)
    assert doc["schema"] == "levi.marvel0.run.v1" and doc["state"] == "at_gate"
    assert "/" not in json.dumps(
        {k: v for k, v in doc.items() if k != "phrases"}
    ).replace("/s", "")
    assert doc["phrases"]["start"] == "start client {group}"


def test_no_text_calls_anything_an_emergency_stop():
    texts = [
        *run.REASONS.values(),
        *run.WHY_NOT.values(),
        *run.NOTICES.values(),
        *run.CHECK_OK.values(),
        *run.CHECK_REFUSE.values(),
    ]
    for text in texts:
        # (the physical one, and "not an emergency stop", are what the page says)
        low = (
            text.lower()
            .replace("physical emergency stop", "")
            .replace("not an emergency stop", "")
        )
        low = low.replace("physical e-stop", "")
        assert (
            "emergency stop" not in low and "e-stop" not in low and "estop" not in low
        )
    assert "not an emergency stop" in run.NOTICES["hold_not_estop"]
    assert "same account" in run.NOTICES["gates_scope"]


# --- the one-time token --------------------------------------------------------------------


class Ticker:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_a_token_is_used_once():
    tick = Ticker()
    tokens = run.Challenges(tick, lambda: 5000.0)
    bind = {"run_id": "r", "state_seq": 3}
    issued = tokens.issue("enter", bind)
    assert len(issued["challenge"]) >= 21 and issued["ttl_s"] == 30.0
    tokens.consume(issued["challenge"], "enter", bind)
    assert (
        refusal(tokens.consume, issued["challenge"], "enter", bind)[1]
        == "challenge_used"
    )


def test_a_token_expires_after_thirty_seconds():
    tick = Ticker()
    tokens = run.Challenges(tick, lambda: 5000.0)
    bind = {"state_seq": 1}
    issued = tokens.issue("enter", bind)
    tick.t += 29.9
    tokens.consume(issued["challenge"], "enter", bind)
    again = tokens.issue("enter", bind)
    tick.t += 30.1
    assert (
        refusal(tokens.consume, again["challenge"], "enter", bind)[1]
        == "challenge_expired"
    )
    # still "expired", not "unknown", after other tokens were issued meanwhile
    old = tokens.issue("enter", bind)
    tick.t += 31
    tokens.issue("enter", bind)
    assert (
        refusal(tokens.consume, old["challenge"], "enter", bind)[1]
        == "challenge_expired"
    )


def test_a_token_is_stale_when_the_state_moved_on():
    tokens = run.Challenges(Ticker(), lambda: 0.0)
    issued = tokens.issue(
        "label", {"run_id": "r", "session_id": "s", "episode_no": 1, "state_seq": 4}
    )
    stale = {"run_id": "r", "session_id": "s", "episode_no": 1, "state_seq": 5}
    assert (
        refusal(tokens.consume, issued["challenge"], "label", stale)[1]
        == "challenge_stale"
    )
    # a stale token is spent too
    assert (
        refusal(tokens.consume, issued["challenge"], "label", stale)[1]
        == "challenge_used"
    )


@pytest.mark.parametrize(
    "field, other",
    [("run_id", "r2"), ("session_id", "s2"), ("episode_no", 2), ("state_seq", 9)],
)
def test_a_token_is_bound_to_run_session_episode_and_sequence(field, other):
    bind = {"run_id": "r", "session_id": "s", "episode_no": 1, "state_seq": 4}
    tokens = run.Challenges(Ticker(), lambda: 0.0)
    issued = tokens.issue("label", bind)
    assert (
        refusal(tokens.consume, issued["challenge"], "label", {**bind, field: other})[1]
        == "challenge_stale"
    )


def test_a_token_is_for_one_purpose():
    tokens = run.Challenges(Ticker(), lambda: 0.0)
    issued = tokens.issue("enter", {"state_seq": 1})
    assert (
        refusal(tokens.consume, issued["challenge"], "label", {"state_seq": 1})[1]
        == "challenge_invalid"
    )


def test_an_unknown_token_is_invalid_and_the_memory_is_bounded():
    tokens = run.Challenges(Ticker(), lambda: 0.0)
    for bad in ("nope", "", None, 5, ["x"]):
        assert refusal(tokens.consume, bad, "enter", {})[1] == "challenge_invalid"
    for _ in range(run.CHALLENGES_KEPT * 3):
        tokens.issue("enter", {})
    assert len(tokens._open) <= run.CHALLENGES_KEPT
    for _ in range(run.CHALLENGES_KEPT * 6):
        token = tokens.issue("enter", {})["challenge"]
        tokens.consume(token, "enter", {})
    assert len(tokens._closed) <= run.CHALLENGES_KEPT * 4
    assert tokens.newest() is None or tokens.newest()["purpose"] == "enter"


def test_the_newest_open_token_is_shown_without_its_value():
    tokens = run.Challenges(Ticker(), lambda: 7.0)
    assert tokens.newest() is None
    issued = tokens.issue("exit_home", {})
    assert tokens.newest() == {"purpose": "exit_home", "expires_at": 37.0}
    tokens.void()
    assert tokens.newest() is None
    assert issued["challenge"] not in json.dumps(tokens.newest())


# --- the child's environment and command ------------------------------------------------------


def test_the_child_environment_is_an_allowlist():
    env = run.child_env(
        {
            "HOME": "/home/x",
            "USER": "x",
            "PATH": "/anything",
            "LEVI_UI_TOKEN": "t",
            "HF_TOKEN": "h",
            "HUGGING_FACE_HUB_TOKEN": "h",
            "ROS_DISTRO": "jazzy",
            "AMENT_PREFIX_PATH": "/opt/ros",
            "CONDA_PREFIX": "/opt/conda",
            "VIRTUAL_ENV": "/venv",
            "DISPLAY": ":1",
            "LD_LIBRARY_PATH": "/x",
            "ANTHROPIC_API_KEY": "k",
        }
    )
    assert set(env) == {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "TERM",
        "PYTHONUNBUFFERED",
        "UV_OFFLINE",
        "no_proxy",
    }
    assert env["UV_OFFLINE"] == "1" and env["PYTHONUNBUFFERED"] == "1"
    assert env["PATH"] == "/home/x/.local/bin:/usr/local/bin:/usr/bin:/bin"
    assert "t" not in env.values() and "h" not in env.values()


def test_a_child_environment_needs_a_home():
    for environ in ({}, {"HOME": "relative"}, {"HOME": ""}):
        with pytest.raises(run.Refuse):
            run.child_env(environ)


def test_the_hosted_command_is_the_rendered_one_plus_one_switch(tmp_path):
    site = make_site(tmp_path)
    form = command.validate(site, MODEL, {"model_id": SFT, "prompt": PROMPT})
    preview = command.render(site, MODEL, form)
    hosted = run.hosted_argv(preview.argv)
    assert hosted == [*preview.argv, "--no-display-images"]
    assert hosted[: len(command.PREFIX)] == command.PREFIX
    assert run.hosted_argv([]) == ["--no-display-images"]
    assert run.Host().client_argv is run.hosted_argv


def test_the_launch_line_is_a_scope_of_its_own_with_the_allowlist_inside():
    allow = run.child_env({"HOME": "/home/x", "USER": "x"})
    environ = {
        "HOME": "/home/x",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "LEVI_UI_TOKEN": "secret",
        "HF_TOKEN": "secret",
    }
    line, process_env = run.scope_launch(
        "levi-aeri-client-marvel0-r1",
        "/h/hostchild.py",
        "/s/spec.json",
        "/py",
        allow,
        environ,
    )
    assert line[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
    assert "--unit=levi-aeri-client-marvel0-r1" in line
    inside = line[line.index("--") + 1 :]
    assert inside[:2] == ["/usr/bin/env", "-i"]
    assert inside[-4:] == ["/py", "-I", "/h/hostchild.py", "/s/spec.json"]
    assert sorted(inside[2:-4]) == sorted(f"{k}={v}" for k, v in allow.items())
    # systemd-run reaches the user bus and holds nothing else of ours
    assert process_env == {
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    }
    assert "secret" not in " ".join(line)
    assert run.scope_launch("u", "/h", "/s", "/py", allow, {})[1] == {}
    direct, direct_env = run.direct_launch("u", "/h", "/s", "/py", allow, environ)
    assert direct == ["/py", "-I", "/h", "/s"] and direct_env == allow


def test_only_systemd_run_can_be_started(tmp_path):
    for bad in (
        ["ls"],
        [],
        ["systemd-run"],
        ["systemd-run", "--scope"],
        ["python3", "x"],
    ):
        with pytest.raises(ValueError):
            run._spawn(bad, stdin=None, stdout=None, env={}, cwd=str(tmp_path))


def test_the_session_file_is_named_as_the_client_names_it():
    assert (
        run.sanitize_task_name(" pick the eggplant, in the blue plate! ")
        == "pick_the_eggplant_in_the_blue_plate"
    )
    assert run.sanitize_task_name("a/b\\c  d") == "a_b_c_d"
    with pytest.raises(ValueError):
        run.sanitize_task_name("   ")
    assert run.session_name(
        "g", "pick_screws_of_same_size_into_the_empty_slot_of_screw_box"
    ) == ("g__pick_screws_of_same_size_into_the_empty_slot_of_screw_box.json")
    # the client files a restricted task under its own spelling
    assert run.canonical_prompt("stack_the_plates_of_same_color_together ") == PLATES
    assert run.canonical_prompt(PROMPT) == PROMPT


def test_the_fake_client_speaks_the_commands_grammar():
    import fake_client

    assert fake_client.FLAGS == {flag for flag, _, _ in command.FLAGS} | {
        "--display-images"
    }
    assert fake_client.TUPLE == command.TUPLE_ARITY


def test_the_fake_client_prints_the_strings_of_the_fixture_the_reader_reads():
    import fake_client

    reader = run.LogScan()
    for text in (
        fake_client.PROMPTS["gate_banner"]["render"].format(
            ep=3, progress="1/5", prompt="x"
        ),
        fake_client.PROMPTS["episode_ended"]["render"].format(
            ep=3, outcome="timeout", steps=30, dur=2.0
        ),
        fake_client.PROMPTS["judge_prompt"]["render"].format(max_steps=30),
        fake_client.PROMPTS["homing_over"]["render"].format(n=30),
        fake_client.PROMPTS["home_reached"]["render"],
        fake_client.PROMPTS["closing_env"]["render"],
        fake_client.PROMPTS["no_tty"]["render"].format(key="q"),
    ):
        reader.feed(text.encode() + b"\n")
    v = reader.view
    assert (v.gate_ep, v.ended_ep, v.judge_ep, v.home_over, v.home_reached) == (
        3,
        3,
        3,
        1,
        1,
    )
    assert v.closing and v.no_tty
    assert fake_client.PROMPTS["gate_reason"]["render"] == run.GATE_REASON


# --- the floor of a task (RIG-08) ------------------------------------------------------------


def test_a_restricted_task_below_its_floor_is_refused_though_the_command_form_allows_it(
    tmp_path,
):
    site = make_site(tmp_path)
    for prompt, floor in ((SCREWS, 0.075), (PLATES, 0.05325)):
        # the command form allows it: an explicit --min-z beats the client's table
        form = command.validate(
            site, SP_MODEL, {"model_id": SP, "prompt": prompt, "min_z": 0.03}
        )
        assert form.min_z == 0.03
        assert run.min_z_problem(prompt, form.min_z) == {
            "floor": floor,
            "min_z": "0.03",
        }
        assert run.min_z_problem(prompt, floor) is None
        assert run.min_z_problem(prompt, floor + 0.01) is None


def test_an_unlisted_task_is_not_refused_for_its_floor():
    # demo.md's measured eggplant setting is 0.025; the client has no entry for it
    assert run.min_z_problem(PROMPT, 0.025) is None
    assert run.task_floor(PROMPT) is None
    assert (
        run.task_floor("pick_screws_of_same_size_into_the_empty_slot_of_screw_box")
        == 0.075
    )


# --- the strings the state rules read, and the drift check --------------------------------------


def fixture() -> dict:
    return json.loads((HERE / "fixtures" / "client_prompts.json").read_text())[
        "prompts"
    ]


def test_every_literal_the_state_rules_read_is_pinned_by_the_fixture():
    """If the client words one of these differently, scripts/check_client_prompts.py
    says so; this keeps the fixture and the reader from drifting apart."""
    sources = "\n".join(entry["source"] for entry in fixture().values())
    for literal in (
        "=== ready | ep",
        "Episode {ep} ended: ",
        "-step limit. Judge it",
        "Homing over",
        "Home reached.",
        "Closing env.",
        "REFUSING TO START",
        "(no tty: choosing",
        "Interrupt received, stopping after this step",
        "SIGTERM received, stopping after this step",
        "Second interrupt, exiting hard",
        "Second termination request",
        run.GATE_REASON,
        '"user_quit"',
        '"target reached"',
        '"stopped", "task_changed"',
        "STALE_AFTER_S = 10.0",
        "TASK_KEYS",
        "display_images: bool = True",
        "WINS over the table",
    ):
        assert literal in sources, literal
    assert run.STALE_AFTER_S == 10.0
    assert run.RESTRICTED_FLOORS == {SCREWS: 0.075, PLATES: 0.05325}


def test_the_fixture_names_a_file_a_line_and_a_source_for_every_string():
    for key, entry in fixture().items():
        assert set(entry) >= {"file", "line", "source"}, key
        assert entry["file"].endswith(".py") and isinstance(entry["line"], int), key
        assert entry["source"].strip() == entry["source"], key


def tree_with(tmp_path, drop=None) -> Path:
    """A folder of client files holding every source string of the fixture
    (one file per name), without ``drop``'s string."""
    root = tmp_path / "client"
    root.mkdir()
    texts: dict = {}
    for key, entry in fixture().items():
        if key != drop:
            texts.setdefault(entry["file"], []).append(entry["source"])
    for name in {e["file"] for e in fixture().values()}:
        (root / name).write_text("\n".join(texts.get(name, [])) + "\n")
    return root


def test_the_drift_check_passes_on_a_client_that_has_every_string(tmp_path, capsys):
    from check_client_prompts import main

    assert main(["--root", str(tree_with(tmp_path))]) == 0
    assert "0 missing" in capsys.readouterr().out


def test_the_drift_check_names_the_string_that_is_gone(tmp_path, capsys):
    from check_client_prompts import main

    assert main(["--root", str(tree_with(tmp_path, drop="gate_banner"))]) == 1
    out = capsys.readouterr().out
    assert "MISSING gate_banner" in out and "1 missing" in out


def test_the_drift_check_is_read_only_and_needs_a_folder(tmp_path):
    from check_client_prompts import main

    root = tree_with(tmp_path)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    main(["--root", str(root)])
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert main(["--root", str(tmp_path / "nowhere")]) == 2
