"""The way out always exists (the fourth review of the arm stack's buttons asked
for a stuck state to be looked for, not argued about).

Random walks over the fake machine of ``stack_act_machine.py``: each one takes
a few dozen steps, most of them the next step of the bring-up the document
offers (M, marker, the checks, A, B, the gripper test, C), the rest anything a
person or the machine can do (a stop out of order, a check with the wrong
answer, the clock jumping, the monitor going quiet, the socket table or systemd
not answering, a unit that hangs on its stop, a restart of the page, a
guided start and its cancel). Then the machine is let to answer again, and the
page's **own** buttons must be able to bring everything of the page's down:
every unit of ours stopped or acknowledged (a unit that hung ends on a second
SIGINT, which is what the button sends), the marker released and the monitor
stoppable.

A walk that fails names its seed and the last steps; the same seed repeats it
(``python -m pytest tests/marvel0/test_marvel0_stack_act_walk.py -k <seed>``).
This is not a proof: it found no stuck state in the walks it was written with,
and it fails when a stop, an acknowledge or the release is made to refuse for
ever (``mutate_stack_act.py`` has such mutants). None of it starts a process or
opens a socket."""

import random

import pytest
from stack_act_machine import CONTROLLERS_OUT, NO_MANAGER, ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

from levi.marvel0 import stack, stack_act

STEPS = 70
WALKS_PER_TEST = 12
GROUPS = 5
# (a step named twice is taken twice as often)
OTHER_STEPS = [
    *("marker", "pre_a", "probe_b", "start_a", "start_b", "start_c"),
    *("stop_a", "stop_b", "stop_c", "stop_m", "grip", "seq", "adv", "adv"),
    *("time", "time", "hang", "table", "systemd", "sockets"),
    *("monitor_off", "monitor_on", "restart", "up", "up", "pre_c", "cancel"),
    *("release", "m_ready"),
]


class Walk:
    """One walk. ``log`` is what to read when it fails."""

    def __init__(self, seed, tmp_path):
        self.rng = random.Random(seed)
        folder = tmp_path / f"walk-{seed}"
        folder.mkdir()
        self.m = ActMachine(folder)
        self.m.fci_time_wait = self.rng.random() < 0.5  # (the check's own socket)
        self.monitor_alive = True
        self.log = []
        self.n = 0

    def rid(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n:08d}"

    def monitor_writes(self, force=False):
        """The health file as the real monitor writes it for the machine as it
        is (fr3_health_monitor.py:115-120: no manager, no hardware state)."""
        m = self.m
        if (not self.monitor_alive and not force) or m.monitor is None:
            return
        a_up = "a" in m.units and m.units["a"]["active"] == "active"
        c_up = "c" in m.units and m.units["c"]["active"] == "active"
        if a_up:
            m.health(
                hardware=True,
                controller=c_up,
                red=False,
                mode="MOVE" if c_up else "IDLE",
            )
        else:
            m.no_manager()

    def attempt(self, name, call):
        """One step: a refusal is an answer; an error is not."""
        self.log.append(name)
        try:
            call()
        except stack.Refuse:
            pass
        assert "error" not in self.m.doc()

    def next_of_the_bring_up(self):
        """The step the document offers next (a person following the buttons)."""
        doc = self.m.doc()
        t = doc["terminals"]
        a, b, c = t["a"], t["b"], t["c"]
        if t["m"]["state"] != "ready":
            return "m_ready"
        if a["state"] == "idle":
            if doc["marker"]["state"] == "absent":
                return "marker"
            if a["can_start"]:
                return "start_a"
            needed = [x["code"] for x in a["start_blockers"]]
            if "gate.fci_probe" in needed or "gate.layers_known" in needed:
                return "pre_a"
            return "time"
        if a["state"] == "ready":
            if b["can_start"]:
                return "start_b"
            if b["state"] == "unknown":
                return "probe_b"
            if b["state"] == "ready":
                if c["state"] == "idle":
                    if any(x["code"] == "c_preflight" for x in c["start_blockers"]):
                        return "pre_c"
                    return "start_c" if c["can_start"] else "time"
                if c["state"] != "ready":
                    return "up"
                return self.rng.choice(["grip", "time", "adv", "up"])
        return "up"

    def step(self):
        m, rng = self.m, self.rng
        name = (
            self.next_of_the_bring_up()
            if rng.random() < 0.7
            else rng.choice(OTHER_STEPS)
        )
        if name == "m_ready":
            self.attempt(name, lambda: (m.ours(), self.monitor_writes(True)))
        elif name == "marker":
            m.run_no += 1  # (a new request id: the fake counts them)
            self.attempt(name, m.set_marker_old)
        elif name == "pre_a":
            m.controllers_out, m.controllers_code = rng.choice(
                [(NO_MANAGER, 124)] * 7 + [(CONTROLLERS_OUT, 0)] * 3
            )
            self.attempt(name, lambda: m.act.preflight_a({"request_id": self.rid("a")}))
        elif name == "probe_b":
            m.controllers_out, m.controllers_code = rng.choice(
                [(NO_MANAGER, 124), (CONTROLLERS_OUT, 0), ("garbage\n", 1)]
            )
            self.attempt(name, lambda: m.act.probe_b({"request_id": self.rid("b")}))
        elif name.startswith("start_"):
            key = name[-1]
            self.attempt(
                name, lambda: m.act.start(key, m.start_body(key, self.rid("st")))
            )
        elif name in ("stop_a", "stop_b", "stop_c"):
            key = name[-1]
            extra = {}
            if rng.random() < 0.3:
                extra["client_override"] = stack_act.CLIENT_OVERRIDE
            self.attempt(
                name, lambda: m.act.stop(key, m.stop_body(key, self.rid("sp"), **extra))
            )
        elif name == "stop_m":
            self.attempt(
                name, lambda: m.act.stop("m", m.body("monitor_stop", self.rid("sm")))
            )
        elif name == "grip":
            action = rng.choice(["open", "close"])
            body = m.body(f"gripper_{action}", self.rid("gr"))
            self.attempt(
                name,
                lambda: m.act.gripper(
                    {**body, "action": action, "attest": {"hands_clear": True}}
                ),
            )
        elif name == "seq":
            body = m.body(
                "arm_start",
                self.rid("sq"),
                attest={"desk_done": True, "estop_hand": True},
            )
            self.attempt(name, lambda: m.act.sequence_start(body))
        elif name == "adv":
            self.attempt(name, m.act.advance)
        elif name == "cancel":
            self.attempt(
                name, lambda: m.act.sequence_cancel({"request_id": self.rid("cn")})
            )
        elif name == "time":
            m.t += rng.choice([2, 5, 30, 70, 100, 400, 700, 4000])
            self.monitor_writes()
        elif name == "hang":
            key = rng.choice("abc")
            if key in m.units:
                m.units[key]["hangs"] = True  # (its next stop times out)
        elif name == "table":
            m.table_ok = not m.table_ok
        elif name == "systemd":
            m.systemd_down = not m.systemd_down
        elif name == "sockets":
            m.sockets = rng.choice(
                [
                    {"state": "unknown", "detail": "arm_guard failed"},
                    {"state": "active", "detail": "1 socket in TIME_WAIT"},
                    {"state": "inactive", "detail": "no socket to the arm"},
                ]
            )
        elif name == "monitor_off":
            self.monitor_alive = False
        elif name == "monitor_on":
            self.monitor_alive = True
            self.monitor_writes(True)
        elif name == "restart":
            again = stack_act.Act(
                stack.Stack(m.host()),
                m.act_host(),
                setup=lambda: (m.loaded, m.specs),
            )
            again.stack.act = again
            m.act, m.sup = again, again.stack
            self.log.append(name)
            again.resume()
        elif name == "up":
            for key, up in (("a", m.a_up), ("b", m.b_up), ("c", m.c_up)):
                if key in m.units and m.units[key]["active"] == "active":
                    up()
            self.monitor_writes()
        elif name == "pre_c":
            m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
            self.attempt(name, lambda: m.act.preflight_c({"request_id": self.rid("c")}))
        elif name == "release":
            self.attempt(
                name,
                lambda: m.act.marker_op(
                    "release", m.body("marker_clear", self.rid("rl"))
                ),
            )

    def machine_answers_again(self):
        """Whatever was wrong with the reads is over; what hung gets a second
        SIGINT from the page's stop (the fake's ``ends_on_kill``)."""
        m = self.m
        m.systemd_down, m.table_ok, self.monitor_alive = False, True, True
        m.show_failing.clear()
        for unit in m.units.values():
            unit["hangs"] = False
            unit["ends_on_kill"] = True
        m.sockets = (
            {"state": "active", "detail": "2 control sockets"}
            if "a" in m.units
            else {"state": "inactive", "detail": "no socket to the arm"}
        )
        if m.monitor is None:
            m.ours()
        self.monitor_writes(True)
        self.log.append("(the machine answers again)")

    def stop_everything(self):
        """The person presses the stop buttons the document offers, C first,
        and again while a unit says processes are left. What is still there of
        the page's, if anything."""
        m = self.m
        for _ in range(12):
            m.t += 5
            self.monitor_writes(True)
            pressed = False
            for key in ("c", "a", "b"):
                if not m.terminal(key)["can_stop"]:
                    continue
                extra = {"client_override": stack_act.CLIENT_OVERRIDE}
                try:
                    m.act.stop(
                        key,
                        m.stop_body(
                            key, self.rid("end"), **(extra if key == "c" else {})
                        ),
                    )
                    pressed = True
                except stack.Refuse as refusal:
                    self.log.append(f"stop {key} refused: {refusal.code}")
                    pressed = pressed or refusal.code == "stop_left_processes"
            m.t += 5
            self.monitor_writes(True)
            left = {
                key: (m.terminal(key)["state"], m.terminal(key)["reason"])
                for key in "abc"
                if m.terminal(key)["owned"]
                or m.terminal(key)["state"] in ("failed", "dead", "stopping")
            }
            if not left or not pressed:
                return left
        return left

    def stop_the_rest(self):
        """Everything of the page's is down: the marker and M must be
        releasable too (after the arm's sockets have run out)."""
        m = self.m
        m.sockets = {"state": "inactive", "detail": "no socket to the arm"}
        m.time_wait_until = 0.0
        m.t += 200
        m.no_manager()
        rest = {}
        if m.doc()["checks"]["can_probe_b"]:  # (a gripper stack seen earlier)
            m.controllers_out, m.controllers_code = NO_MANAGER, 124
            try:
                m.act.probe_b({"request_id": self.rid("last")})
            except stack.Refuse as refusal:
                rest["check of B"] = refusal.code
        monitor = m.terminal("m")
        running = m.monitor is not None and monitor["state"] in ("ready", "starting")
        if running and not monitor["can_stop"]:
            rest["monitor"] = [b["code"] for b in monitor["stop_blockers"]]
        marker = m.doc()["marker"]
        if marker["state"] == "present" and marker["ours"]:
            try:
                m.act.marker_op("release", m.body("marker_clear", self.rid("last")))
            except stack.Refuse as refusal:
                rest["marker"] = refusal.code
        return rest


@pytest.mark.parametrize("group", range(GROUPS))
def test_the_pages_own_buttons_can_always_bring_everything_down(group, tmp_path):
    for seed in range(group * WALKS_PER_TEST, (group + 1) * WALKS_PER_TEST):
        walk = Walk(seed, tmp_path)
        for _ in range(STEPS):
            walk.step()
        walk.machine_answers_again()
        stuck = walk.stop_everything() or walk.stop_the_rest()
        assert not stuck, f"seed {seed}: {stuck}; the last steps: " + " -> ".join(
            walk.log[-30:]
        )


def test_the_walks_reach_the_states_they_are_meant_to_cover(tmp_path):
    # A walk that never got A, B and C up would prove nothing.
    seen = set()
    for seed in range(8):
        walk = Walk(seed, tmp_path)
        for _ in range(STEPS):
            walk.step()
            doc = walk.m.doc()["terminals"]
            seen |= {(key, doc[key]["state"]) for key in "abc"}
    assert {("a", "ready"), ("b", "ready"), ("c", "ready")} <= seen, sorted(seen)
