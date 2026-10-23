"""A fake of the evaluation client, for the tests of the hosted run
(``levi/marvel0/run.py``). It stands where
``run_robotiq_client_collect_data.py`` (CD below) and ``run_robotiq_client.py``
(RC) stand in ``--levi-mode off``, manual gate, and keeps the behaviours the
hosting depends on, with the real strings (``fixtures/client_prompts.json``,
checked against the client by ``scripts/check_client_prompts.py``) and the real
traps:

* keys are read **one byte at a time, only when the client asks** (RC:575), in
  cbreak mode with ISIG on (RC:552). Nothing is drained, so a key sent while
  the client is busy waits in the tty and is read at its next question: the
  start gate reads ``s`` as "RUN screws" (RC:127; CD:1390), the incident of
  2026-10-20;
* the start gate allows ``\\r i q s p`` (CD:1375); ``s`` and ``p`` choose the
  task and *fall through to the start* (CD:1390-1417);
* a running episode reads one key per step: ``s``, ``f``, ``q`` (RC:1549-1556);
  every other byte is read and dropped;
* a step-limit episode ends as ``timeout`` and asks for ``d s f`` (CD:1455);
  without a tty the answer is ``d``, at the gate ``q`` (CD:1385, 1460);
* the episode close (video mux) is slow and keeps the session at ``running``
  (CD:1470); keys sent then wait in the tty;
* the arm goes home after every episode but ``aborted`` (CD:1509) in ``Homing
  over`` steps that never look at the stop flag (RC:924-960);
* SIGINT and SIGTERM set the stop flag, the second one exits (RC:438-467); at
  the step-limit prompt the stop flag answers ``d`` (RC:1236); SIGHUP is not
  handled (the process just ends);
* the session file is ``levi.eval.session.v1`` with a heartbeat thread, and a
  task switch at the gate closes the old file as ``stopped``/``task_changed``
  (eval_session.py:236-260);
* the exit code is 0 for ``q`` and for a signal stop, 1 for an exception.

Every arm movement it "makes" is a line in ``<rollout-root>/fake_arm.jsonl``
(``home``, ``step``), so a test can assert that nothing moved after a hold or an
end. It opens no socket, camera or device.

Tuning for a test: ``<rollout-root>/.fake_client.json`` (read at start; the
child environment is an allowlist, so nothing else reaches it): ``connect_s``,
``step_s``, ``home_steps``, ``home_step_s``, ``mux_s``, ``heartbeat_s``,
``crash_at_step``, ``refuse``, ``silent`` (never reaches the gate), ``no_session``,
``own_group`` (the client moves to a process group of its own), ``ignore_hup``
(it ignores SIGHUP, as a program started with ``nohup`` or one that handles it
would: only the launcher's own SIGKILL of the group then ends it), ``override``
(prompt key -> text it prints instead).

The argument grammar is ``levi.marvel0.command.FLAGS`` plus
``--display-images``; an unknown flag ends the process with 2 like the client's
parser (a test compares the two tables).
"""

import datetime
import json
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS = json.loads((HERE / "fixtures" / "client_prompts.json").read_text())["prompts"]
SCREWS = "pick screws of same size into the empty slot of screw box"  # RC:122
PLATES = "stack the plates of same color together"  # RC:123
TASK_KEYS = {"s": SCREWS, "p": PLATES}  # RC:127

VALUE = [
    "--remote-host", "--remote-port", "--view-camera-id", "--wrist-camera-id",
    "--franka-server-url", "--prompt", "--max-timesteps", "--control-hz", "--eval-num",
    "--min-z", "--home-duration-s", "--max-xy", "--max-step-m", "--max-step-rad", "--levi-mode",
    "--vlash-overlap-steps", "--rtc-execute-horizon", "--open-loop-horizon", "--rollout-root",
    "--rollout-group", "--eval-note",
]  # fmt: skip
SWITCH = [
    "--binarize-gripper", "--require-matching-server", "--allow-unknown-prompt",
    "--new", "--step-by-step", "--rtc", "--display-images",
]  # fmt: skip
TUPLE = {"--home-pose": 6, "--rx-range": 2, "--ry-range": 2, "--rz-range": 2}
FLAGS = {*VALUE, *SWITCH, *TUPLE}

_STOP = False  # RC:_STOP: set by the first SIGINT/SIGTERM


def say(name, **fields):
    print(PROMPTS[name]["render"].format(**fields), flush=True)


def usage_error(text):
    print(f"error: {text}", file=sys.stderr, flush=True)
    sys.exit(2)


def parse(argv) -> dict:
    """The flags as ``{flag: value}``; ``--no-<switch>`` is False."""
    found, i = {}, 0
    while i < len(argv):
        name, equals, value = argv[i].partition("=")
        off = False
        if name not in FLAGS and name.startswith("--no-") and not equals:
            name, off = "--" + name[5:], True
        if name not in FLAGS:
            usage_error(f"unrecognized arguments: {argv[i]}")
        if name in VALUE:
            if not equals:
                usage_error(f"{name} needs a value")
            found[name] = value
        elif name in SWITCH:
            found[name] = not off
        else:
            count = TUPLE[name]
            numbers = argv[i + 1 : i + 1 + count]
            if len(numbers) != count:
                usage_error(f"{name} needs {count} numbers")
            found[name] = [float(n) for n in numbers]
            i += count
        i += 1
    return found


def sanitize_task_name(text: str) -> str:
    """rollout_writer.py:93-99, byte for byte."""
    normalized = text.strip().replace(" ", "_")
    normalized = re.sub(r"[\\/]+", "_", normalized)
    normalized = re.sub(r"[^\w\-]+", "_", normalized, flags=re.UNICODE)
    return re.sub(r"_+", "_", normalized).strip("_")


def normalise(prompt: str) -> str:
    """RC:736: what the tokenizer makes of a prompt."""
    return prompt.strip().replace("_", " ").replace("\n", " ")


def canonical(prompt: str) -> str:
    for task in (SCREWS, PLATES):
        if normalise(task) == normalise(prompt):
            return task
    return prompt


def iso(stamp: float) -> str:
    return (
        datetime.datetime.fromtimestamp(stamp)
        .astimezone()
        .strftime("%Y-%m-%dT%H:%M:%S%z")
    )


class Arm:
    """What the 'arm' did: one line per movement, in a file a test reads."""

    def __init__(self, root: Path):
        self.path = root / "fake_arm.jsonl"

    def move(self, kind, **fields):
        line = json.dumps({"t": time.time(), "kind": kind, **fields})
        with open(self.path, "a") as handle:
            handle.write(line + "\n")


class Session:
    """eval_session.py, reduced to what the hosting reads: the file, a heartbeat
    every ``beat_s``, ``update``, ``retarget`` and ``stop``."""

    def __init__(self, root, group, task_folder, prompt, target, max_steps, beat_s):
        self.root, self.group, self.task_folder = Path(root), group, task_folder
        self.beat_s = beat_s
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.step_fn = None
        self.data = {
            "schema": "levi.eval.session.v1",
            "session_id": uuid.uuid4().hex,
            "run_id": "",
            "pid": os.getpid(),
            "host": os.uname().nodename,
            "started_at": iso(time.time()),
            "updated_at": iso(time.time()),
            "state": "standby",
            "reason": "",
            "waiting_reset_since": None,
            "prompt": prompt,
            "group": group,
            "task_folder": task_folder,
            "rollout_dir": str(self.root / group / task_folder),
            "policy": {"config": "fake", "checkpoint_dir": None},
            "episode": {
                "no": 0,
                "target": target,
                "counted": 0,
                "demo_index": None,
                "step": 0,
                "max_steps": max_steps,
            },
            "levi": {"enabled": False, "reset_wait_s": None},
            "fr3": {
                "ok": True,
                "robot_mode": 2,
                "robot_mode_name": "Move",
                "errors": [],
                "hardware_active": True,
                "state_age_s": 0.1,
                "source": "fake",
                "reasons": [],
            },
            "last_episode": None,
        }

    @property
    def path(self) -> Path:
        return self.root / ".eval_sessions" / f"{self.group}__{self.task_folder}.json"

    def start(self, state, reason):
        self.data["state"], self.data["reason"] = state, reason
        self.write()
        threading.Thread(target=self.beat, daemon=True).start()

    def beat(self):
        while not self.stop_event.wait(self.beat_s):
            self.write()

    def update(self, state=None, reason=None, episode=None, last=None, prompt=None):
        with self.lock:
            if state is not None:
                self.data["state"] = state
                reason = "" if reason is None else reason
            if reason is not None:
                self.data["reason"] = reason
            if episode:
                self.data["episode"].update(episode)
            if last is not None:
                self.data["last_episode"] = last
            if prompt is not None:
                self.data["prompt"] = prompt
        self.write()

    def retarget(self, prompt, group, task_folder):
        """eval_session.py:236: close the old file as stopped/task_changed and go
        on under the new key with a new session id."""
        if (group, task_folder) == (self.group, self.task_folder):
            self.update(prompt=prompt)
            return
        with self.lock:
            before = self.data["state"]
            self.data["state"], self.data["reason"] = "stopped", "task_changed"
            self.write()
            self.group, self.task_folder = group, task_folder
            self.data.update(
                group=group,
                task_folder=task_folder,
                prompt=prompt,
                rollout_dir=str(self.root / group / task_folder),
                state=before,
                reason="",
                last_episode=None,
                session_id=uuid.uuid4().hex,
            )
            self.data["episode"].update(no=0, counted=0, demo_index=None, step=0)
        self.write()

    def stop(self, state, reason):
        self.stop_event.set()
        with self.lock:
            self.data["state"], self.data["reason"] = state, reason
        self.write()

    def write(self):
        with self.lock:
            if self.step_fn is not None and self.data["state"] == "running":
                self.data["episode"]["step"] = int(self.step_fn())
            self.data["updated_at"] = iso(time.time())
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2) + "\n")
            os.replace(tmp, path)


class RawKeys:
    """RC:536-587: cbreak when stdin is a tty, one byte per ``get``."""

    def __init__(self):
        self.fd = None
        self.saved = None

    def __enter__(self):
        if sys.stdin.isatty():
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        self.restore()

    def restore(self):
        if self.fd is not None and self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            self.fd, self.saved = None, None

    def get(self):
        if self.fd is None:
            return None
        if select.select([self.fd], [], [], 0)[0]:
            try:
                raw = os.read(self.fd, 1)  # RC:575: one byte, nothing hidden
            except OSError:
                # A hung-up pty: Linux answers EIO, which the real client does not
                # catch, but the review read it as "b''" (RC:574-583), i.e. a
                # client that goes on without a keyboard. The fake takes the
                # worse reading, so only the launcher can stop it.
                return None
            return raw.decode("utf-8", errors="ignore").lower() or None
        return None


def wait_key(keys, allowed, banner, headless):
    """RC:1215-1245: block until one of ``allowed``; the stop flag answers q if
    q is allowed, else the headless answer."""
    print(banner, flush=True)
    if keys.fd is None:
        say("no_tty", key=headless)
        return headless
    while True:
        if _STOP:
            return "q" if "q" in allowed else headless
        k = keys.get()
        if k == "\n":
            k = "\r"
        if k and k in allowed:
            return k
        time.sleep(0.02)


def install_signals():
    """RC:438-467."""

    def install(first, second, name):
        def on(signum, frame):
            global _STOP
            if _STOP:
                print(PROMPTS[second]["render"], flush=True)
                sys.exit(1)
            _STOP = True
            print(PROMPTS[first]["render"], flush=True)

        signal.signal(name, on)

    install("interrupt_first", "interrupt_second", signal.SIGINT)
    install("sigterm_first", "sigterm_second", signal.SIGTERM)


def go_home(cfg, arm):
    """RC:924-960: the stop flag is never looked at."""
    n = int(cfg.get("home_steps", 30))
    say("homing_over", n=n)
    for i in range(1, n + 1):
        arm.move("home", i=i, n=n)
        time.sleep(float(cfg.get("home_step_s", 0.01)))
    say("home_reached")


def run_episode(cfg, arm, keys, max_steps, ep):
    """RC:1437-1644, manual mode: one key per step."""
    print(PROMPTS["running_banner"]["render"], flush=True)
    steps = 0
    for n in range(1, max_steps + 1):
        if _STOP:
            return "aborted", steps
        key = keys.get()
        if key == "s":
            print(PROMPTS["marked_success"]["render"], flush=True)
            return "success", steps
        if key == "f":
            print(PROMPTS["marked_fail"]["render"], flush=True)
            return "failure", steps
        if key == "q":
            print(PROMPTS["marked_quit"]["render"], flush=True)
            return "quit", steps
        if cfg.get("crash_at_step") == n:
            raise RuntimeError("fake crash")
        arm.move("step", ep=ep, n=n)
        print(f"[{n:3d}] [0.1 0.2 0.3]", flush=True)
        steps += 1
        time.sleep(float(cfg.get("step_s", 0.01)))
    return "timeout", steps


def next_demo_index(folder: Path) -> int:
    best = 0
    for child in folder.glob("*_[0-9][0-9][0-9][0-9]") if folder.is_dir() else []:
        best = max(best, int(child.name.rsplit("_", 1)[1]))
    return best + 1


def main(argv) -> int:
    args = parse(argv)
    root = Path(args["--rollout-root"])
    root.mkdir(parents=True, exist_ok=True)
    cfg_path = root / ".fake_client.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    arm = Arm(root)
    (root / "fake_start.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "pgid": os.getpgrp(),
                "sid": os.getsid(0),
                "tty": os.isatty(0),
                "cwd": os.getcwd(),
                "argv": argv,
                "env": dict(os.environ),
            }
        )
    )
    for key, text in cfg.get(
        "override", {}
    ).items():  # a client that words it differently
        PROMPTS[key] = {**PROMPTS[key], "render": text}
    if cfg.get("refuse"):
        print(PROMPTS["refusing"]["render"].format(exc="busy"), flush=True)
        return 2
    install_signals()
    if cfg.get("ignore_hup"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if cfg.get("own_group"):
        # (the worst case of the review: a client in a group of its own, so the
        # terminal's hang-up of its foreground group misses it, that goes on
        # without a keyboard; job control would stop it, so it ignores that)
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        signal.signal(signal.SIGTTIN, signal.SIG_IGN)
        os.setpgid(0, 0)
    group0 = args.get("--rollout-group") or "unknown_model"
    print(f"Rollout data: {root / group0}/<task>/demo_XXXX", flush=True)  # CD:1295
    print(
        f"Connecting to policy server at {args.get('--remote-host', '127.0.0.1')}:"
        f"{args.get('--remote-port', '8000')} ...",
        flush=True,
    )
    time.sleep(float(cfg.get("connect_s", 0.2)))
    say("connected", meta="{'config': 'fake'}")
    say("env_ready")
    if cfg.get("silent"):
        time.sleep(3600)
    prompt = canonical(args["--prompt"])
    group = args.get("--rollout-group") or "unknown_model"
    max_steps = int(args.get("--max-timesteps", 400))
    target = int(args.get("--eval-num", 0))
    session = None
    if not cfg.get("no_session"):
        session = Session(
            root,
            group,
            sanitize_task_name(prompt),
            prompt,
            target,
            max_steps,
            float(cfg.get("heartbeat_s", 0.2)),
        )
        session.start("standby", "session started")  # CD:1325
    ep = n_counted = 0
    end_state, end_reason = "stopped", "user_quit"  # CD:1351
    keys = RawKeys()
    keys.__enter__()  # CD:1346
    try:
        while not _STOP and (not target or n_counted < target):
            if session:
                session.update(
                    "standby",
                    PROMPTS["gate_reason"]["render"],
                    episode={"no": ep + 1, "counted": n_counted},
                )  # CD:1372
            progress = f"{n_counted}/{target}" if target else f"{n_counted} counted"
            task = sanitize_task_name(prompt)
            index = next_demo_index(root / group / task)
            banner = (
                PROMPTS["gate_banner"]["render"].format(
                    ep=ep + 1, progress=progress, prompt=prompt
                )
                + PROMPTS["gate_min_z"]["render"].format(
                    min_z=float(args.get("--min-z", 0))
                )
                + PROMPTS["gate_recording"]["render"].format(task=task, index=index)
                + PROMPTS["gate_keys_a"]["render"]
                + PROMPTS["gate_keys_b"]["render"]
            )
            k = wait_key(keys, "\riqsp", banner, headless="q")  # CD:1374
            if k == "q":
                break
            if k in TASK_KEYS:  # CD:1390: s and p choose AND start
                prompt = TASK_KEYS[k]
                say("task_selected", prompt=prompt)
            elif k == "i":
                continue  # (input() is not modelled: the hosting never sends i)
            ep += 1
            if session:
                session.retarget(prompt, group, sanitize_task_name(prompt))  # CD:1414
                session.update("homing", "", episode={"no": ep})
            go_home(cfg, arm)  # CD:1417
            demo = (
                root
                / group
                / sanitize_task_name(prompt)
                / f"demo_{next_demo_index(root / group / sanitize_task_name(prompt)):04d}"
            )
            demo.mkdir(parents=True, exist_ok=True)
            (demo / "metadata.json").write_text(
                json.dumps({"prompt": prompt, "steps": 0})
            )
            frames = [0]
            if session:
                session.update(
                    "running", "", episode={"no": ep, "step": 0, "max_steps": max_steps}
                )
                session.step_fn = lambda f=frames: f[0]
            t0 = time.monotonic()
            outcome, steps = run_episode(cfg, arm, keys, max_steps, ep)
            frames[0] = steps
            dur = time.monotonic() - t0
            say(
                "episode_ended", ep=ep, outcome=outcome, steps=steps, dur=dur
            )  # CD:1446
            verdict_by = "key"
            if outcome == "timeout":  # CD:1455
                k = wait_key(
                    keys,
                    "dsf",
                    PROMPTS["judge_prompt"]["render"].format(max_steps=max_steps),
                    headless="d",
                )
                outcome = {"s": "success", "f": "failure", "d": "discarded"}[k]
                verdict_by = "timeout-adjudicated"
                print(f"  -> {outcome}", flush=True)
            time.sleep(
                float(cfg.get("mux_s", 0.05))
            )  # CD:1470: the slow close, still 'running'
            counted = outcome in ("success", "failure")
            (demo / "metadata.json").write_text(
                json.dumps(
                    {
                        "eval": {
                            "outcome": outcome,
                            "verdict_by": verdict_by,
                            "counted": counted,
                        },
                        "stopped_at": iso(time.time()),
                    }
                )
            )
            if not counted:
                demo.rename(demo.with_name(demo.name.replace("demo_", "discarded_")))
            if outcome not in ("quit", "aborted"):
                n_counted += int(counted)
            if session:
                session.update(
                    last={
                        "outcome": outcome,
                        "steps": steps,
                        "duration_s": round(dur, 1),
                    },
                    episode={"counted": n_counted},
                )
            if outcome != "aborted":  # CD:1509
                if session:
                    session.update("homing", "after episode")
                go_home(cfg, arm)
            if outcome == "quit":
                break
        end_state, end_reason = (
            ("finished", "target reached")
            if target and n_counted >= target
            else ("stopped", "user_quit" if not _STOP else "interrupted")
        )  # CD:1515-1518
    except BaseException:
        end_state, end_reason = "fault", "exception"
        raise
    finally:
        keys.restore()
        say("closing_env")
        if session:
            session.stop(end_state, end_reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
