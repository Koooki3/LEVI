"""Predicate requests using the existing online Judge's ownership and GPU gate.

Only specs explicitly registered on this Judge are served. Deadline cancellation
returns promptly but keeps the Judge busy and its GPU admission until the actual
model call exits; a transport that ignores cancellation cannot overlap another call.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import ipaddress
import math
import threading
import time
import uuid
from urllib.parse import urlsplit

from levi.automatic import provider_model as pm

POLL_S = 0.01


def register(judge, spec):
    if not isinstance(spec, pm.FrozenSpec):
        spec = pm.FrozenSpec.load(spec)
    if not judge._busy.acquire(blocking=False):
        raise ValueError("cannot change registered specifications during a model call")
    try:
        judge._aeri_specs[spec.sha256] = spec
    finally:
        judge._busy.release()
    return spec.sha256


def query(judge, spec_sha256, payload, images, *, cancel=None, probe=False, ask=None):
    spec = judge._aeri_specs.get(spec_sha256)
    if spec is None:
        return {
            "status": "unavailable",
            "reason": "frozen specification not registered",
        }
    cancel = cancel or threading.Event()
    now = time.monotonic_ns()
    try:
        due = (
            now + min(spec.value["deadlines_ns"].values())
            if probe
            else pm.validate_input(spec, payload, now_ns=now)
        )
        if not probe:
            if len(images) != len(payload["frames"]):
                raise ValueError("current image count mismatch")
            for frame, image in zip(payload["frames"], images, strict=True):
                if (
                    not isinstance(image, bytes)
                    or not 0 < len(image) <= 16 * 1024 * 1024
                    or not image.startswith(b"\xff\xd8")
                    or not image.endswith(b"\xff\xd9")
                    or hashlib.sha256(image).hexdigest() != frame["sha256"]
                ):
                    raise ValueError("current JPEG hash/format mismatch")
    except (ValueError, TypeError, KeyError) as exc:
        return {"status": "unavailable", "reason": str(exc)}
    if cancel.is_set() or now >= due:
        return {"status": "unavailable", "reason": "cancelled or expired"}
    if not judge._busy.acquire(blocking=False):
        return {"status": "unavailable", "reason": "busy"}
    # Own cancellation is registered before admission; C5 abort sees this owner.
    owner = "live-aeri:" + uuid.uuid4().hex
    judge._owner, judge._stop_reason = owner, None
    from levi.inference import transport

    done, box = threading.Event(), {}
    folder = judge.config.live_dir / "aeri-online-tmp" / owner.split(":", 1)[1]
    # The actual callable receives the same abort event used by the monitor.
    aborted = threading.Event()

    def work():
        admitted = False
        try:
            with transport.requests_of(owner):
                if (
                    aborted.is_set()
                    or cancel.is_set()
                    or judge._stop_reason
                    or time.monotonic_ns() >= due
                ):
                    raise ValueError("cancelled before model access")
                # Admission can wake an existing server and block. Monitor its
                # time in the same deadline, not before the watchdog begins.
                ok, code, why = judge.gpu.online_admit(time.time())
                if not ok:
                    raise ValueError(f"{code}: {why}")
                admitted = True
                if aborted.is_set() or cancel.is_set() or time.monotonic_ns() >= due:
                    raise ValueError("cancelled during GPU admission")
                config = judge.provider()
                expected = spec.value["provider"]
                actual = config.model_dump(mode="json")
                # Compare every declared frozen profile field, not only the name.
                if any(actual.get(k) != v for k, v in expected.items()):
                    raise ValueError(
                        "served model/profile differs from the frozen model"
                    )
                if not probe:
                    folder.mkdir(parents=True, exist_ok=False)
                    names = []
                    for n, image in enumerate(images):
                        name = f"{n:02d}.jpg"
                        (folder / name).write_bytes(image)
                        names.append(name)
                    if (
                        aborted.is_set()
                        or cancel.is_set()
                        or time.monotonic_ns() >= due
                    ):
                        raise ValueError("cancelled before inference")
                    if ask is None:
                        from levi.agent.schema import Budget
                        from levi.inference.provider import LocalProvider

                        raw, usage = LocalProvider().ask(
                            config,
                            pm.question(spec, payload),
                            names,
                            folder,
                            pm.answer_schema(),
                            spec.value["max_output_tokens"],
                            Budget(
                                max_calls=1,
                                max_tokens=config.context_tokens,
                                max_seconds=max(
                                    10, math.ceil((due - time.monotonic_ns()) / 1e9)
                                ),
                            ),
                        )
                    else:
                        raw, usage = ask(spec, payload, folder, names, aborted)
                    if (
                        aborted.is_set()
                        or cancel.is_set()
                        or time.monotonic_ns() >= due
                    ):
                        raise ValueError("answer arrived after cancellation/deadline")
                    box["result"] = pm.model_result(
                        spec, payload, raw, produced_ns=time.monotonic_ns()
                    )
                    box["usage"] = usage
                    prompt = usage.get("prompt_tokens")
                    total = usage.get("reported_tokens")
                    completion = (
                        total - prompt
                        if type(total) is int
                        and type(prompt) is int
                        and total >= prompt >= 0
                        else None
                    )
                    box["result"]["cost"] = {
                        "elapsed_ms": (time.monotonic_ns() - now) // 1_000_000,
                        "prompt_tokens": prompt
                        if type(prompt) is int and prompt >= 0
                        else None,
                        "completion_tokens": completion,
                    }
                box["model_digest"] = config.model_digest
        except Exception as exc:  # noqa: BLE001 - contain every provider failure
            box["error"] = str(exc)[:300]
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)
            judge._owner = None
            try:
                if admitted:
                    judge.gpu.online_done()
            finally:
                judge._busy.release()
                done.set()

    thread = threading.Thread(target=work, name="live-aeri-ask", daemon=True)
    try:
        thread.start()
    except BaseException:
        judge._owner = None
        judge._busy.release()
        raise
    reason = None
    while not done.wait(POLL_S):
        if cancel.is_set() or judge._stop_reason:
            reason = "cancelled"
        elif time.monotonic_ns() >= due:
            reason = "timeout"
        else:
            try:
                ok, code, why = judge.gpu.online_check(time.time())
                if not ok:
                    reason = f"{code}: {why}"
            except Exception:  # noqa: BLE001 - unknown gate state blocks admission
                reason = "gate_check_failed"
        if reason:
            aborted.set()
            transport.abort(owner)
            # Locks remain owned until work() finally actually exits.
            return {"status": "unavailable", "reason": reason}
    if cancel.is_set() or time.monotonic_ns() >= due:
        return {"status": "unavailable", "reason": "cancelled or expired"}
    try:
        ok, code, why = judge.gpu.online_check(time.time())
    except Exception:  # noqa: BLE001 - unknown gate state blocks success
        ok, code, why = False, "gate_check_failed", "unknown"
    if not ok:
        return {"status": "unavailable", "reason": f"{code}: {why}"}
    if "error" in box:
        return {"status": "unavailable", "reason": box["error"]}
    return {"status": "ok", **box}


class SharedChannel:
    """In-process worker mount on an explicitly registered existing Judge."""

    def __init__(self, judge, *, ask=None):
        self.judge, self.ask = judge, ask

    def probe(self, spec, cancel=None):
        return query(
            self.judge, spec.sha256, None, [], cancel=cancel, probe=True, ask=self.ask
        )

    def ask_model(self, spec, payload, images, cancel):
        return query(
            self.judge, spec.sha256, payload, images, cancel=cancel, ask=self.ask
        )


def handle(judge, raw):
    """The new loopback route; accepts no caller-supplied prompt or spec."""
    value = pm.strict_json(raw)
    pm.exact(
        value,
        ("spec_sha256", "payload", "images", "token", "probe"),
        "online AERI request",
    )
    token = value["token"]
    if not isinstance(token, str) or not __import__("re").fullmatch(
        r"[a-f0-9]{32}", token
    ):
        raise ValueError("invalid cancellation identity")
    if type(value["probe"]) is not bool:
        raise ValueError("invalid probe flag")
    if not isinstance(value["images"], list) or len(value["images"]) > 32:
        raise ValueError("bounded image list required")
    images = [base64.b64decode(image, validate=True) for image in value["images"]]
    event = threading.Event()
    with judge._aeri_cancel_lock:
        if token in judge._aeri_cancel:
            raise ValueError("duplicate cancellation identity")
        now = time.monotonic()
        judge._aeri_cancelled = {
            k: t for k, t in judge._aeri_cancelled.items() if now - t < 30
        }
        if token in judge._aeri_cancelled:
            event.set()
        judge._aeri_cancel[token] = event
    try:
        return query(
            judge,
            value["spec_sha256"],
            value["payload"],
            images,
            cancel=event,
            probe=value["probe"],
        )
    finally:
        with judge._aeri_cancel_lock:
            judge._aeri_cancel.pop(token, None)


def cancel_request(judge, raw):
    value = pm.strict_json(raw)
    pm.exact(value, ("token",), "online AERI cancellation")
    token = value["token"]
    if not isinstance(token, str) or not __import__("re").fullmatch(
        r"[a-f0-9]{32}", token
    ):
        raise ValueError("invalid cancellation identity")
    with judge._aeri_cancel_lock:
        if len(judge._aeri_cancelled) >= 1024:
            judge._aeri_cancelled.pop(next(iter(judge._aeri_cancelled)))
        judge._aeri_cancelled[token] = time.monotonic()
        event = judge._aeri_cancel.get(token)
        if event is not None:
            event.set()
    return {"cancelled": event is not None}


class HttpChannel:
    """Existing numeric loopback endpoint only; no proxies, redirects or retries."""

    def __init__(self, url):
        target = urlsplit(url)
        if (
            target.scheme != "http"
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path not in ("", "/")
            or not ipaddress.ip_address(target.hostname).is_loopback
            or target.port
            in (5000, 5001, 5100, 7470, 8000, 7860, 7861, 7880, 7881, 8100)
        ):
            raise ValueError(
                "only the explicitly configured online Judge endpoint is allowed"
            )
        self.host, self.port = target.hostname, target.port

    def _post(self, path, value, timeout):
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=max(0.01, timeout)
        )
        try:
            connection.request(
                "POST",
                path,
                body=pm.canonical(value),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(body) > 1024 * 1024:
                raise ValueError("online provider transport refused or oversized")
            return pm.strict_json(body)
        finally:
            connection.close()

    def _call(self, spec, payload, images, cancel, probe):
        cancel = cancel or threading.Event()
        token = uuid.uuid4().hex
        due = (
            time.monotonic_ns() + min(spec.value["deadlines_ns"].values())
            if probe
            else payload["request"]["deadline_due_ns"]
        )
        if cancel.is_set() or time.monotonic_ns() >= due:
            return {"status": "unavailable", "reason": "cancelled or expired"}
        box, done = {}, threading.Event()

        def work():
            try:
                box["value"] = self._post(
                    "/v1/aeri/provider",
                    {
                        "spec_sha256": spec.sha256,
                        "payload": payload,
                        "images": [
                            base64.b64encode(image).decode() for image in images
                        ],
                        "token": token,
                        "probe": probe,
                    },
                    (due - time.monotonic_ns()) / 1e9,
                )
            except Exception as exc:  # noqa: BLE001 - transport failure is unavailable
                box["value"] = {"status": "unavailable", "reason": str(exc)[:200]}
            finally:
                done.set()

        threading.Thread(target=work, name="aeri-channel", daemon=True).start()
        while not done.wait(POLL_S):
            if cancel.is_set() or time.monotonic_ns() >= due:
                with contextlib.suppress(Exception):
                    self._post("/v1/aeri/cancel", {"token": token}, 0.2)
                return {"status": "unavailable", "reason": "cancelled or expired"}
        return box["value"]

    def probe(self, spec, cancel=None):
        return self._call(spec, None, [], cancel, True)

    def ask_model(self, spec, payload, images, cancel):
        return self._call(spec, payload, images, cancel, False)
