"""A fake of terminal D (``scripts/aeri.py execute``), for the tests of the hosted
AERI run (``levi/marvel0/aeri_run.py``). It stands where ``fr3_run.main`` stands
under ``uv run`` and keeps the behaviours the page depends on, with the **real
words** (``levi/marvel0/aeri_prompts.json``, copied from ``console_text.py`` and
checked against the AERI worktree by ``scripts/check_aeri_prompts.py``), the
**real journal lines** (``levi.aeri.run_event.v1``, the shape of the run of
18 Oct) and the real traps:

* **stdin and stdout must be terminals**, else exit 2 (``fr3_run.py``: "real
  execution requires an operator terminal"); ``input()`` reads the phrase in the
  canonical mode, its prompt goes to stderr, which is the output pty;
* every single-key prompt first **flushes the keys typed before it** and reads
  one byte at a time in ``cbreak`` (``tty.setcbreak``, which also drops what is
  pending), accepts ``y/Y n/N q/Q Ctrl-D`` where D does, and **ignores
  everything else**; Enter is ``\\n`` or ``\\r``; Escape swallows what follows;
  EOF is read as ``q``;
* the heartbeat thread reads ``s/f/d`` every 50 ms while a forward round runs
  (the first one counts; a reverse round only says they do nothing) and writes
  ``runner.json`` every ``heartbeat_s``; **SIGINT/SIGTERM only set a flag after
  the services are up** (before that SIGINT is a ``KeyboardInterrupt`` that
  shuts the services down), the heartbeat thread then holds the arm, says the
  ``interrupt`` line and notes ``operator_interrupt``; **the shutdown starts a
  child process in the foreground group, so a second Ctrl+C kills it** and the
  unit stays (``svc_not_stopped``, exit 1);
* notes are **buffered and folded** (``2x, first: ...``) and written at the next
  transaction, as the orchestrator does; ``subtask_inventory`` etc. are written
  at once; a long inventory note moves to ``subtasks/<seq>-subtask_inventory.json``;
* SIGHUP is not handled (the process just ends), SIGKILL cannot be.

Every arm movement it "makes" is a line in ``<rollout>/fake_arm.jsonl``
(``home``, ``step``, ``hold``), so a test can assert that nothing moved after a
stop. It opens no socket, camera or device.

Tuning for a test: ``<folder of --config>/.fake_aeri.json`` (read at start; the
environment of D is an allowlist, so nothing else reaches it). See ``KNOBS``.

The argument grammar is ``scripts/aeri.py``'s: ``execute --config J --binding B
--expect-plan SHA --expect-binding SHA``."""

import argparse
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = json.loads(
    (HERE.parents[1] / "levi" / "marvel0" / "aeri_prompts.json").read_text("utf-8")
)
POLL_S = 0.1
STEPS = 7  # the service steps
KNOBS = {
    "rollout_root": None,  # where .aeri/ lives (required)
    "profile": "harvest-5bc750ca",
    "service_s": 0.05,  # between service lines
    "warmup_s": 0.1,  # the policy warm-up before the journal's first line
    "heartbeat_s": 0.2,  # runner.json
    "heartbeat_dead_after_s": None,  # the heartbeat thread stops writing
    "step_s": 0.05,  # one forward step (and its progress line)
    "forward_steps": 30,
    "reverse_steps": 10,
    "home_s": 0.1,
    "tx_s": 0.01,
    "think_s": 0.1,  # planning and the VLM
    "shutdown_s": 0.3,  # 7/7
    "fragile_shutdown": True,  # the shutdown starts a child in the foreground group
    "arm_at_home": True,  # False: the startup home asks for o, p, h (start_home)
    "start_home": False,  # the job asks for startup home
    "swap_unclean": False,
    "objects": None,  # [{id, colour, kind, on, graspable}]
    "wait_at": None,  # {"round": 1, "reason": "operator_stop"} after that forward loop
    "fault_at": None,  # {"round": 1, "code": "policy_error"}
    "refuse_start_pose": False,  # exit 2 after the services ("start pose refused")
    "ignore_sigint": False,  # the handler does nothing (a wrap-up that hangs)
    "silent_after_services": False,  # nothing more is printed or written
    "no_take_keys": False,  # prompts never consume a key
    "take_delay_s": 0.0,  # a slow reader: waits before reading the key
    "foreign": [],  # lines printed after every prompt (other programs' output)
    "reword": {},  # template key -> text printed instead (drift)
    "health_warning": False,  # prints the health warning after each prompt
    "service_fail": None,  # {"step": 2, "code": "x"}
    "folded_pose": True,  # "2x, first:" in the startup pose note, as r08 has it
    "no_runner": False,  # never writes runner.json
    "signal_log": None,  # a file that gets one line per SIGINT/SIGTERM D receives
    "hang_after_stop": False,  # after a stop the main thread blocks (a wrap-up that is stuck)
}


def load_knobs(config: Path) -> dict:
    knobs = dict(KNOBS)
    path = config.parent / ".fake_aeri.json"
    if path.exists():
        knobs.update(json.loads(path.read_text()))
    return knobs


class Keys:
    """``levi.automatic.console.StdinKeys``, line for line."""

    def __init__(self, fd=0):
        self.fd = fd

    def poll(self, timeout=0.0):
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        data = os.read(self.fd, 1)
        if not data:
            return "\x04"
        char = data.decode("utf-8", "replace")
        if char == "\x1b":
            while select.select([self.fd], [], [], 0.02)[0]:
                os.read(self.fd, 1)
        return char

    def flush(self):
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass

    def cbreak(self):
        keys = self

        class Cm:
            def __enter__(self):
                self.saved = termios.tcgetattr(keys.fd)
                tty.setcbreak(keys.fd)  # TCSAFLUSH: drops what is pending
                return self

            def __exit__(self, *exc):
                try:
                    termios.tcsetattr(keys.fd, termios.TCSADRAIN, self.saved)
                except (termios.error, OSError):
                    pass  # (a hung-up terminal: nothing to restore)

        return Cm()


ENTER = ("\n", "\r")
QUIT = ("q", "Q", "\x04")
CONFIRM = ("y", "Y")
CANCEL = ("n", "N")


class Fake:
    def __init__(self, args, knobs):
        self.args, self.knobs = args, knobs
        self.profile = DATA["profiles"][knobs["profile"]]
        self.lang = os.environ.get("LEVI_AERI_CONSOLE_LANG", "zh")
        self.texts = self.profile["texts"][self.lang]
        self.say_lock = threading.Lock()
        self.keys = Keys()
        config = Path(args.config)
        job = config.read_text() if config.exists() else ""
        self.name = _grab(job, r"(?m)^  name: (.+)$") or config.parent.name
        self.rounds = int(_grab(job, r"(?m)^  episodes: (\d+)$") or 2)
        self.max_steps = int(
            _grab(job, r"(?m)^  max_steps: (\d+)$") or knobs["forward_steps"]
        )
        self.rollout = Path(knobs["rollout_root"])
        self.run_dir = self.rollout / ".aeri" / "runs" / self.name
        self.units = self.rollout / ".aeri" / "units"
        self.t0 = time.time()
        # the journal
        self.seq = 0
        self.prev = "0" * 64
        self.state = "PREFLIGHT"
        self.pending: list = []
        self.journal = None
        # signals
        self.sig = None
        self.assembled = False
        self.finished = threading.Event()
        self.stopping = False
        self.interrupted = False
        # the forward round
        self.phase = None  # None | forward | reverse
        self.verdict = None
        self.verdict_sent = None
        self.episode = None
        self.steps_done = 0
        self.hb_started = time.monotonic()
        self.cm = None
        self.round_no = 0
        self.completed = 0
        self.discarded = 0
        self.objects = knobs["objects"] or [
            {
                "id": "blue_plate_1",
                "colour": "blue",
                "kind": "plate",
                "on": "table",
                "graspable": False,
            },
            {
                "id": "red_pepper_1",
                "colour": "red",
                "kind": "pepper",
                "on": "blue_plate_1",
                "graspable": True,
            },
            {
                "id": "purple_eggplant_1",
                "colour": "purple",
                "kind": "eggplant",
                "on": "blue_plate_1",
                "graspable": True,
            },
        ]

    # ---- words and screen

    def t(self, key, **fields):
        text = self.knobs["reword"].get(key) or self.texts[key]
        try:
            return text.format(**fields)
        except (KeyError, IndexError, ValueError):
            return text

    def say(self, text="", err=False):
        with self.say_lock:
            print(text, file=sys.stderr if err else sys.stdout, flush=True)

    def after_prompt(self):
        for line in self.knobs["foreign"]:
            self.say(line)
        if self.knobs["health_warning"]:
            self.say(self.t("health_warning"))

    def tag(self):
        if not self.round_no:
            return self.t("tag_setup")
        return self.t("tag", n=self.round_no, total=self.rounds)

    def line(self, happened, todo=None):
        body = self.t("join", happened=happened, todo=todo) if todo else happened
        return self.tag() + " " + body

    # ---- the arm

    def arm(self, kind, **extra):
        with open(self.rollout / "fake_arm.jsonl", "a") as handle:
            handle.write(json.dumps({"kind": kind, "t": time.time(), **extra}) + "\n")

    # ---- the journal (levi.aeri.run_event.v1, line for line the shape of r08)

    def _auth(self, kind="orchestrator"):
        return {
            "command_id": None,
            "principal_id": "aeri-orchestrator"
            if kind == "orchestrator"
            else "operator",
            "principal_kind": kind,
            "process": {"boot_id": "fake-boot", "pid": os.getpid(), "start_ticks": 1},
            "session_id": "s-aeri",
        }

    def write(self, record, **kw):
        row = {
            "ack": None,
            "action": None,
            "authority": self._auth(kw.pop("kind", "orchestrator")),
            "clock_domain": "host-mono:fake",
            "control_epoch": 1,
            "emitted_wall_ns": time.time_ns(),
            "episode_id": self.episode,
            "episode_result": None,
            "episode_role": None,
            "evidence_ids": [],
            "from_state": None,
            "header": None,
            "minor": 1,
            "mono_ns": time.monotonic_ns(),
            "note": None,
            "policy_epoch": None,
            "prev_sha256": self.prev,
            "reason": None,
            "record": record,
            "run_id": self.name,
            "schema": "levi.aeri.run_event.v1",
            "sequence_no": self.seq,
            "to_state": None,
            "transaction_id": None,
            **kw,
        }
        raw = (json.dumps(row, sort_keys=True) + "\n").encode()
        os.write(self.journal, raw)
        os.fsync(self.journal)
        self.prev = hashlib.sha256(raw).hexdigest()
        self.seq += 1

    def note_now(self, code, detail, sidecar=None):
        """``_sub_note``: written at once; a long detail goes to a side file."""
        detail = (
            json.dumps(detail, separators=(",", ":"), sort_keys=True)
            if isinstance(detail, dict)
            else detail
        )
        if sidecar is not None:
            name = f"subtasks/{self.seq:06d}-{code}.json"
            (self.run_dir / "subtasks").mkdir(exist_ok=True)
            raw = json.dumps(sidecar, sort_keys=True).encode()
            (self.run_dir / name).write_bytes(raw)
            detail = json.dumps(
                {"file": name, "file_sha256": hashlib.sha256(raw).hexdigest()},
                separators=(",", ":"),
            )
        self.write("note", note={"code": code, "detail": detail[:300]})

    def note(self, code, detail):
        """``Orchestrator._note``: buffered until the next transaction."""
        self.pending.append((code, detail))

    def flush_notes(self):
        folded: dict = {}
        for code, detail in self.pending:
            folded.setdefault(code, []).append(detail)
        self.pending = []
        for code, details in folded.items():
            text = (
                details[0]
                if len(details) == 1
                else f"{len(details)}x, first: {details[0]}"
            )
            self.write("note", note={"code": code, "detail": text[:300]})

    def tx(self, to, reason, kind="orchestrator"):
        self.flush_notes()
        was = self.state
        common = {"from_state": was, "to_state": to, "reason": reason, "kind": kind}
        self.write("prepared", **common)
        time.sleep(self.knobs["tx_s"])
        self.write("acknowledged", kind=kind)
        self.write("committed", **common)
        self.state = to

    # ---- services and files

    def step(self, n, key, ok=None, **fields):
        line = self.t(f"svc_{key}", n=n, total=STEPS, **fields)
        if ok is not None:
            line += " " + self.t("svc_pass" if ok else "svc_fail")
        self.say(line)
        record = {"step": n, "key": key, "ok": ok, **fields, "wall_ns": time.time_ns()}
        self.units.mkdir(parents=True, exist_ok=True)
        with open(self.units / f"{self.name}-steps.jsonl", "a") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.records.append(record)
        time.sleep(self.knobs["service_s"])

    def runner(self, state="running"):
        if self.knobs["no_runner"]:
            return
        body = {
            "backend": "standalone",
            "final_state": self.state if state == "exited" else None,
            "identity": {"boot": "fake-boot", "start_ticks": _start_ticks()},
            "origin": "operator_terminal",
            "pid": os.getpid(),
            "plan_sha256": self.args.expect_plan,
            "run_id": self.name,
            "started_wall_ns": int(self.t0 * 1e9),
            "state": state,
            "updated_wall_ns": time.time_ns(),
        }
        tmp = self.run_dir / ".runner.tmp"
        tmp.write_text(json.dumps(body))
        os.replace(tmp, self.run_dir / "runner.json")

    # ---- the heartbeat thread

    def heartbeat(self):
        last = 0.0
        while not self.finished.wait(0.05):
            if self.sig is not None and not self.interrupted:
                self.interrupted = True
                self.arm("hold")
                self.stopping = True
                self.say("")
                self.say(self.t("interrupt", name=signal.Signals(self.sig).name))
                self.note_now(
                    "operator_interrupt",
                    f"{signal.Signals(self.sig).name} {self.where()}; state {self.state}",
                )
            if self.sig is None:
                self.poll_verdict()
            dead = self.knobs["heartbeat_dead_after_s"]
            if dead is not None and time.monotonic() - self.hb_started > dead:
                continue
            if time.monotonic() - last >= self.knobs["heartbeat_s"]:
                last = time.monotonic()
                self.runner()
                self.tick()

    def where(self):
        return "while running" if self.phase else "at a prompt"

    def tick(self):
        if self.phase == "forward":
            self.say(
                self.t(
                    "progress_subtask", steps=self.steps_done, max_steps=self.max_steps
                )
            )

    # ---- keys

    def wait_key(self, handle, moved=lambda: False):
        warned = False
        while True:
            if self.stopping:
                return "stopped"
            if moved():
                return "moved"
            if self.knobs["take_delay_s"]:
                time.sleep(self.knobs["take_delay_s"])
            if self.knobs["no_take_keys"]:
                time.sleep(POLL_S)
                continue
            key = self.keys.poll(POLL_S)
            if key is None:
                continue
            result = handle(key)
            if result is not None:
                return result
            del warned

    def open_verdict(self, reverse=False):
        self.keys.flush()
        self.cm = self.keys.cbreak()
        self.cm.__enter__()
        self.verdict_sent = None
        if reverse and "sub_reverse_hint" in self.texts:
            self.say(self.line(self.t("sub_reverse_hint")))
        elif not reverse:
            self.say(self.line(self.t("sub_forward_hint")))
        self.after_prompt()

    def close_verdict(self):
        cm, self.cm = self.cm, None
        if cm is not None:
            cm.__exit__(None, None, None)
            self.keys.flush()

    def poll_verdict(self):
        if self.cm is None or self.verdict_sent is not None and self.phase == "forward":
            return
        if self.phase not in ("forward", "reverse"):
            return
        key = self.keys.poll(0)
        if key is None:
            return
        verdicts = self.profile["keys"]["verdict"]
        if key not in verdicts:
            return
        if self.phase == "reverse":
            self.say(self.line(self.t("sub_reverse_ignored")))
            return
        self.verdict_sent = verdicts[key]
        self.verdict = verdicts[key]
        self.note_now(
            "operator_verdict",
            {
                "label": verdicts[key],
                "source": "terminal",
                "episode_id": self.episode,
                "step": self.steps_done,
            },
        )
        word = self.t(
            {
                "success": "verdict_success",
                "failure": "verdict_failure",
                "discard": "verdict_discard",
            }[verdicts[key]]
        )
        self.say(self.line(self.t("verdict_taken", verdict=word)))

    # ---- the prompts (console.py)

    def confirm_start_home(self):
        for field, key in (
            ("object_handled", "o"),
            ("path_clear", "p"),
            ("gripper_will_open", "h"),
        ):
            self.keys.flush()
            self.say(self.t("startup_home_" + field))
            self.after_prompt()
            with self.keys.cbreak():
                found = self.wait_key(
                    lambda pressed, expected=key: (
                        "ok"
                        if pressed == expected
                        else ("quit" if pressed in QUIT else None)
                    )
                )
            if found != "ok":
                self.stopping = True
                return False
        return True

    def confirm_inventory(self):
        self.keys.flush()
        self.say("")
        self.say(self.line(self.t("sub_inv_title")))
        for row in self.objects:
            self.say(
                self.t(
                    "sub_inv_row",
                    id=row["id"],
                    colour=row["colour"],
                    kind=row["kind"],
                    on=row["on"],
                    grasp=self.t(
                        "sub_inv_grasp" if row["graspable"] else "sub_inv_place"
                    ),
                )
            )
        self.say(self.line(self.t("sub_inv_todo")))
        self.after_prompt()

        def handle(key):
            if key in CONFIRM:
                return "yes"
            if key in CANCEL:
                return "no"
            if key in QUIT:
                return "quit"
            return None

        with self.keys.cbreak():
            found = self.wait_key(handle)
        if found in ("quit", "stopped"):
            self.stopping = True
            return "stopped"
        self.say(self.line(self.t("sub_inv_yes" if found == "yes" else "sub_inv_no")))
        return found

    def release_round(self):
        self.keys.flush()
        self.say("")
        self.say(self.t("sub_release", n=self.round_no + 1, total=self.rounds))
        self.after_prompt()

        def handle(key):
            if key in ENTER:
                return "released"
            if key in QUIT:
                return "quit"
            return None

        with self.keys.cbreak():
            found = self.wait_key(handle)
        if found != "released":
            self.stopping = True
            return "stopped"
        self.say(self.t("sub_released", n=self.round_no + 1, total=self.rounds))
        return "released"

    def wait_human(self, reason):
        """``Console.wait_human``: the block, Enter, the second step."""
        happened_key, todo_key = self.profile["wait_reasons"].get(
            reason, ("wait_other", "todo_other_resume")
        )
        while True:
            self.keys.flush()
            self.say("")
            self.say(
                self.line(
                    self.t(
                        happened_key,
                        loop="forward",
                        reason="x",
                        index=1,
                        attempt=1,
                        limit=3,
                        by="terminal",
                        free=1,
                        threshold=2,
                        mem=1,
                        need=1,
                    ),
                    self.t(todo_key),
                )
            )
            self.say(self.line(self.t("sub_wait_idle")))
            self.say(self.t("recent_note", code="motion_refused", detail="held"))
            self.after_prompt()
            with self.keys.cbreak():
                found = self.wait_key(
                    lambda k: "enter" if k in ENTER else ("quit" if k in QUIT else None)
                )
            if found == "enter" and reason in self.profile["confirm_reasons"]:
                self.keys.flush()
                self.say(self.line(self.t("confirm_resume")))
                with self.keys.cbreak():
                    found = self.wait_key(
                        lambda k: (
                            "enter"
                            if k in CONFIRM
                            else (
                                "no" if k in CANCEL else ("quit" if k in QUIT else None)
                            )
                        )
                    )
                if found == "no":
                    self.say(self.line(self.t("confirm_cancelled")))
                    continue
            if found in ("quit", "stopped"):
                self.stopping = True
                return False
            if reason in self.profile["no_resume_reasons"]:
                continue
            self.tx("VERIFY_INITIAL", "human_resumed", kind="operator")
            self.say(self.line(self.t("st_resumed")))
            return True

    # ---- the run

    def run(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("real execution requires an operator terminal", file=sys.stderr)
            return 2
        self.records = []
        cfg = self.args
        self.banner()
        phrase = "execute " + self.name
        if self.knobs["swap_unclean"]:
            self.say(self.t("swap_unclean_head"))
            self.say("  .aeri/policy-swap/x.jsonl: unfinished")
            phrase += " after policy switch check"
        self.say(self.t("run_confirm_warning"))
        try:
            typed = input(self.t("run_type_phrase", phrase=phrase)).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n" + self.t("run_cancelled"), file=sys.stderr)
            return 130
        if typed != phrase:
            print("operator did not authorize motion", file=sys.stderr)
            return 2
        del cfg
        try:
            return self.execute()
        except KeyboardInterrupt:
            # before the handlers exist: the services shut down on it
            self.shutdown(code=130)
            return 130

    def banner(self):
        self.say("")
        self.say("=" * 60)
        self.say(self.t("sum_title", run_id=self.name))
        self.say("-" * 60)
        self.say(self.t("sum_task", text="pick the pepper into the white plate"))
        self.say("=" * 60)
        self.say("")

    def execute(self):
        k = self.knobs
        (self.run_dir).mkdir(parents=True, exist_ok=True)
        fail = k["service_fail"]
        self.step(1, "preflight")
        self.step(1, "gpu", ok=True, free=25113, need=9021, policy=7600, vlm=1421)
        self.step(1, "mem", ok=True, mem=87.6, need=28.0)
        self.step(1, "ports", ok=True, ports="8000, 8100")
        if fail and fail["step"] == 1:
            return self.service_failed(fail)
        self.step(2, "vlm_start")
        self.step(2, "vlm_ready", ok=True, unit="levi-aeri-vlm-8100", pid=1, mib=0)
        if fail and fail["step"] == 2:
            return self.service_failed(fail)
        self.step(3, "vlm_sleep")
        self.step(3, "vlm_asleep", ok=True, level=1, mem=87.5)
        self.say(
            "  server config: pi05_fr3_all_state  (rebase dim=3, offset=+3.141593)"
        )
        self.say("  NOTE: this model consumes state (binned into the prompt).")
        self.step(4, "policy_ready", ok=True, used=15087)
        self.step(5, "inventory")
        self.step(6, "rounds")
        if k["refuse_start_pose"]:
            print("start pose refused: the arm is not at home", file=sys.stderr)
            self.shutdown(code=2)
            return 2
        # --- Orchestrator.create: the journal
        self.journal = os.open(
            self.run_dir / "state_journal.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        self.write(
            "run_header",
            header={"plan_sha256": self.args.expect_plan, "reset_mode": "subtask_loop"},
        )
        for record in self.records:
            self.write(
                "note",
                note={
                    "code": "subtask_service_step",
                    "detail": json.dumps(record)[:300],
                },
            )
        self.runner()
        time.sleep(k["warmup_s"])
        self.write(
            "note",
            note={
                "code": "policy_warmup",
                "detail": "forward warm-up completed; actions discarded",
            },
        )
        self.install_handlers()
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()
        if k["silent_after_services"]:
            while True:
                time.sleep(0.2)
        code = self.drive()
        self.finished.set()
        thread.join(2)
        self.close_verdict()
        self.finish_screen(code)
        self.runner("exited")
        self.shutdown(code=code)
        return code

    def install_handlers(self):
        def stop(signum, frame):
            if self.knobs["signal_log"]:
                with open(self.knobs["signal_log"], "a") as handle:
                    handle.write(f"{signum} {time.monotonic():.3f}\n")
            if self.knobs["ignore_sigint"]:
                self.sig = signum  # noted, never acted on by the main thread
                return
            self.sig = signum

        if self.knobs["ignore_sigint"]:
            signal.signal(signal.SIGINT, lambda *a: None)
            signal.signal(signal.SIGTERM, lambda *a: None)
            self.assembled = True
            return
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        self.assembled = True

    def service_failed(self, fail):
        self.say(
            self.t(
                "svc_failed", n=fail["step"], total=STEPS, code=fail["code"], detail="x"
            )
        )
        self.shutdown(code=1)
        return 1

    def drive(self):
        k = self.knobs
        # --- PREFLIGHT: the startup home, when the job asks for it
        if (
            k["start_home"]
            and self.profile["capabilities"]["start_home"]
            and not k["arm_at_home"]
        ):
            text = "explicit startup home confirmation required: error_mm=43.6 tolerance_mm=10.0"
            self.note("start_pose_checked", text)
            if k["folded_pose"]:
                self.note("start_pose_checked", text)
            self.flush_notes()
            if not self.confirm_start_home():
                return self.end_run()
            self.write(
                "prepared",
                from_state="PREFLIGHT",
                to_state="PREFLIGHT",
                reason="startup_home_requested",
            )
            self.write("acknowledged")
            for n in range(3):
                self.say(
                    f"Homing over {n + 30} steps -> [ 0.482 -0.011 ] (rotating 11.6 deg)"
                )
                self.arm("home", n=n)
                time.sleep(k["home_s"])
            self.say("Home reached.")
            self.write(
                "committed",
                from_state="PREFLIGHT",
                to_state="PREFLIGHT",
                reason="startup_home_requested",
            )
            self.write("note", note={"code": "home_measured", "detail": "error_mm=5.5"})
            self.write(
                "note",
                note={
                    "code": "startup_home_completed",
                    "detail": "gate closed; independent round release required",
                },
            )
        self.tx("VERIFY_INITIAL", "preflight_passed")
        # --- the object list
        time.sleep(k["think_s"])
        ids = [r["id"] for r in self.objects]
        self.note_now(
            "subtask_inventory",
            {"items": ids, "frames": ["side:1", "wrist:1"] * 6},
            sidecar={"parsed": self.objects, "kind": "inventory"},
        )
        answer = self.confirm_inventory()
        if answer == "stopped":
            return self.end_run()
        if answer == "no":
            self.note_now(
                "subtask_inventory_rejected", {"items": ids, "by": "terminal"}
            )
            return self.wait_then_end("subtask_inventory_rejected")
        self.note_now(
            "subtask_inventory_confirmed",
            {"items": ids, "by": "terminal", "sha256": "x"},
            sidecar={"objects": self.objects},
        )
        # --- the rounds
        while self.completed + self.discarded < self.rounds and not self.stopping:
            self.note("start_pose_checked", "error_mm=5.0 tolerance_mm=10.0 gripper=1")
            if self.release_round() != "released":
                return self.end_run()
            self.round_no = self.completed + self.discarded + 1
            round_id = f"{self.name}:round{self.round_no}:s{self.seq}"
            self.note_now(
                "subtask_round_released",
                {"round": self.round_no, "by": "terminal", "round_id": round_id},
            )
            time.sleep(k["think_s"])
            if self.stopping:
                break
            outcome = self.forward()
            if self.stopping:
                break
            self.reverse()
            if self.stopping:
                break
            if outcome == "discard":
                self.discarded += 1
            else:
                self.completed += 1
            self.tx("VERIFY_INITIAL", "subtask_reverse_done")
            wait = k["wait_at"]
            more = self.completed + self.discarded < self.rounds
            held = bool(wait) and wait["round"] == self.round_no and more
            if held and not self.wait_human_state(wait["reason"]):
                return self.end_run()
        if self.stopping:
            return self.end_run()
        self.tx("COMPLETED", "run_completed")
        self.say(
            self.line(self.t("st_completed", done=self.completed, total=self.rounds))
        )
        return 0

    def wait_human_state(self, reason):
        self.tx("WAIT_HUMAN", reason)
        return self.wait_human(reason)

    def wait_then_end(self, reason):
        self.tx("WAIT_HUMAN", reason)
        self.wait_human(reason)
        return self.end_run()

    def forward(self):
        k = self.knobs
        self.episode = f"{self.name}.forward.{self.round_no:04d}"
        self.verdict = None
        self.tx("FORWARD_ACTIVE", "subtask_dispatched")
        self.phase = "forward"
        self.steps_done = 0
        self.say(
            self.t(
                "round_start", n=self.round_no, total=self.rounds, episode=self.episode
            )
        )
        self.open_verdict()
        limit = self.max_steps
        fault = k["fault_at"]
        while self.steps_done < limit and not self.stopping and self.verdict is None:
            self.steps_done += 1
            self.arm("step", n=self.steps_done)
            time.sleep(k["step_s"])
            if fault and fault["round"] == self.round_no and self.steps_done >= 3:
                self.phase = None
                self.close_verdict()
                self.tx("FAULT_LOCKED", fault["code"])
                self.say("")
                self.say(self.t("fault_title", code=fault["code"]))
                self.say(self.t("fault_meaning", meaning="x"))
                self.stopping = True
                return "fault"
        self.phase = None
        if self.interrupted and k["hang_after_stop"]:
            while True:  # the heartbeat thread goes on; the main thread does not
                time.sleep(1)
        self.close_verdict()
        if self.stopping:
            return "stopped"
        self.say(
            self.t(
                "subtask_end_shown", steps=self.steps_done, max_steps=limit, ended="x"
            )
        )
        self.tx("FORWARD_STOPPING", "horizon_exhausted")
        self.tx("FORWARD_FINALIZE", "horizon_exhausted")
        self.tx("ROBOT_HOME", "horizon_exhausted")
        self.say("Homing over 30 steps -> [ 0.482 ]  (rotating 11.0 deg)")
        self.arm("home")
        time.sleep(k["home_s"])
        self.say("Home reached.")
        return self.verdict or "done"

    def reverse(self):
        k = self.knobs
        self.tx("SCENE_ASSESS", "subtask_forward_done")
        self.episode = f"{self.name}.reset.{self.round_no:04d}"
        self.tx("RESET_ACTIVE", "subtask_dispatched")
        self.phase = "reverse"
        self.say(self.line(self.t("st_reset_policy")))
        self.open_verdict(reverse=True)
        for n in range(k["reverse_steps"]):
            if self.stopping:
                break
            self.arm("step", n=n, reverse=True)
            time.sleep(k["step_s"])
        self.phase = None
        self.close_verdict()
        if self.stopping:
            return
        self.tx("RESET_VERIFY", "subtask_ended")
        self.tx("RESET_FINALIZE", "subtask_ended")
        self.say("Home reached.")

    def end_run(self):
        """A stop at a prompt, a quit, a signal: the run is over."""
        if self.state in ("FAULT_LOCKED",):
            return 1
        if self.interrupted:
            self.tx("FAULT_LOCKED", "safety_stop")
            return 130
        if self.state != "WAIT_HUMAN":
            self.tx("WAIT_HUMAN", "operator_stop", kind="operator")
        self.tx("COMPLETED", "operator_stop", kind="operator")
        self.say(
            self.line(self.t("st_stopped", done=self.completed, total=self.rounds))
        )
        return 0

    def finish_screen(self, code):
        if self.state == "FAULT_LOCKED" or code == 130:
            self.say("")
            self.say("=" * 60)
            self.say(
                self.t(
                    "fault_title",
                    code="safety_stop" if self.interrupted else "policy_error",
                )
            )
            if self.interrupted:
                self.say(self.t("fault_ctrl_c"))
            self.say("=" * 60)

    def shutdown(self, code):
        """7/7: stop the units. The stand-in for systemctl is a child in the
        foreground group, so a Ctrl+C aimed at D reaches it too."""
        self.say(self.t("svc_shutdown", n=7, total=STEPS))
        if self.knobs["fragile_shutdown"]:
            try:
                done = subprocess.run(
                    ["sleep", str(self.knobs["shutdown_s"])], check=False
                )
                if done.returncode < 0:
                    self.say(
                        self.t(
                            "svc_not_stopped", unit="levi-aeri-vlm-8100", why="signal"
                        )
                    )
                    os._exit(1)
            except KeyboardInterrupt:
                self.say(
                    self.t(
                        "svc_not_stopped", unit="levi-aeri-vlm-8100", why="interrupted"
                    )
                )
                os._exit(1)
        else:
            time.sleep(self.knobs["shutdown_s"])
        self.say(
            self.t(
                "svc_released",
                n=7,
                total=STEPS,
                stopped="levi-aeri-vlm-8100",
                used=7494,
                baseline=7494,
            )
        )
        self.final = code


def _grab(text, pattern):
    match = re.search(pattern, text)
    return match.group(1).strip().strip('"') if match else None


def _start_ticks() -> str:
    try:
        return Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return "0"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["execute", "check"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--expect-plan", default="")
    parser.add_argument("--expect-binding", default="")
    args = parser.parse_args(argv)
    knobs = load_knobs(Path(args.config))
    fake = Fake(args, knobs)
    code = fake.run()
    return code


if __name__ == "__main__":
    sys.exit(main())
