"""Mutation check of the gates of the arm stack actions: each gate is removed
(or one safety property of a unit is turned round) in turn, the quick tests of
the stack actions run, and every mutation must make at least one of them fail.
A mutation that survives means a gate nobody tests.

    uv run --no-sync python tests/marvel0/mutate_stack_act.py [name ...]
    uv run --no-sync python tests/marvel0/mutate_stack_act.py --check   # do they all apply?

The mutations are made in a **copy** of ``levi/`` and ``tests/`` in a temporary
folder (the checkout itself is never edited, so nobody else's test run can meet
a broken gate), and the tests run there with that folder first on
``PYTHONPATH``. It starts no process but pytest."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="mutate-stack-act-"))
PKG = ROOT / "levi" / "marvel0"
# (MUTATE_TESTS names other test files, space separated: a quick run of one
# file, or the only kind of run to make while the arm controller is active)
TESTS = os.environ.get("MUTATE_TESTS", "").split() or [
    "tests/marvel0/test_marvel0_stack_act.py",
    "tests/marvel0/test_marvel0_stack_act_units.py",
    "tests/marvel0/test_marvel0_stack_act_routes.py",
    "tests/marvel0/test_marvel0_stack_act_control.py",
    "tests/marvel0/test_marvel0_stack_act_review.py",
    "tests/marvel0/test_marvel0_stack_act_residual.py",
    "tests/marvel0/test_marvel0_stack_act_walk.py",
]
COPIED = (
    "levi",
    "tests",
    "docs/MARVEL0_STACK.md",
    "src/components/marvel0/__tests__/marvel0-stack-act.samples.json",
    "pyproject.toml",
)

# (name, file, old text that occurs exactly once, new text)
MUTATIONS = [
    (
        "control file: require_enabled",
        "stack_act.py",
        '        if not self.enabled():\n            raise ActRefuse("control_disabled")',
        "        pass",
    ),
    (
        "control file: start blockers",
        "stack_act.py",
        '    def start_blockers(self, key, ctx: Ctx) -> list:\n        if not ctx.enabled:\n            return [row("control_disabled")]',
        "    def start_blockers(self, key, ctx: Ctx) -> list:\n        if False:\n            return []",
    ),
    (
        "control file: gripper blockers",
        "stack_act.py",
        '    def gripper_blockers(self, ctx: Ctx) -> list:\n        if not ctx.enabled:\n            return [row("control_disabled")]',
        "    def gripper_blockers(self, ctx: Ctx) -> list:\n        if False:\n            return []",
    ),
    (
        "attestations all ticked",
        "stack_act.py",
        '        if missing:\n            raise ActRefuse("attest_missing", keys=", ".join(missing))',
        "        if False:\n            pass",
    ),
    (
        "attestations known",
        "stack_act.py",
        '        if unknown:\n            raise ActRefuse("attest_invalid", keys=", ".join(unknown))',
        "        if False:\n            pass",
    ),
    ("fci answered", "stack_act.py", 'item["ok"] = fci_ok', 'item["ok"] = True'),
    (
        "fci probe is fresh",
        "stack_act.py",
        'fci_ok = _fresh(ctx.fci, FCI_TTL_S, now) and ctx.fci.get("state") == "open"',
        'fci_ok = ctx.fci is not None and ctx.fci.get("state") == "open"',
    ),
    (
        "marker old enough",
        "stack_act.py",
        "None if age is None else age >= wait,",
        "True,",
    ),
    (
        "layers known (B)",
        "stack_act.py",
        '"idle": "stopped",\n            "dead": "stopped",\n            "unknown": "unknown",',
        '"idle": "stopped",\n            "dead": "stopped",\n            "unknown": "stopped",',
    ),
    (
        "previous start closed",
        "stack_act.py",
        'c("record_open", st[key]["state"] not in ("dead", "failed")),',
        'c("record_open", True),',
    ),
    (
        "no hosted run (start)",
        "stack_act.py",
        'c("run_active", ctx.run_active is False),',
        'c("run_active", True),',
    ),
    (
        "arm lock free (start)",
        "stack_act.py",
        'c("arm_lock_held", None if ctx.arm_lock is None else not ctx.arm_lock),',
        'c("arm_lock_held", True),',
    ),
    (
        "health file is the control file's",
        "stack_act.py",
        'c("control_health_file", ctx.health_matches),',
        'c("control_health_file", True),',
    ),
    (
        "one operation at a time (start)",
        "stack_act.py",
        'c("operation_running", not ctx.busy),',
        'c("operation_running", True),',
    ),
    (
        "reviewed wrapper",
        "stack_act.py",
        '        if specs.profile == "real" and (\n            stack_units.wrapper_sha256(specs.wrapper) != stack_units.WRAPPER_SHA256\n        ):\n            raise ActRefuse("wrapper_changed")',
        "        pass",
    ),
    (
        "serial device present",
        "stack_act.py",
        "bool(serial) and bool(self.host.path_exists(serial)),",
        "True,",
    ),
    (
        "checks before C",
        "stack_act.py",
        '_fresh(pre, C_PREFLIGHT_TTL_S, now) and bool(pre.get("ok")),',
        "True,",
    ),
    (
        "ready arm for C",
        "stack_act.py",
        'rows.append(c("health_not_ok", self._health_ok(ctx.r["health"])))',
        'rows.append(c("health_not_ok", True))',
    ),
    (
        "C's port free",
        "stack_act.py",
        'rows.append(c("port_taken", free, port=port))',
        'rows.append(c("port_taken", True, port=port))',
    ),
    (
        "no client on the policy server",
        "stack_act.py",
        'item["ok"] = client_ok',
        'item["ok"] = True',
    ),
    (
        "stop: C before A and B",
        "stack_act.py",
        '            if c["state"] not in ("idle", "unknown", "dead") or held:\n                out.append(row("stop_c_first"))',
        "            if False:\n                pass",
    ),
    (
        "stop: no client on C",
        "stack_act.py",
        'if key == "c" and not override and self._client_on_c(ctx) is not False:',
        "if False:",
    ),
    (
        "stop: no hosted run",
        "stack_act.py",
        '        if ctx.run_active is True:\n            out.append(row("run_active"))\n        if key == "c" and not override',
        '        if key == "c" and not override',
    ),
    (
        "stop: not a foreign unit",
        "stack_act.py",
        '        elif state == "foreign_running":\n            out.append(\n                row("unit_foreign" if reason.endswith("unit_foreign") else "not_ours")\n            )',
        "        elif False:\n            pass",
    ),
    (
        "stop: the recorded command",
        "stack_units.py",
        '    return shown.get("Id") == f"{unit}.service" and (\n        stack.exec_argv(shown.get("ExecStart", "")) == list(wanted)\n    )',
        '    return shown.get("Id") == f"{unit}.service"',
    ),
    (
        "stop: M last",
        "stack_act.py",
        '        if busy or any(r["units"][k.upper()] is not False for k in stack_units.KEYS):\n            raise ActRefuse("b_running" if busy == ["b"] else "stack_running")',
        "        pass",
    ),
    (
        "product only",
        "stack.py",
        '        if r["profile"]["profile"] != "real":\n            raise Refuse("not_product")',
        "        pass",
    ),
    (
        "gripper: B ready",
        "stack_act.py",
        '        if ctx.states["b"]["state"] != "ready":\n            out.append(row("gripper_b_not_ready"))',
        "        pass",
    ),
    (
        "gripper: no hosted run",
        "stack_act.py",
        '        if ctx.run_active is not False:\n            out.append(row("run_active"))\n        if self._client_on_c(ctx) is not False:',
        "        if self._client_on_c(ctx) is not False:",
    ),
    (
        "gripper: no client on C",
        "stack_act.py",
        "        if self._client_on_c(ctx) is not False:\n            # (not in the design record",
        "        if False:\n            # (not in the design record",
    ),
    (
        "gripper: arm lock free",
        "stack_act.py",
        '        if ctx.arm_lock is None or ctx.arm_lock:\n            out.append(row("arm_lock_held"))',
        "        pass",
    ),
    (
        "gripper: hands clear",
        "stack_act.py",
        '        self._attested("gripper", body)\n',
        "        pass\n",
    ),
    (
        "gripper: one at a time (blocker)",
        "stack_act.py",
        '        if ctx.busy:\n            out.append(row("operation_running"))',
        "        pass",
    ),
    (
        "one operation at a time (begin)",
        "stack_act.py",
        '            elif running:\n                raise ActRefuse("operation_running")',
        "            elif False:\n                pass",
    ),
    (
        "sequence: never starts B",
        "stack_act.py",
        '        self._goto(seq, "await_a")\n\n    @staticmethod\n    def _a_was_stopped',
        '        self._goto(seq, "await_a")\n        self._locked("sequence_b", lambda r: self._do_start("b", {}, r, attested={"hands_clear": True}))\n\n    @staticmethod\n    def _a_was_stopped',
    ),
    (
        "marker: release only ours (sequence)",
        "stack_act.py",
        "            if not self._marker_is_ours():\n                return True  # not ours: left alone",
        "            pass",
    ),
    (
        "marker: release only ours (request)",
        "stack_act.py",
        '        if not self._marker_is_ours():\n            raise ActRefuse("marker_not_ours")',
        "        pass",
    ),
    (
        "marker: created exclusively",
        "stack_act.py",
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,\n                0o644,",
        "os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,\n                0o644,",
    ),
    (
        "fci check not while A exists",
        "stack_act.py",
        '        if a["state"] != "idle" or r["units"]["A"] is not False or not quiet:\n            raise ActRefuse("probe_arm_present")',
        "        pass",
    ),
    (
        "fci check: A must be positively idle",
        "stack_act.py",
        '        if a["state"] != "idle" or r["units"]["A"] is not False or not quiet:',
        '        if a["state"] not in ("idle", "unknown") or r["units"]["A"] is not False or not quiet:',
    ),
    # ("no unit of A" in the same condition has no mutant: A's state reads the
    # same unit, so that conjunct cannot be the only one to refuse)
    # (An unreadable socket table needs no mutant of its own: A never reads idle
    # with it, ``_terminal``, and the check needs A idle.)
    (
        "fci check: the sockets of the page's own check do not forbid another",
        "stack_act.py",
        '        quiet = sockets == "inactive" or (sockets == "active" and draining)',
        '        quiet = sockets == "inactive"',
    ),
    (
        "fci check: sockets drain only on a quiet monitor",
        "stack_act.py",
        '        if health.get("state") not in ("ok", "red"):\n            return False',
        "        if False:\n            return False",
    ),
    (
        "fci check: protected ports",
        "stack_act.py",
        '    if int(port) in PROTECTED_PORTS:\n        raise ValueError("a protected port is never connected to")',
        "    pass",
    ),
    (
        "log: only the page's own files",
        "stack_act.py",
        "        if path.parent != folder or not LOG_NAME.fullmatch(path.name):\n            return None",
        "        pass",
    ),
    (
        "unit: stops with SIGINT",
        "stack_units.py",
        '"--property=KillSignal=SIGINT",\n        "--property=SendSIGKILL=no",',
        '"--property=KillSignal=SIGKILL",\n        "--property=SendSIGKILL=no",',
    ),
    (
        "unit: never killed by systemd",
        "stack_units.py",
        '"--property=SendSIGKILL=no",',
        '"--property=SendSIGKILL=yes",',
    ),
    (
        "unit: no restart",
        "stack_units.py",
        '"--property=Restart=no",\n        "--property=StandardInput=null",',
        '"--property=Restart=on-failure",\n        "--property=StandardInput=null",',
    ),
    (
        "unit: time to stop",
        "stack_units.py",
        "STOP_TIMEOUT_S = 30",
        "STOP_TIMEOUT_S = 5",
    ),
    (
        "unit: realtime limit",
        "stack_units.py",
        '("LimitRTPRIO=99", "LimitMEMLOCK=infinity")',
        '("LimitMEMLOCK=infinity",)',
    ),
    (
        "ready: priority 98",
        "stack_units.py",
        'rf"Successful set up FIFO RT scheduling policy with priority {FIFO_PRIORITY}\\b"',
        'r"Successful set up FIFO RT scheduling policy"',
    ),
    (
        "ready: health file",
        "stack_units.py",
        "    if spec.health_flag:\n        fresh =",
        "    if False:\n        fresh =",
    ),
    (
        "ready: duplicate lines",
        "stack_units.py",
        "if item.max_count is not None and n > item.max_count and duplicate is None:",
        "if False:",
    ),
    (
        "gripper: a stall is no pass",
        "gripper.py",
        '    if parsed["stalled"]:\n        return {"ok": False, "code": "stalled", **out}',
        "    if False:\n        pass",
    ),
    (
        "control file: ports not the policy's",
        "control_site.py",
        "FORBIDDEN_PORTS = (5001, 5100, 7470, 8000)",
        "FORBIDDEN_PORTS = ()",
    ),
    (
        "control file: not writable by others",
        "control_site.py",
        '    if before.st_mode & 0o022:\n        return _invalid("writable")',
        "    pass",
    ),
    # --- the findings of the reviews of 2026-10-21 (R1 M1-M5, R2 1-4, 8, 9, 11)
    (
        "order: B needs A ready",
        "stack_act.py",
        'rows.append(c("a_not_ready", a_state == "ready", state=a_state))',
        'rows.append(c("a_not_ready", True, state=a_state))',
    ),
    (
        "order: C needs B ready",
        "stack_act.py",
        'rows.append(c("gripper_b_not_ready", st["b"]["state"] == "ready"))',
        'rows.append(c("gripper_b_not_ready", True))',
    ),
    (
        "gripper test: SIGINT, not SIGTERM, ends it",
        "stack_units.py",
        '        "timeout",\n        "-s",\n        "INT",',
        '        "timeout",\n        "-s",\n        "TERM",',
    ),
    (
        "gripper test: a cancel that was not seen may still be moving",
        "gripper.py",
        "    if killed or ended_unknown or GAVE_UP in text:",
        "    if False:",
    ),
    (
        "probe: the warning is the answer",
        "stack_units.py",
        "    if _NO_SERVICE.search(plain):",
        "    if False:",
    ),
    (
        "probe: bounded from inside",
        "stack_units.py",
        "LIST_TIMEOUT_S = 20",
        "LIST_TIMEOUT_S = 5",
    ),
    (
        "probe: a listing is rows of name, type and state",
        "stack_units.py",
        '    r"^(\\S+) +(\\S+) +(active|inactive|unconfigured|finalized)\\s*$"',
        '    r"^(\\S+)\\s*\\[[^\\]]*\\]\\s+(\\S+)\\s*$"',
    ),
    (
        "acknowledge: not while processes are left",
        "stack_act.py",
        "            if left is None or left > 0:",
        "            if False:",
    ),
    (
        "acknowledge: only SIGINT is sent again",
        "stack_act.py",
        '["systemctl", "--user", "kill", "--signal=SIGINT", unit],',
        '["systemctl", "--user", "kill", "--signal=SIGKILL", unit],',
    ),
    (
        "ready latch: the start-up rules end when it is ready",
        "stack_act.py",
        'spec, lines, ctx_in["health"], listening, latched=latched is not None',
        'spec, lines, ctx_in["health"], listening, latched=False',
    ),
    (
        "ready latch: lost evidence is unknown",
        "stack_act.py",
        'return {**base, "state": "unknown", "reason": f"{key}.health_lost"}',
        'return {**base, "state": "starting", "reason": f"{key}.starting"}',
    ),
    (
        "ready latch: a process that dies later still fails A and B",
        "stack_units.py",
        "    return tuple(item for item in spec.fail if item.late)",
        "    return ()",
    ),
    (
        "stop: a run probe that cannot answer does not block it",
        "stack_act.py",
        '        if ctx.run_active is True:\n            out.append(row("run_active"))\n        if key == "c" and not override',
        '        if ctx.run_active is not False:\n            out.append(row("run_active"))\n        if key == "c" and not override',
    ),
    (
        "stop: an unreadable socket table only warns",
        "stack_act.py",
        '                out.append(row("stop_c_unknown", "warn", reason=c["reason"]))',
        '                out.append(row("stop_c_unknown", reason=c["reason"]))',
    ),
    (
        "dead A: the log says why it vanished",
        "stack_act.py",
        '                "failure": self._vanished_failure(key, spec, rec, ctx_in),',
        '                "failure": None,',
    ),
    (
        "stop: not blocked by the other operations",
        "stack_act.py",
        '        stopping = kind.startswith("stop_")',
        "        stopping = False",
    ),
    (
        "stop: ends the checks that run",
        "stack_act.py",
        '                        other["_superseded"] = True\n                        ended.append(other)',
        "                        ended.append(other)",
    ),
    (
        "stop of B: ends the gripper test through its unit",
        "stack_act.py",
        '            if other["kind"] == "gripper":\n                self._end_gripper_test(other)',
        "            if False:\n                pass",
    ),
    (
        "a superseded check keeps nothing",
        "stack_act.py",
        '        if op["_superseded"]:\n            return  # a stop came meanwhile: this answer is for a stack that is going',
        "        if False:\n            return",
    ),
    (
        "an operation ends once",
        "stack_act.py",
        '            if op["state"] != "running":\n                return\n            op.update(',
        "            op.update(",
    ),
    (
        "a stop is told after it is done",
        "stack_act.py",
        'return STOP_NOTES["c"] if state == "idle" and held else None',
        'return STOP_NOTES["c"]',
    ),
    (
        "start: a timeout of systemd-run is not a failure",
        "stack_act.py",
        "            unconfirmed = code == 124",
        "            unconfirmed = False",
    ),
    (
        "sequence: the ticks expire",
        "stack_act.py",
        '        if step != "await_a" and now - seq["begun_at"] > window:',
        "        if False:",
    ),
    (
        "sequence: an A that cannot be vouched for is not a lost A",
        "stack_act.py",
        '        if st["a"]["state"] == "unknown":\n            return\n',
        "",
    ),
    (
        "product: the main checkout's workspace",
        "stack_act.py",
        'item["ok"] = bool(item["ok"]) and self._workspace_ok()',
        'item["ok"] = bool(item["ok"])',
    ),
    (
        "request id: the body is part of the request",
        "stack_act.py",
        "        fingerprint = record.fingerprint(action, body)",
        "        fingerprint = action",
    ),
    (
        "request id: another request is refused after a restart",
        "stack_act.py",
        '        if entry.get("fingerprint") != fingerprint:\n            raise stack.Refuse("request_id_used")',
        "        if False:\n            pass",
    ),
    (
        "request id: answers are kept across a restart",
        "stack_act.py",
        '        try:\n            folder = self._folder("replay")',
        '        return\n        try:\n            folder = self._folder("replay")',
    ),
    (
        "marker: released after a restart",
        "stack_act.py",
        '        self._resumed = True\n        left = self._record("a")',
        '        self._resumed = True\n        return\n        left = self._record("a")',
    ),
    (
        "log: a ready unit's log is cut",
        "stack_units.py",
        "        if not stat.S_ISREG(info.st_mode) or info.st_size <= cap:",
        "        if not stat.S_ISREG(info.st_mode) or True:",
    ),
    # --- the residual findings of verify-round1 (V1 low, V2 medium and low)
    (
        "b check: an old check is stale, never absent",
        "stack_act.py",
        "        if not _fresh(probe, B_PROBE_TTL_S, now):\n            # Asked once",
        "        if False:\n            # Asked once",
    ),
    (
        "b check: an old check says it is old, not that none ran",
        "stack_act.py",
        '"reason": "b.probe_stale"',
        '"reason": "b.not_probed"',
    ),
    (
        "b check: allowed while A runs",
        "stack_act.py",
        '        if r["units"]["B"] is True or ctx.states["b"]["owned"]:',
        '        if r["units"]["B"] is True or ctx.states["b"]["owned"] or ctx.states["a"]["state"] != "idle":',
    ),
    (
        "b check: not while a unit of B exists",
        "stack_act.py",
        '        if r["units"]["B"] is True or ctx.states["b"]["owned"]:',
        "        if False:",
    ),
    (
        "b check: a stop ends it",
        "stack_act.py",
        'PREEMPTED_BY_STOP = ("preflight_a", "preflight_c", "probe_b")',
        'PREEMPTED_BY_STOP = ("preflight_a", "preflight_c")',
    ),
    (
        "b check: a press settles an old check only when nothing else is unseen",
        "stack_act.py",
        '            and stack.unknown_layers(ctx.terms) == ["B"]',
        "            and True",
    ),
    (
        "b check: a press never starts a B that cannot be seen",
        "stack_act.py",
        '        if key == "b" and self._settled_by_a_press(key, ctx):\n            # Nothing is started',
        "        if False:\n            # Nothing is started",
    ),
    (
        "b check: a press with an old check can be made",
        "stack_act.py",
        '                or (key == "b" and st["reason"] in B_ASK_AGAIN)  # (a press checks)',
        "                or False",
    ),
    (
        "b check: a guided start keeps it fresh",
        "stack_act.py",
        '        if seq["next"] == "b" and not ctx.busy and self._b_probe_due(ctx):',
        "        if False:",
    ),
    (
        "b check: a guided start looks again sooner at what it saw",
        "stack_act.py",
        '        wait = B_PROBE_REFRESH_S if probe.get("state") == "absent" else B_PROBE_RETRY_S',
        "        wait = B_PROBE_REFRESH_S",
    ),
    (
        "b check: a refused optional check does not end the sequence",
        "stack_act.py",
        '            self._locked("sequence_probe_b", self._do_probe_b)\n        except stack.Refuse:\n            pass',
        '            self._locked("sequence_probe_b", self._do_probe_b)\n        except stack.Refuse:\n            raise',
    ),
    (
        "drain: the arm's sockets must be known",
        "stack_act.py",
        '        if key != "a" or sockets != "active":',
        '        if key != "a":',
    ),
    (
        "drain: a manager that answers after the check is not its socket",
        "stack_act.py",
        '        return hardware is None and recent((self._fci or {}).get("at"))',
        '        return recent((self._fci or {}).get("at"))',
    ),
    (
        "drain: an active hardware is never leftovers",
        "stack_act.py",
        "        if hardware is True:\n            return False",
        "        if False:\n            return False",
    ),
    (
        "stop: a C of ours that cannot be vouched for is still C first",
        "stack_act.py",
        '            held = c["owned"] and c["unit_state"] in (',
        '            held = False and c["unit_state"] in (',
    ),
    (
        "gripper test: ended by any other status without a result",
        "gripper.py",
        "    ended_unknown = no_result and code not in (0, 124)",
        "    ended_unknown = False",
    ),
    (
        "stop of B: waits for the gripper test's unit to be gone",
        "stack_act.py",
        "            if asked and shown is not None and not loaded:\n                return True",
        "            if asked:\n                return True",
    ),
    (
        "stop of B: waits for a gripper unit not yet created",
        "stack_act.py",
        "            if loaded and not asked:",
        "            if not asked:",
    ),
    (
        "stop of B: a gripper unit that will not go does not hold it back",
        "stack_act.py",
        "        for _ in range(max(1, int(GRIPPER_END_WAIT_S / GRIPPER_END_POLL_S))):",
        "        for _ in range(max(1, int(GRIPPER_END_WAIT_S / GRIPPER_END_POLL_S)) * 400):",
    ),
    (
        "acknowledge: only a positive main pid counts",
        "stack_act.py",
        "        return pid if pid else None",
        "        return pid",
    ),
    (
        "acknowledge: a unit with no control group has nothing left",
        "stack_act.py",
        "            return 0\n        pids = self.host.cgroup_procs(group) if group else None",
        "            return None\n        pids = self.host.cgroup_procs(group) if group else None",
    ),
    (
        "acknowledge: a question about processes that failed is not an answer",
        "stack_act.py",
        "            if left is None or left > 0:",
        "            if left is not None and left > 0:",
    ),
    (
        "sequence: a stop of A while it is awaited ends it",
        "stack_act.py",
        '        if step == "await_a" and self._a_was_stopped(ctx):',
        "        if False:",
    ),
    (
        "sequence: the press of a stop ends it before the unit is gone",
        "stack_act.py",
        '        return ctx.states["a"]["state"] in ("idle", "stopping") or phase in (\n            "stopping",\n            "stopped",\n            "acknowledged",\n        )',
        '        return ctx.states["a"]["state"] in ("idle", "stopping")',
    ),
    (
        "marker: released after a stop the page did not live to finish",
        "stack_act.py",
        '        if left and left.get("phase") == "stopping" and self._a_unit_gone(left):',
        "        if False:",
    ),
    (
        "marker: a stop still in progress is not closed by a restart",
        "stack_act.py",
        '        if left and left.get("phase") == "stopping" and self._a_unit_gone(left):',
        '        if left and left.get("phase") == "stopping":',
    ),
    (
        "fci check: the unit of A must be known absent",
        "stack_act.py",
        '        if a["state"] != "idle" or r["units"]["A"] is not False or not quiet:',
        '        if a["state"] != "idle" or not quiet:',
    ),
    (
        "ready latch: only the run it was read for",
        "stack_act.py",
        '                    and current.get("started_at") == rec.get("started_at")\n',
        "",
    ),
    (
        "drain: the sockets of the page's own stop are its leftovers",
        "stack_act.py",
        '        if recent((rec or {}).get("stopped_at")):\n            return True\n',
        "        if False:\n            return True\n",
    ),
    (
        "drain: the stack reads A as stopped while its sockets are leftovers",
        "stack_act.py",
        '        if states["a"]["reason"] == "a.sockets_draining":',
        "        if False:",
    ),
    (
        "drain: the monitor's stop blockers read A as stopped too",
        "stack_act.py",
        'r["profile"], r["m_unit"], ctx.terms, r["script_ok"]',
        'r["profile"], r["m_unit"], r["terms"], r["script_ok"]',
    ),
    (
        "drain: the monitor's stop reads A as stopped too",
        "stack_act.py",
        'return self.stack._stop({**r, "terms": ctx.terms})  # (A\'s draining read)',
        "return self.stack._stop(r)",
    ),
    (
        "b check: the document says when it can be made",
        "stack_act.py",
        '            "can_probe_b": ctx.enabled\n            and not ctx.busy\n            and self._probe_b_refusal(ctx.r, ctx) is None,',
        '            "can_probe_b": False,',
    ),
    (
        "walk: an acknowledge that is refused for ever is a stuck unit",
        "stack_act.py",
        "            if left is None or left > 0:",
        "            if True:",
    ),
    (
        "drain: no controller manager is null in the real health file, not false",
        "stack_act.py",
        '        hardware = health.get("hardware_active")\n        if hardware is True:\n            return False\n',
        '        hardware = health.get("hardware_active")\n        if hardware is not False:\n            return False\n',
    ),
]


def copy_sources():
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".state", "node_modules")
    for name in COPIED:
        source, target = SOURCE / name, ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=skip)
        else:
            shutil.copy2(source, target)


def run_tests() -> int:
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
            *TESTS,
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
    return done.returncode


def check_applicable() -> int:
    """Does every mutation apply, to exactly one place? (No test runs.)"""
    bad = 0
    for name, file, old, _ in MUTATIONS:
        count = (SOURCE / "levi" / "marvel0" / file).read_text().count(old)
        if count != 1:
            bad += 1
            print(f"NOT APPLICABLE ({count}x)  {name}")
    print(f"{len(MUTATIONS) - bad} of {len(MUTATIONS)} mutations apply once")
    return 1 if bad else 0


def main(argv) -> int:
    if argv == ["--check"]:
        shutil.rmtree(ROOT, ignore_errors=True)
        return check_applicable()
    chosen = [m for m in MUTATIONS if not argv or m[0] in argv]
    copy_sources()
    survived = []
    try:
        print(f"baseline: pytest exit {run_tests()} (must be 0)", flush=True)
        for name, file, old, new in chosen:
            path = PKG / file
            text = path.read_text()
            if text.count(old) != 1:
                print(
                    f"SKIPPED  {name}: the text occurs {text.count(old)} times",
                    flush=True,
                )
                survived.append(name + " (not applied)")
                continue
            path.write_text(text.replace(old, new))
            try:
                code = run_tests()
            finally:
                path.write_text(text)
            print(f"{'killed  ' if code else 'SURVIVED'} {name}", flush=True)
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
