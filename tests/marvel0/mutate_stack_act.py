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
    "tests/marvel0/test_marvel0_stack_act_stopall.py",
    "tests/marvel0/test_marvel0_stack_act_chain.py",
    "tests/marvel0/test_marvel0_stack_act_race.py",
    "tests/marvel0/test_marvel0_stack_act_stopping.py",
    "tests/marvel0/test_marvel0_stack_act_fast.py",
    "tests/marvel0/test_marvel0_stack_act_fast_review.py",
    "tests/marvel0/test_marvel0_stack_act_heavy.py",
    "tests/marvel0/test_marvel0_stack_act_timeline.py",
]
CHAIN = "tests/marvel0/test_marvel0_stack_act_chain.py"
RACE = "tests/marvel0/test_marvel0_stack_act_race.py"
STOPPING = "tests/marvel0/test_marvel0_stack_act_stopping.py"
ROUTES = "tests/marvel0/test_marvel0_stack_act_routes.py"
FAST = "tests/marvel0/test_marvel0_stack_act_fast.py"
FAST_REVIEW = "tests/marvel0/test_marvel0_stack_act_fast_review.py"
HEAVY = "tests/marvel0/test_marvel0_stack_act_heavy.py"
TIMELINE = "tests/marvel0/test_marvel0_stack_act_timeline.py"
FAST_PROCESSES = "tests/marvel0/test_marvel0_stack_act_fast_processes.py"
UNITS = "stack_units.py"
COPIED = (
    "levi",
    "tests",
    "docs/MARVEL0_STACK.md",
    "src/components/marvel0/__tests__/marvel0-stack-act.samples.json",
    "pyproject.toml",
)

# (name, file, old text that occurs exactly once, new text[, test file]): a
# mutation with a test file is run against that one file only (the new gates of
# the guided start through B and C each have the one file that guards them).
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
        '        missing = [k for k in ATTEST_NEEDED[key] if given.get(k) is not True]\n        if missing:\n            raise ActRefuse("attest_missing", keys=", ".join(missing))',
        "        missing = []",
    ),
    (
        "attestations known",
        "stack_act.py",
        '        if unknown:\n            raise ActRefuse("attest_invalid", keys=", ".join(unknown))\n        missing = [k for k in ATTEST_NEEDED[key]',
        "        missing = [k for k in ATTEST_NEEDED[key]",
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
        'c("operation_running", not (ctx.busy_main if key == "a" else ctx.busy)),',
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
        '            elif any((o["kind"] in SIDE_OPS) == (kind in SIDE_OPS) for o in running):\n                raise ActRefuse("operation_running")',
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
        '        if window is not None and now - seq["begun_at"] > window:',
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
        "        except stack.Refuse:\n            pass\n\n    # -- the chain: B and C by the guided start",
        "        except stack.Refuse:\n            raise\n\n    # -- the chain: B and C by the guided start",
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
    # --- "stop whole stack" (``Act.sequence_stop``): each gate of the guided stop
    (
        "stop all: a hosted run known to be active refuses it",
        "stack_act.py",
        '    "record_corrupt",\n    "run_active",\n    "client_connected",\n)',
        '    "record_corrupt",\n    "client_connected",\n)',
    ),
    (
        "stop all: a client connected to C refuses it",
        "stack_act.py",
        '    "run_active",\n    "client_connected",\n)\n',
        '    "run_active",\n)\n',
    ),
    (
        "stop all: systemd that cannot be read refuses it",
        "stack_act.py",
        '    "gate.product",\n    "systemd_unreadable",\n    "record_corrupt",',
        '    "gate.product",\n    "record_corrupt",',
    ),
    (
        "stop all: a record that cannot be read refuses it",
        "stack_act.py",
        '    "systemd_unreadable",\n    "record_corrupt",\n    "run_active",',
        '    "systemd_unreadable",\n    "run_active",',
    ),
    (
        "stop all: only the product acts",
        "stack_act.py",
        'STOP_ALL_CODES = (\n    "gate.product",\n',
        "STOP_ALL_CODES = (\n",
    ),
    (
        "stop all: the sequence offers no override of a client",
        "stack_act.py",
        'STOP_ALL_RENAMED = {"client_connected": "stop_all_client_connected"}',
        "STOP_ALL_RENAMED = {}",
    ),
    (
        "stop all: the order is C, A, B",
        "stack_act.py",
        'STOP_ORDER = ("c", "a", "b")',
        'STOP_ORDER = ("a", "c", "b")',
    ),
    (
        "stop all: it ends a guided start that would start what it stops",
        "stack_act.py",
        '            if self._seq.get("kind") == "start":\n                self._seq_end("cancelled", "superseded")',
        "            pass",
    ),
    (
        "stop all: nothing to stop is answered, not started",
        "stack_act.py",
        '            if "nothing_to_stop" in codes:\n                return {"done": "already_stopped"}',
        "            pass",
    ),
    (
        "stop all: the ticks of a start are not a stop's (unknown keys refused)",
        "stack_act.py",
        '            raise ActRefuse("attest_invalid", keys=", ".join(unknown))\n        ctx = self._context(r, holding=True)\n        rows = self.stop_all_blockers(ctx)',
        "            pass\n        ctx = self._context(r, holding=True)\n        rows = self.stop_all_blockers(ctx)",
    ),
    (
        "stop all: the phrase is typed",
        "stack_act.py",
        '            body, "sequence_stop", lambda r: self._seq_begin_stop(body, r)\n        )',
        '            body,\n            "sequence_stop",\n            lambda r: self._seq_begin_stop(body, r),\n            phrase=False,\n        )',
    ),
    (
        "stop all: a stop that failed ends the sequence",
        "stack_act.py",
        '                if op_state == "failed":\n                    return self._seq_end("failed", "stop_failed", seq)',
        '                if False:\n                    return self._seq_end("failed", "stop_failed", seq)',
    ),
    (
        "stop all: the next step waits for the unit to be gone",
        "stack_act.py",
        '                if self._unit_gone(key, ctx):\n                    return self._stop_step_done(seq, "done")\n                return\n',
        '                return self._stop_step_done(seq, "done")\n',
    ),
    (
        "stop all: after a stop only the unit is asked, not the evidence that lingers",
        "stack_act.py",
        '            if seq["_issued"]:\n                # Something was sent',
        "            if False:\n                # Something was sent",
    ),
    (
        "stop all: a part that cannot be read is not skipped",
        "stack_act.py",
        '            if refusing:\n                code = refusing[0]["code"]\n                return self._seq_end("failed", STOP_ALL_RENAMED.get(code, code), seq)',
        '            if False:\n                code = refusing[0]["code"]\n                return self._seq_end("failed", STOP_ALL_RENAMED.get(code, code), seq)',
    ),
    (
        "stop all: a part with nothing of ours is skipped",
        "stack_act.py",
        '                if self._stop_step_done(seq, "skipped"):',
        '                if self._stop_step_done(seq, "done"):',
    ),
    (
        "stop all: a stop that is on its way is waited for, not repeated",
        "stack_act.py",
        '            if st["state"] == "stopping":\n                return  # (a stop is on its way: this one, or a person\'s)',
        "            if False:\n                return  # (a stop is on its way: this one, or a person's)",
    ),
    (
        "stop all: a step that never ends fails",
        "stack_act.py",
        '            if self.now() - seq["step_at"] > limit:\n                return self._seq_end("failed", "step_timeout", seq)',
        '            if False:\n                return self._seq_end("failed", "step_timeout", seq)',
    ),
    (
        "stop all: a busy machine-wide lock is tried again, not a failure",
        "stack_act.py",
        '            if exc.code not in TRANSIENT:\n                self._seq_end("failed", STOP_ALL_RENAMED.get(exc.code, exc.code), seq)',
        '            if True:\n                self._seq_end("failed", STOP_ALL_RENAMED.get(exc.code, exc.code), seq)',
    ),
    (
        "stop all: a step ends its own sequence, not the one that replaced it",
        "stack_act.py",
        "            target = self._seq if seq is None else seq",
        "            target = self._seq",
    ),
    (
        "stop all: a unit that failed or vanished is something to stop",
        "stack_act.py",
        '        return bool(st["owned"]) or st["state"] in ("failed", "dead")',
        '        return bool(st["owned"])',
    ),
    (
        "stop all: the button is off when a blocker refuses",
        "stack_act.py",
        '            "can_stop_all": not any(b["level"] == "required" for b in stop_all),',
        '            "can_stop_all": True,',
    ),
    (
        "stop all: the document says what is nothing to stop",
        "stack_act.py",
        '            out.append(row("nothing_to_stop"))',
        "            pass",
    ),
    (
        "stop all: a request while a guided stop runs shows sequence_active",
        "stack_act.py",
        '                out.append(row("sequence_active"))\n        for key in STOP_ORDER:',
        "                pass\n        for key in STOP_ORDER:",
    ),
    (
        "stop all: the sequence view says its kind",
        "stack_act.py",
        '        seq.setdefault("kind", None)  # (idle: neither a start nor a stop)',
        "        pass",
    ),
    (
        "stop all: a start sequence says it is one",
        "stack_act.py",
        '                "state": "running",\n                "kind": "start",\n',
        '                "state": "running",\n',
    ),
    (
        "stop all: the bookkeeping of a stop is not shown",
        "stack_act.py",
        '            if k not in hidden and not k.startswith("_")',
        "            if k not in hidden",
    ),
    (
        "stop all: the page's phrase for it",
        "stack.py",
        '    "sequence_stop": "stop whole stack",\n',
        '    "sequence_stop": "stop",\n',
    ),
    (
        "race: a step looks at the sequence again inside the lock",
        "stack_act.py",
        '                if self._seq is not seq or seq["state"] not in live:\n                    return None',
        "                if False:\n                    return None",
        RACE,
    ),
    (
        "race: sequence_monitor looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_monitor" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: sequence_probe looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_probe" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: sequence_marker looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_marker" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: sequence_arm looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_arm" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: sequence_probe_b looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_probe_b" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: sequence_b looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_b" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: sequence_preflight_c looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_preflight_c" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: sequence_c looks again inside the lock",
        "stack_act.py",
        'if self._seq is not seq or seq["state"] not in live:',
        'if action != "sequence_c" and (self._seq is not seq or seq["state"] not in live):',
        RACE,
    ),
    (
        "race: a refusal that says its work was started still runs it",
        "stack_act.py",
        "        except ActRefuse as refusal:\n            if refusal.keep_work:\n                self._flush()\n            raise\n        self._flush()",
        "        except ActRefuse as refusal:\n            if False:\n                self._flush()\n            raise\n        self._flush()",
        CHAIN,
    ),
    (
        "chain: b_probe_started takes the chain back to the check",
        "stack_act.py",
        '            if refusal.code == "b_probe_started":',
        "            if False:",
        CHAIN,
    ),
    (
        "chain: the step that goes back to the check is pending again",
        "stack_act.py",
        '                return self._goto(seq, back, how="pending")',
        "                return self._goto(seq, back)",
        CHAIN,
    ),
    (
        "stopping: no start while a guided stop is live",
        "stack_act.py",
        '        if self._stopping():\n            out.append(row("sequence_stopping"))\n        settled =',
        "        settled =",
        STOPPING,
    ),
    (
        "stopping: no gripper test while a guided stop is live",
        "stack_act.py",
        '        if self._stopping():\n            out.append(row("sequence_stopping"))\n        if not self._is_real(ctx.r):\n            out.append(row("gate.product"))\n        if ctx.states["b"]',
        '        if not self._is_real(ctx.r):\n            out.append(row("gate.product"))\n        if ctx.states["b"]',
        STOPPING,
    ),
    (
        "stopping: no marker while a guided stop is live",
        "stack_act.py",
        '        if self._stopping():\n            raise ActRefuse("sequence_stopping")',
        '        if False:\n            raise ActRefuse("sequence_stopping")',
        STOPPING,
    ),
    (
        "stopping: the document says the marker is held back",
        "stack_act.py",
        '                "touch_blockers": [row("sequence_stopping")]\n                if self._stopping()\n                else [],',
        '                "touch_blockers": [],',
        STOPPING,
    ),
    (
        "stopping: only a stop holds the starts back, not a start",
        "stack_act.py",
        '            return self._seq.get("kind") == "stop" and self._seq_live(self._seq)',
        "            return self._seq_live(self._seq)",
        STOPPING,
    ),
    (
        "stopping: a c that cannot be told ends the stop at c",
        "stack_act.py",
        '                if key == "c" and st["state"] == "unknown":',
        "                if False:",
        STOPPING,
    ),
    (
        "stopping: the steps after the end were not reached",
        "stack_act.py",
        '                    elif item["state"] == "pending" and state != "done":\n                        self._item_state(item, "not_reached")',
        '                    elif item["state"] == "pending" and state != "done":\n                        pass',
        STOPPING,
    ),
    (
        "stopping: a stop asked for and not carried leaves a notice",
        "stack_act.py",
        '                rec.get("phase") == "stopping"\n                and st[key]["owned"]',
        '                st[key]["owned"]',
        STOPPING,
    ),
    (
        "stopping: the notice is for a unit that is still there",
        "stack_act.py",
        '                and st[key]["owned"]\n                and st[key]["state"] != "stopping"',
        '                and st[key]["state"] != "stopping"',
        STOPPING,
    ),
    (
        "stopping: no notice while systemd is stopping the unit",
        "stack_act.py",
        '                and st[key]["state"] != "stopping"\n                and not self._stop_running(key)',
        "                and not self._stop_running(key)",
        STOPPING,
    ),
    (
        "stopping: no notice while this process carries the stop",
        "stack_act.py",
        '                and st[key]["state"] != "stopping"\n                and not self._stop_running(key)',
        '                and st[key]["state"] != "stopping"',
        STOPPING,
    ),
    (
        "chain: every stage's confirmations are asked at the request",
        "stack_act.py",
        '        if missing:\n            raise ActRefuse("attest_missing", keys=", ".join(missing))\n        return {stage:',
        "        if False:\n            pass\n        return {stage:",
        CHAIN,
    ),
    (
        "chain: an unknown confirmation is refused at the request",
        "stack_act.py",
        '        if unknown:\n            raise ActRefuse("attest_invalid", keys=", ".join(unknown))\n        missing = []',
        "        if False:\n            pass\n        missing = []",
        CHAIN,
    ),
    (
        "chain: the through option is checked",
        "stack_act.py",
        "        if through not in SEQUENCE_THROUGH:",
        "        if False:",
        CHAIN,
    ),
    (
        "chain: a pause option must be a bool",
        "stack_act.py",
        "            if not isinstance(value, bool):",
        "            if False:",
        CHAIN,
    ),
    (
        "chain: a pause asks for no confirmation of the stage it waits before",
        "stack_act.py",
        '                    and not paused\n                    and st[key]["state"] not in ("ready", "starting")',
        '                    and st[key]["state"] not in ("ready", "starting")',
        CHAIN,
    ),
    (
        "chain: a part that is up asks for no confirmation",
        "stack_act.py",
        '                    and not paused\n                    and st[key]["state"] not in ("ready", "starting")',
        "                    and not paused",
        CHAIN,
    ),
    (
        "chain: a guided start through A reports the pauses it has",
        "stack_act.py",
        '                "pauses": {"b": pause_b or not chain, "c": pause_c or not chain},',
        '                "pauses": {"b": pause_b, "c": pause_c},',
        CHAIN,
    ),
    (
        "chain: an A that is up is not started again",
        "stack_act.py",
        '            a_up = chain and st["a"]["state"] in ("ready", "starting")',
        "            a_up = False",
        CHAIN,
    ),
    (
        "chain: A's gates are not asked again when A is up",
        "stack_act.py",
        "            if not a_up:\n                hard = [",
        "            if True:\n                hard = [",
        CHAIN,
    ),
    (
        "chain: everything up is already started",
        "stack_act.py",
        '            if chain and all(st[k]["state"] == "ready" for k in "abc"):\n                return {"done": "already_started"}',
        "            if False:\n                return {}",
        CHAIN,
    ),
    (
        "chain: A's checks are skipped when A is up",
        "stack_act.py",
        '            if seq.get("_a_up"):',
        "            if False:",
        CHAIN,
    ),
    (
        "chain: the pause before B is kept",
        "stack_act.py",
        '        if seq["through"] == "c" and not seq["pauses"]["b"]:',
        '        if seq["through"] == "c":',
        CHAIN,
    ),
    (
        "chain: the pause before C is kept",
        "stack_act.py",
        '        if seq["through"] == "c" and not seq["pauses"]["c"]:',
        '        if seq["through"] == "c":',
        CHAIN,
    ),
    (
        "chain: a part that is not ready any more ends the chain",
        "stack_act.py",
        '            if state != "ready":\n                self._seq_end("failed", f"{key}_lost", seq)\n                return False',
        "            if False:\n                return False",
        CHAIN,
    ),
    (
        "chain: a part that cannot be told is waited for",
        "stack_act.py",
        '            if state == "unknown":\n                return False\n            if state != "ready":',
        '            if False:\n                return False\n            if state != "ready":',
        CHAIN,
    ),
    (
        "chain: the check of B is skipped when B is up",
        "stack_act.py",
        '        if ctx.states["b"]["state"] in ("ready", "starting", "failed", "dead"):\n            # B is up',
        "        if False:\n            # B is up",
        CHAIN,
    ),
    (
        "chain: the check of B is not made when it is fresh",
        "stack_act.py",
        '        if not self._b_probe_due(ctx):\n            return self._goto(seq, "b")',
        '        if False:\n            return self._goto(seq, "b")',
        CHAIN,
    ),
    (
        "chain: a check of B that got no answer ends the chain",
        "stack_act.py",
        '            if state == "failed":\n                return self._seq_end("failed", "b_not_seen", seq)\n            return self._goto(seq, "b")',
        '            if False:\n                return self._seq_end("failed", "b_not_seen", seq)\n            return self._goto(seq, "b")',
        CHAIN,
    ),
    (
        "chain: B that is starting is waited for",
        "stack_act.py",
        '        if b == "starting":\n            return\n        if seq.get("_started_b"):',
        '        if False:\n            return\n        if seq.get("_started_b"):',
        CHAIN,
    ),
    (
        "chain: B that failed or vanished ends the chain",
        "stack_act.py",
        '        if b in ("failed", "dead"):\n            return self._seq_end("failed", f"b_{b}", seq)',
        '        if False:\n            return self._seq_end("failed", f"b_{b}", seq)',
        CHAIN,
    ),
    (
        "chain: a B that was started and is gone ends the chain",
        "stack_act.py",
        '            return self._seq_end("failed", "b_lost", seq)\n        self._chain_start(seq, "b", back="b_probe")',
        '            return\n        self._chain_start(seq, "b", back="b_probe")',
        CHAIN,
    ),
    (
        "chain: the checks before C are skipped when C is up",
        "stack_act.py",
        '        if ctx.states["c"]["state"] in ("ready", "starting", "failed", "dead"):\n            return self._goto(seq, "c", how="skipped")',
        '        if False:\n            return self._goto(seq, "c", how="skipped")',
        CHAIN,
    ),
    (
        "chain: the checks before C are asked again once",
        "stack_act.py",
        '                and listing in ("absent", "unknown")',
        "                and False",
        CHAIN,
    ),
    (
        "chain: only a listing with no answer is asked again",
        "stack_act.py",
        '                and listing in ("absent", "unknown")',
        "                and True",
        CHAIN,
    ),
    (
        "chain: the checks before C are asked again only once",
        "stack_act.py",
        '                and not seq.get("_pre_retried")',
        "                and True",
        CHAIN,
    ),
    (
        "chain: the second ask is a new check",
        "stack_act.py",
        '                seq["_pre_op"] = None\n                return',
        "                return",
        CHAIN,
    ),
    (
        "chain: C that is starting is waited for",
        "stack_act.py",
        '        if c == "starting":\n            return\n        if seq.get("_started_c"):',
        '        if False:\n            return\n        if seq.get("_started_c"):',
        CHAIN,
    ),
    (
        "chain: C that failed or vanished ends the chain",
        "stack_act.py",
        '        if c in ("failed", "dead"):\n            return self._seq_end("failed", f"c_{c}", seq)',
        '        if False:\n            return self._seq_end("failed", f"c_{c}", seq)',
        CHAIN,
    ),
    (
        "chain: the chain is done when C is ready",
        "stack_act.py",
        '                if seq["state"] == "running":\n                    self._after_c(seq)',
        "                if False:\n                    self._after_c(seq)",
        CHAIN,
    ),
    (
        "chain: what the person started while the sequence waited is passed",
        "stack_act.py",
        '                self._skip_pending(seq, ("b_probe", "b"))',
        "                pass",
        CHAIN,
    ),
    (
        "chain: B's window",
        "stack_act.py",
        "            return window + SEQUENCE_A_READY_S + SEQUENCE_B_READY_S\n        if step in CHAIN_STEPS[2:]:",
        "            return None\n        if step in CHAIN_STEPS[2:]:",
        CHAIN,
    ),
    (
        "chain: C's window",
        "stack_act.py",
        "                + SEQUENCE_C_CHECK_S\n                + SEQUENCE_C_READY_S\n            )",
        "                + 100000\n            )",
        CHAIN,
    ),
    (
        "chain: B's window is later than A's",
        "stack_act.py",
        "            return window + SEQUENCE_A_READY_S + SEQUENCE_B_READY_S\n        if step in CHAIN_STEPS[2:]:",
        "            return window\n        if step in CHAIN_STEPS[2:]:",
        CHAIN,
    ),
    (
        "chain: the step that was pending is not pending after a skip",
        "stack_act.py",
        '        for name in names:\n            self._mark(seq, name, "skipped", only=("pending",))',
        "        for name in names:\n            pass",
        CHAIN,
    ),
    (
        "chain: the body is typed",
        "stack_act_api.py",
        '    through: Literal["a", "c"] = "a"',
        '    through: str = "a"',
        ROUTES,
    ),
    (
        "chain: nothing to do is a 200",
        "stack_act_api.py",
        "        else accepted",
        "        else 202",
        ROUTES,
    ),
    # --- the fast start: heavy jobs, the check of the graph, the order, the ticker ---
    (
        "heavy: a terminal record ends the job",
        "stack_act.py",
        '            ended.add(unit)\n        elif event in ("launching", "start") and (',
        '            pass\n        elif event in ("launching", "start") and (',
        HEAVY,
    ),
    (
        "heavy: the end counts only when the scope was confirmed empty",
        "stack_act.py",
        'if event in HEAVY_TERMINAL and rec.get("emptied", True) is True:',
        "if event in HEAVY_TERMINAL:",
        HEAVY,
    ),
    (
        "heavy: only a launch makes a job",
        "stack_act.py",
        '        elif event in ("launching", "start") and (',
        '        elif event in ("launching", "start", "queued") and (',
        HEAVY,
    ),
    (
        "heavy: a scope with no process is not a job",
        "stack_act.py",
        '        return "running" if tasks > 0 else "empty"',
        '        return "running"',
        HEAVY,
    ),
    (
        "heavy: a scope that is gone is not a job",
        "stack_act.py",
        '    if shown.get("LoadState") == "not-found":\n        return "gone"',
        '    if shown.get("LoadState") == "not-found":\n        return "running"',
        HEAVY,
    ),
    (
        "heavy: events older than six hours are not looked at",
        "stack_act.py",
        "        if at is not None and t_now - at > HEAVY_LOG_MAX_AGE_S:\n            continue",
        "        if False:\n            continue",
        HEAVY,
    ),
    (
        "heavy: only the gate's own units",
        "stack_act.py",
        "        if not isinstance(unit, str) or not HEAVY_UNIT.fullmatch(unit):\n            continue",
        "        if not isinstance(unit, str):\n            continue",
        HEAVY,
    ),
    (
        "heavy: a log that is not there is unknown",
        "stack_act.py",
        '    except FileNotFoundError:\n        return [], "log_missing"',
        "    except FileNotFoundError:\n        return [], None",
        HEAVY,
    ),
    (
        "heavy: a log that cannot be read is unknown",
        "stack_act.py",
        '    except OSError:\n        return [], "log_unreadable"',
        "    except OSError:\n        return [], None",
        HEAVY,
    ),
    (
        "heavy: a systemd that does not answer is unknown",
        "stack_act.py",
        "    if code != 0:\n        return None\n    blocks = {}",
        "    if code != 0:\n        return {}\n    blocks = {}",
        HEAVY,
    ),
    (
        "heavy: a unit with no block of its own is unknown",
        "stack_act.py",
        '        scope: _scope_verdict(blocks[scope]) if scope in blocks else "unknown"',
        '        scope: _scope_verdict(blocks[scope]) if scope in blocks else "gone"',
        HEAVY,
    ),
    (
        "heavy: a scope without a task count is unknown",
        "stack_act.py",
        '        if tasks is None:\n            return "unknown"\n        return "running" if tasks > 0',
        '        if tasks is None:\n            return "empty"\n        return "running" if tasks > 0',
        HEAVY,
    ),
    (
        "heavy: a job that is seen decides",
        "stack_act.py",
        '    if running:\n        return {\n            "state": "running",',
        '    if running and not doubtful:\n        return {\n            "state": "running",',
        HEAVY,
    ),
    (
        "heavy: what cannot be told is unknown",
        "stack_act.py",
        '    if doubtful:\n        return {\n            "state": "unknown",',
        '    if False:\n        return {\n            "state": "unknown",',
        HEAVY,
    ),
    (
        "heavy: the scopes systemd lists are a second source",
        "stack_act.py",
        '        for name, shown in blocks.items():\n            unit = name.removesuffix(".scope")',
        '        for name, shown in {}.items():\n            unit = name.removesuffix(".scope")',
        HEAVY,
    ),
    (
        "heavy: a listed scope that is not the gate's naming is not a job",
        "stack_act.py",
        "            if name == unit or not HEAVY_UNIT.fullmatch(unit):\n                continue",
        "            if name == unit:\n                continue",
        HEAVY,
    ),
    (
        "heavy: a glob that was not expanded proves nothing",
        "stack_act.py",
        '    if blocks is None or any("*" in name for name in blocks):',
        "    if blocks is None:",
        HEAVY,
    ),
    (
        "heavy: a scope that is not active and still has processes is a job",
        "stack_act.py",
        '        return "running" if tasks else "gone"',
        '        return "gone"',
        HEAVY,
    ),
    (
        "heavy: too many candidates are unknown",
        "stack_act.py",
        "    if len(candidates) > HEAVY_MAX_UNITS:",
        "    if False:",
        HEAVY,
    ),
    (
        "heavy: a job without a scope cannot be told",
        "stack_act.py",
        '        if rec.get("mode", "scope") != "scope":',
        "        if False:",
        HEAVY,
    ),
    (
        "heavy: a tail that begins on a line keeps that line",
        "stack_act.py",
        "        begin = max(0, start - 1)",
        "        begin = start",
        HEAVY,
    ),
    (
        "heavy: a log is not followed through a link",
        "stack_act.py",
        "    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)",
        "    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)",
        HEAVY,
    ),
    (
        "heavy: a failure to ask is unknown",
        "stack_act.py",
        '            return {\n                "state": "unknown",\n                "count": 0,\n                "kinds": [],\n                "empty": 0,\n                "why": "heavy_failed",',
        '            return {\n                "state": "clear",\n                "count": 0,\n                "kinds": [],\n                "empty": 0,\n                "why": "heavy_failed",',
        FAST,
    ),
    (
        "heavy: unknown falls back to the fixed wait of old",
        "stack_act.py",
        '        if heavy is not None and heavy["state"] == "unknown":\n            wait = max(configured, control_site.LEGACY_MARKER_WAIT_S)',
        "        if False:\n            wait = max(configured, control_site.LEGACY_MARKER_WAIT_S)",
        HEAVY,
    ),
    (
        "heavy: the end of the wait is not known while a job runs",
        "stack_act.py",
        '        running = heavy is not None and heavy["state"] == "running"',
        "        running = False",
        HEAVY,
    ),
    (
        "heavy: a running job is a blocker of A's start",
        "stack_act.py",
        '            if heavy and heavy["state"] == "running":\n                rows.append(',
        "            if False:\n                rows.append(",
        HEAVY,
    ),
    (
        "heavy: asked only before A has a unit",
        "stack_act.py",
        'if r["marker"]["state"] == "present" and r["units"]["A"] is not True',
        'if r["marker"]["state"] == "present"',
        HEAVY,
    ),
    (
        "wait: a job that does not end ends the start",
        "stack_act.py",
        "            if waited > HEAVY_WAIT_CAP_S:",
        "            if False:",
        TIMELINE,
    ),
    (
        "wait: the cap names how many jobs",
        "stack_act.py",
        '                    params={"count": heavy["count"]},',
        "                    params=None,",
        TIMELINE,
    ),
    (
        "wait: two clean looks in a row",
        "stack_act.py",
        '            or seq.get("_clean_looks", 0) >= HEAVY_CLEAN_LOOKS',
        "            or True",
        FAST,
    ),
    (
        "wait: a look with a running job starts the count again",
        "stack_act.py",
        'seq["_clean_looks"] = seq.get("_clean_looks", 0) + 1 if clean else 0',
        'seq["_clean_looks"] = seq.get("_clean_looks", 0) + 1',
        FAST,
    ),
    (
        "wait: sockets quiet before A",
        "stack_act.py",
        '            and ctx.r["sockets"]["state"] == "inactive"\n        ):\n            self._goto(seq, "arm")',
        '            and True\n        ):\n            self._goto(seq, "arm")',
        FAST,
    ),
    (
        "order: the marker comes right after M",
        "stack_act.py",
        'A_STEPS = ("monitor", "marker", "probe", "wait", "arm", "await_a")',
        'A_STEPS = ("monitor", "probe", "marker", "wait", "arm", "await_a")',
        FAST,
    ),
    (
        "order: the check of B begins with the marker",
        "stack_act.py",
        '                with self._seq_lock:\n                    seq["_marker_made"] = True  # (this start made it: it gives it back)\n                self._start_b_probe(seq, ctx)',
        '                with self._seq_lock:\n                    seq["_marker_made"] = True  # (this start made it: it gives it back)',
        FAST,
    ),
    (
        "order: a marker that is there is no reason to skip the check of B",
        "stack_act.py",
        '        elif state == "present" and ctx.marker["ours"]:\n            self._start_b_probe(seq, ctx)\n            self._goto(seq, "probe")',
        '        elif state == "present" and ctx.marker["ours"]:\n            self._goto(seq, "probe")',
        FAST,
    ),
    (
        "order: no check of B when B is known and fresh",
        "stack_act.py",
        '        needed = ctx.states["b"]["state"] == "unknown" or (',
        "        needed = True or (",
        FAST,
    ),
    (
        "order: an old answer is refreshed during A's start-up, for a start that goes on to B",
        "stack_act.py",
        '            seq.get("through") == "c" and self._b_probe_due(ctx)',
        "            False and self._b_probe_due(ctx)",
        FAST,
    ),
    (
        "order: a start that stops at A makes no early check for B's sake",
        "stack_act.py",
        '            seq.get("through") == "c" and self._b_probe_due(ctx)',
        "            self._b_probe_due(ctx)",
        FAST,
    ),
    (
        "order: a check of B that got no answer ends the start",
        "stack_act.py",
        '            if state == "running":\n                return\n            return self._seq_end("failed", "b_not_seen", seq)\n        self._goto(seq, "wait")',
        '            if state == "running":\n                return\n            return\n        self._goto(seq, "wait")',
        FAST,
    ),
    (
        "order: the probe step waits for B to be known",
        "stack_act.py",
        '        if ctx.states["b"]["state"] == "unknown":\n            op = seq.get("_early_op")',
        '        if False:\n            op = seq.get("_early_op")',
        FAST,
    ),
    (
        "order: the FCI check beside the check of B, not behind it",
        "stack_act.py",
        '            if ctx.busy_main:\n                return\n            self._seq_locked(\n                seq,\n                "sequence_probe",',
        '            if ctx.busy:\n                return\n            self._seq_locked(\n                seq,\n                "sequence_probe",',
        FAST,
    ),
    (
        "review: a one-shot check's unit is unique per call, not per second",
        "stack_act.py",
        '        unit = (\n            f"{stack_units.ONESHOT_PREFIX}{name}-{int(self.now())}-"\n            f"{uuid.uuid4().hex[:6]}"\n        )',
        '        unit = f"{stack_units.ONESHOT_PREFIX}{name}-{int(self.now())}"',
        FAST_REVIEW,
    ),
    (
        "tick: the pause is the slow one once A has a unit",
        "stack_act.py",
        "        return SEQUENCE_TICK_S if self._a_unit is False else SEQUENCE_TICK_ARM_S",
        "        return SEQUENCE_TICK_S",
        TIMELINE,
    ),
    (
        "tick: the look says whether A has a unit",
        "stack_act.py",
        '                self._a_unit = ctx.r["units"]["A"]\n',
        "",
        TIMELINE,
    ),
    (
        "b confirm: one unconfirmed answer is settled by a press, never started on",
        "stack_act.py",
        "            or Act._b_absence_unconfirmed(ctx)\n        )",
        "            or False\n        )",
        FAST_REVIEW,
    ),
    (
        "b confirm: the unconfirmed answer shows its own warning",
        "stack_act.py",
        "            if self._b_absence_unconfirmed(ctx):\n                out.append(",
        "            if False:\n                out.append(",
        FAST_REVIEW,
    ),
    (
        "b confirm: the check A's start waits for is one answer (marker step)",
        "stack_act.py",
        '                "sequence_early_probe_b",\n                lambda r: self._do_probe_b(r, confirm=False),\n                keep="_early_op",\n            )\n',
        '                "sequence_early_probe_b",\n                lambda r: self._do_probe_b(r),\n                keep="_early_op",\n            )\n',
        FAST_REVIEW,
    ),
    (
        "b confirm: the check A's start waits for is one answer (probe step)",
        "stack_act.py",
        '                    "sequence_early_probe_b",\n                    lambda r: self._do_probe_b(r, confirm=False),',
        '                    "sequence_early_probe_b",\n                    lambda r: self._do_probe_b(r),',
        FAST,
    ),
    (
        "b confirm: an answer older than its validity is not a first answer",
        "stack_act.py",
        "                    and _fresh(prior, B_PROBE_TTL_S, began)\n",
        "",
        FAST_REVIEW,
    ),
    (
        "b confirm: a second answer that is present is final",
        "stack_act.py",
        '                        state = graph if graph == "present" else None',
        "                        state = None",
        FAST_REVIEW,
    ),
    (
        "b confirm: a second answer that proves nothing goes to the manager",
        "stack_act.py",
        '                        state = graph if graph == "present" else None',
        '                        state = graph if graph == "present" else "absent"',
        FAST_REVIEW,
    ),
    (
        "b confirm: the manager's own word needs no second answer",
        "stack_act.py",
        '        if by == "probe" and state == "absent":\n            confirmed = True',
        '        if by == "probe" and state == "absent":\n            confirmed = False',
        FAST_REVIEW,
    ),
    (
        "b confirm: B's own stop leaves a confirmed absence",
        "stack_act.py",
        '                "by": "stop",\n                "confirmed": True,',
        '                "by": "stop",\n                "confirmed": False,',
        FAST_REVIEW,
    ),
    (
        "b confirm: the second answer is asked while A starts up",
        "stack_act.py",
        "        else:\n            self._confirm_b_while_a_starts(seq, ctx)",
        "        else:\n            pass",
        FAST_REVIEW,
    ),
    (
        "b confirm: only a chain that goes on to B asks while A starts up",
        "stack_act.py",
        '            seq["pauses"]["b"]  # (a start that stops at A pauses before B)\n            or seq.get("_probe_op") is not None',
        '            False\n            or seq.get("_probe_op") is not None',
        FAST_REVIEW,
    ),
    (
        "b confirm: an unconfirmed answer is due again at once",
        "stack_act.py",
        '        if probe.get("state") == "absent" and not probe.get("confirmed"):\n            return True',
        "        if False:\n            return True",
        FAST_REVIEW,
    ),
    (
        "b confirm: a check a person asks for is two answers of the graph",
        "stack_act.py",
        "                if confirm and not confirmed:",
        "                if False:",
        FAST_REVIEW,
    ),
    (
        "marker: a start that ended before A gives the marker back",
        "stack_act.py",
        '            seq["_marker_given_back"] = True\n        for _ in range(3):',
        '            seq["_marker_given_back"] = True\n            return\n        for _ in range(3):',
        FAST,
    ),
    (
        "marker: only a marker this start made",
        "stack_act.py",
        '                or not seq.get("_marker_made")\n',
        "",
        FAST,
    ),
    (
        "marker: never with A started",
        "stack_act.py",
        '                or seq.get("_arm_started")\n',
        "",
        FAST,
    ),
    (
        "marker: once",
        "stack_act.py",
        '                or seq.get("_marker_given_back")\n',
        "",
        FAST,
    ),
    (
        "marker: given back at the next look after a cancel",
        "stack_act.py",
        "            if over:\n                self._release_unused_marker(seq)  # (ended by a cancel, say)\n                return shown",
        "            if over:\n                return shown",
        FAST,
    ),
    (
        "marker: given back at the end of a pass",
        "stack_act.py",
        "            self._release_unused_marker(seq)\n            with self._seq_lock:\n                return self._seq_public(seq)",
        "            with self._seq_lock:\n                return self._seq_public(seq)",
        FAST,
    ),
    (
        "marker: this start made it",
        "stack_act.py",
        '                    seq["_marker_made"] = True  # (this start made it: it gives it back)',
        "                    pass",
        FAST,
    ),
    (
        "marker: A's start is remembered",
        "stack_act.py",
        '                seq["_arm_started"] = True',
        "                pass",
        FAST,
    ),
    (
        "side check: A's start does not wait for the check of B",
        "stack_act.py",
        '(ctx.busy_main if key == "a" else ctx.busy)',
        "ctx.busy",
        FAST,
    ),
    (
        "side check: B and C do wait for it",
        "stack_act.py",
        '(ctx.busy_main if key == "a" else ctx.busy)',
        "ctx.busy_main",
        FAST,
    ),
    (
        "side check: the main busy ignores it",
        "stack_act.py",
        '                o["state"] == "running" and not (main_only and o["kind"] in SIDE_OPS)',
        '                o["state"] == "running"',
        FAST,
    ),
    (
        "side check: it may begin beside the FCI check",
        "stack_act.py",
        '            elif any((o["kind"] in SIDE_OPS) == (kind in SIDE_OPS) for o in running):',
        "            elif running:",
        FAST,
    ),
    (
        "graph: the FCI check and the check of B run together",
        "stack_act.py",
        "        if len(calls) == 1 or not runner:\n            return [call() for call in calls]",
        "        if True:\n            return [call() for call in calls]",
        FAST,
    ),
    (
        "graph: the graph first, the manager only when it proves nothing",
        "stack_act.py",
        '            if graph in ("present", "absent"):\n                state = graph',
        '            if True:\n                state = graph',
        FAST,
    ),
    (
        "graph: the graph is asked at all",
        "stack_act.py",
        "        if specs.nodes_cmd:\n            fast = self._graph_check(specs, op)",
        "        if False:\n            fast = self._graph_check(specs, op)",
        FAST,
    ),
    (
        "graph: a stopped check drops its answer",
        "stack_act.py",
        '        if op["_superseded"]:\n            return None\n        return stack_units.parse_nodes(text, code)',
        "        return stack_units.parse_nodes(text, code)",
        FAST,
    ),
    (
        "graph: the monitor's node is the proof of absence",
        "stack_units.py",
        "    elif code == 0 and canary:",
        "    elif code == 0:",
        FAST,
    ),
    (
        "graph: a command that did not end well proves nothing",
        "stack_units.py",
        "    elif code == 0 and canary:",
        "    elif canary:",
        FAST,
    ),
    (
        "graph: a gripper node is present",
        "stack_units.py",
        '    if gripper:\n        state = "present"',
        '    if False:\n        state = "present"',
        FAST,
    ),
    (
        "graph: the namespace is the gripper's, not a name that starts like it",
        "stack_units.py",
        'n == GRIPPER_NAMESPACE or n.startswith(GRIPPER_NAMESPACE + "/")',
        "n.startswith(GRIPPER_NAMESPACE)",
        FAST,
    ),
    (
        "graph: no daemon, bounded",
        "stack_units.py",
        '("ros2", "node", "list", "--no-daemon", "--spin-time", str(NODES_SPIN_S)),',
        '("ros2", "node", "list", "--spin-time", str(NODES_SPIN_S)),',
        FAST,
    ),
    (
        "graph: the command is bounded by timeout",
        "stack_units.py",
        '    nodes = _bounded(\n        NODES_TIMEOUT_S,\n        NODES_KILL_AFTER_S,\n        ("ros2", "node", "list", "--no-daemon", "--spin-time", str(NODES_SPIN_S)),\n    )',
        '    nodes = ("ros2", "node", "list", "--no-daemon", "--spin-time", str(NODES_SPIN_S))',
        FAST,
    ),
    (
        "fci: closed with a reset, so that no socket is left",
        "stack_act.py",
        '        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))',
        "        pass",
        FAST_PROCESSES,
    ),
    (
        "checks before C: the listing and the frame check run together",
        "stack_act.py",
        "        (_, listed_text), (ee_code, ee_text) = self._parallel(",
        "        (_, listed_text), (ee_code, ee_text) = (lambda *calls: [c() for c in calls])(",
        FAST,
    ),
    (
        "ticker: looks again at once after a change",
        "stack_act.py",
        "                if self._progress != before and burst < SEQUENCE_BURST:",
        "                if False:",
        FAST,
    ),
    (
        "ticker: not for ever",
        "stack_act.py",
        "                if self._progress != before and burst < SEQUENCE_BURST:",
        "                if self._progress != before:",
        FAST,
    ),
    (
        "ticker: slow while the sequence waits for the person",
        "stack_act.py",
        '        if seq["state"] != "running":\n            return SEQUENCE_TICK_WAITING_S',
        "        if False:\n            return SEQUENCE_TICK_WAITING_S",
        FAST,
    ),
    (
        "ticker: a quarter of a second",
        "stack_act.py",
        "SEQUENCE_TICK_S = 0.25",
        "SEQUENCE_TICK_S = 2.0",
        FAST,
    ),
    (
        "ticker: an operation that ends wakes it",
        "stack_act.py",
        "                code=code,\n            )\n        self._poke()",
        "                code=code,\n            )",
        TIMELINE,
    ),
    (
        "ticker: a step that moves wakes it",
        "stack_act.py",
        '            self._mark(seq, step, "running")\n        self._poke()',
        '            self._mark(seq, step, "running")',
        TIMELINE,
    ),
    (
        "ticker: the wake sets the event",
        "stack_act.py",
        "        self._progress += 1\n        self._wake.set()",
        "        self._progress += 1",
        FAST,
    ),
    (
        "ticker: the pause ends at a change",
        "stack_act.py",
        "        self._wake.wait(seconds)",
        "        time.sleep(seconds)",
        FAST,
    ),
    (
        "page: a change drops the cached read",
        "stack_act_api.py",
        "        stack_obj.act.on_change = stack_api.invalidate",
        "        pass",
        FAST,
    ),
    (
        "page: the change is announced",
        "stack_act.py",
        "        notify = self.on_change\n        if notify is not None:",
        "        notify = None\n        if notify is not None:",
        FAST,
    ),
    (
        "timing: a step adds up what it took",
        "stack_act.py",
        '                spent = (item.get("took_s") or 0.0) + max(0.0, now - began)',
        "                spent = max(0.0, now - began)",
        FAST,
    ),
    (
        "timing: the sequence adds up its steps",
        "stack_act.py",
        '                spent = [i["took_s"] for i in target.get("steps", ()) if i["took_s"]]\n                target["took_s"] = round(sum(spent), 2)',
        '                target["took_s"] = None',
        FAST,
    ),
    (
        "control file: the minimum age may be a few seconds",
        "control_site.py",
        "MARKER_WAIT_FLOOR_S = 2.0",
        "MARKER_WAIT_FLOOR_S = 45.0",
        FAST,
    ),
    (
        "control file: the minimum age defaults to a few seconds",
        "control_site.py",
        "DEFAULT_MARKER_WAIT_S = 3.0",
        "DEFAULT_MARKER_WAIT_S = 60.0",
        FAST,
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


def run_tests(tests=None) -> int:
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
            os.environ.get("MUTATE_CPUS", "16-19"),
            sys.executable,
            "-m",
            "pytest",
            *(tests or TESTS),
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
    for name, file, old, _new, *_tests in MUTATIONS:
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
        for name, file, old, new, *own in chosen:
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
                code = run_tests(own)
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
