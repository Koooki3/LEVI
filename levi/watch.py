"""Keep the product core alive while ``levi serve`` runs: restart it after a crash, never after a stop.

A core that was stopped on purpose (``levi stop``, the stop request of the API, SIGTERM) removes its
``instance.json`` and ``api.sock`` and is marked ``stop-requested``; one that crashed or was killed leaves
``instance.json`` behind with a dead process and no such mark. ``levi serve`` used to notice neither: the web
page stayed up and every request failed until a person ran ``levi serve`` again. ``CoreWatch`` asks every few
seconds and restarts a crashed core, with a cap (``limit`` restarts per ``window`` seconds) so a core that cannot
start is not restarted in a loop. A ``veto`` can hold the restart back with a reason (job workers still running
that a restarted core would stop; main moved on since ``levi serve`` started): it is logged once per reason and
the restart waits for a person.
"""

import signal
import subprocess
import time


def web_exit_status(code):
    """Exit status of ``levi serve`` for the status its web page ended with. ``next start`` answers SIGTERM and
    SIGINT with status 0 (bun itself dies by the signal): somebody stopped it on purpose, so 0. Anything else
    (SIGKILL/OOM 137, a hangup 129, an error) is a failure and never 0, which a supervisor (systemd
    ``Restart=on-failure``) restarts."""
    if code in (0, -signal.SIGTERM, -signal.SIGINT):
        return 0
    return code if code > 0 else 1


RUNTIME_PATHS = (
    "levi",
    "backend",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "src",
    "bun.lock",
    "next.config.ts",
)


def git_head(project):
    """The commit the checkout is on, or None (not a git checkout, no git)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def runtime_changed(project, since):
    """Runtime files that differ between commit ``since`` (the one ``levi serve`` started on) and the checkout's HEAD:
    main moved on, so a restarted core would run different code than the web page it belongs to. Edits that are
    not committed do not count (they were there when it started, or are somebody's work in progress). Empty when
    unknown."""
    if not since:
        return []
    try:
        done = subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "diff",
                "--name-only",
                since,
                "HEAD",
                "--",
                *RUNTIME_PATHS,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return done.stdout.split() if done.returncode == 0 else []


def restart_veto(project, started_at, orphaned_workers):
    """The reason an automatic core restart should wait, or None: job workers (export, RECAP, segmentation) whose
    core died are still running and a new core would stop them (the watch keeps looking and restarts the core when
    they have finished); or runtime files were committed since ``levi serve`` started, so a restarted core would
    run newer code than its web page (restart LEVI as a whole: ``levi stop``, then start it again)."""
    workers = orphaned_workers()
    if workers:
        return f"{len(workers)} job worker(s) still run and a new core would stop them; it restarts when they finish"
    changed = runtime_changed(project, started_at)
    if changed:
        return f"{len(changed)} runtime file(s) were committed since LEVI started ({changed[0]}…); restart LEVI as a whole"
    return None


class CoreWatch:
    def __init__(
        self,
        crashed,
        restart,
        log=lambda text: print(text, flush=True),
        *,
        veto=lambda: None,
        clock=time.monotonic,
        interval=5.0,
        limit=5,
        window=600.0,
    ):
        self.crashed = crashed  # () -> bool: instance.json present, its process gone, no stop requested
        self.restart = restart  # () -> None: start a core (agent.core.ensure)
        self.veto = (
            veto  # () -> str | None: a reason not to restart it automatically now
        )
        self.log = log
        self.clock = clock
        self.interval = interval
        self.limit = limit
        self.window = window
        self.next_check = 0.0
        self.restarts: list[float] = []
        self.gave_up = False
        self.vetoed = None
        self.failed = None

    def tick(self) -> bool:
        """One look; returns True when it restarted the core. Cheap enough to call every loop turn."""
        now = self.clock()
        if now < self.next_check:
            return False
        self.next_check = now + self.interval
        try:
            if not self.crashed():
                self.gave_up = False
                self.vetoed = None
                return False
            reason = self.veto()
        except Exception as exc:  # noqa: BLE001 - a failing look must never end `levi serve`; the next one retries
            if str(exc) != self.failed:
                self.failed = str(exc)
                self.log(f"[LEVI] could not check the core: {exc} / 无法检查核心")
            return False
        self.failed = None
        if reason:
            if reason != self.vetoed:
                self.vetoed = reason
                self.log(
                    f"[LEVI] the core died; not restarting it automatically: {reason}"
                )
            return False
        self.vetoed = None
        self.restarts = [t for t in self.restarts if now - t < self.window]
        if len(self.restarts) >= self.limit:
            if not self.gave_up:
                self.gave_up = True
                self.log(
                    f"[LEVI] the core crashed {self.limit} times in {int(self.window)} s: no more restarts until "
                    "that window has passed; read agent/core/core.log, then `levi stop` and start LEVI again. "
                    f"/ 核心 {int(self.window)} 秒内崩溃 {self.limit} 次，窗口过去之前不再自动重启"
                )
            return False
        self.restarts.append(now)
        self.log(
            "[LEVI] the core died without a stop request: restarting it / 核心意外退出，正在重启"
        )
        try:
            self.restart()
        except Exception as exc:  # noqa: BLE001 - the next look tries again, within the cap
            self.log(f"[LEVI] the core did not restart: {exc} / 核心重启失败")
            return False
        return True
