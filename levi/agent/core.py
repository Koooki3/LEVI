"""One workspace-owned API process, shared by UI, stdio bridges and terminals."""

import fcntl
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time


def directory():
    from levi.paths import STATE

    path = STATE / "agent" / "core"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=125)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(path, payload=None, *, token=None, human=False, binary=False):
    root = directory()
    headers = {"Content-Type": "application/json"}
    if human:
        if os.getenv("LEVI_PILOT_CHILD"):
            raise PermissionError(
                "Pilot processes cannot use the human control channel"
            )
        headers["x-levi-ui-token"] = (root / "human.key").read_text()
    elif token:
        headers["Authorization"] = "Bearer " + token
    conn = UnixConnection(root / "api.sock")
    try:
        conn.request(
            "POST" if payload is not None else "GET",
            path,
            json.dumps(payload) if payload is not None else None,
            headers,
        )
        response = conn.getresponse()
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("Response exceeds 16 MiB; use bounded evidence pages")
        if response.status >= 400:
            raise ValueError(_detail(response.status, path, data))
        if binary:
            return data
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise ValueError(_detail(response.status, path, data)) from None
    finally:
        conn.close()


def _detail(status, path, data):
    """A readable failure: an empty or non-JSON body is still an answer."""
    try:
        return json.loads(data).get("detail", "LEVI request failed")
    except (json.JSONDecodeError, AttributeError):
        text = data.decode("utf-8", "replace").strip()
        if not text:
            text = "empty response body"
        return f"{path} returned HTTP {status}: {text[:400]}"


def status():
    try:
        value = json.loads((directory() / "instance.json").read_text())
        health = request("/api/levi/health")
        return value if health.get("instance") == value["instance"] else None
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return None


STOP_MARKER = "stop-requested"


def mark_stop(pid):
    """Record, before it happens, that this core is about to be stopped on purpose (``levi stop``, the stop
    request of the API). ``crashed()`` then never reads its leftovers as a crash, even if the shutdown hangs
    and someone finishes it with ``kill -9``."""
    try:
        (directory() / STOP_MARKER).write_text(str(pid))
    except OSError:
        pass


def crashed():
    """The core died without being stopped: ``instance.json`` is still there, its process is gone and no stop
    was requested for it. A core stopped on purpose removes the file on SIGTERM (``clean_exit_handler``);
    ``levi.watch`` restarts only the case that remains (killed, OOM, a crash).

    A core killed while its parent (``levi serve``) lives stays a zombie until reaped, and ``kill(pid, 0)``
    still succeeds on a zombie: reap it if it is our child, then ask ``children.identity`` (None for a zombie)."""
    from levi import children

    root = directory()
    try:
        pid = int(json.loads((root / "instance.json").read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if pid <= 0:  # a damaged file: waitpid(-1) would reap any child
        return False
    try:
        if int((root / STOP_MARKER).read_text()) == pid:
            return False  # its stop was asked for
    except (OSError, ValueError):
        pass
    try:
        os.waitpid(pid, os.WNOHANG)  # our own child that already died: collect it
    except ChildProcessError:
        pass  # not our child (a core started by another process)
    return children.identity(pid) is None


def orphaned_workers():
    """Job workers (pool export, RECAP, segmentation labelling, ...) that still run although the core that owns
    them is gone: a restarted core would terminate them (``children.reclaim``). Live sessions (a Pilot, a
    segmentation overlay) are not jobs: nothing is lost by restarting them."""
    from levi import activity, children

    return [
        row
        for row in children.listed()
        if row["running"] and not row["owner_running"] and activity._is_job(row["kind"])
    ]


def ensure(port=7861):
    root = directory()
    with (root / "start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = status()
        if current:
            if os.getenv("LEVI_CPU_ONLY") == "1" and not current.get("cpu_only"):
                raise RuntimeError(
                    "The running LEVI core was not started with LEVI_CPU_ONLY=1; "
                    "stop it before starting a CPU-only session"
                )
            return current
        if os.getenv("LEVI_PILOT_CHILD"):
            raise RuntimeError("Owning core stopped; managed Pilot cannot restart it")
        if len(os.fsencode(root / "api.sock")) > 103:
            raise ValueError(
                "LEVI_WORKSPACE path is too long for a Unix socket; use a shorter workspace path"
            )
        env = dict(os.environ)
        env["LEVI_CORE_PORT"] = str(port)
        with (root / "core.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "levi.agent.core"],
                env=env,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        for _ in range(150):
            if value := status():
                return value
            if child.poll() is not None:
                raise RuntimeError("Core failed to start; inspect agent/core/core.log")
            time.sleep(0.1)
        child.terminate()
        raise RuntimeError("Core startup timed out")


JOB_POLL_SECONDS = 15.0


def _stop_core(wait):
    """Ask the running core to stop, or tidy the leftovers of a dead one. Called with ``start.lock`` held, so a
    core that ``levi serve`` is starting right now is either stopped here or not yet started, never missed."""
    from levi import children

    result = {"status": "stopped"}
    if current := status():
        who = children.identity(current["pid"])
        mark_stop(current["pid"])
        # The server, not an unverified PID file, handles its own termination.
        request("/api/levi/agent/v1/core/stop", {"force": True}, human=True)
        # It stops answering before it has stopped its workers: wait for the
        # process itself, so "stopped" is true when it is printed.
        deadline = time.monotonic() + wait
        while (
            who
            and children.identity(current["pid"]) == who
            and time.monotonic() < deadline
        ):
            time.sleep(0.2)
        if who and children.identity(current["pid"]) == who:
            result["status"] = "stopping"
    else:
        # No core answers. A stale instance.json of a dead core would make `levi serve` restart it later: this
        # stop says it is meant to stay down.
        try:
            dead = int(json.loads((directory() / "instance.json").read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            dead = 0
        if dead > 0 and children.identity(dead) is None:
            mark_stop(dead)
            for name in ("instance.json", "api.sock"):
                (directory() / name).unlink(missing_ok=True)
    return result


def stop(*, models=False, wait=20.0, force=False, wait_jobs=None):
    """Stop the core and everything it started; ``models`` also stops the
    Ollama service LEVI started itself (never a shared one).

    Stopping kills the workers of running jobs (a pool export, a conversion,
    a RECAP or segmentation run). So it refuses while any is running --
    returning ``{"status": "refused", "running": [...]}`` -- unless ``force``
    (pool exports are then marked interrupted and can be resumed) or
    ``wait_jobs`` minutes pass for them to finish."""
    from levi import activity, children

    if not force:
        running = activity.running_jobs()
        deadline = time.monotonic() + (wait_jobs or 0) * 60
        while running and wait_jobs and time.monotonic() < deadline:
            print(
                f"waiting for {len(running)} running job(s):\n"
                f"{activity.describe(running)}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(min(JOB_POLL_SECONDS, max(0.01, deadline - time.monotonic())))
            running = activity.running_jobs()
        if running:
            return {
                "status": "refused",
                "running": running,
                "hint": "Stopping now would kill these jobs. Wait for them, use "
                "`levi stop --wait [minutes]`, or `levi stop --force` (pool exports "
                "are kept as interrupted and can be resumed).",
            }
    with (directory() / "start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = _stop_core(wait)
    # A core that was killed rather than stopped leaves its workers behind.
    reclaimed = children.reclaim()
    if reclaimed:
        result["reclaimed"] = [
            {"kind": r["kind"], "label": r["label"], "pid": r["pid"]} for r in reclaimed
        ]
    if models:
        from levi.inference.runtime import OllamaRuntimeManager
        from levi.paths import ROOT, STATE

        owned = OllamaRuntimeManager(ROOT, STATE)
        result["ollama"] = (
            "stopped" if owned.stop().get("stop_requested") else "not running"
        )
    return result


def clean_exit_handler(root):
    """SIGTERM handler of the core: remove ``api.sock`` and ``instance.json``, then die by the signal.

    uvicorn re-raises the signal it captured once it has shut down, with the handler that was installed
    before it ran: with the default one the process is gone before ``serve()``'s ``finally`` can run, so a
    core stopped on purpose (``levi stop``, the UI, ``kill``) left ``instance.json`` behind exactly like a
    crashed one. ``crashed()`` could not tell them apart; now a stop leaves no file and only a kill -9 or a
    crash does."""

    def handler(signum, frame):
        for name in ("api.sock", "instance.json"):
            (root / name).unlink(missing_ok=True)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    return handler


def serve():
    import uvicorn

    root = directory()
    with (root / "owner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        os.umask(0o077)
        key = (
            (root / "human.key").read_text()
            if (root / "human.key").exists()
            else secrets.token_urlsafe(32)
        )
        (root / "human.key").write_text(key)
        os.environ["LEVI_UI_TOKEN"] = key
        instance = secrets.token_urlsafe(24)
        os.environ["LEVI_CORE_INSTANCE"] = instance
        port = int(os.getenv("LEVI_CORE_PORT", "7861"))
        sockets = []
        try:
            tcp = socket.socket()
            sockets.append(tcp)
            tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind(("127.0.0.1", port))
            tcp.listen(128)
            path = root / "api.sock"
            path.unlink(missing_ok=True)
            uds = socket.socket(socket.AF_UNIX)
            sockets.append(uds)
            uds.bind(str(path))
            uds.listen(128)
            signal.signal(
                signal.SIGTERM, clean_exit_handler(root)
            )  # before the file: no window
            (root / "instance.json").write_text(
                json.dumps(
                    {
                        "instance": instance,
                        "pid": os.getpid(),
                        "port": port,
                        "cpu_only": os.getenv("LEVI_CPU_ONLY") == "1",
                    }
                )
            )
            # After the file: a marker of an older core (other pid) never matches this one, and between the two
            # steps a looking watch must not find "no marker, dead pid".
            (root / STOP_MARKER).unlink(missing_ok=True)
            uvicorn.Server(uvicorn.Config("levi.service:app", log_level="warning")).run(
                sockets=sockets
            )
        finally:
            for sock in sockets:
                sock.close()
            (root / "api.sock").unlink(missing_ok=True)
            (root / "instance.json").unlink(missing_ok=True)


if __name__ == "__main__":
    serve()
