"""Opt-in owned model execution using the original broker/role authority.

No hardware hold, paused-camera or role window is fabricated here. A caller
must supply independently authorized fresh windows and physical evidence to
its existing inference cycle. No default launch or external-server adoption.
"""

import threading
import time

from levi.live.runtime_ownership import RequestStopProof


class OwnedRoleRuntime:
    """Software execution companion, not another scheduler.

    authority is the one RuntimeOwnership already bound to the physical
    broker. observer supplies actual owned group evidence. Tokens returned
    here can be used by the existing inference cycle; it must not create a
    second RuntimeOwnership or begin a second request for that same action.
    """

    def __init__(self, authority, runtime, lease, observer, *, clock=time.monotonic_ns):
        if (
            authority.broker is None
            or runtime.lease_id != lease.lease_id
            or runtime.lease_epoch != lease.lease_epoch
        ):
            raise ValueError("original broker admission and runtime token required")
        self.authority, self.runtime, self.lease = authority, runtime, lease
        self.observer, self.clock = observer, clock
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._active = None
        self._collected = None
        for worker in observer.workers.values():
            worker.bind_authority(authority, observer)

    def begin(self, role, deadline, window, payload, *, episode_id, policy_epoch):
        with self._lock:
            if self._closed.is_set() or self._active is not None:
                raise ValueError("owned role runtime closed or request unresolved")
            request = self.authority.begin_role_request(
                self.runtime,
                role,
                deadline,
                self.observer.observe(self.lease),
                window,
                episode_id=episode_id,
                policy_epoch=policy_epoch,
                now_ns=self.clock(),
            )
            # Record authority before dispatch: unknown send cannot clear it.
            self._active = request
            worker = self.observer.workers[role]
            try:
                worker.submit(request, payload)
            except Exception:
                self.authority.revoke_request(request, now_ns=self.clock())
                raise
            return request

    def collect(self, request, *, timeout_s=1.0):
        with self._lock:
            if request != self._active:
                raise ValueError("stale or foreign owned role request")
            try:
                if self._collected is None:
                    result = self.observer.workers[request.role].collect(
                        timeout_s=timeout_s
                    )
                    self._collected = (request, result)
                result = self._collected[1]
                observation = self.observer.observe(self.lease)
            except TimeoutError:
                raise  # a bounded poll is not an affirmative idle/result proof
            except Exception:
                self.authority.revoke_request(request, now_ns=self.clock())
                raise  # unknown ACK/probe retains the original slot and withdraws result
            if self._closed.is_set():
                self.authority.revoke_request(request, now_ns=self.clock())
            permitted = self.authority.finish_request(
                request,
                RequestStopProof(
                    request,
                    observation.observed_ns,
                    observation.clock_domain,
                    True,
                    True,
                ),
                observation,
                now_ns=self.clock(),
            )
            self._active = None
            self._collected = None
            return result if permitted and not self._closed.is_set() else None

    def renew(self, deadline, pressure, window):
        with self._lock:
            if self._closed.is_set() or self._active is not None:
                raise ValueError("closed or active owned role runtime cannot renew")
            self.runtime = self.authority.renew_resident(
                self.runtime,
                deadline,
                pressure,
                window,
                self.observer.observe(self.lease),
                now_ns=self.clock(),
            )
            self.lease = self.authority._lease
            return self.runtime

    def revoke(self, request):
        # The original authority validates the exact token and retains its
        # slot. No model/collection mutex is taken; stale tokens cannot close
        # a newer worker. A cancelled, proven-idle request can later drain.
        return self.authority.revoke_request(request, now_ns=self.clock())

    def close(self):
        # Supervisor stop withdraws all ingress without waiting for model work.
        self._closed.set()
        for worker in self.observer.workers.values():
            worker.close_ingress()
        request = self._active
        if request is not None:
            try:
                self.authority.revoke_request(request, now_ns=self.clock())
            except ValueError:
                pass  # a concurrently finished token cannot revoke a newer one
