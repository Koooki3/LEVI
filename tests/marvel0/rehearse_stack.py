"""L3 of the stack actions: the page's code driving real ``systemd --user``
units that run stand-in programs. Run it once, by hand, with the arm off (**it refuses to run, exit 2, when a robot
process is on the machine**: ``ros2_control_node``, ``franka_server``,
``enpire_franka``, ``serve_policy`` or ``run_robotiq_client``):

    uv run --no-sync python tests/marvel0/rehearse_stack.py [--work DIR]

It proves what no fake machine can: that the units the page names are accepted
by the user manager with the properties the design record lists (realtime
limit, memory lock, OOM preference, SIGINT, no SIGKILL), that the unit's
control group holds exactly the stand-in (the wrapper replaced itself), that a
SIGINT stop ends it, that the units the page started survive the stop of the
process that started them (what a restart of the product LEVI does to its
children), that ``systemd-run --pipe --wait`` returns the output of a one-shot
check, and that the ``levi-aeri-*`` listing sees the units while they run. It also
measures what the page can only assume (the findings of the reviews of
2026-10-21): the exit status and output ``systemd-run --wait`` gives after
``RuntimeMaxSec`` ended a command, what ``timeout -s INT`` does inside a unit,
how many SIGINTs a child of a launch-like parent gets under
``KillMode=control-group`` (the stop signal of the unit and the parent's own),
and whether a unit whose stop timed out under ``SendSIGKILL=no`` keeps its
processes and a task count.

**Nothing real runs.** The profile is ``fake``: the units are named with
``rehearsal``, their commands are the stand-ins of ``fake_stack/`` (this
interpreter on those scripts, behind the reviewed wrapper and a fake
environment file), the marker, the health file, the stack lock and the
records are in a temporary folder, port 5056 is the only port used, and the
real ``levi-fr3-health-monitor`` unit is only read. The real argv of A, B and C
is never built here. Everything it started is stopped and listed at the end;
leftovers fail the run."""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from fake_stack import fake_specs

from levi.marvel0 import (
    control_site,
    gripper,
    net,
    stack,
    stack_act,
    stack_units,
    status,
)

PRODUCT_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/levi-product.service\n"
)
TAG = "levi-aeri-stack-marvel0-rehearsal-"
NAMES = {k: f"{TAG}{k}" for k in "abc"}
PARENT = f"{TAG}parent"
CHILD = f"{TAG}orphan"
STUCK = f"{TAG}stuck"
SIGNALS = f"{TAG}signals"
ROBOT_PATTERN = (
    "ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)
PORT = 5056
RESULTS: list = []


def say(text=""):
    print(text, flush=True)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    say(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return ok


def systemctl(*args, timeout=15):
    done = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def show(unit, *props):
    _, text = systemctl("show", f"{unit}.service", "--property=" + ",".join(props))
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def rehearsal_units():
    _, text = systemctl(
        "list-units", "--all", "--no-legend", "--plain", "--full", f"{TAG}*"
    )
    return [line.split()[0] for line in text.splitlines() if line.strip()]


def passive_robot_check():
    out = subprocess.run(
        ["ps", "-eo", "args"], capture_output=True, text=True, check=False
    ).stdout
    return [
        line
        for line in out.splitlines()
        if re.search(ROBOT_PATTERN, line) and "rehearse" not in line
    ]


def wait_state(act, key, state, timeout=15.0):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = act.stack.view()["act"]["terminals"][key]
        if last["state"] == state:
            return last
        time.sleep(0.1)
    raise RuntimeError(
        f"{key} never became {state}; last {last['state']} {last.get('failure')}"
    )


class Rehearsal:
    def __init__(self, work):
        self.work = Path(work)
        self.health = self.work / "franka_control" / "run" / "fr3_health.json"
        self.health.parent.mkdir(parents=True)
        self.hub = self.work / "hub"
        self.hub.mkdir()
        self.locks = self.work / "locks"
        self.locks.write_text("")
        (self.work / "qos.py").write_text(f"{stack_units.QOS_OK}\n")
        self.fci = {"state": "open"}
        self.fci_calls = []  # (what the FCI check was asked: it must not be asked by the B check)
        self.specs = fake_specs.fake_specs(
            self.health,
            port=PORT,
            via_wrapper=True,
            names=NAMES,
            ready_timeout_s=20.0,
            stop_timeout_s=5,
            marker_wait_s=0.5,
            qos_file=self.work / "qos.py",
            alive_file=self.work / "b.alive",
        )
        fake_site = control_site.ControlSite(
            control_site.StackControl(
                enabled=True,
                franka_control=str(self.work / "franka_control"),
                robot_ip="10.42.0.157",
                com_port=str(Path(__file__)),
                reset_joint_target=(0.0,) * 7,
            ),
            control_site.ClientControl(),
        )
        self.loaded = control_site.ControlLoad("ok", fake_site)
        base = stack.Host(
            run=stack.guarded(self.run),
            marker=lambda: self.hub / ".arm-controller-active",
            lock_path=lambda: self.hub / stack.LOCK_FILE,
            health_file=lambda: (str(self.health), 3.0),
            arm_sockets=self.arm_sockets,
            sessions=lambda now: {
                "state": "ok",
                "roots": 1,
                "sessions": [],
                "detail": None,
            },
            net_table=net.read,
            cgroup=lambda: PRODUCT_CGROUP,
            in_main_checkout=lambda: True,
            policy_port=lambda: 8000,
            environ={},
            locks_path="/proc/locks",
            uid=os.getuid(),
        )
        self.sup = stack.Stack(base)
        host = stack_act.ActHost(
            base=base,
            state_root=lambda: self.work / "state" / "marvel0" / "stack",
            fci_probe=self.fci_probe,
            arm_lock=str(self.work / "arm.lock"),
            background=False,
            sleep=lambda s: time.sleep(min(s, 0.2)),
        )
        self.act = stack_act.Act(
            self.sup, host, setup=lambda: (self.loaded, self.specs)
        )
        self.sup.act = self.act

    def fci_probe(self, ip, port):
        self.fci_calls.append((ip, port))
        return dict(self.fci)

    @staticmethod
    def run(argv, timeout_s):
        """The real user manager, except that the real monitor unit (which may
        be running for the owner) is not looked at: the rehearsal's health file
        is its own."""
        if (
            argv[:3] == ["systemctl", "--user", "show"]
            and stack.MONITOR_SERVICE in argv
        ):
            return (
                0,
                f"Id={stack.MONITOR_SERVICE}\nLoadState=not-found\nActiveState=inactive\n",
            )
        return stack.run_command(argv, timeout_s)

    def arm_sockets(self):
        """The arm has a control connection while the stand-in A is up."""
        shown = stack.show_unit(stack.guarded(self.run), f"{NAMES['a']}.service")
        up = bool(shown) and shown.get("ActiveState") in (
            "active",
            "activating",
            "deactivating",
        )
        return {"state": "active" if up else "inactive", "detail": "stand-in"}

    # -- request helpers
    def body(self, action, rid, **extra):
        return {"request_id": rid, "confirm": stack.PHRASES[action], **extra}

    def start(self, key):
        need = stack_act.ATTEST_NEEDED[key]
        return self.act.start(
            key,
            self.body(
                stack_act.ACTIONS[("start", key)],
                f"rehearse-start-{key}",
                attest=dict.fromkeys(need, True),
            ),
        )

    def stop(self, key, **extra):
        return self.act.stop(
            key,
            self.body(
                stack_act.ACTIONS[("stop", key)], f"rehearse-stop-{key}", **extra
            ),
        )

    def write_health(self, **flags):
        from fake_stack.fake_common import write_health

        write_health(str(self.health), **flags)


def scenario(r: Rehearsal):
    act = r.act
    say("1. The stack is seen, nothing exists yet")
    doc = r.sup.view()
    check(
        "profile is real for the product cgroup and the specs are the stand-ins",
        doc["profile"] == "real" and doc["act"]["spec_profile"] == "fake",
    )
    check("no rehearsal unit exists", rehearsal_units() == [])

    say(
        "2. Marker, FCI answer, probe of the gripper stack through a real one-shot unit"
    )
    r.write_health(
        hardware_active=None,  # (no manager: null, as fr3_health_monitor.py writes it)
        controller_active=None,
        red_light=True,
        robot_mode_name="IDLE",
    )
    act.marker_op("touch", r.body("marker_touch", "rehearse-marker"))
    time.sleep(0.7)
    r.write_health(
        hardware_active=None,  # (no manager: null, as fr3_health_monitor.py writes it)
        controller_active=None,
        red_light=True,
        robot_mode_name="IDLE",
    )
    act.preflight_a({"request_id": "rehearse-pre-a"})
    doc = r.sup.view()["act"]
    check(
        "the one-shot unit returned its output through --pipe --wait (gripper stack seen absent)",
        doc["terminals"]["b"]["state"] == "idle",
        doc["terminals"]["b"]["reason"],
    )
    check(
        "the one-shot unit is gone (--collect)",
        not [u for u in rehearsal_units() if "oneshot" in u],
    )
    check(
        "A may start",
        doc["terminals"]["a"]["can_start"],
        str([b["code"] for b in doc["terminals"]["a"]["start_blockers"]]),
    )

    say("3. Start A as a real transient unit behind the wrapper")
    started = time.monotonic()
    answer = r.start("a")
    check(
        "systemd accepted every property of the unit",
        answer["done"] == "started",
        answer.get("unit", ""),
    )
    t = wait_state(act, "a", "ready")
    check(
        "A is ready from its log and the health file",
        t["state"] == "ready",
        f"{time.monotonic() - started:.1f} s",
    )
    say("3b. The gripper-stack check alone, with A up: no connection to the arm")
    act._b_probe["at"] -= stack_act.B_PROBE_TTL_S + 1  # (the person comes to B late)
    old = r.sup.view()["act"]["terminals"]["b"]
    check(
        "an old check reads unknown, not absent",
        (old["state"], old["reason"]) == ("unknown", "b.probe_stale"),
        f"{old['state']} {old['reason']}",
    )
    fci_before = len(r.fci_calls)
    act.probe_b({"request_id": "rehearse-probe-b"})
    again = r.sup.view()["act"]["terminals"]["b"]
    check(
        "the check alone, through a real one-shot unit, makes B known again",
        (again["state"], again["reason"]) == ("idle", "b.not_running"),
        f"{again['state']} {again['reason']}",
    )
    check("it made no connection to the arm", len(r.fci_calls) == fci_before)
    check(
        "its one-shot unit is gone (--collect)",
        not [u for u in rehearsal_units() if "oneshot" in u],
    )
    props = show(
        NAMES["a"],
        "ControlGroup",
        "MainPID",
        "Type",
        "KillMode",
        "KillSignal",
        "SendSIGKILL",
        "TimeoutStopUSec",
        "Restart",
        "LimitRTPRIO",
        "LimitMEMLOCK",
        "ManagedOOMPreference",
        "StandardInput",
        "Nice",
        "CPUAffinity",
        "ActiveState",
        "SubState",
    )
    say(f"     systemd shows: {props}")
    expect = {
        "Type": "exec",
        "KillMode": "control-group",
        "KillSignal": "2",
        "SendSIGKILL": "no",
        "Restart": "no",
        "LimitRTPRIO": "99",
        "LimitMEMLOCK": "infinity",
        "ManagedOOMPreference": "avoid",
        "ActiveState": "active",
    }
    for key, want in expect.items():
        check(f"unit property {key}", props.get(key) == want, f"{props.get(key)!r}")
    check(
        "TimeoutStopUSec is 5 s as configured",
        props.get("TimeoutStopUSec") == "5s",
        props.get("TimeoutStopUSec"),
    )
    group = props.get("ControlGroup", "")
    procs_file = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
    pids = (
        [int(x) for x in procs_file.read_text().split()] if procs_file.exists() else []
    )
    check(
        "the control group holds exactly the stand-in (the wrapper replaced itself)",
        pids == [int(props["MainPID"])],
        f"{pids} vs MainPID {props['MainPID']}",
    )
    check(
        "the unit sits in app.slice beside, not under, the product's unit",
        "/app.slice/" in group and "levi-product" not in group,
        group,
    )
    log = Path(
        stack_act.record.read_record(r.work / "state" / "marvel0" / "stack" / "a")[
            "log"
        ]
    ).read_text()
    check(
        "the wrapper's environment reached the program with the colour switch off",
        "FAKE_ROS_SOURCED=1 RCUTILS_COLORIZED_OUTPUT=0" in log,
    )
    check(
        "the program wrote to the file the page named",
        "Successful set up FIFO RT scheduling policy" in log,
    )
    seen = re.search(r"RTPRIO=\(([^)]*)\) MEMLOCK=\(([^)]*)\)", log)
    say(
        f"     limits the program saw inside the unit: rtprio={seen and seen.group(1)} memlock={seen and seen.group(2)}"
    )
    check(
        "the program inside the unit saw rtprio 99 and unlimited memlock",
        bool(seen) and seen.group(1) == "99, 99" and seen.group(2).startswith("-1, -1"),
        seen.group(0) if seen else "no line",
    )

    say("4. B, the gripper test through real one-shot units, the checks before C, C")
    r.start("b")
    wait_state(act, "b", "ready")
    for action in ("open", "close"):
        act.gripper(
            {
                **r.body(f"gripper_{action}", f"rehearse-grip-{action}"),
                "action": action,
                "attest": {"hands_clear": True},
            }
        )
        last = r.sup.view()["act"]["gripper_test"]["last"]
        check(
            f"gripper {action} returned a result",
            last["ok"] and last["code"] == "reached",
            str(last),
        )
    act.preflight_c({"request_id": "rehearse-pre-c"})
    pre = r.sup.view()["act"]["checks"]["c_preflight"]
    check("the three checks before C passed through real units", pre["ok"], str(pre))
    r.start("c")
    t = wait_state(act, "c", "ready")
    check(
        "C listens on the stand-in port and shows the controller active",
        t["state"] == "ready",
    )
    check(
        "the kernel shows a LISTEN row on the stand-in port only",
        bool(net.listeners(net.read(), PORT)),
    )

    say(
        "5. The listing of levi-aeri-* units sees them, the monitor's unit is not in it"
    )
    part = status.units_part()
    names = {u["name"] for u in part["items"]}
    check(
        "status.units_part lists the rehearsal units as active",
        {f"{NAMES[k]}.service" for k in "abc"} <= names and part["state"] == "ok",
    )
    check(
        "the real monitor unit is not matched by the glob",
        "levi-fr3-health-monitor.service" not in names,
    )

    say(
        "6. The units the page started survive the stop of the process that started them"
    )
    survive_check(r)

    say("7. Stop in order: C, A, B; SIGINT; the page never kills")
    for key in "cab":
        before = time.monotonic()
        r.stop(key)
        took = time.monotonic() - before
        gone = show(NAMES[key], "LoadState")["LoadState"] == "not-found"
        check(f"stop {key.upper()}: the unit is gone", gone, f"{took:.1f} s")
    check(
        "the marker was released after A stopped",
        not (r.hub / ".arm-controller-active").exists(),
    )
    check("no rehearsal unit is left", rehearsal_units() == [], str(rehearsal_units()))

    say("8. A one-shot that hangs is ended by RuntimeMaxSec (the backstop), once")
    hang_check(r)

    say("9. What the page assumes about the commands that never end by themselves")
    probe_check(r)
    timeout_check(r)

    say(
        "10. How many SIGINTs reach a child of a launch-like parent (KillMode=control-group)"
    )
    signal_count_check(r)

    say("11. A unit whose stop timed out under SendSIGKILL=no keeps its processes")
    stuck_check(r)


def survive_check(r: Rehearsal):
    """A parent unit starts a child unit with systemd-run and is then stopped,
    as the product's unit is when it restarts: the child must keep running."""
    parent_script = r.work / "parent.py"
    parent_script.write_text(
        "import subprocess, sys\n"
        f"subprocess.run(['systemd-run', '--user', '--quiet', '--unit={CHILD}',\n"
        "  '--property=KillSignal=SIGINT', '--property=SendSIGKILL=no', '--',\n"
        f"  {sys.executable!r}, {str(fake_specs.SCRIPTS['fake_b'])!r}, '--mode=ok'], check=True)\n"
        "import time\ntime.sleep(120)\n"
    )
    systemctl_run(
        [
            "--unit=" + PARENT,
            "--property=KillMode=control-group",
            "--property=Type=exec",
            "--collect",
            "--",
            sys.executable,
            str(parent_script),
        ]
    )
    time.sleep(1.0)
    child_before = show(CHILD, "ActiveState", "ControlGroup")
    systemctl("stop", f"{PARENT}.service")
    time.sleep(0.5)
    child_after = show(CHILD, "ActiveState", "ControlGroup")
    check(
        "a child unit started by a parent unit is a sibling in app.slice",
        "/app.slice/" in child_before.get("ControlGroup", "")
        and PARENT not in child_before.get("ControlGroup", ""),
        child_before.get("ControlGroup", ""),
    )
    check(
        "stopping the parent unit leaves the child unit running",
        child_after.get("ActiveState") == "active",
        str(child_after),
    )
    systemctl("stop", f"{CHILD}.service")


def systemctl_run(args):
    done = subprocess.run(
        ["systemd-run", "--user", "--quiet", *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def hang_check(r: Rehearsal):
    hanging = fake_specs.fake_specs(
        r.health, port=PORT, via_wrapper=True, gripper="hang", names=NAMES
    )
    started = time.monotonic()
    argv = stack_units.oneshot_argv(
        f"{TAG}oneshot-hang", hanging.gripper_cmd(0.0), 3, "rehearsal hang"
    )
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    took = time.monotonic() - started
    check(
        "RuntimeMaxSec ended a hung one-shot",
        took < 15 and done.returncode != 0,
        f"{took:.1f} s, exit {done.returncode}",
    )
    check(
        "the hung one-shot's unit is gone",
        not [u for u in rehearsal_units() if "hang" in u],
    )


def oneshot(r, name, argv, runtime_max_s):
    """One real one-shot unit through ``stack_units.oneshot_argv`` (the page's
    own builder): ``(exit status, output, seconds)``."""
    started = time.monotonic()
    done = subprocess.run(
        stack_units.oneshot_argv(f"{TAG}oneshot-{name}", argv, runtime_max_s, name),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    text = done.stdout + (done.stderr if done.returncode else "")
    return done.returncode, text, time.monotonic() - started


def probe_check(r: Rehearsal):
    """The probe whether a gripper stack runs: the stand-in logs the warning of
    the real command and never ends. RuntimeMaxSec ends it. What the page needs
    is the output; the exit status is printed so that it is known."""
    alive = r.work / "b.absent"  # (never created)
    argv = (
        sys.executable,
        str(fake_specs.SCRIPTS["fake_oneshot"]),
        "controllers",
        "hang",
        str(alive),
    )
    code, text, took = oneshot(r, "probe-hang", argv, 4)
    say(f"     exit status after RuntimeMaxSec: {code}; {took:.1f} s; output: {text!r}")
    check("RuntimeMaxSec ended the hung probe", took < 20, f"{took:.1f} s")
    check(
        "the output printed before the kill came back (warning included)",
        "Could not contact service" in text,
    )
    check(
        "the page reads 'absent' from that output whatever the status was",
        stack_units.parse_controllers(text)["state"] == "absent",
    )
    check(
        "the hung probe's unit is gone",
        not [u for u in rehearsal_units() if "probe-hang" in u],
    )


def timeout_check(r: Rehearsal):
    """`timeout -s INT -k` inside a unit, as the gripper test and the probe run
    it: a program that handles SIGINT says so and the status is 124; one that
    ignores it is killed. The statuses systemd-run passes on are printed, and
    each one is given to ``gripper.judge`` as the gripper test would be (the
    review of 2026-10-21 expects 255 for the killed one, not the 137 a shell
    sees): the verdict is asserted, not the number, so that any status but 0 and
    124 passes and a 0 or 124 for a killed program (which would make the page
    call the gripper stopped) fails."""
    code_src = (
        "import signal, sys, time\n"
        "def h(sig, frm):\n"
        "    print('Goal canceled.', flush=True); sys.exit(0)\n"
        "if sys.argv[1] == 'cancel': signal.signal(signal.SIGINT, h)\n"
        "else: signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    for mode in ("cancel", "ignore"):
        argv = (
            "timeout",
            "-s",
            "INT",
            "-k",
            "2",
            "2",
            sys.executable,
            "-c",
            code_src,
            mode,
        )
        code, text, took = oneshot(r, f"timeout-{mode}", argv, 15)
        say(f"     {mode}: exit status {code}, {took:.1f} s, output {text!r}")
        verdict = gripper.judge(code, text)["code"]
        wanted = {"cancel": "timeout", "ignore": "timeout_killed"}[mode]
        check(
            f"gripper.judge reads the {mode} program's end (status {code}) as {wanted}",
            verdict == wanted,
            verdict,
        )
        if mode == "cancel":
            check(
                "SIGINT reached the program in the unit (it cancelled)",
                "Goal canceled." in text,
            )
        check(
            f"timeout ended the {mode} program within its bounds",
            took < 10,
            f"{took:.1f} s",
        )
        check(
            f"the {mode} one-shot's unit is gone",
            not [u for u in rehearsal_units() if f"timeout-{mode}" in u],
        )


SIGNAL_PARENT = (
    "import signal, subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
    "signal.signal(signal.SIGINT, lambda s, f: child.send_signal(signal.SIGINT))\n"
    "child.wait()\n"
)
SIGNAL_COUNTER = (
    "import signal, sys, time\n"
    "def h(sig, frm):\n"
    "    open(sys.argv[1], 'a').write('SIGINT\\n')\n"
    "    global stop_at\n"
    "    stop_at = stop_at or time.monotonic() + 1.0\n"
    "stop_at = None\n"
    "signal.signal(signal.SIGINT, h)\n"
    "while stop_at is None or time.monotonic() < stop_at: time.sleep(0.05)\n"
)


def signal_count_check(r: Rehearsal):
    """A parent that forwards SIGINT to its child the way a non-interactive
    ``ros2 launch`` does (launch/actions/execute_local.py:448-452), run as a
    unit with A's stop properties; the child counts what it receives."""
    parent = r.work / "sig_parent.py"
    counter = r.work / "sig_counter.py"
    log = r.work / "sig.log"
    parent.write_text(SIGNAL_PARENT)
    counter.write_text(SIGNAL_COUNTER)
    systemctl_run(
        [
            "--unit=" + SIGNALS,
            "--property=Type=exec",
            "--property=KillMode=control-group",
            "--property=KillSignal=SIGINT",
            "--property=SendSIGKILL=no",
            "--property=TimeoutStopSec=10",
            "--",
            sys.executable,
            str(parent),
            str(counter),
            str(log),
        ]
    )
    time.sleep(1.0)
    systemctl("stop", f"{SIGNALS}.service", timeout=30)
    count = len(log.read_text().split()) if log.exists() else 0
    say(f"     SIGINTs the child received on one stop: {count}")
    check("the child was told to stop", count >= 1, str(count))
    check(
        "the unit is gone after the stop",
        show(SIGNALS, "LoadState").get("LoadState") == "not-found",
    )


def stuck_check(r: Rehearsal):
    """A program that ignores SIGINT: the stop times out, the unit is failed,
    and (this is what the page's 'acknowledge' relies on) its processes and
    task count are still visible. Cleaned up here with SIGKILL, as only a
    rehearsal may."""
    script = r.work / "stuck.py"
    script.write_text(
        "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(300)\n"
    )
    systemctl_run(
        [
            "--unit=" + STUCK,
            "--property=Type=exec",
            "--property=KillMode=control-group",
            "--property=KillSignal=SIGINT",
            "--property=SendSIGKILL=no",
            "--property=TimeoutStopSec=2",
            "--",
            sys.executable,
            str(script),
        ]
    )
    time.sleep(0.5)
    code, _ = systemctl("stop", f"{STUCK}.service", timeout=30)
    shown = show(
        STUCK,
        "ActiveState",
        "SubState",
        "Result",
        "TasksCurrent",
        "MainPID",
        "ControlGroup",
    )
    say(f"     stop exit {code}; systemd shows {shown}")
    check(
        "the stop failed (nothing killed it)",
        code != 0 or shown.get("ActiveState") == "failed",
        str(shown),
    )
    group = shown.get("ControlGroup", "")
    procs_file = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
    pids = (
        [int(x) for x in procs_file.read_text().split()]
        if group and procs_file.exists()
        else []
    )
    tasks = shown.get("TasksCurrent", "")
    check(
        "its processes are still in the control group, and a count or a process list says so",
        bool(pids) and (tasks.isdigit() and int(tasks) > 0 or not tasks.isdigit()),
        f"tasks={tasks!r} pids={pids}",
    )
    code, _ = systemctl("kill", "--signal=SIGINT", f"{STUCK}.service")
    time.sleep(0.5)
    after = show(STUCK, "TasksCurrent")
    check(
        "a second SIGINT (what the page sends) is ignored by this program: still there",
        after.get("TasksCurrent", "0") not in ("0",),
        str(after),
    )
    systemctl("kill", "--signal=SIGKILL", f"{STUCK}.service")  # (rehearsal only)
    time.sleep(0.5)


def cleanup(r: Rehearsal | None):
    say("Cleanup")
    for name in (*NAMES.values(), PARENT, CHILD, STUCK, SIGNALS):
        if name == STUCK:
            systemctl("kill", "--signal=SIGKILL", f"{name}.service")  # (rehearsal only)
        systemctl("stop", f"{name}.service")
        systemctl("reset-failed", f"{name}.service")
    for unit in rehearsal_units():
        systemctl("stop", unit)
        systemctl("reset-failed", unit)
    left = rehearsal_units()
    check("leftover units after cleanup: none", left == [], str(left))
    stray = subprocess.run(
        ["pgrep", "-f", str(fake_specs.HERE / "fake_")],
        capture_output=True,
        check=False,
    )
    check("leftover stand-in processes after cleanup: none", stray.returncode == 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", default=None)
    args = parser.parse_args()
    robot = passive_robot_check()
    say(f"passive check for robot processes: {'none' if not robot else robot}")
    if robot:
        say(
            "REFUSED: a robot process is on this machine (the arm controller or a "
            "demo may be live). This rehearsal starts real systemd units and is "
            "not run beside them; nothing was started."
        )
        return 2
    base = args.work or tempfile.gettempdir()
    work = Path(tempfile.mkdtemp(prefix="rehearse-stack-", dir=base))
    say(f"work folder: {work}")
    # The names the page asks about: this run only, this process only.
    stack.STACK_UNITS.update({k.upper(): f"{v}.service" for k, v in NAMES.items()})
    stack_units.ONESHOT_PREFIX = f"{TAG}oneshot-"
    rehearsal = None
    try:
        rehearsal = Rehearsal(work)
        scenario(rehearsal)
    except Exception as exc:  # noqa: BLE001 - report, then clean up
        check("the rehearsal ran to its end", False, f"{type(exc).__name__}: {exc}")
    finally:
        cleanup(rehearsal)
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    say(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for name in failed:
        say(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
