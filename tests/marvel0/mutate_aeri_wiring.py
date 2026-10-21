"""Mutation check of the wiring around the hosted AERI terminal D and of the
readiness plumbing (``levi/activity.py``, ``levi/service.py``,
``levi/marvel0/{aeri_activity,aeri_ready,aeri_api,run,deploy}.py``): each gate is
removed (or one safety property turned round) in turn, the one test file that is
meant to guard it runs, and every mutation must make at least one test fail. A
mutation that survives means a gate nobody tests.

    uv run --no-sync python tests/marvel0/mutate_aeri_wiring.py [--check] [--no-baseline] [name ...]

``--check`` only verifies that every mutation applies (its old text occurs
exactly once) and runs nothing; ``--no-baseline`` skips the run of each test file
on the unbroken copy. The mutations are made in a **copy** of ``levi/``,
``tests/`` and the few other files the tests read, in a temporary folder (the
checkout itself is never edited, so nobody else's test run can meet a broken
gate), and the tests run there with that folder first on ``PYTHONPATH``. It starts
no process but pytest, one test file at a time, under ``nice -n 19 taskset -c
16-19``. It refuses to run while a robot process is alive (the passive ``ps``
check of setup.md) and while the arm guard says the controller is on:
a sweep is many test runs, and the arm's control loop is not to be disturbed by
it; ``--even-if-robot-runs`` lifts the first (one file at a time stays)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="mutate-aeri-wiring-"))
COPIED = (
    "levi",
    "tests",
    "backend",
    "docs/SUPERVISION.md",
    "src/components/marvel0/autonomy-logic.ts",
    "src/components/marvel0/__tests__/marvel0-aeri.samples.json",
    "pyproject.toml",
)
ROBOT = re.compile(
    r"ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)

ACT = "tests/marvel0/test_marvel0_aeri_activity.py"
READY = "tests/marvel0/test_marvel0_aeri_ready.py"

# (name, file under the checkout, old text that occurs exactly once, new text, test file)
MUTATIONS = [
    # --- levi stop and the restart see the hosted D -----------------------------------
    (
        "activity: the hosted D is not among the job sources",
        "levi/activity.py",
        "EXTRA_SOURCES = (_hosted_jobs,)",
        "EXTRA_SOURCES = ()",
        ACT,
    ),
    (
        "activity: running_jobs does not ask the extra sources",
        "levi/activity.py",
        "    for source in EXTRA_SOURCES:\n        out.extend(source(workspace))\n",
        "",
        ACT,
    ),
    (
        "activity: the jobs are asked through the page's supervisor (its adopt sends a SIGINT)",
        "levi/activity.py",
        "    return aeri_activity.job_rows(workspace)",
        "    from .marvel0 import aeri_run_api\n\n    aeri_run_api.active()\n    return aeri_activity.job_rows(workspace)",
        ACT,
    ),
    (
        "activity: a run of an earlier process with the same pid is a D",
        "levi/marvel0/aeri_activity.py",
        "    return _row(rec, pid, now, kind) if same else None",
        "    return _row(rec, pid, now, kind)",
        ACT,
    ),
    (
        "activity: a record that says started is a D whatever its launcher is",
        "levi/marvel0/aeri_activity.py",
        "    got = identity(pid)\n    if got is None:\n        return None\n",
        "    got = identity(pid) or {}\n",
        ACT,
    ),
    (
        "activity: a start in flight never ends",
        "levi/marvel0/aeri_activity.py",
        "        young = isinstance(began, (int, float)) and 0 <= now - began < INTENT_GRACE_S",
        "        young = True",
        ACT,
    ),
    (
        "activity: a leftover intent is not young for ever",
        "levi/marvel0/aeri_activity.py",
        "0 <= now - began < INTENT_GRACE_S",
        "now - began < INTENT_GRACE_S",
        ACT,
    ),
    (
        "activity: a record that cannot be read is no run",
        "levi/marvel0/aeri_activity.py",
        '        why = UNREADABLE if isinstance(exc, record.RecordCorrupt) else "error"\n',
        '        return []\n        why = UNREADABLE if isinstance(exc, record.RecordCorrupt) else "error"\n',
        ACT,
    ),
    (
        "activity: a reader that fails is no run",
        "levi/marvel0/aeri_activity.py",
        '        why = UNREADABLE if isinstance(exc, record.RecordCorrupt) else "error"\n',
        '        if not isinstance(exc, record.RecordCorrupt):\n            return []\n        why = UNREADABLE if isinstance(exc, record.RecordCorrupt) else "error"\n',
        ACT,
    ),
    (
        "activity: the record folder may be a link",
        "levi/marvel0/aeri_activity.py",
        "        info = os.lstat(target)",
        "        info = os.stat(target)",
        ACT,
    ),
    (
        "activity: the reader looks in another folder than the page writes",
        "levi/marvel0/aeri_activity.py",
        'REL = ("marvel0", "aeri")',
        'REL = ("marvel0", "aeri-run")',
        ACT,
    ),
    (
        "activity: the hosted evaluation client is not among the jobs",
        "levi/marvel0/aeri_activity.py",
        "HOSTED = ((KIND, REL), (CLIENT_KIND, CLIENT_REL))",
        "HOSTED = ((KIND, REL),)",
        ACT,
    ),
    (
        "activity: the reader looks in another folder than the client's page writes",
        "levi/marvel0/aeri_activity.py",
        'CLIENT_REL = ("marvel0", "run")',
        'CLIENT_REL = ("marvel0", "client")',
        ACT,
    ),
    # --- the start gate's check -----------------------------------------------------------
    (
        "gate: the wiring check always passes",
        "levi/marvel0/aeri_activity.py",
        "        return _canary_listed(identity)",
        "        return True",
        ACT,
    ),
    (
        "gate: the check after the launch accepts any run",
        "levi/marvel0/aeri_activity.py",
        '                row.get("kind") == KIND and row.get("id") == run_id',
        '                row.get("kind") == KIND',
        ACT,
    ),
    (
        "gate: a check that cannot run passes",
        "levi/marvel0/aeri_activity.py",
        "    except Exception:  # noqa: BLE001 - a check that cannot run does not pass\n        return False",
        "    except Exception:  # noqa: BLE001 - a check that cannot run does not pass\n        return True",
        ACT,
    ),
    # --- the service: adopt at start, the orderly end first -------------------------------
    (
        "service: the take-over at start is dropped",
        "levi/service.py",
        "    aeri_activity.adopt_on_start()\n",
        "",
        ACT,
    ),
    (
        "service: the orderly end of D is dropped",
        "levi/service.py",
        "        aeri_activity.shutdown_hosted()\n",
        "",
        ACT,
    ),
    (
        "service: the orderly end of D comes after the other stops",
        "levi/service.py",
        "        aeri_activity.shutdown_hosted()\n        if monitor_auto:\n            monitor_auto.close()\n",
        "        if monitor_auto:\n            monitor_auto.close()\n        aeri_activity.shutdown_hosted()\n",
        ACT,
    ),
    (
        "shutdown: D is not told to stop",
        "levi/marvel0/aeri_activity.py",
        "    sent = _stop(supervisor, job)",
        '    sent = "sent"',
        ACT,
    ),
    (
        "shutdown: the wrap-up is not waited for",
        "levi/marvel0/aeri_activity.py",
        "    while monotonic() < deadline:",
        "    while False:",
        ACT,
    ),
    (
        "shutdown: a run that was stopped already gets a second Ctrl+C",
        "levi/marvel0/aeri_activity.py",
        '        supervisor.stop({"request_id": f"shutdown-{job.get(\'id\')}"[:80]})\n        return "sent"',
        '        supervisor._send_stop(supervisor._current()[0])\n        return "sent"',
        ACT,
    ),
    (
        "shutdown: a failing supervisor stops the shutdown",
        "levi/marvel0/aeri_activity.py",
        '        log.exception("asking for a hosted AERI terminal D failed at shutdown")\n        return {"state": "error", "stop": None}',
        '        log.exception("asking for a hosted AERI terminal D failed at shutdown")\n        raise',
        ACT,
    ),
    (
        "adopt: a failing take-over stops the service from starting",
        "levi/marvel0/aeri_activity.py",
        '        log.exception("taking over a hosted AERI terminal D failed")',
        "        raise",
        ACT,
    ),
    # --- the evaluation client and the deploy ---------------------------------------------
    (
        "run: a client may start beside the hosted D",
        "levi/marvel0/run.py",
        '        if hosted:\n            return check_row("no_aeri", False, hosted=True)',
        '        if False:\n            return check_row("no_aeri", False, hosted=True)',
        ACT,
    ),
    (
        "run: a probe of the hosted D that fails rules it out",
        "levi/marvel0/run.py",
        '            hosted = bool(self.host.hosted_aeri())\n        except Exception:  # noqa: BLE001\n            return check_row("no_aeri", None)',
        "            hosted = bool(self.host.hosted_aeri())\n        except Exception:  # noqa: BLE001\n            hosted = False",
        ACT,
    ),
    (
        "deploy: a policy server may be deployed under the hosted D",
        "levi/marvel0/deploy.py",
        '        if hosted:\n            raise Refuse("aeri_run_live", run_id=str(hosted.get("id")))',
        '        if False:\n            raise Refuse("aeri_run_live", run_id=str(hosted.get("id")))',
        ACT,
    ),
    (
        "deploy: a probe of the hosted D that fails rules it out",
        "levi/marvel0/deploy.py",
        '        except Exception:  # noqa: BLE001 - any failure reading it fails closed\n            raise Refuse("aeri_registry_unreadable") from None\n        if hosted:',
        "        except Exception:  # noqa: BLE001 - any failure reading it fails closed\n            hosted = None\n        if hosted:",
        ACT,
    ),
    # --- the cost of the card --------------------------------------------------------------
    (
        "ready: a switched off card reads everything",
        "levi/marvel0/aeri_ready.py",
        '    if control["enabled"] is not True:\n        facts["light"] = "disabled"',
        '    if False:\n        facts["light"] = "disabled"',
        READY,
    ),
    (
        "ready: a switched off card still builds the rows",
        "levi/marvel0/aeri_ready.py",
        '    if aeri is None or facts.get("light") == "disabled":\n        return []',
        "    if aeri is None:\n        return []",
        READY,
    ),
    (
        "ready: a busy card reads everything",
        "levi/marvel0/aeri_ready.py",
        "    if hosted_d is True or hosted_e is True:",
        "    if False:",
        READY,
    ),
    (
        "ready: a light read still builds the live and leftover blocks",
        "levi/marvel0/aeri_ready.py",
        '    full = configured and not facts.get("light")',
        "    full = configured",
        READY,
    ),
    (
        "ready: the cache time is the page's whole poll",
        "levi/marvel0/aeri_ready.py",
        "CACHE_S = 1.0",
        "CACHE_S = 3.0",
        READY,
    ),
    (
        "ready: a second row may be warn",
        "levi/marvel0/aeri_ready.py",
        "WARN_OK = WARN_ROWS",
        'WARN_OK = ("stack_ready", "gpu_free")',
        READY,
    ),
    (
        "api: a stop under the page's own client reads and starts a batch",
        "levi/marvel0/aeri_api.py",
        '        if facts.get("light") == "busy":',
        "        if False:",
        READY,
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
        timeout=600,
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


def arm_active() -> bool:
    """The arm guard of the lab (sockets to the arm and the marker), when it is
    there; its absence says nothing."""
    guard = SOURCE.parents[1] / "levi-hub" / "arm_guard.py"
    if not guard.is_file():
        return False
    return (
        subprocess.run(
            [sys.executable, str(guard)], capture_output=True, check=False
        ).returncode
        != 0
    )


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
    if (robot_processes() and "--even-if-robot-runs" not in flags) or arm_active():
        print("a robot process is alive or the arm guard says active; not running")
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
