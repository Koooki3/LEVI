"""The runner of one launched automatic run (T-CL-07/08, design X2 §3).

    python -m levi.automatic.runner --run-dir D --plan-sha256 X [--attach]

``launch.launch`` prepares the run folder (``launch.json``: the request and
the plan digest) and starts this process: under ``systemd-run --user`` (its
own unit, never the product's cgroup, no ``Restart``), in the foreground of a
terminal, or in the calling thread for a short dry run.

On start the runner reads the job file again and computes the plan digest
again; a digest that differs from ``--plan-sha256`` (the job file changed
after the launch) is refused before anything is written to the journal
(exit 2). Only dry runs run in this version (in-process fakes, no network):
any other execution mode is refused again here (``E_NO_ROBOT_ADAPTER``).

It names itself in ``<run_dir>/runner.json`` and in the index
``$LEVI_AERI_HOME/runs/<run_id>.json`` (pid and ``children.identity``), and
never in the product's ``processes.json``. A second runner of the same run
is refused: the journal has one writer (``journal.lock``, an flock).

The run is driven until it needs a person (``WAIT_HUMAN``,
``FAULT_LOCKED``) or completes. While it waits the runner stays: operator
commands reach it through the run folder's command channel
(``levi/automatic/control.py``), polled every 50 ms. It exits when the run
completes, on SIGTERM (``systemctl --user stop``, Ctrl+C, a logout without
lingering) or, for an in-process dry run, at its deadline. A SIGTERM while
the run is under way is a controlled stop by ``system:sigterm`` (the
episode ends, the run waits for a person); while it already waits for a
person nothing is stopped and the run is left as it is (``attach`` locks
it). A runner that owns its process drives the dry run with every socket
connection refused (``cli.no_network``).

``--attach`` takes over a run whose runner is gone (``Orchestrator.restore``:
the journal's recovery moves it to ``FAULT_LOCKED``, ``recovery_ambiguous``,
nothing is replayed; leaving it is an operator's ``resume``).
"""

import argparse
import contextlib
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import cli, control, launch
from .journal import JournalBusy, JournalError

EXIT_OK, EXIT_PROBLEM, EXIT_REFUSED = 0, 1, 2
HOLD = ("WAIT_HUMAN", "FAULT_LOCKED", "COMPLETED")
# How often the main loop looks at the state while the run waits.
WAIT_S = 0.2


class Flag:
    """Set once, read from any thread, safe to set from a signal handler:
    a plain attribute, no lock (a ``threading.Event`` takes its lock in
    ``set``, so a second signal arriving inside the first ``set`` would wait
    for a lock its own thread holds)."""

    __slots__ = ("value",)

    def __init__(self):
        self.value = False

    def set(self) -> None:
        self.value = True

    def is_set(self) -> bool:
        return self.value


@dataclass
class RunnerResult:
    exit_code: int
    state: str | None
    code: str
    detail: str = ""
    identity: dict | None = None
    robot_motions: int | None = None


class Runner:
    """One run's runner in this process (``serve`` drives it)."""

    def __init__(
        self,
        run_dir,
        plan_sha256: str,
        *,
        attach: bool = False,
        path=None,
        crash_hook=None,
    ):
        self.run_dir = Path(os.path.abspath(run_dir))
        self.plan_sha256 = plan_sha256
        self.attach = attach
        self.path = path
        self.crash_hook = crash_hook
        self.dry = None
        self.orch = None
        self.run_id = None
        self.unit = None
        self.backend = None
        self.wake = threading.Event()
        self.sigterm = Flag()
        self.pump = None
        self.identity = None

    # --- start -------------------------------------------------------------------------------------

    def _refuse(self, code: str, detail: str, *, record: bool = True) -> RunnerResult:
        """Refused before taking the run. ``record=False`` when another
        runner owns (or owned) the run: its record stays as it is."""
        result = RunnerResult(EXIT_REFUSED, None, code, detail[:500])
        if record:
            with contextlib.suppress(OSError):
                self._record("refused", code=code, detail=detail[:500])
        return result

    def _record(self, state: str, **extra) -> None:
        value = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "identity": self.identity,
            "plan_sha256": self.plan_sha256,
            "backend": self.backend,
            "unit": self.unit,
            "attach": self.attach,
            "started_wall_ns": self.started,
            "state": state,
            "updated_wall_ns": time.time_ns(),
            **extra,
        }
        launch.write_record(self.run_dir / launch.RUNNER_RECORD, value)
        if self.run_id:
            launch.update_index(self.run_id, self.path, runner=value)

    def open(self) -> RunnerResult | None:
        """Check the launch and take the run; a ``RunnerResult`` when refused."""
        from levi.children import identity

        self.started = time.time_ns()
        found = identity(os.getpid())
        self.identity = (
            {"start_ticks": found["start_ticks"], "boot": found["boot"]}
            if found
            else None
        )
        try:
            record = launch.read_record(self.run_dir / launch.LAUNCH_RECORD)
            request = launch.LaunchRequest.from_record(record.get("request"))
            self.run_id = launch._id(record.get("run_id"), "run id")
            self.unit = (
                record.get("unit") if isinstance(record.get("unit"), str) else None
            )
            recorded = record.get("plan_sha256")
        except (OSError, ValueError, launch.LaunchRefused) as exc:
            return RunnerResult(
                EXIT_REFUSED, None, "E_REQUEST", f"no usable launch record: {exc}"
            )
        self.backend = request.backend
        if request.execution_mode != launch.DRY_RUN:
            return self._refuse(
                "E_NO_ROBOT_ADAPTER",
                f"{request.execution_mode}: no real robot adapter in this version",
            )
        expected = launch.run_dir_of(
            launch.dry_folder(self.run_id, request.keep_dir, self.path), self.run_id
        )
        if Path(os.path.abspath(expected)) != self.run_dir:
            return self._refuse(
                "E_REQUEST",
                f"the launch record belongs to {expected}, not {self.run_dir}",
            )
        try:
            job = launch.normalise(request)
        except (cli.JobError, launch.LaunchRefused, OSError) as exc:
            return self._refuse(
                "E_PLAN_CHANGED", f"the job file no longer plans: {exc}"
            )
        if not (job["plan_sha256"] == self.plan_sha256 == recorded):
            return self._refuse(
                "E_PLAN_CHANGED",
                f"the job file now plans {job['plan_sha256'][:12]}, the launch was "
                f"{str(self.plan_sha256)[:12]}: launch again",
            )
        if job["config"].run_id != self.run_id:
            return self._refuse("E_PLAN_CHANGED", "the job names another run")
        try:
            self.dry = cli.DryRun(
                job,
                self.run_dir.parents[2],
                scenes=job["overrides"].get("scenes", ()),
                restore=self.attach,
                crash_hook=self.crash_hook,
            )
        except JournalBusy:
            return self._refuse(
                "E_RUNNER_ALIVE", "another runner writes this run", record=False
            )
        except JournalError as exc:
            if exc.code == "E_EXISTS":
                return self._refuse(
                    "E_RUN_EXISTS",
                    "the run was started before: attach to it",
                    record=False,
                )
            return self._refuse(exc.code, exc.detail)
        except (cli.JobError, cli.ConfigError) as exc:
            return self._refuse("E_JOB_INVALID", str(exc))
        self.orch = self.dry.orch
        self._record("running", state_of_run=self.orch.state)
        return None

    def on_signal(self, signum, frame) -> None:
        """SIGTERM/SIGINT: only a flag. This runs on the main thread, which
        may be inside ``wake.wait``/``wake.clear`` holding that event's
        lock (review CL3 B2), and may hold the orchestrator's re-entrant
        lock: the command thread reads the flag (within 50 ms) and the main
        loop looks at it at least every ``WAIT_S``."""
        self.sigterm.set()  # a plain attribute: no lock to take

    # --- the loop ----------------------------------------------------------------------------------

    def _motions(self) -> int | None:
        robot = getattr(self.dry, "robot", None)
        motions = getattr(robot, "motions", None)
        return len(motions) if motions is not None else None

    def drive(self, deadline_s: float | None = None) -> RunnerResult:
        """Run until the run completes, SIGTERM has been handled, or the
        deadline passes while the run waits for a person."""
        deadline = None if deadline_s is None else time.monotonic() + deadline_s
        state = self.orch.state
        try:
            while True:
                if state not in HOLD:
                    state = self.orch.run()
                    if self.orch.halted:
                        break
                    continue
                if state == "COMPLETED":
                    break
                if self.sigterm.is_set() and (
                    self.pump is None or self.pump.stop_settled()
                ):
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self.wake.wait(WAIT_S)
                self.wake.clear()
                state = self.orch.state
        except Exception as exc:  # noqa: BLE001 - the orchestrator locked the run
            return self.finish(EXIT_PROBLEM, "E_RUNNER", f"{type(exc).__name__}: {exc}")
        if self.orch.halted:
            return self.finish(EXIT_PROBLEM, "E_HALTED", self.orch.halted)
        return self.finish(EXIT_OK, "exited", "")

    def finish(self, exit_code: int, code: str, detail: str) -> RunnerResult:
        if self.pump is not None:
            self.pump.close()
        state = self.orch.state if self.orch is not None else None
        motions = self._motions()
        if self.dry is not None:
            with contextlib.suppress(Exception):
                self.dry.orch.close()
            with contextlib.suppress(Exception):
                self.dry.recorder.close()
        with contextlib.suppress(OSError):
            self._record(
                "exited",
                exit_code=exit_code,
                final_state=state,
                code=code,
                detail=detail[:500],
                robot_motions=motions,
            )
        return RunnerResult(exit_code, state, code, detail, self.identity, motions)


def serve(
    run_dir,
    plan_sha256: str,
    *,
    attach: bool = False,
    signals: bool = False,
    deadline_s: float | None = None,
    path=None,
    crash_hook=None,
    keep_handlers: bool = False,
) -> RunnerResult:
    """Run the runner of ``run_dir`` in this process. ``signals``: the
    runner owns this process (a systemd unit, ``--foreground``, ``main``):
    SIGTERM and SIGINT are handled (main thread only), and the dry run is
    driven under ``cli.no_network()`` (every socket connection refused, as
    for ``run --dry-run``). Without ``signals`` (``inprocess``: the runner
    in a thread of another program, such as a service) neither is done: a
    process-wide guard would cut that program's own connections.
    ``keep_handlers`` (``main``: the process ends with the runner): SIGTERM
    and SIGINT stay ignored afterwards instead of getting their old
    handlers back."""
    guard = cli.no_network() if signals else contextlib.nullcontext()
    with guard:
        return _serve(
            run_dir,
            plan_sha256,
            attach=attach,
            signals=signals,
            deadline_s=deadline_s,
            path=path,
            crash_hook=crash_hook,
            keep_handlers=keep_handlers,
        )


def _serve(
    run_dir,
    plan_sha256,
    *,
    attach,
    signals,
    deadline_s,
    path,
    crash_hook,
    keep_handlers,
):
    runner = Runner(
        run_dir, plan_sha256, attach=attach, path=path, crash_hook=crash_hook
    )
    previous = {}
    if signals:
        # Before the runner names itself: from then on a SIGTERM is its.
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, runner.on_signal)
    try:
        refused = runner.open()
        if refused is not None:
            return refused
        try:
            runner.pump = control.CommandPump(runner)
        except (OSError, control.CommandError) as exc:
            return runner.finish(EXIT_PROBLEM, "E_CONTROL", str(exc))
        runner.pump.start()
        return runner.drive(deadline_s)
    finally:
        for number, handler in previous.items():
            # A process that ends with its runner ignores later signals: the
            # interpreter's shutdown would otherwise put back the default
            # action, and a late SIGTERM would kill an exit that is under way.
            signal.signal(number, signal.SIG_IGN if keep_handlers else handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m levi.automatic.runner",
        description="The runner of one launched automatic run (started by "
        "levi automatic run/attach, not by hand). / 一次已启动的自动测评运行的"
        "运行器（由 levi automatic run/attach 启动，不要手动运行）。",
    )
    parser.add_argument("--run-dir", required=True, help="the run folder / 运行目录")
    parser.add_argument(
        "--plan-sha256", required=True, help="the launched plan / 已启动的计划摘要"
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help="restore a run whose runner is gone / 接管运行器已退出的运行",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = serve(
        args.run_dir,
        args.plan_sha256,
        attach=args.attach,
        signals=True,
        keep_handlers=True,
    )
    if result.exit_code != EXIT_OK:
        print(f"{result.code}: {result.detail}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
