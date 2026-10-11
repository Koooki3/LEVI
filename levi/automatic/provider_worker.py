"""Bounded file worker for real scene/reset predicates, never a motion worker.

``check`` is pure-file validation. ``run`` requires an already mounted shared
online Judge; it neither starts a model nor creates an independent GPU path.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path

from levi.domain import aeri

from . import provider_model as pm
from .journal import process_identity

MAX_REQUEST_BYTES = 256 * 1024
MAX_FRAME_BYTES = 16 * 1024 * 1024


def directory(path):
    """Open every component without symlinks; final directory must be ours."""
    path = Path(path).absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            nxt = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = nxt
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError("worker directory belongs to another identity")
        return fd
    except BaseException:
        os.close(fd)
        raise


def relative(path):
    path = Path(path)
    if (
        path.is_absolute()
        or not path.parts
        or any(x in ("", ".", "..") for x in path.parts)
    ):
        raise ValueError("bounded relative path required")
    return path.parts


def parent_at(rootfd, name, *, create=False):
    parts = relative(name)
    fd = os.dup(rootfd)
    try:
        for part in parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
            nxt = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            if os.fstat(nxt).st_uid != os.getuid():
                os.close(nxt)
                raise ValueError("worker subtree belongs to another identity")
            os.close(fd)
            fd = nxt
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def read_at(rootfd, name, limit):
    parent, leaf = parent_at(rootfd, name)
    fd = None
    try:
        fd = os.open(
            leaf,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or not 0 < info.st_size <= limit
        ):
            raise ValueError("bounded regular owned file required")
        chunks, size = [], 0
        while chunk := os.read(fd, min(65536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("file exceeds byte budget")
        after = os.fstat(fd)
        if size != info.st_size or (
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("file changed during bounded read")
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def write_at(rootfd, name, data):
    parent, leaf = parent_at(rootfd, name, create=True)
    temp = "." + leaf + "." + uuid.uuid4().hex + ".tmp"
    fd = None
    try:
        fd = os.open(
            temp,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temp, leaf, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=parent)
        os.close(parent)


def load_spec(path):
    path = Path(path).absolute()
    fd = directory(path.parent)
    try:
        return pm.FrozenSpec.load(
            pm.strict_json(read_at(fd, path.name, MAX_REQUEST_BYTES))
        )
    finally:
        os.close(fd)


class Worker:
    def __init__(self, root, spec, evidence_root, channel):
        self.root, self.spec = Path(root).absolute(), spec
        self.evidence_root = Path(evidence_root).absolute()
        self.fd = directory(self.root)
        try:
            self.evidence_fd = directory(self.evidence_root)
        except BaseException:
            os.close(self.fd)
            raise
        self.channel = channel
        self.stop = threading.Event()
        self.lockfd = None
        self.heartbeat = None

    def __enter__(self):
        self.lockfd = os.open(
            "worker.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            info = os.fstat(self.lockfd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise ValueError("unsafe worker identity lock")
            try:
                fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another provider worker is alive") from None
        except BaseException:
            os.close(self.lockfd)
            self.lockfd = None
            os.close(self.fd)
            os.close(self.evidence_fd)
            # No ownership: do not touch the live worker's READY.
            raise
        self.refresh_ready()

        def heartbeat():
            while not self.stop.wait(2):
                self.refresh_ready()

        self.heartbeat = threading.Thread(
            target=heartbeat, name="aeri-worker-heartbeat", daemon=True
        )
        self.heartbeat.start()
        return self

    def refresh_ready(self):
        """Only a successfully checked live channel publishes capabilities."""
        try:
            result = self.channel.probe(self.spec, self.stop)
            if (
                result.get("status") != "ok"
                or result.get("model_digest")
                != self.spec.value["provider"]["model_digest"]
            ):
                raise ValueError("model channel unavailable")
            contract = self.spec.contract
            row = {
                "schema": "levi.aeri.provider_ready.v1",
                "contract_id": contract.contract_id,
                "contract_version": contract.contract_version,
                "capabilities": ["scene", "reset_judgement"],
                "updated_wall_ns": time.time_ns(),
                "identity": process_identity(),
                "run_id": self.spec.value["run_id"],
                "plan_sha256": self.spec.value["plan_sha256"],
                "spec_sha256": self.spec.sha256,
                "model_digest": result["model_digest"],
            }
            if not self.stop.is_set():
                write_at(self.fd, "READY.json", pm.canonical(row))
            return True
        except Exception:  # noqa: BLE001 - any failed probe removes readiness
            with contextlib.suppress(FileNotFoundError):
                os.unlink("READY.json", dir_fd=self.fd)
            return False

    def unavailable(self, payload, reason, code="contract_violation"):
        request = payload["request"]
        kind = "scene" if payload["kind"] == "scene" else "judgement"
        value = {
            "schema": aeri.SCHEMAS[kind],
            "minor": aeri.MINOR,
            "run_id": request["run_id"],
            "emitted_wall_ns": time.time_ns(),
            "kind": "unavailable",
            "request_id": request["request_id"],
            "retryable": False,
            "code": code,
            "detail": str(reason)[:300],
            "produced_ns": time.monotonic_ns(),
            "clock_domain": aeri.host_clock_domain(),
        }
        return aeri.validate(value, kind).model_dump(mode="json", by_alias=True)

    def process(self, kind, name):
        if kind not in ("scene", "reset_judgement") or not __import__("re").fullmatch(
            r"[a-f0-9]{64}\.json", name
        ):
            raise ValueError("invalid worker request path")
        raw = read_at(self.fd, f"{kind}/requests/{name}", MAX_REQUEST_BYTES)
        payload = pm.strict_json(raw)
        if (
            payload.get("kind") != kind
            or hashlib.sha256(payload["request"]["request_id"].encode()).hexdigest()
            + ".json"
            != name
        ):
            raise ValueError("request path/identity mismatch")
        request_sha = hashlib.sha256(raw).hexdigest()
        intent = f"{kind}/processed/{name}"
        try:
            previous = pm.strict_json(read_at(self.fd, intent, MAX_REQUEST_BYTES))
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if (
                previous.get("request_sha256") != request_sha
                or previous.get("spec_sha256") != self.spec.sha256
            ):
                raise ValueError("request ID reused with changed content/spec")
            if previous.get("response_sha256"):
                response = read_at(
                    self.fd, f"{kind}/responses/{name}", MAX_REQUEST_BYTES
                )
                if hashlib.sha256(response).hexdigest() != previous["response_sha256"]:
                    raise ValueError("previous response was changed")
                return False
            # Never repeat an inference whose previous process ended ambiguously.
            result = self.unavailable(
                payload, "worker restarted with incomplete request", "provider_error"
            )
        else:
            write_at(
                self.fd,
                intent,
                pm.canonical(
                    {"request_sha256": request_sha, "spec_sha256": self.spec.sha256}
                ),
            )
            cancel = threading.Event()
            finished = threading.Event()

            def monitor():
                while not finished.wait(0.01):
                    try:
                        read_at(self.fd, f"{kind}/cancelled/{name}", MAX_REQUEST_BYTES)
                        cancel.set()
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError):
                        cancel.set()
                    if self.stop.is_set():
                        cancel.set()

            watcher = threading.Thread(
                target=monitor, name="aeri-worker-cancel", daemon=True
            )
            try:
                pm.validate_input(self.spec, payload, now_ns=time.monotonic_ns())
                if payload.get("evidence_root") != str(self.evidence_root):
                    raise ValueError("request evidence root is not authorized")
                images = []
                for frame in payload["frames"]:
                    image = read_at(self.evidence_fd, frame["file"], MAX_FRAME_BYTES)
                    if hashlib.sha256(image).hexdigest() != frame["sha256"]:
                        raise ValueError("retained frame hash mismatch")
                    images.append(image)
                # A prior cancellation is authoritative even before the model starts.
                try:
                    read_at(self.fd, f"{kind}/cancelled/{name}", MAX_REQUEST_BYTES)
                    cancel.set()
                except FileNotFoundError:
                    pass
                watcher.start()
                if cancel.is_set():
                    raise ValueError("request cancelled before inference")
                answer = self.channel.ask_model(self.spec, payload, images, cancel)
                pm.validate_input(self.spec, payload, now_ns=time.monotonic_ns())
                if cancel.is_set() or self.stop.is_set():
                    raise ValueError("request cancelled")
                # Re-read files before publishing: no changed evidence may grant readiness.
                for frame in payload["frames"]:
                    if (
                        hashlib.sha256(
                            read_at(self.evidence_fd, frame["file"], MAX_FRAME_BYTES)
                        ).hexdigest()
                        != frame["sha256"]
                    ):
                        raise ValueError("retained evidence changed during inference")
                if answer.get("status") != "ok" or "result" not in answer:
                    raise ValueError(answer.get("reason", "model channel unavailable"))
                result = answer["result"]
                message = pm.validate_result(
                    self.spec, payload, result, now_ns=time.monotonic_ns()
                )
                if message.model_digest != self.spec.value["provider"]["model_digest"]:
                    raise ValueError("response model mismatch")
            except (ValueError, TypeError, KeyError, OSError) as exc:
                result = self.unavailable(payload, exc)
            finally:
                finished.set()
                if watcher.ident:
                    watcher.join(timeout=1)
        response = pm.canonical(result)
        write_at(self.fd, f"{kind}/responses/{name}", response)
        write_at(
            self.fd,
            intent,
            pm.canonical(
                {
                    "request_sha256": request_sha,
                    "spec_sha256": self.spec.sha256,
                    "response_sha256": hashlib.sha256(response).hexdigest(),
                }
            ),
        )
        return True

    def poll(self):
        import heapq

        count = 0
        for kind in ("scene", "reset_judgement"):
            try:
                parent, _leaf = parent_at(self.fd, kind + "/requests/item", create=True)
                names = os.listdir(parent)
                os.close(parent)
                for name in heapq.nsmallest(1000, names):
                    if name.endswith(".json"):
                        try:
                            count += bool(self.process(kind, name))
                        except (ValueError, OSError, KeyError, TypeError):
                            # Corrupt/unsafe input is never interpreted as a successful judgement.
                            continue
            except OSError:
                continue
        return count

    def close(self):
        self.stop.set()
        if self.heartbeat is not None:
            self.heartbeat.join(timeout=4)
        with contextlib.suppress(FileNotFoundError):
            os.unlink("READY.json", dir_fd=self.fd)
        if self.lockfd is not None:
            os.close(self.lockfd)
            self.lockfd = None
        os.close(self.fd)
        os.close(self.evidence_fd)

    def __exit__(self, *_):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run"))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--root")
    parser.add_argument("--evidence-root")
    parser.add_argument("--online-url")
    args = parser.parse_args(argv)
    spec = load_spec(args.spec)
    if args.action == "check":
        print(
            json.dumps(
                {
                    "spec_sha256": spec.sha256,
                    "run_id": spec.value["run_id"],
                    "plan_sha256": spec.value["plan_sha256"],
                }
            )
        )
        return 0
    if not all((args.root, args.evidence_root, args.online_url)):
        parser.error("run requires root, evidence-root and online-url")
    from levi.live.aeri_online import HttpChannel

    with Worker(
        args.root, spec, args.evidence_root, HttpChannel(args.online_url)
    ) as worker:
        try:
            while not worker.stop.wait(0.02):
                worker.poll()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
