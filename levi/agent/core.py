"""One workspace-owned API process, shared by UI, stdio bridges and terminals."""

import fcntl
import http.client
import json
import os
import secrets
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


def ensure(port=7861):
    root = directory()
    with (root / "start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = status()
        if current:
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


def stop():
    value = status()
    if value:
        # The server, not an unverified PID file, handles its own termination.
        return request("/api/levi/agent/v1/core/stop", {}, human=True)
    return {"status": "stopped"}


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
            (root / "instance.json").write_text(
                json.dumps({"instance": instance, "pid": os.getpid(), "port": port})
            )
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
