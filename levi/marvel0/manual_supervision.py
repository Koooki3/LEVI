"""Manual child mode of the existing run.Supervisor; no second owner/arm lock.

The explicitly enabled production factory starts a bound v1 robot session only
on a person action. Synthetic factories remain injectable for offline tests.
Browser current poses and preview-plan hashes never authorize motion.
"""

import hashlib
import json
import os
import secrets
import threading

from levi.live import jsonio

from .manual_worker import production_factory

SCHEMA = "levi.marvel0.manual.v1"
CONFIRM = "start synthetic translation session"
REAL_CONFIRM = "start real translation session"


class ManualSessions:
    def __init__(self, supervisor):
        self.sup = supervisor
        self.engine = None
        self.fd = None
        self.began = None
        self.generation = 0
        self.preparing = None
        self.start_request_id = None
        self._receipts = {}
        self._receipt_lock = threading.Lock()

    def _refuse(self, code):
        from .run import Refuse

        raise Refuse("manual_refused", reason_code=code)

    def _audit(self, event, **fields):
        # Unlike hosted-run best-effort history, manual release requires its
        # audit callback to succeed. Stop has already revoked before it writes.
        from . import record
        from .run import HISTORY_MAX_BYTES

        if self.sup._dir is None:
            raise OSError("manual audit unavailable")
        jsonio.append_line(
            self.sup._dir / record.HISTORY,
            {
                "time": self.sup._iso(),
                "actor": "person",
                "via": "manual_real"
                if self.sup.host.manual_factory is production_factory
                else "manual_synthetic",
                "event": event,
                **fields,
            },
            max_bytes=HISTORY_MAX_BYTES,
            keep=1,
        )

    def _gate(self):
        # This comparison is not an authorization boundary against same-account
        # Python edits. It prevents an accidental real enable through site/UI.
        h = self.sup.host
        if h.manual_factory is production_factory and not h.manual_enabled():
            return "stop_unproven"
        if not h.manual_enabled():
            return "manual_disabled"
        if self.sup._profile()["profile"] != "real":
            return "not_product"
        return None

    def active(self):
        # Called with the parent's RLock; retained on unknown delivery/stop.
        if self.preparing is None and self.fd is None:
            return None
        return {
            "kind": "marvel0-manual",
            "id": self.preparing["session_id"]
            if self.preparing
            else self.engine.view()["session_id"],
            "running_seconds": max(0, round(self.sup.host.now() - self.began)),
        }

    def view(self):
        with self.sup._mutex:
            if self.preparing is not None:
                intent = self.preparing
                return {
                    "schema": SCHEMA,
                    "session_id": intent["session_id"],
                    "generation": intent["generation"],
                    "start_request_id": intent["request_id"],
                    "anchor_id": None,
                    "anchor": None,
                    "state": "starting",
                    "reason_code": "start_cancelled"
                    if intent["cancelled"]
                    else "preparing",
                    "admission": "closed",
                    "stop_evidence": "unproven",
                    "environment": "production"
                    if self.sup.host.manual_factory is production_factory
                    else "synthetic",
                    "capabilities": {
                        "can_start": False,
                        "real_motion": self.sup.host.manual_factory
                        is production_factory,
                        "synthetic_only": self.sup.host.manual_factory
                        is not production_factory,
                        "gripper": False,
                    },
                }
            if self.engine is not None:
                body = self.engine.view()
                body["start_request_id"] = self.start_request_id
                body["environment"] = (
                    "production"
                    if body["capabilities"].get("real_motion")
                    else "synthetic"
                )
                body["capabilities"]["can_start"] = (
                    self.fd is None and self._gate() is None
                )
                return body
            gate = self._gate()
            return {
                "schema": SCHEMA,
                "state": "unavailable" if gate else "idle",
                "reason_code": gate,
                "session_id": None,
                "start_request_id": None,
                "generation": None,
                "anchor_id": None,
                "anchor": None,
                "admission": "closed",
                "stop_evidence": "unproven",
                "environment": "production"
                if self.sup.host.manual_factory is production_factory
                else "synthetic",
                "capabilities": {
                    "can_start": gate is None,
                    "real_motion": self.sup.host.manual_factory is production_factory
                    and gate is None,
                    "synthetic_only": self.sup.host.manual_factory
                    is not production_factory,
                    "gripper": self.sup.host.manual_factory is production_factory
                    and gate is None,
                },
            }

    @staticmethod
    def _binding(body):
        data = json.dumps(
            {"limits": body["limits"], "stop_limits": body.get("stop_limits")},
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {"manual_limits": hashlib.sha256(data.encode()).hexdigest()}

    def _require_idle(self, aeri_row):
        gate = self._gate()
        if gate:
            self._refuse(gate)
        if self.sup.active() is not None or self.sup._preflighting:
            self._refuse("owner_busy")
        # Hosted D owns admission before its phrase takes the arm flock too.
        # Reuse the evaluation guard: an unreadable AERI state is not idle.
        if aeri_row["level"] != "ok":
            self._refuse("owner_busy")

    def challenge(self, body):
        gate = self._gate()
        if gate:
            self._refuse(gate)
        self.sup.adopt()
        # AERI's view may ask for our active owner while holding its mutex.
        # Never call across supervisors with our state mutex held.
        aeri_row = self.sup._aeri_row()
        with self.sup._mutex:
            self._require_idle(aeri_row)
            issued = self.sup.challenges.issue("manual_start", self._binding(body))
            return {
                "schema": SCHEMA,
                "confirm": REAL_CONFIRM
                if self.sup.host.manual_factory is production_factory
                else CONFIRM,
                **issued,
            }

    def start(self, body):
        sup = self.sup
        request_id = sup._request_id(body)
        expected_confirm = (
            REAL_CONFIRM
            if sup.host.manual_factory is production_factory
            and sup.host.manual_enabled()
            else CONFIRM
        )
        if body.get("confirm") != expected_confirm:
            self._refuse("confirm_required")
        attest = body.get("attest") or {}
        if not all(
            attest.get(key) is True for key in ("estop", "scene_reset", "area_clear")
        ):
            self._refuse("attest_required")
        # Production refuses before creating folders, locks or request records.
        gate = self._gate()
        if gate:
            self._refuse(gate)
        if sup.host.manual_factory is production_factory:
            from .manual import ManualRefused
            from .manual_transport import stop_limits

            try:
                stop_limits(body.get("stop_limits"))
            except ManualRefused as exc:
                self._refuse(exc.code)
        sup.adopt()
        directory = sup._folder(sup.site_loader(), create=True)
        sup._dir = directory
        return sup._transaction(
            "manual_start",
            directory,
            request_id,
            body,
            lambda sent: self._start(body, sent),
        )

    def _start(self, body, mark_sent):
        from .manual import ManualRefused
        from .run import take_arm_lock

        sup = self.sup
        # _transaction already holds the run lock shared with AERI/E starts.
        # Recheck D outside our state mutex, then reserve the local owner.
        aeri_row = sup._aeri_row()
        with sup._mutex:
            self._require_idle(aeri_row)
            sup.challenges.consume(
                body.get("challenge"), "manual_start", self._binding(body)
            )
            self.generation += 1
            intent = {
                "session_id": secrets.token_urlsafe(16),
                "generation": self.generation,
                "cancelled": False,
                "request_id": body["request_id"],
            }
            self.preparing, self.began = intent, sup.host.now()
        fd = None
        try:
            # No callback or disk operation holds the parent state mutex. Stop
            # can revoke this identity even before a factory has returned it.
            fd = take_arm_lock(sup.host.arm_lock_path())
            with sup._mutex:
                self.fd = fd
            mark_sent()
            with sup._mutex:
                cancelled = intent["cancelled"]
            if cancelled:
                # No worker has been constructed: no command could have escaped.
                with sup._mutex:
                    os.close(fd)
                    self.fd, self.preparing = None, None
                self._refuse("start_cancelled")
            engine = sup.host.manual_factory(
                {
                    "session_id": intent["session_id"],
                    "generation": intent["generation"],
                    "limits": body["limits"],
                    "audit": self._audit,
                    **(
                        {
                            "stop_limits": body.get("stop_limits"),
                            "robot_server_url": sup.site_loader().server.robot_server_url,
                            "now": sup.host.monotonic,
                        }
                        if sup.host.manual_factory is production_factory
                        else {}
                    ),
                }
            )
            if (
                sup.host.manual_factory is not production_factory
                and engine.view()["capabilities"].get("real_motion") is not False
            ):
                engine.stop(reason="synthetic_required")
                self._refuse("synthetic_required")
        except ManualRefused as exc:
            with sup._mutex:
                if self.fd is not None:
                    os.close(self.fd)
                self.fd, self.preparing = None, None
            self._refuse(exc.code)
        except BaseException:
            # No engine has been admitted or pumped. A factory exception in this
            # synthetic-only boundary cannot be mistaken for a reusable session.
            with sup._mutex:
                if self.fd is not None:
                    os.close(self.fd)
                self.fd, self.preparing = None, None
            raise
        with sup._mutex:
            cancelled = intent["cancelled"]
            if not cancelled:
                self.engine, self.preparing = engine, None
                self.start_request_id = intent["request_id"]
                with self._receipt_lock:
                    self._receipts = {}
        if cancelled:
            # Keep the preparing identity visible (target/pump refused) until
            # late construction is revoked; never publish it briefly open.
            close_error = None
            try:
                engine.stop(reason="start_cancelled")
            except ManualRefused as exc:
                close_error = exc.code
            finally:
                with sup._mutex:
                    self.engine, self.preparing = engine, None
                    self.start_request_id = intent["request_id"]
                    with self._receipt_lock:
                        self._receipts = {}
            if close_error:
                self._refuse(close_error)
        try:
            self._audit(
                "manual_start",
                session_id=intent["session_id"],
                generation=intent["generation"],
            )
        except Exception:  # noqa: BLE001 - any audit failure must close/refuse
            try:
                engine.stop(reason="audit_failed")
            except ManualRefused:
                pass  # admission was closed before the failed audit callback
            self._refuse("audit_failed")
        if sup.host.background and not cancelled:
            threading.Thread(
                target=self._watch,
                args=(engine,),
                daemon=True,
                name="levi-manual-session",
            ).start()
        return self.view()

    def _engine(self, body):
        with self.sup._mutex:
            engine = self.engine
            if self.preparing is not None:
                self._refuse("preparing")
            if engine is None:
                self._refuse("no_manual_session")
            view = engine.view()
            if (
                body.get("session_id") != view["session_id"]
                or body.get("generation") != view["generation"]
            ):
                self._refuse("session_mismatch")
            return engine

    def _call(self, body, method, **kwargs):
        from .manual import ManualRefused
        from .record import fingerprint

        request_id = self.sup._request_id(body)
        engine = self._engine(body)
        key = (body["session_id"], request_id)
        stamp = fingerprint("manual_" + method, body)
        # Memory receipts never wait on disk, a factory, or a send. No eviction
        # can turn an old request into a fresh action; a full window closes this
        # synthetic session instead. Restart voids identity/challenge authority.
        with self._receipt_lock:
            receipts = self._receipts
            prior = receipts.get(key)
            if prior is not None:
                if prior["fingerprint"] != stamp:
                    self._refuse("request_id_used")
                if prior.get("error"):
                    self._refuse(prior["error"])
                if "answer" in prior:
                    return {**prior["answer"], "replayed": True}
                self._refuse("request_in_flight")
            full = len(receipts) >= 4096
            if not full:
                receipts[key] = {"fingerprint": stamp}
        if full:
            try:
                engine.stop(reason="receipt_limit")
            except ManualRefused as exc:
                self._refuse(exc.code)
            self._refuse("receipt_limit")
        try:
            getattr(engine, method)(**kwargs)
            answer = self.view()
        except ManualRefused as exc:
            with self._receipt_lock:
                receipts[key]["error"] = exc.code
            self._refuse(exc.code)
        with self._receipt_lock:
            receipts[key]["answer"] = answer
        return answer

    def target(self, body):
        return self._call(
            body,
            "target",
            **{
                k: body[k]
                for k in (
                    "session_id",
                    "generation",
                    "anchor_id",
                    "sequence",
                    "delta_position_m",
                )
            },
        )

    def gripper(self, body):
        return self._call(
            body,
            "gripper",
            **{
                k: body[k]
                for k in ("session_id", "generation", "sequence", "opening_ratio")
            },
        )

    def deadman(self, body):
        return self._call(
            body,
            "deadman",
            **{k: body[k] for k in ("session_id", "generation", "sequence", "pressed")},
        )

    def stop(self, body):
        # Does not take the request/start/run guard or wait for pump/send.
        self.sup._request_id(body)
        with self.sup._mutex:
            intent = self.preparing
            if intent is not None:
                if (
                    body.get("session_id") != intent["session_id"]
                    or body.get("generation") != intent["generation"]
                ):
                    self._refuse("session_mismatch")
                intent["cancelled"] = True
                return self.view()
        engine = self._engine(body)
        from .manual import ManualRefused

        try:
            engine.stop(reason="person")
        except ManualRefused as exc:
            self._refuse(exc.code)
        answer = self.view()
        try:
            self._audit("manual_stop", session_id=body["session_id"])
        except Exception:  # noqa: BLE001 - any audit failure must close/refuse
            self._refuse("audit_failed")
        return answer

    def end(self, body):
        from .manual import ManualRefused

        self.sup._request_id(body)
        engine = self._engine(body)
        try:
            engine.stop(reason="end_requested")
            evidence = (
                engine.physical_evidence()
                if engine.view()["capabilities"].get("real_motion")
                else self.sup.host.manual_settled(engine)
            )
            engine.end(evidence)
        except ManualRefused as exc:
            self._refuse(exc.code)
        try:
            self._audit("manual_end", session_id=body["session_id"])
        except Exception:  # noqa: BLE001 - any audit failure must close/refuse
            self._refuse("audit_failed")
        with self.sup._mutex:
            if self.engine is engine and self.fd is not None:
                os.close(self.fd)
                self.fd = None
        return self.view()

    def tick(self):
        with self.sup._mutex:
            engine = (
                self.engine if self.fd is not None and self.preparing is None else None
            )
        if engine is None:
            return False
        engine.tick()
        engine.pump()
        return True

    def _watch(self, engine):
        # Synthetic-only; production cannot reach this thread. The engine
        # invalidates deadlines independently of incoming page messages.
        while True:
            with self.sup._mutex:
                if self.engine is not engine or self.fd is None:
                    return
            engine.tick()
            engine.pump()
            self.sup.host.sleep(engine.limits.period_s)
