"""Explicit adapter for a frozen, exclusive vLLM synchronized pause endpoint.

No endpoint is defaulted or contacted at import/construction. The composition
must verify the installed implementation hash, version, single engine, exclusive
request ingress and port owner independently before supplying its capability.
HTTP disconnect, abort_requests, is_paused and sleep are not idle barriers.
"""

from levi.inference.transport import OpenAILocalTransport
from levi.live.runtime_bridge import BackendAck, BackendCapability


class _BarrierTransport(OpenAILocalTransport):
    paths = frozenset(
        {
            "/pause?mode=wait&clear_cache=false",
            "/pause?mode=abort&clear_cache=false",
            "/resume",
        }
    )

    def acknowledged(self, path):
        # These utility endpoints can return an empty 200 response. _chunks
        # still enforces pinned loopback, status, byte and elapsed-time bounds.
        chunks = self._chunks("POST", path, None)
        try:
            for _chunk in chunks:
                pass
        finally:
            chunks.close()


class VllmPauseBackend:
    def __init__(self, config, capability, *, transport_factory=_BarrierTransport):
        if not isinstance(capability, BackendCapability):
            raise TypeError("frozen exclusive backend capability required")
        self.config, self.capability = config, capability
        self.transport_factory = transport_factory

    def _call(self, path, timeout_s):
        transport = self.transport_factory(
            self.config,
            timeout=timeout_s,
            max_response_bytes=4096,
        )
        transport.acknowledged(path)

    def pause(self, worker, request_epoch, mode, timeout_s):
        if mode not in ("abort", "wait"):
            raise ValueError("only synchronized abort/wait pauses are barriers")
        self._call(f"/pause?mode={mode}&clear_cache=false", timeout_s)
        # Epoch is attached at the receiver under the bridge lifecycle mutex;
        # exact driver/flock/endpoint incarnation is checked before AND after.
        return BackendAck(worker, request_epoch, self.capability, mode)

    def resume(self, worker, request_epoch, timeout_s):
        self._call("/resume", timeout_s)
