"""A made-up AERI machine for the tests of the preparation card (``aeri_prep``,
``aeri_ready``, ``aeri_leftovers``, ``aeri_api``): every path is below the
``tmp_path`` the caller hands in, nothing touches the real machine.

What is real here: the git repository of the "worktree" (``git init`` and a
commit), the stand-in ``scripts/aeri.py`` program (a real child process: its argv,
working folder, environment and niceness are recorded and asserted), the templates (copies of the real ones, with the real
``SOURCES.sha256``), the site and control parsers, the policy-switch log reader
(``fr3_policy_swap.pending``) and every parser of a command's output. What is
scripted: the clock, ``nvidia-smi``, ``/proc/meminfo``, the socket tables, the
flock table, ``systemctl``, the other parts of the page (stack, deploy, hosted
client) and the AERI run registry: each is one field of ``aeri_ready.Host``.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from site_sample import merged

from levi.marvel0 import (
    aeri_api,
    aeri_prep,
    aeri_ready,
    control_site,
    net,
)
from levi.marvel0 import site as marvel0_site

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "aeri_templates"
# 2026-10-21 12:00 UTC: the 21st on every clock from UTC-12 to UTC+11
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC).timestamp()
MEMINFO = (
    "MemTotal:       129450224 kB\n"
    "MemFree:        90012000 kB\n"
    "MemAvailable:   99259152 kB\n"
    "Buffers:          123456 kB\n"
)
CHECK_STDOUT = (FIXTURES / "check.sample.json").read_text()

GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

AERI_STANDIN = """\
import json, os, sys, time
CONTROL = {control!r}
SEEN = {seen!r}
b = json.load(open(CONTROL)) if os.path.exists(CONTROL) else {{}}
json.dump({{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ),
           "nice": os.nice(0)}}, open(SEEN, "w"))
mode = b.get("mode", "ok")
if mode == "sleep":
    time.sleep(b.get("seconds", 5))
if mode == "fail":
    print("job: subtask.max_steps is out of range", file=sys.stderr)
    sys.exit(2)
if mode == "text":
    print("this is not json")
    sys.exit(0)
out = json.loads({check!r})
out.update(b.get("override", {{}}))
print(json.dumps(out, indent=2))
"""


class Systemctl:
    """``systemctl`` is never run for real by these tests: a ``stop`` is kept and
    answered (``fail`` names the units that refuse); the live unit's stop acts on
    the world's scripted unit state the way systemd would (``world.live_stop``:
    ``stops`` False leaves it running, ``rc`` and ``say`` are the command's exit
    code and text). Every other command (git, the stand-in programs) runs for
    real."""

    def __init__(self, world=None):
        self.calls = []
        self.kws = []
        self.fail = set()
        self.world = world

    def __call__(self, argv, **kw):
        if argv[0] != "systemctl":
            return aeri_prep.run_capture(argv, **kw)
        self.calls.append(list(argv))
        self.kws.append(dict(kw))
        name = argv[-1]
        if name in self.fail:
            return aeri_prep.Ran(1, "", f"Failed to stop {name}: Unit is masked.")
        world = self.world
        if world is not None and name == world.site.live.unit:
            how = world.live_stop
            if how.get("stops", True):
                world.set_live("stopped")
            say = how.get("say", "")
            return aeri_prep.Ran(how.get("rc", 0), "", say)
        return aeri_prep.Ran(0, "", "")


def git(*args, cwd):
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


class World:
    """The made-up machine under ``tmp_path / "w"``."""

    def __init__(self, tmp_path, profile="harvest-5bc750ca", local=True):
        self.tmp = Path(tmp_path)
        self.root = self.tmp / "w"
        r = self.root
        self.ext = r / "aeri-external"
        self.runs = self.ext / "runs"
        self.templates = self.ext / "templates"
        self.tools = self.ext / "tools"
        self.wt = r / "worktree"
        self.openpi = r / "openpi"
        self.rollout = r / "rollout"
        self.registry = r / "home" / ".levi-aeri" / "runs"
        self.state = r / "state"
        self.hub = r / "hub"
        self.levi_root = r / "LEVI"
        self.levi_bin = (
            self.levi_root / ".venv" / "bin" / "levi"
        )  # (named by the site; not run)
        self.local_model = r / "models" / "qwen3-vl-4b"
        self.behaviour = r / "behaviour.json"
        self.seen = r / "seen.json"
        self.live_stop: dict = {}
        self.live_file = r / "live.json"
        self.profile = profile
        for folder in (
            self.runs,
            self.templates,
            self.tools,
            self.openpi,
            self.rollout / ".aeri" / "runs",
            self.registry,
            self.state,
            self.hub,
            self.levi_bin.parent,
            self.local_model.parent,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        for name in (*aeri_prep.TEMPLATE_FILES, aeri_prep.SOURCES_FILE):
            shutil.copyfile(FIXTURES / name, self.templates / name)
        self._worktree()
        self.set_live("running")
        self.local = local
        self.facts = {
            "gpu": (1000, 32607),
            "meminfo": MEMINFO,
            "sockets": (),
            "flock": {},
            "units": [],
            "aeri_runs": [],
            "stack": self.stack_doc(),
            "deploy": {"state": "idle"},
            "hosted_e": False,
            "hosted_d": False,
            "sessions": {"state": "ok", "sessions": []},
            "product": True,
            "control": {"enabled": True},
            "matches": (True, "ok"),
            # the product's user unit, asked how long it waits at a stop
            "unit_stop": {"load": "loaded", "timeout_s": 150.0},
        }
        self.site = self._site()
        self.reads: dict = {}
        self.systemctl = Systemctl(self)

    # ---- the pieces

    def _worktree(self):
        (self.wt / "scripts").mkdir(parents=True, exist_ok=True)
        (self.wt / "scripts" / "aeri.py").write_text(
            AERI_STANDIN.format(
                control=str(self.behaviour),
                seen=str(self.seen),
                check=CHECK_STDOUT,
            )
        )
        git("init", "-q", "-b", "main", cwd=self.wt)
        git("add", "-A", cwd=self.wt)
        git("commit", "-q", "-m", "stand-in AERI code", cwd=self.wt)

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.wt)

    def commit_change(self):
        """A new commit in the worktree (HEAD moves; the pin no longer matches)."""
        (self.wt / "scripts" / "extra.txt").write_text("x\n")
        git("add", "-A", cwd=self.wt)
        git("commit", "-q", "-m", "more", cwd=self.wt)

    def dirty(self, how="modify"):
        if how == "modify":
            with open(self.wt / "scripts" / "aeri.py", "a") as handle:
                handle.write("# edit\n")
        else:
            (self.wt / "untracked.txt").write_text("x\n")

    def site_dict(self, **aeri) -> dict:
        values = {
            "profile": self.profile,
            "pin_sha": self.head()[:8],
            "worktree": str(self.wt),
            "runs_root": str(self.runs),
            "templates_root": str(self.templates),
            "rollout_root": str(self.rollout),
            "openpi_root": str(self.openpi),
            "levi_python": sys.executable,
            "levi_bin": str(self.levi_bin),
            "console_lang": "zh",
            "uv_offline": True,
            "tools_root": str(self.tools),
        }
        if getattr(self, "local", True):
            values["local_model"] = str(self.local_model)
        values.update(aeri)
        data = merged(self.tmp)
        data["paths"]["rollout_root"] = str(self.tmp / "e_rollouts")
        data["paths"]["gpu_lock_file"] = str(self.hub / ".gpu.lock")
        data["aeri"] = values
        return data

    def _site(self):
        return marvel0_site.parse(self.site_dict(), "env")

    def reparse_site(self, **aeri):
        self.site = marvel0_site.parse(self.site_dict(**aeri), "env")

    @property
    def aeri(self):
        return self.site.aeri

    def set_check(self, **behaviour):
        self.behaviour.write_text(json.dumps(behaviour))

    def set_live_stop(self, **behaviour):
        """How the stop of the live unit answers: ``stops``, ``rc``, ``say``."""
        self.live_stop = dict(behaviour)

    def set_live(self, state):
        shown = {
            "running": {"load": "loaded", "active": "active", "sub": "running"},
            "stopped": {"load": "loaded", "active": "inactive", "sub": "dead"},
            "absent": {"load": "not-found", "active": "inactive", "sub": "dead"},
        }[state]
        self.live_file.write_text(json.dumps(shown))

    def seen_check(self) -> dict:
        return json.loads(self.seen.read_text())

    def live_stops(self) -> list:
        """``{"argv", "env", "timeout"}`` of every stop of the live unit, in order."""
        unit = self.site.live.unit
        return [
            {"argv": argv, **kw}
            for argv, kw in zip(self.systemctl.calls, self.systemctl.kws, strict=True)
            if argv[-1] == unit
        ]

    def seen_live_stop(self) -> dict:
        return self.live_stops()[-1]

    @staticmethod
    def stack_doc(a="running", b="unknown", c="running", m="running", health="ok"):
        def term(state):
            return {"state": state, "evidence": [], "code": f"x.{state}"}

        return {
            "terminals": {
                "M": term(m),
                "A": term(a),
                "B": term(b),
                "C": term(c),
            },
            "health": {"state": health},
        }

    # ---- the host

    def _table(self):
        return net.Table(True, tuple(self.facts["sockets"]))

    def listen(self, *ports):
        self.facts["sockets"] = tuple(
            net.Sock("tcp", p, 0, net.LISTEN, 1000 + p, "127.0.0.1", "0.0.0.0")
            for p in ports
        )

    def _count(self, name, value):
        self.reads[name] = self.reads.get(name, 0) + 1
        return value

    def host(self, **replace) -> aeri_ready.Host:
        facts = self.facts
        prep = aeri_prep.Host(
            now=lambda: NOW,
            run=self.systemctl,
            registry=lambda: self.registry,
        )

        def control():
            if facts["control"]["enabled"] is None:
                return control_site.ControlLoad("missing", None)
            return control_site.ControlLoad(
                "ok",
                control_site.ControlSite(
                    control_site.StackControl(),
                    control_site.ClientControl(),
                    aeri=control_site.AeriControl(enabled=facts["control"]["enabled"]),
                ),
            )

        def site_load():
            return marvel0_site.SiteLoad("ok", self.site, Path("site.toml"), "env", [])

        def flock(path):
            return facts["flock"].get(Path(path).name, [])

        values = {
            "now": lambda: NOW,
            "prep": prep,
            "control": control,
            "site": site_load,
            "product": lambda: facts["product"],
            "gpu": lambda: self._count("gpu", facts["gpu"]),
            "meminfo": lambda: facts["meminfo"],
            "net_table": self._table,
            "flock": flock,
            "unit_show": lambda unit: json.loads(self.live_file.read_text()),
            "unit_timeout": lambda: self._count("unit_timeout", facts["unit_stop"]),
            "units": lambda: {"state": "ok", "items": list(facts["units"])},
            "aeri_runs": lambda: list(facts["aeri_runs"]),
            "stack_view": lambda: facts["stack"],
            "deploy_view": lambda: facts["deploy"],
            "hosted_e": lambda: facts["hosted_e"],
            "hosted_d": lambda: facts["hosted_d"],
            "sessions": lambda root, now: facts["sessions"],
            "profile_matches": lambda worktree, profile: facts["matches"],
            "state_root": lambda: self.state,
            "monotonic": self.clock,
        }
        values.update(replace)
        return aeri_ready.Host(**values)

    # the monotonic clock the readiness cache reads
    _mono = 1000.0

    def clock(self):
        return self._mono

    def advance(self, seconds):
        self._mono += seconds

    def preparer(self, **host) -> aeri_api.Preparer:
        reader = aeri_ready.Readiness(self.host(**host))
        return aeri_api.Preparer(
            reader,
            lock_path=lambda: self.hub / ".marvel0-run.lock",
            locks=str(self.root / "locks"),
        )

    def form(self, **change) -> dict:
        form = {
            "mode": "external",
            "task": "pick the pepper into the white plate",
            "rounds": 5,
            "steps": 200,
            "gripper_confirm_s": 4.0,
            "start_home": True,
        }
        form.update(change)
        return form

    def prepare_body(self, request_id="req-prepare-0001", **change) -> dict:
        return {
            "request_id": request_id,
            **self.form(**change),
            "confirm": aeri_prep.PHRASE_PREPARE,
        }


def installed_profiles(monkeypatch, table):
    """Make ``aeri_prep.known_profiles`` read ``table`` ({id: start_home}). The
    hosted run's module (``aeri_state``) is another package's: when it exists its
    ``PROFILES`` is replaced for the test and the rest of it (``profile_matches``)
    stays real; when it does not, a stand-in module with only ``PROFILES`` is
    installed."""
    import importlib
    import types

    values = {k: {"start_home": v} for k, v in table.items()}
    try:
        module = importlib.import_module("levi.marvel0.aeri_state")
    except ImportError:
        module = types.ModuleType("levi.marvel0.aeri_state")
        monkeypatch.setitem(sys.modules, "levi.marvel0.aeri_state", module)
        monkeypatch.setattr("levi.marvel0.aeri_state", module, raising=False)
    monkeypatch.setattr(module, "PROFILES", values, raising=False)
    return module


DEFAULT_PROFILES = {
    "harvest-5bc750ca": True,
    "bfdee2cf": False,
    "main-297a74c3": True,
}
