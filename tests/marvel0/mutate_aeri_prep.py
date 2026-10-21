"""Mutation check of the gates of the AERI preparation card: each gate is removed
(or one safety property turned round) in turn, the one test file that is meant
to guard it runs, and every mutation must make at least one test fail. A
mutation that survives means a gate nobody tests.

    uv run --no-sync python tests/marvel0/mutate_aeri_prep.py [--check] [--no-baseline] [name ...]

``--check`` only verifies that every mutation applies (its old text occurs
exactly once) and runs nothing; ``--no-baseline`` skips the run of each test file
on the unbroken copy. The mutations are made in a **copy** of ``levi/`` and
``tests/`` in a temporary folder (the checkout itself is never edited, so nobody
else's test run can meet a broken gate), and the tests run there with that
folder first on ``PYTHONPATH``. It starts no process but pytest, one test file at
a time, under ``nice -n 19 taskset -c 16-19``. It refuses to run while a robot
process is alive (the passive ``ps`` check of setup.md): a sweep is many test
runs, and the arm's control loop is not to be disturbed by it;
``--even-if-robot-runs`` lifts that (one file at a time stays)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="mutate-aeri-prep-"))
PKG = ROOT / "levi" / "marvel0"
COPIED = (
    "levi",
    "tests",
    "src/components/marvel0/__tests__/marvel0-aeri.samples.json",
    "src/components/marvel0/autonomy-logic.ts",
    "docs/SUPERVISION.md",
    "pyproject.toml",
)
ROBOT = re.compile(
    r"ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)

PREP = "tests/marvel0/test_marvel0_aeri_prep.py"
READY = "tests/marvel0/test_marvel0_aeri_ready.py"
LEFT = "tests/marvel0/test_marvel0_aeri_leftovers.py"
ROUTE = "tests/marvel0/test_marvel0_aeri_route.py"
SITE = "tests/marvel0/test_marvel0_aeri_site.py"

# (name, file of levi/marvel0, old text that occurs exactly once, new text, test file)
MUTATIONS = [
    # --- the code pin ----------------------------------------------------------------
    (
        "pin: any commit will do",
        "aeri_prep.py",
        "and head.startswith(pin)",
        "and True",
        PREP,
    ),
    (
        "pin: a dirty tree is fine",
        "aeri_prep.py",
        'if not pin_matches(aeri.pin_sha, state["head"]) or state["dirty"]:',
        'if not pin_matches(aeri.pin_sha, state["head"]):',
        PREP,
    ),
    (
        "pin: git that cannot be read is fine",
        "aeri_prep.py",
        'if state["head"] is None or state["dirty"] is None:',
        "if False:",
        PREP,
    ),
    (
        "pin: a folder inside another repository answers with its HEAD",
        "aeri_prep.py",
        "if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != (",
        "if top.returncode != 0 or False and (",
        PREP,
    ),
    (
        "pin: the digits need not be lower case hex of 8 or more",
        "aeri_prep.py",
        'and re.fullmatch(r"[0-9a-f]{8,40}", pin) is not None',
        "and True",
        PREP,
    ),
    # --- templates and profile --------------------------------------------------------
    (
        "templates: a changed template is used",
        "aeri_prep.py",
        'if state["ok"] is not True:',
        "if False:",
        PREP,
    ),
    (
        "templates: the sums are not compared",
        "aeri_prep.py",
        "if wanted.get(name) != digests[name]:",
        "if False:",
        PREP,
    ),
    (
        "profile: an unknown profile is hosted",
        "aeri_prep.py",
        'if profile["known"] is not True:',
        "if False:",
        PREP,
    ),
    (
        "profile: startup homing for a version without it",
        "aeri_prep.py",
        'if start_home and profile.get("start_home") is not True:',
        "if False:",
        PREP,
    ),
    (
        "profile: a table that cannot be read counts as no profiles at all",
        "aeri_prep.py",
        "    if not isinstance(table, Mapping):\n        return None",
        "    if not isinstance(table, Mapping):\n        return {}",
        PREP,
    ),
    # --- the form -----------------------------------------------------------------------
    (
        "form: a task with edge spaces or no letter",
        "aeri_prep.py",
        'if task != task.strip() or not re.search(r"[a-z]", task):',
        "if False:",
        PREP,
    ),
    (
        "form: rounds and steps out of range",
        "aeri_prep.py",
        'if not bounds["min"] <= value <= bounds["max"]:',
        "if False:",
        PREP,
    ),
    (
        "form: a bool is a count",
        "aeri_prep.py",
        "if type(value) is not int:",
        "if False:",
        PREP,
    ),
    (
        "form: a start_home that is not a bool",
        "aeri_prep.py",
        "if type(start_home) is not bool:",
        "if False:",
        PREP,
    ),
    (
        "form: local mode without a model",
        "aeri_prep.py",
        'if mode == "local" and not aeri.local_model:',
        "if False:",
        PREP,
    ),
    (
        "form: a gripper time out of range (and NaN)",
        "aeri_prep.py",
        'GRIPPER_CONFIRM_S["min"] <= gripper <= GRIPPER_CONFIRM_S["max"]',
        "True",
        PREP,
    ),
    # --- names ------------------------------------------------------------------------------
    (
        "names: the rollout's AERI runs are not looked at",
        "aeri_prep.py",
        '("rollout", Path(aeri.rollout_root) / ".aeri" / "runs"),\n        ("registry", Path(registry)),\n    ):\n        for name in _listing',
        '("registry", Path(registry)),\n    ):\n        for name in _listing',
        PREP,
    ),
    (
        "names: the AERI registry is not looked at",
        "aeri_prep.py",
        '("registry", Path(registry)),\n    ):\n        for name in _listing',
        "):\n        for name in _listing",
        PREP,
    ),
    (
        "names: the page's own record is not looked at",
        "aeri_prep.py",
        "    for name in record_names:\n        if pattern.fullmatch(name):",
        "    for name in ():\n        if pattern.fullmatch(name):",
        PREP,
    ),
    (
        "names: an existing folder is reused",
        "aeri_prep.py",
        "os.mkdir(candidate, 0o755)",
        "os.makedirs(candidate, exist_ok=True)",
        PREP,
    ),
    # --- the check ---------------------------------------------------------------------------
    (
        "check: a failed check leaves its folder",
        "aeri_prep.py",
        "        _remove_made(run_dir, runs_root)\n        raise",
        "        raise",
        PREP,
    ),
    (
        "check: the check inherits our environment",
        "aeri_prep.py",
        '    env = {\n        "PYTHONPATH": aeri.worktree,',
        '    env = {\n        **os.environ,\n        "PYTHONPATH": aeri.worktree,',
        PREP,
    ),
    (
        "check: hardware_verified is not read",
        "aeri_prep.py",
        'if value.get("hardware_verified") is not False:',
        "if False:",
        PREP,
    ),
    (
        "check: the strategy is not read",
        "aeri_prep.py",
        'if value.get("reset_strategy") != "subtask_loop":',
        "if False:",
        PREP,
    ),
    (
        "check: a non-zero exit is fine",
        "aeri_prep.py",
        '    if done.returncode != 0:\n        raise Refuse(\n            "check_failed", reason="returncode"',
        '    if False:\n        raise Refuse(\n            "check_failed", reason="returncode"',
        PREP,
    ),
    (
        "check: the digests are not checked",
        "aeri_prep.py",
        'if not isinstance(text, str) or not re.fullmatch(r"[0-9a-f]{64}", text):\n            raise Refuse("check_failed", reason="digest"',
        'if False:\n            raise Refuse("check_failed", reason="digest"',
        PREP,
    ),
    # --- the prepared run, read again ------------------------------------------------------
    (
        "prepared: changed files are not noticed",
        "aeri_prep.py",
        "if _sha256_file(run_dir / file) != recorded[key]:",
        "if False:",
        PREP,
    ),
    (
        "prepared: moved code is not noticed",
        "aeri_prep.py",
        "if head is not None and not (isinstance(seen, str) and head.startswith(seen)):",
        "if False:",
        PREP,
    ),
    (
        "prepared: another profile is not noticed",
        "aeri_prep.py",
        'if run_json.get("profile") != aeri.profile:',
        "if False:",
        PREP,
    ),
    (
        "prepared: any name is read",
        "aeri_prep.py",
        "if not isinstance(name, str) or not NAME.fullmatch(name):\n        return None\n    run_dir",
        "if not isinstance(name, str):\n        return None\n    run_dir",
        PREP,
    ),
    (
        "prepared: a linked folder is read",
        "aeri_prep.py",
        "if not stat.S_ISDIR(os.lstat(run_dir).st_mode):",
        "if False:",
        PREP,
    ),
    (
        "cleanup: any folder is removed",
        "aeri_prep.py",
        "        and run_dir.parent == runs_root\n        and NAME.fullmatch(run_dir.name)",
        "        and True",
        PREP,
    ),
    (
        "state: the page's folder may lie in source data",
        "aeri_prep.py",
        "if target.is_relative_to(Path(root).expanduser().resolve()):",
        "if False:",
        PREP,
    ),
    # --- the rows ------------------------------------------------------------------------------
    (
        "ready: unknown counts as ready",
        "aeri_ready.py",
        'return bool(rows_) and all(r["level"] in ("ok", "warn") for r in rows_)',
        'return bool(rows_) and all(r["level"] in ("ok", "warn", "unknown") for r in rows_)',
        READY,
    ),
    (
        "ready: a card without rows is ready",
        "aeri_ready.py",
        'return bool(rows_) and all(r["level"] in ("ok", "warn") for r in rows_)',
        'return all(r["level"] in ("ok", "warn") for r in rows_)',
        READY,
    ),
    (
        "rows: the arm stack need not be up",
        "aeri_ready.py",
        'if a != "running" or c != "running":',
        "if False:",
        READY,
    ),
    (
        "rows: a terminal client is not an evaluation run",
        "aeri_ready.py",
        "if e_hosted is True or e_session is True:",
        "if e_hosted is True:",
        READY,
    ),
    (
        "rows: any free GPU memory will do",
        "aeri_ready.py",
        'row("gpu_free", free >= need,',
        'row("gpu_free", free > 0,',
        READY,
    ),
    (
        "rows: any memory will do",
        "aeri_ready.py",
        "None if mem is None else mem >= MEM_NEED_MIB,",
        "None if mem is None else mem > 0,",
        READY,
    ),
    (
        "rows: port 8110 is not looked at",
        "aeri_ready.py",
        "PORTS = (8000, 8100, 8110)",
        "PORTS = (8000, 8100)",
        READY,
    ),
    (
        "rows: the drift comparison is not read",
        "aeri_ready.py",
        '    if ok is True:\n        return row("prompts_checked", True)',
        '    if True:\n        return row("prompts_checked", True)',
        READY,
    ),
    (
        "rows: the monitor need not be green",
        "aeri_ready.py",
        'None if hstate is None or hstate == "unknown" else hstate == "ok",',
        'None if hstate is None or hstate == "unknown" else True,',
        READY,
    ),
    (
        "digest: the numbers are part of it",
        "aeri_ready.py",
        '"rows": [[r["code"], r["level"]] for r in rows_],',
        '"rows": [[r["code"], r["level"], r["params"]] for r in rows_],',
        READY,
    ),
    (
        "digest: the levels are not part of it",
        "aeri_ready.py",
        '"rows": [[r["code"], r["level"]] for r in rows_],',
        '"rows": [[r["code"]] for r in rows_],',
        READY,
    ),
    (
        "digest: the prepared run is not part of it",
        "aeri_ready.py",
        '"prepared": None\n        if not prepared\n        else {',
        '"prepared": None\n        if True\n        else {',
        READY,
    ),
    (
        "cache: a slow read is kept as long as a quick one",
        "aeri_ready.py",
        "            self._cache = (began, facts)",
        "            self._cache = (mono(), facts)",
        READY,
    ),
    (
        "cache: a clock that goes back serves the cache",
        "aeri_ready.py",
        "0 <= mono() - cached[0] < CACHE_S",
        "mono() - cached[0] < CACHE_S",
        READY,
    ),
    (
        "cache: a fresh snapshot may come from the cache",
        "aeri_ready.py",
        "if not fresh and cached is not None",
        "if cached is not None",
        READY,
    ),
    (
        "live: the stop is offered while the arm is in use",
        "aeri_ready.py",
        'k: v for k, v in _obstacles(facts).items() if k != "gpu_held"',
        'k: False for k, v in _obstacles(facts).items() if k != "gpu_held"',
        READY,
    ),
    (
        "leftovers: the stop is offered with no leftover",
        "aeri_ready.py",
        '    elif not units:\n        reason = _why("nothing_to_stop")',
        '    elif False:\n        reason = _why("nothing_to_stop")',
        READY,
    ),
    # --- what is stopped ---------------------------------------------------------------------
    (
        "leftovers: a policy server started by hand is a leftover",
        "aeri_leftovers.py",
        "and not stem.startswith(MANUAL_PREFIX)",
        "and True",
        LEFT,
    ),
    (
        "leftovers: a scope is a leftover",
        "aeri_leftovers.py",
        'if not name.endswith(".service"):\n            continue\n        stem',
        "if False:\n            continue\n        stem",
        LEFT,
    ),
    (
        "leftovers: an unreadable obstacle does not withhold the stop",
        "aeri_leftovers.py",
        "    for code, value, _ in checks:\n        if value is None:",
        "    for code, value, _ in checks:\n        if False:",
        LEFT,
    ),
    (
        "leftovers: the order of the reasons is reversed",
        "aeri_leftovers.py",
        "    for code, value, text in checks:\n        if value is True:",
        "    for code, value, text in reversed(checks):\n        if value is True:",
        LEFT,
    ),
    (
        "live: the command gets our whole environment",
        "aeri_leftovers.py",
        "env.update({k: environ[k] for k in LIVE_ENV_KEYS if environ.get(k)})",
        "env.update(environ)",
        LEFT,
    ),
    (
        "live: the unit is killed, not stopped",
        "aeri_leftovers.py",
        'argv = ["systemctl", "--user", "stop", unit]',
        'argv = ["systemctl", "--user", "kill", unit]',
        LEFT,
    ),
    (
        "live: any unit the site file names is stopped",
        "aeri_leftovers.py",
        "        or LIVE_UNIT.fullmatch(unit) is None\n",
        "",
        LEFT,
    ),
    (
        "live: the product's own unit is stopped",
        "aeri_leftovers.py",
        "        or LIVE_UNIT_FORBIDDEN in unit\n",
        "",
        LEFT,
    ),
    (
        "live: the stop waits less than the unit's own stop job",
        "aeri_leftovers.py",
        "LIVE_STOP_TIMEOUT_S = 210.0",
        "LIVE_STOP_TIMEOUT_S = 120.0",
        LEFT,
    ),
    # --- the routes ----------------------------------------------------------------------------
    (
        "api: the same id for another request is accepted",
        "aeri_api.py",
        '        if entry.get("fingerprint") != fingerprint:\n            raise Refuse("request_id_used")',
        '        if False:\n            raise Refuse("request_id_used")',
        ROUTE,
    ),
    (
        "api: a second write waits instead of being refused",
        "aeri_api.py",
        "if not self._guard.acquire(blocking=False):",
        "if not self._guard.acquire(blocking=True):",
        ROUTE,
    ),
    (
        "api: the live stop does not hold the run lock",
        "aeri_api.py",
        '"live_stop", directory, request_id, body, work, run_lock=True',
        '"live_stop", directory, request_id, body, work, run_lock=False',
        ROUTE,
    ),
    (
        "api: the leftover stop does not hold the run lock",
        "aeri_api.py",
        '"leftovers_stop", directory, request_id, body, work, run_lock=True',
        '"leftovers_stop", directory, request_id, body, work, run_lock=False',
        ROUTE,
    ),
    (
        "api: preparing takes the run lock",
        "aeri_api.py",
        '"prepare", directory, request_id, body, work, run_lock=False',
        '"prepare", directory, request_id, body, work, run_lock=True',
        ROUTE,
    ),
    (
        "api: leftovers stop while the robot side is in use",
        "aeri_api.py",
        "            self._obstacles(facts, with_gpu=True)",
        "            pass",
        ROUTE,
    ),
    (
        "api: the live service stops while the robot side is in use",
        "aeri_api.py",
        "            self._obstacles(facts, with_gpu=False)",
        "            pass",
        ROUTE,
    ),
    (
        "api: a live stop that left the service running is a success",
        "aeri_api.py",
        'if after["live"] == "running" or after["live"] is None:',
        "if False:",
        ROUTE,
    ),
    (
        "api: the phrase is not checked",
        "aeri_api.py",
        'if body.get("confirm") != phrase:',
        "if False:",
        ROUTE,
    ),
    (
        "api: the switch is not checked",
        "aeri_api.py",
        'if facts["control"]["enabled"] is not True:',
        "if False:",
        ROUTE,
    ),
    (
        "api: any instance may write",
        "aeri_api.py",
        'if facts["product"] is not True:',
        "if False:",
        ROUTE,
    ),
    (
        "api: prepare is open to an agent",
        "aeri_api.py",
        '@router.post("/aeri/prepare", dependencies=[Depends(person_only)])',
        '@router.post("/aeri/prepare")',
        ROUTE,
    ),
    (
        "api: the live stop is open to an agent",
        "aeri_api.py",
        '@router.post("/aeri/live/stop", dependencies=[Depends(person_only)])',
        '@router.post("/aeri/live/stop")',
        ROUTE,
    ),
    (
        "api: the leftover stop is open to an agent",
        "aeri_api.py",
        '@router.post("/aeri/leftovers/stop", dependencies=[Depends(person_only)])',
        '@router.post("/aeri/leftovers/stop")',
        ROUTE,
    ),
    (
        "api: every listed unit is stopped, the hand-started one too",
        "aeri_api.py",
        '[u["name"] for u in units], self.host.prep.run',
        '[u["name"] for u in facts["units"]], self.host.prep.run',
        ROUTE,
    ),
    (
        "api: a failed prepare is remembered as an answer",
        "aeri_api.py",
        "            except Refuse as exc:\n                record.drop_request(directory, request_id)",
        "            except Refuse as exc:\n                pass",
        ROUTE,
    ),
    # --- the sections of the site and control files ---------------------------------------
    (
        "site: a path with characters a shell would read",
        "site.py",
        "if not _AERI_SAFE_PATH.fullmatch(value):",
        "if False:",
        SITE,
    ),
    (
        "site: a pin of one digit",
        "site.py",
        '_AERI_PIN = re.compile(r"[0-9a-f]{8,40}")',
        '_AERI_PIN = re.compile(r"[0-9a-f]{1,40}")',
        SITE,
    ),
    (
        "site: an unknown key in the section is accepted",
        "site.py",
        '            errors.append((f"{AERI_SECTION}.{key}", "unknown_key"))',
        "            pass",
        SITE,
    ),
    (
        "site: a section with errors is used",
        "site.py",
        '    if errors:\n        return None, tuple(errors)\n    if kept["tools_root"] is None:',
        '    if False:\n        return None, tuple(errors)\n    if kept["tools_root"] is None:',
        SITE,
    ),
    (
        "control: a malformed section switches AERI on",
        "control_site.py",
        "    if errors:\n        return AeriControl(), tuple(errors)\n    return AeriControl(**out), ()",
        "    if errors:\n        return AeriControl(**out), tuple(errors)\n    return AeriControl(**out), ()",
        SITE,
    ),
    (
        "control: a section that is not a table is read as on",
        "control_site.py",
        '        return AeriControl(), (("aeri", "type"),)',
        '        return AeriControl(enabled=True), (("aeri", "type"),)',
        SITE,
    ),
    (
        "control: a missing section is on",
        "control_site.py",
        '    if "aeri" not in data:\n        return AeriControl(), ()',
        '    if "aeri" not in data:\n        return AeriControl(enabled=True), ()',
        SITE,
    ),
    (
        "control: a malformed section is shown as valid",
        "control_site.py",
        '    if errors:\n        return {\n            "state": "invalid",\n            "code": "aeri.invalid",',
        '    if False:\n        return {\n            "state": "invalid",\n            "code": "aeri.invalid",',
        SITE,
    ),
]


def applies() -> list:
    """The mutations whose old text is not found exactly once."""
    bad = []
    for name, file, old, new, _ in MUTATIONS:
        text = (SOURCE / "levi" / "marvel0" / file).read_text()
        if text.count(old) != 1 or old == new:
            bad.append((name, text.count(old)))
    return bad


def copy_sources():
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".state", "node_modules")
    for name in COPIED:
        source, target = SOURCE / name, ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=skip)
        else:
            shutil.copy2(source, target)


def run_tests(test_file: str) -> tuple:
    """``(exit code, the first FAILED or ERROR line)`` of one test file."""
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
            "16-19",
            sys.executable,
            "-m",
            "pytest",
            test_file,
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
    lines = [x for x in done.stdout.splitlines() if x.startswith(("FAILED", "ERROR"))]
    return done.returncode, (lines[0][:150] if lines else "")


def robot_processes() -> bool:
    """The passive check: any robot-related process in the process list."""
    listing = subprocess.run(
        ["ps", "-eo", "args"], capture_output=True, text=True, check=False
    ).stdout
    return any(ROBOT.search(line) for line in listing.splitlines())


def main(argv) -> int:
    flags = [a for a in argv if a.startswith("--")]
    names = [a for a in argv if not a.startswith("--")]
    bad = applies()
    for name, count in bad:
        print(f"DOES NOT APPLY ({count} matches): {name}")
    if "--check" in flags:
        print(f"{len(MUTATIONS) - len(bad)} of {len(MUTATIONS)} mutations apply")
        shutil.rmtree(ROOT, ignore_errors=True)
        return 1 if bad else 0
    if robot_processes() and "--even-if-robot-runs" not in flags:
        print("a robot process is alive; not running a sweep (see --help text)")
        shutil.rmtree(ROOT, ignore_errors=True)
        return 2
    chosen = [m for m in MUTATIONS if not names or m[0] in names]
    copy_sources()
    survived = []
    try:
        if "--no-baseline" not in flags:
            for test_file in sorted({m[4] for m in chosen}):
                code, _ = run_tests(test_file)
                print(f"baseline {test_file}: pytest exit {code}", flush=True)
        for name, file, old, new, test_file in chosen:
            path = PKG / file
            text = path.read_text()
            if text.count(old) != 1:
                print(f"SKIPPED  {name}: the text occurs {text.count(old)} times")
                survived.append(name + " (not applied)")
                continue
            path.write_text(text.replace(old, new))
            try:
                code, first = run_tests(test_file)
            finally:
                path.write_text(text)
            print(
                f"{'killed  ' if code else 'SURVIVED'} {name}"
                + (f"  <- {first}" if first else ""),
                flush=True,
            )
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
