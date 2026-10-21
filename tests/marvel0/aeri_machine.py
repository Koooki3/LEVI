"""One fake machine and the AERI ``Supervisor`` on it, for the tests of the hosted
terminal D (``levi/marvel0/aeri_run.py``).

Everything is a temporary folder: a site with an ``[aeri]`` section, an openpi
checkout, a runs folder with a **prepared run** (RUN.json in the form
``new_run.py`` prints it), a rollout folder, a fake worktree whose four source
files a registered test profile is made from (so ``profile_matches`` is the real
function on real bytes), a fake socket table, health file and lock folder.
Nothing of the robot or of the real AERI is touched.

Two modes, the way the tests of the hosted client do it:

* ``live=False``: **scripted**. The launcher is a ``FakeChild`` that keeps the
  client's end of the keyboard pty, so a test reads every byte the page ever
  writes; the test plays terminal D by writing its journal, heartbeat and screen
  files (``script``) from the **real** recording of 18 Oct (r08);
* ``live=True``: the real ``hostchild.py`` and ``fake_aeri.py`` under real ptys.
"""

import contextlib
import dataclasses
import hashlib
import json
import os
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

from site_sample import make_openpi_tree, make_site

from levi.marvel0 import aeri_journal as jr
from levi.marvel0 import aeri_run, net, record
from levi.marvel0 import aeri_state as st
from levi.marvel0 import site as marvel0_site
from levi.workspace import Workspace

try:
    from conftest import scaled
except ImportError:  # run as a script (the regeneration of the samples)

    def scaled(seconds):
        return seconds


HERE = Path(__file__).resolve().parent
FAKE_AERI = HERE / "fake_aeri.py"
R08 = HERE / "fixtures" / "aeri_r08"
NAME = "aeri-20261021-ext-r01"
PLAN = "a" * 64
BINDING = "b" * 64
PRODUCT_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/levi-product.service\n"
)
SOURCES = (
    "levi/automatic/console_text.py",
    "levi/automatic/console.py",
    "levi/automatic/subtask_units.py",
    "levi/automatic/fr3_run.py",
)
HEAD = "abcdef1234567890" * 2 + "abcdef12"  # 40 hex digits
LOOPBACK = "0100007F"
HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def scrubbed_screen() -> bytes:
    return (R08 / "screen.txt").read_bytes()


def cut(marker: str) -> bytes:
    """The real screen of r08 up to and including the line that has ``marker``
    (``请输入`` markers cut the unfinished prompt itself)."""
    text = scrubbed_screen().decode()
    at = text.index(marker)
    if marker.startswith("请输入"):
        return text[: at + len(marker)].encode()
    end = text.find("\r\n", at)
    return text[: end + 2 if end >= 0 else len(text)].encode()


class FakeChild:
    """What ``Host.spawn`` returns in scripted mode: a launcher the test controls.
    It keeps the client's end of the keyboard pty, so the test reads every byte
    the page sends and sets the modes D would set."""

    def __init__(self, argv, stdin, stdout, env, cwd):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.pid = 4_100_000 + os.getpid() % 1000
        self.keyboard = os.dup(stdin)
        self.returncode = None
        self.cooked()  # the phrase: input() in the canonical mode

    def cooked(self):
        attrs = termios.tcgetattr(self.keyboard)
        attrs[3] |= termios.ICANON | termios.ISIG
        # (no process of this test is in the terminal's foreground group; with
        # the interrupt character disabled, a Ctrl+C the page writes arrives as
        # the byte the test reads. The real SIGINT is tested with real ptys.)
        attrs[6][termios.VINTR] = b"\x00"
        termios.tcsetattr(self.keyboard, termios.TCSANOW, attrs)

    def cbreak(self):
        tty.setcbreak(self.keyboard, termios.TCSANOW)  # ICANON off, ISIG on
        attrs = termios.tcgetattr(self.keyboard)
        attrs[6][termios.VINTR] = b"\x00"
        termios.tcsetattr(self.keyboard, termios.TCSANOW, attrs)

    def isig(self, on):
        attrs = termios.tcgetattr(self.keyboard)
        attrs[3] = (attrs[3] | termios.ISIG) if on else (attrs[3] & ~termios.ISIG)
        termios.tcsetattr(self.keyboard, termios.TCSANOW, attrs)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def exits(self, code=0):
        self.returncode = code

    def typed(self) -> bytes:
        """Everything written to the keyboard since the last call (CR and LF
        both read as Enter)."""
        out = b""
        os.set_blocking(self.keyboard, False)
        while True:
            try:
                chunk = os.read(self.keyboard, 256)
            except (BlockingIOError, OSError):
                return out.replace(b"\n", b"\r")
            if not chunk:
                return out.replace(b"\n", b"\r")
            out += chunk

    def close(self):
        with contextlib.suppress(OSError):
            os.close(self.keyboard)


class AeriMachine:
    def __init__(self, tmp_path, monkeypatch, live=False, **knobs):
        self.tmp = t = Path(tmp_path)
        self.live = live
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(marvel0_site, "ARM_LOCK_FILE", str(t / "arm.lock"))
        base = make_site(tmp_path)
        make_openpi_tree(tmp_path)
        self.worktree = t / "wt"
        files = {}
        for rel in SOURCES:
            path = self.worktree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"# fake AERI source {rel}\n".encode())
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        real = st.PROFILES["harvest-5bc750ca"]
        self.profile = st.Profile(
            id="test-profile",
            commit="abcdef12",
            title="the harvest words on fake sources",
            start_home=real.start_home,
            progress_line=real.progress_line,
            camera_stale=real.camera_stale,
            data={**real.data, "sources_sha256": files},
        )
        monkeypatch.setitem(st.PROFILES, "test-profile", self.profile)
        self.runs = t / "runs"
        self.rollout = t / "aeri_rollout"
        for folder in (
            self.runs,
            self.rollout / ".aeri" / "runs",
            self.rollout / ".aeri" / "units",
        ):
            folder.mkdir(parents=True, exist_ok=True)
        self.aeri = marvel0_site.Aeri(
            profile="test-profile",
            pin_sha="abcdef12",
            worktree=str(self.worktree),
            runs_root=str(self.runs),
            templates_root=str(t / "templates"),
            rollout_root=str(self.rollout),
            openpi_root=str(t / "openpi"),
            levi_python=str(t / "py"),
            levi_bin=str(t / "levi"),
            console_lang="zh",
            uv_offline=True,
            tools_root=str(t / "tools"),
            local_model=None,
        )
        self.site = dataclasses.replace(base, aeri=self.aeri)
        self.proc = t / "proc"
        (self.proc / "net").mkdir(parents=True)
        self.hub = t / "hub"
        self.hub.mkdir()
        self.health_path = t / "franka" / "run" / "fr3_health.json"
        self.health_path.parent.mkdir(parents=True)
        self.workspace = Workspace.at(t / "ws")
        self.children: list = []
        self.spawned: list = []
        self.signals: list = []
        self.enabled = True
        self.e_running = False
        self.visible = True  # whether levi stop would list the hosted D (scripted)
        self.git_head = HEAD
        self.git_dirty = ""
        self.snap = {"rows": [], "ready": True, "sha256": "snap-0001"}
        self.profile_cgroup = PRODUCT_CGROUP
        self.environ = {
            "HOME": str(t / "home"),
            "USER": "tester",
            "PATH": "/usr/bin",
            "LEVI_UI_TOKEN": "secret-ui-token",
            "HF_TOKEN": "secret-hf-token",
            "ROS_DISTRO": "jazzy",
            "CONDA_PREFIX": "/opt/conda",
            "DISPLAY": ":1",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        }
        (t / "home").mkdir()
        self.write_net()
        self.n = 0
        self.knobs = {
            "rollout_root": str(self.rollout),
            "profile": "harvest-5bc750ca",
            "service_s": 0.02,
            "warmup_s": 0.05,
            "heartbeat_s": 0.1,
            "step_s": 0.02,
            "forward_steps": 40,
            "reverse_steps": 5,
            "home_s": 0.02,
            "think_s": 0.05,
            "shutdown_s": 0.3,
            **knobs,
        }
        self.host = aeri_run.Host(
            spawn=self.spawn,
            launch=aeri_run.direct_launch,
            client_argv=self.client_argv,
            killpg=self.killpg,
            kill=self.kill,
            identity=self.identity,
            proc_state=self.proc_state,
            run_lock_path=lambda: self.hub / ".marvel0-run.lock",
            arm_lock_path=lambda: self.tmp / "arm.lock",
            locks_path="/proc/locks",
            health_file=lambda: (str(self.health_path), 3.0),
            net_table=lambda: net.read(self.proc / "net"),
            snapshot=lambda: dict(self.snap),
            e_active=lambda: self.e_running,
            # live: the real check against the real list of jobs (the launcher is a
            # real process); scripted: the launcher is not, so the answer is told
            hosted_visible=(aeri_run._hosted_visible if live else self.visible_check),
            control_enabled=lambda: self.enabled,
            cgroup=lambda: self.profile_cgroup,
            in_main_checkout=lambda: True,
            git=self.git,
            units=list,
            swap_clean=lambda root: True,
            environ=self.environ,
            now=time.time,
            monotonic=time.monotonic,
            sleep=time.sleep,
        )
        self.sup = aeri_run.Supervisor(
            self.host, site_loader=lambda: self.site, workspace=self.workspace
        )
        self.health()
        self.tab = "tab-00000001"
        self.prepare()

    # ---- hooks of the host

    def spawn(self, argv, *, stdin, stdout, env, cwd):
        self.spawned.append({"argv": argv, "env": dict(env), "cwd": cwd})
        if self.live:
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

    def visible_check(self, workspace=None, run_id=None):
        """Scripted ``Host.hosted_visible``: ``visible`` is a bool, or a function
        of ``(workspace, run_id)``."""
        see = self.visible
        return bool(see(workspace, run_id) if callable(see) else see)

    def client_argv(self, argv):
        """The fake profile: terminal D's fake with the flags the page built
        (never the real AERI)."""
        rest = argv[argv.index("execute") + 1 :]
        return [sys.executable, str(FAKE_AERI), "execute", *rest]

    def killpg(self, pid, sig):
        self.signals.append(("killpg", pid, sig))
        if self.live:
            os.killpg(pid, sig)
        elif sig == signal.SIGKILL:
            self.child.exits(-9)

    def kill(self, pid, sig):
        self.signals.append(("kill", pid, sig))
        if self.live:
            os.kill(pid, sig)

    def identity(self, pid):
        if self.live:
            return aeri_run.run._identity(pid)
        alive = self.children and self.children[-1].returncode is None
        return {"start_ticks": "1", "boot": "b"} if alive and pid else None

    def proc_state(self, pid):
        if self.live:
            return aeri_run.run.proc_state(pid)
        alive = self.children and self.children[-1].returncode is None
        return "S" if alive else None

    def git(self, worktree, *args, home=""):
        if args[:2] == ("rev-parse", "HEAD"):
            return self.git_head + "\n"
        if args[:2] == ("status", "--porcelain"):
            return self.git_dirty
        return None

    @property
    def child(self):
        return self.children[-1]

    # ---- the fake kernel and files

    def write_net(self, listening=()):
        rows = []
        for n, port in enumerate(listening):
            rows.append(
                f"   {n}: {LOOPBACK}:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 "
                f"00000000  1000        0 {900 + n} 1 0 100 0 0 10 0\n"
            )
        (self.proc / "net" / "tcp").write_text(HEADER + "".join(rows))
        (self.proc / "net" / "tcp6").write_text(HEADER)

    def health(self, red=False, age=0.0):
        self.health_path.write_text(
            json.dumps(
                {
                    "updated_at_epoch": time.time() - age,
                    "red_light": red,
                    "hardware_active": True,
                    "controller_active": True,
                    "robot_mode_name": "Move",
                    "monitor_pid": 1234,
                }
            )
        )

    # ---- the prepared run

    def execute_text(self, name=NAME, plan=PLAN, binding=BINDING) -> str:
        """The command ``new_run.py`` prints for a terminal, written out here by
        hand (not by the page's code)."""
        wt, folder = self.aeri.worktree, f"{self.aeri.runs_root}/{name}"
        return (
            f"cd {self.aeri.openpi_root} && env -u VIRTUAL_ENV PYTHONPATH={wt} "
            f"uv run --no-sync --with pyrealsense2==2.55.1.6486 python -B "
            f"{wt}/scripts/aeri.py execute --config {folder}/job.yaml "
            f"--binding {folder}/binding.json --expect-plan {plan} "
            f"--expect-binding {binding}"
        )

    def prepare(self, name=NAME, mode="external", rounds=2, steps=30, **knobs):
        folder = self.runs / name
        folder.mkdir(parents=True, exist_ok=True)
        job = f"experiment:\n  name: {name}\n  episodes: {rounds}\nsubtask:\n  max_steps: {steps}\n"
        (folder / "job.yaml").write_text(job)
        (folder / "binding.json").write_text(json.dumps({"fake": True}))
        run_json = {
            "name": name,
            "mode": mode,
            "task": "pick the pepper into the white plate",
            "rounds": rounds,
            "steps": steps,
            "aeri_head": "abcdef12",
            "plan_sha256": PLAN,
            "binding_sha256": BINDING,
            "execute": self.execute_text(name),
            "confirm": f"execute {name}",
            "files_sha256": {
                "job.yaml": hashlib.sha256(job.encode()).hexdigest(),
                "binding.json": hashlib.sha256(b'{"fake": true}').hexdigest(),
            },
        }
        (folder / "RUN.json").write_text(json.dumps(run_json, indent=1))
        (folder / ".fake_aeri.json").write_text(json.dumps({**self.knobs, **knobs}))
        if mode == "external":
            (folder / "queue").mkdir(exist_ok=True)
        return run_json

    def knob(self, name=NAME, **knobs):
        path = self.runs / name / ".fake_aeri.json"
        data = json.loads(path.read_text())
        data.update(knobs)
        path.write_text(json.dumps(data))

    # ---- the run folder

    @property
    def folder(self) -> Path:
        return aeri_run.state_dir((), self.workspace, create=False)

    def record(self):
        return record.read_record(self.folder)

    def history(self) -> list:
        path = self.folder / record.HISTORY
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def rid(self) -> str:
        self.n += 1
        return f"req-{self.n:06d}-test"

    @property
    def run_dir(self) -> Path:
        return self.rollout / ".aeri" / "runs" / NAME

    @property
    def typescript(self) -> Path:
        return self.runs / NAME / "terminal_d.typescript"

    # ---- requests

    def present(self, tab=None):
        self.sup.presence({"tab_id": tab or self.tab, "visible": True})

    def start(self, name=NAME, **kw):
        self.present()
        c = self.sup.challenge({"purpose": "start", "name": name})
        body = {
            "request_id": self.rid(),
            "name": name,
            "challenge": c["challenge"],
            "confirm": f"start aeri {name}",
            "attest": {k: True for k in st.ATTEST},
            **kw,
        }
        return self.sup.start(body)

    def doc(self) -> dict:
        self.sup._cache = None
        return self.sup.view()

    def look(self) -> dict:
        self.sup.tick()
        return self.doc()

    def wait_until(self, check, timeout=20.0, what="a condition"):
        deadline = time.monotonic() + scaled(timeout)
        view = None
        while True:
            self.health()
            self.present()
            view = self.look()
            if check(view):
                return view
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"{what}: state {view['state']['id']}/{view['state']['reason']}"
                )
            time.sleep(0.03)

    def wait_state(self, state, timeout=20.0, **until):
        def check(view):
            if view["state"]["id"] != state:
                return False
            return all(_dig(view, k) == v for k, v in until.items())

        return self.wait_until(check, timeout, f"state {state}")

    def wait_key(self, action, timeout=20.0):
        """Until the key is offered and open (its delay over)."""

        def check(view):
            entry = next(
                (k for k in view["keys"] if k["id"] == action and k["allowed"]), None
            )
            if entry is None:
                return False
            due = st.not_before_epoch(entry)
            return due is None or time.time() >= due

        return self.wait_until(check, timeout, f"key {action}")

    def body_for(self, view, action, **over):
        entry = next(k for k in view["keys"] if k["id"] == action)
        prompt = view["state"]["prompt"]
        body = {
            "request_id": self.rid(),
            "action": action,
            "seen": {
                "run_id": view["run_id"],
                "state_seq": view["state"]["seq"],
                "prompt_id": prompt["id"],
            },
            "token": entry["token"],
            "tab_id": self.tab,
            "confirm": entry["phrase"] or "",
            # (what the page sends: every tick of the key's window)
            "attest": list(st.KEY_ATTEST.get(action, ())),
        }
        body.update(over)
        return body

    def press(self, action, wait=True, **over):
        """Wait for the key to be offered and open, then click it as the page
        does: the current document's ``seen``, the key's token."""
        view = self.wait_key(action) if wait else self.doc()
        self.present()
        return self.sup.key(self.body_for(view, action, **over))

    def stop(self):
        return self.sup.stop({"request_id": self.rid()})

    def end(self, **kw):
        return self.sup.end(
            {
                "request_id": self.rid(),
                "confirm": "end terminal D",
                "attest": list(st.KEY_ATTEST["end"]),
                **kw,
            }
        )

    # ---- the scripted D (r08's real recording)

    def script(self, journal=None, screen=None, runner=True, cbreak=None, steps=None):
        """Play terminal D up to a moment of the real run: the first ``journal``
        lines of r08's journal, the screen up to ``screen`` (a marker of
        ``cut``), the heartbeat; with ``cbreak`` set, the pty's mode. Files only
        grow: a test goes forward."""
        run = self.run_dir
        run.mkdir(parents=True, exist_ok=True)
        if journal is not None:
            lines = (R08 / "state_journal.jsonl").read_bytes().splitlines(keepends=True)
            body = b"".join(lines[:journal])
            (run / jr.JOURNAL).write_bytes(body)
            (run / "subtasks").mkdir(exist_ok=True)
            for side in (R08 / "subtasks").iterdir():
                (run / "subtasks" / side.name).write_bytes(side.read_bytes())
        if screen is not None:
            data = screen if isinstance(screen, bytes) else cut(screen)
            self.typescript.parent.mkdir(parents=True, exist_ok=True)
            self.typescript.write_bytes(data)
        if steps is not None:
            lines = (R08 / "steps.jsonl").read_bytes().splitlines(keepends=True)
            (self.rollout / ".aeri" / "units" / f"{NAME}-steps.jsonl").write_bytes(
                b"".join(lines[:steps])
            )
        if runner:
            self.heartbeat()
        if cbreak is True:
            self.child.cbreak()
        elif cbreak is False:
            self.child.cooked()

    def append_journal(self, rows):
        """Lines D would write after the ones played so far."""
        path = self.run_dir / jr.JOURNAL
        with open(path, "ab") as handle:
            handle.writelines(json.dumps(row).encode() + b"\n" for row in rows)

    def append_screen(self, text):
        data = text if isinstance(text, bytes) else text.encode()
        with open(self.typescript, "ab") as handle:
            handle.write(data)

    def heartbeat(self, age=0.0, pid=None):
        body = {
            "backend": "standalone",
            "identity": {"boot": "b", "start_ticks": "1"},
            "pid": pid or self.child.pid if self.children else 1,
            "run_id": NAME,
            "state": "running",
            "updated_wall_ns": int((time.time() - age) * 1e9),
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / jr.RUNNER).write_text(json.dumps(body))

    def typed(self) -> bytes:
        return self.child.typed()

    def close(self):
        for child in self.children:
            if isinstance(child, FakeChild):
                child.close()
        sup = self.sup
        child = sup._child
        if self.live and child is not None and child.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(3)
        fd, sup._master = sup._master, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _dig(view, dotted):
    for part in dotted.split("."):
        view = view[part]
    return view
