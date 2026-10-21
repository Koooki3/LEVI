"""Mutation check of the control rate of the client command: each rule is broken
in turn (the code of ``levi/marvel0/`` and of ``src/components/marvel0/``), the
test file meant to guard it runs, and every mutation must make at least one test
fail. A mutation that survives means a rule nobody tests.

    uv run --no-sync python tests/marvel0/mutate_command_hz.py [--check] [name ...]

``--check`` only verifies that every mutation applies (its old text occurs
exactly once) and runs nothing. The mutations are made in a **copy** (``levi/``,
``tests/``, ``scripts/`` and ``src/`` in a temporary folder, ``node_modules``
linked), never in the checkout, and each test file runs there on its own: pytest
with that folder first on ``PYTHONPATH``, or bun, one file per process, under
``nice -n 19 taskset -c 20-23``. It refuses to run while a robot process is alive
(the passive ``ps`` check of setup.md); ``--even-if-robot-runs`` lifts that."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="mutate-hz-"))
COPIED = ("levi", "tests", "scripts", "src", "tsconfig.json", "package.json")
BUN = SOURCE.parents[2] / ".runtime" / "bun-linux-x64"
ROBOT = re.compile(
    r"ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)

CMD = "levi/marvel0/command.py"
API = "levi/marvel0/catalog_api.py"
PY_HZ = "tests/marvel0/test_marvel0_command_hz.py"
PY_RUN = "tests/marvel0/test_marvel0_command_hz_run.py"
PY_ROUTE = "tests/marvel0/test_marvel0_catalog_route.py"
PY_SAMPLE = "tests/marvel0/test_marvel0_command_hz_sample.py"
TS = "src/components/marvel0/"
T = TS + "__tests__/"
CONTROL = TS + "control-logic.ts"
LOGIC = TS + "command-logic.ts"
PANEL = TS + "command-panel.tsx"
START = TS + "run-start.tsx"
FAILURE = TS + "marvel0-failure.ts"

# (name, file, old text that occurs exactly once, new text, test file)
MUTATIONS = [
    # --- the form and the flag --------------------------------------------------------
    (
        "form: the default rate is 5",
        CMD,
        "CONTROL_HZ_DEFAULT = REFERENCE_HZ",
        "CONTROL_HZ_DEFAULT = 5",
        PY_HZ,
    ),
    (
        "form: 15 Hz is an option",
        CMD,
        "CONTROL_HZ_OPTIONS = (5, 10, 20, 25, 50)",
        "CONTROL_HZ_OPTIONS = (5, 10, 15, 20, 25, 50)",
        PY_HZ,
    ),
    (
        "form: 100 Hz is an option",
        CMD,
        "CONTROL_HZ_OPTIONS = (5, 10, 20, 25, 50)",
        "CONTROL_HZ_OPTIONS = (5, 10, 20, 25, 50, 100)",
        PY_HZ,
    ),
    (
        "form: a boolean is a rate",
        CMD,
        'if isinstance(value, bool) or not isinstance(value, int):\n        raise FormError(\n            "invalid_type", "control_hz"',
        'if not isinstance(value, int):\n        raise FormError(\n            "invalid_type", "control_hz"',
        PY_HZ,
    ),
    (
        "form: any positive number is a rate",
        CMD,
        "if value not in CONTROL_HZ_OPTIONS:",
        "if value <= 0:",
        PY_HZ,
    ),
    (
        "form: the refusal lists no options",
        CMD,
        "options=list(CONTROL_HZ_OPTIONS),",
        "",
        PY_HZ,
    ),
    (
        "flag: 10 Hz writes the flag",
        CMD,
        'if form.control_hz != CONTROL_HZ_DEFAULT:\n        values["--control-hz"]',
        'if True:\n        values["--control-hz"]',
        PY_HZ,
    ),
    (
        "flag: another rate writes nothing",
        CMD,
        'if form.control_hz != CONTROL_HZ_DEFAULT:\n        values["--control-hz"]',
        'if False:\n        values["--control-hz"]',
        PY_HZ,
    ),
    (
        "flag: written in the last group",
        CMD,
        '("--control-hz", "value", "form"),\n        ("--eval-num", "value", "form"),',
        '("--eval-num", "value", "form"),\n        ("--control-hz", "value", "form"),',
        PY_HZ,
    ),
    (
        "flag: the form's field list lacks the rate",
        CMD,
        '    "note",\n    "control_hz",\n)',
        '    "note",\n)',
        PY_HZ,
    ),
    (
        "flag: the route drops the field",
        API,
        "    control_hz: int | None = None\n",
        "",
        PY_ROUTE,
    ),
    (
        "flag: the preview has no control",
        API,
        '        "control": preview.control,\n',
        "",
        PY_ROUTE,
    ),
    (
        "flag: the site view has no control",
        API,
        'return {**marvel0_site.public_view(site), "control": command.control_public(site)}',
        "return marvel0_site.public_view(site)",
        PY_ROUTE,
    ),
    # --- the per-step caps -----------------------------------------------------------
    (
        "caps: not scaled above 10 Hz",
        CMD,
        "    return marvel0_site.number(site_value * REFERENCE_HZ / hz)",
        "    return marvel0_site.number(site_value)",
        PY_HZ,
    ),
    (
        "caps: scaled the wrong way",
        CMD,
        "site_value * REFERENCE_HZ / hz",
        "site_value * hz / REFERENCE_HZ",
        PY_HZ,
    ),
    (
        "caps: raised below 10 Hz too",
        CMD,
        "    if hz <= REFERENCE_HZ:\n        return marvel0_site.number(site_value)\n",
        "",
        PY_HZ,
    ),
    (
        "caps: the rad cap keeps the 10 Hz value",
        CMD,
        '        "--max-step-rad": (\n            "value",\n            scaled_cap(site.safety.max_step_rad, form.control_hz),\n        ),',
        '        "--max-step-rad": ("value", marvel0_site.number(site.safety.max_step_rad)),',
        PY_HZ,
    ),
    (
        "caps: the translation cap keeps the 10 Hz value",
        CMD,
        '"--max-step-m": ("value", scaled_cap(site.safety.max_step_m, form.control_hz)),',
        '"--max-step-m": ("value", marvel0_site.number(site.safety.max_step_m)),',
        PY_HZ,
    ),
    (
        "caps: the self check ignores the command",
        CMD,
        "    if _argv(site, form) != list(argv):",
        "    if False:",
        PY_HZ,
    ),
    (
        "caps: the rate is not read back",
        CMD,
        '        if "--control-hz" in found:\n            body["control_hz"] = int(found["--control-hz"])\n',
        "",
        PY_HZ,
    ),
    # --- RTC ----------------------------------------------------------------------------
    (
        "rtc: beats rounded down",
        CMD,
        "return -(-RTC_LATENCY_BEATS * hz // REFERENCE_HZ)",
        "return RTC_LATENCY_BEATS * hz // REFERENCE_HZ",
        PY_HZ,
    ),
    (
        "rtc: beats do not follow the rate",
        CMD,
        "return -(-RTC_LATENCY_BEATS * hz // REFERENCE_HZ)",
        "return RTC_LATENCY_BEATS",
        PY_HZ,
    ),
    (
        "rtc: the empty horizon is not lowered",
        CMD,
        "    return min(site_default, max(1, ACTION_HORIZON - latency_beats(hz)))",
        "    return site_default",
        PY_HZ,
    ),
    (
        "rtc: the empty horizon is lowered below 10 Hz",
        CMD,
        "    if hz <= REFERENCE_HZ:\n        return site_default\n",
        "",
        PY_HZ,
    ),
    (
        "rtc: the empty horizon can reach 0",
        CMD,
        "max(1, ACTION_HORIZON - latency_beats(hz))",
        "(ACTION_HORIZON - latency_beats(hz))",
        PY_HZ,
    ),
    (
        "rtc: the warning counts 3 beats at every rate",
        CMD,
        "    beats = latency_beats(form.control_hz)\n    reached = form.rtc_horizon + beats",
        "    beats = RTC_LATENCY_BEATS\n    reached = form.rtc_horizon + beats",
        PY_HZ,
    ),
    (
        "rtc: no warning when even 1 does not fit",
        CMD,
        "    if 1 + beats > ACTION_HORIZON:",
        "    if False:",
        PY_HZ,
    ),
    (
        "rtc: the default note names no rate",
        CMD,
        'rate = "" if control_hz == CONTROL_HZ_DEFAULT else f" hz={control_hz}"',
        'rate = ""',
        PY_HZ,
    ),
    # --- the warnings ----------------------------------------------------------------------
    (
        "warn: a warning at 10 Hz",
        CMD,
        "    if hz == CONTROL_HZ_DEFAULT:\n        return []\n",
        "",
        PY_HZ,
    ),
    (
        "warn: no first-use note",
        CMD,
        '"control_hz_untested",',
        '"control_hz_untested_gone",',
        PY_HZ,
    ),
    (
        "warn: no trained-at-10 note",
        CMD,
        '"control_hz_not_trained",',
        '"control_hz_not_trained_gone",',
        PY_HZ,
    ),
    (
        "warn: a short episode at 10 Hz or below",
        CMD,
        "if hz > REFERENCE_HZ and seconds < SHORT_EPISODE_S:",
        "if seconds < SHORT_EPISODE_S:",
        PY_HZ,
    ),
    (
        "warn: a short episode at exactly 30 s",
        CMD,
        "seconds < SHORT_EPISODE_S:",
        "seconds <= SHORT_EPISODE_S:",
        PY_HZ,
    ),
    (
        "warn: tight caps never warned",
        CMD,
        "if cap_m < TRAIN_STEP_P99_M or cap_rad < TRAIN_STEP_P99_RAD:",
        "if False:",
        PY_HZ,
    ),
    (
        "warn: tight only by the translation cap",
        CMD,
        "if cap_m < TRAIN_STEP_P99_M or cap_rad < TRAIN_STEP_P99_RAD:",
        "if cap_m < TRAIN_STEP_P99_M:",
        PY_HZ,
    ),
    # --- the numbers shown --------------------------------------------------------------------
    (
        "shown: the episode is steps over 10 Hz",
        CMD,
        '"episode_s": _seconds(form.max_steps / hz),',
        '"episode_s": _seconds(form.max_steps / REFERENCE_HZ),',
        PY_HZ,
    ),
    (
        "shown: the speed cap is the step cap",
        CMD,
        '"speed_cap_m_s": _seconds(cap_m * hz),',
        '"speed_cap_m_s": _seconds(cap_m),',
        PY_HZ,
    ),
    (
        "shown: the RTC margin has the wrong sign",
        CMD,
        '"margin": ACTION_HORIZON - reached,',
        '"margin": reached - ACTION_HORIZON,',
        PY_HZ,
    ),
    (
        "sample: the seconds change and the sample is not regenerated",
        CMD,
        '"episode_s": _seconds(form.max_steps / hz),',
        '"episode_s": _seconds(form.max_steps / hz) + 1,',
        PY_SAMPLE,
    ),
    # --- the page ------------------------------------------------------------------------------
    (
        "page: caps not scaled",
        CONTROL,
        "return Number(((site * control.referenceHz) / hz).toPrecision(10));",
        "return site;",
        T + "control-logic.test.ts",
    ),
    (
        "page: caps scaled the wrong way",
        CONTROL,
        "(site * control.referenceHz) / hz",
        "(site * hz) / control.referenceHz",
        T + "control-logic.test.ts",
    ),
    (
        "page: caps raised below 10 Hz",
        CONTROL,
        "if (hz <= control.referenceHz) return site;",
        "if (hz < 0) return site;",
        T + "control-logic.test.ts",
    ),
    (
        "page: beats rounded down",
        CONTROL,
        "return Math.ceil((control.latencyBeats * hz) / control.referenceHz);",
        "return Math.floor((control.latencyBeats * hz) / control.referenceHz);",
        T + "control-logic.test.ts",
    ),
    (
        "page: the empty horizon is lowered below 10 Hz",
        CONTROL,
        "if (hz <= control.referenceHz) return siteDefault;",
        "if (hz < 0) return siteDefault;",
        T + "control-logic.test.ts",
    ),
    (
        "page: the empty horizon can reach 0",
        CONTROL,
        "Math.max(1, control.actionHorizon - latencyBeats(control, hz))",
        "control.actionHorizon - latencyBeats(control, hz)",
        T + "control-logic.test.ts",
    ),
    (
        "page: the episode is steps over 10 Hz",
        CONTROL,
        "episodeS: round6(steps / hz),",
        "episodeS: round6(steps / control.referenceHz),",
        T + "control-logic.test.ts",
    ),
    (
        "page: the speed cap is the step cap",
        CONTROL,
        "speedCapMS: round6(capM * hz),",
        "speedCapMS: round6(capM),",
        T + "control-logic.test.ts",
    ),
    (
        "page: a default that is not an option is accepted",
        CONTROL,
        "  if (!rates.includes(fields.def as number)) return null;\n",
        "",
        T + "control-logic.test.ts",
    ),
    (
        "page: the default rate is sent",
        LOGIC,
        "  if (body.control_hz === defaultHz) delete body.control_hz;\n",
        "",
        T + "control-logic.test.ts",
    ),
    (
        "page: the rate is never sent",
        LOGIC,
        '  put("control_hz", form.hz, true);\n',
        "",
        T + "control-logic.test.ts",
    ),
    (
        "page: the RTC margin sentence of 10 Hz at every rate",
        LOGIC,
        'warning.code === "rtc_margin_low" && params',
        "false",
        T + "command-hz-warnings.test.ts",
    ),
    (
        "page: the empty horizon ignores the rate",
        LOGIC,
        "form.rtc && site.control",
        "false && site.control",
        T + "control-logic.test.ts",
    ),
    (
        "page: no first-use note",
        PANEL,
        '      {facts.changed && (\n        <div className="rg-span" data-testid="marvel0-command-rate-note">',
        '      {false && (\n        <div className="rg-span" data-testid="marvel0-command-rate-note">',
        T + "command-panel-hz.test.tsx",
    ),
    (
        "page: the start gets no rate",
        PANEL,
        "hz={control ? rateOf(form) : null}",
        "hz={null}",
        T + "command-panel-hz.test.tsx",
    ),
    (
        "page: the start dialog has no note",
        START,
        "hz !== null && hz !== referenceHz && (",
        "false && (",
        T + "command-panel-hz.test.tsx",
    ),
    (
        "page: the refusal of a rate has no field",
        FAILURE,
        '  "control_hz_unsupported",\n  "rollout_group_invalid",',
        '  "rollout_group_invalid",',
        T + "marvel0-hz-catalog.test.ts",
    ),
]


def applies() -> list:
    """The mutations whose old text is not found exactly once."""
    bad = []
    for name, file, old, new, _ in MUTATIONS:
        text = (SOURCE / file).read_text()
        if text.count(old) != 1 or old == new:
            bad.append((name, text.count(old)))
    return bad


def copy_sources():
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".state", ".next")
    for name in COPIED:
        source, target = SOURCE / name, ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=skip)
        else:
            shutil.copy2(source, target)
    (ROOT / "node_modules").symlink_to(SOURCE / "node_modules")
    (ROOT / "backend").symlink_to(SOURCE / "backend")


def run_tests(test_file: str) -> tuple:
    """``(exit code, the first failing line)`` of one test file."""
    env = dict(os.environ)
    env["LEVI_WORKSPACE"] = str(ROOT / ".state")
    env["TMPDIR"] = str(ROOT / "tmp")
    env["PYTHONPATH"] = str(ROOT)
    env["PATH"] = f"{BUN}:{env['PATH']}"
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    if test_file.endswith(".py"):
        argv = [
            sys.executable,
            "-m",
            "pytest",
            test_file,
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            f"--basetemp={ROOT}/pt",
        ]
        marks = ("FAILED", "ERROR")
    else:
        argv = ["bun", "test", test_file]
        marks = ("(fail)", "error:")
    done = subprocess.run(
        ["nice", "-n", "19", "taskset", "-c", "20-23", *argv],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    out = done.stdout + done.stderr
    lines = [x for x in out.splitlines() if x.lstrip().startswith(marks)]
    return done.returncode, (lines[0].strip()[:150] if lines else "")


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
        print("a robot process is alive; not running a sweep")
        shutil.rmtree(ROOT, ignore_errors=True)
        return 2
    chosen = [m for m in MUTATIONS if not names or m[0] in names]
    copy_sources()
    survived = []
    try:
        for test_file in sorted({m[4] for m in chosen}):
            code, first = run_tests(test_file)
            print(f"baseline {test_file}: exit {code} {first}", flush=True)
        for name, file, old, new, test_file in chosen:
            path = ROOT / file
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
