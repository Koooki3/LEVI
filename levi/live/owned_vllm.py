"""Owned private-IPC vLLM SDK adapter; no HTTP or external-server attach.

Installed synchronized-pause implementation and the model factory must be
independently frozen. A private single-engine AsyncLLM is created ONLY inside
the launcher-created contained worker. Missing code/version/containment fails
closed. Existing RuntimeOwnership remains the sole execution scheduler.
"""

import asyncio
import hashlib
import importlib
import importlib.metadata
from dataclasses import dataclass
from pathlib import Path

from levi.live.owned_runtime import (
    FrozenTorchFactory,
    SdkIdleAck,
    _cpu_payload,
    _receive,
    _send,
    process_identity,
)

# Include both device RPC dispatch and the receiving worker implementation.
BARRIER_FILES = (
    "v1/engine/async_llm.py",
    "v1/engine/core.py",
    "v1/engine/core_client.py",
    "v1/worker/worker_base.py",
    "v1/worker/gpu_worker.py",
    "v1/executor/abstract.py",
    "v1/executor/multiproc_executor.py",
    "v1/executor/uniproc_executor.py",
)


@dataclass(frozen=True)
class FrozenVllmFactory(FrozenTorchFactory):
    """torch_version also pins the device synchronization SDK used by vLLM."""

    vllm_version: str
    barrier_hashes: tuple[tuple[str, str], ...]

    def __post_init__(self):
        super().__post_init__()
        if (
            not self.vllm_version
            or tuple(name for name, _ in self.barrier_hashes) != BARRIER_FILES
        ):
            raise ValueError("complete ordered reviewed vLLM barrier manifest required")
        import re

        if any(
            not re.fullmatch(r"[0-9a-f]{64}", value) for _, value in self.barrier_hashes
        ):
            raise ValueError("frozen installed barrier hashes required")

    def load(self):
        distribution = importlib.metadata.distribution("vllm")
        if distribution.version != self.vllm_version:
            raise ValueError("installed vLLM differs from frozen SDK")
        root = Path(distribution.locate_file("vllm"))
        for relative, expected in self.barrier_hashes:
            if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
                raise ValueError("installed vLLM idle-barrier implementation changed")
        factory = self.reviewed_factory()
        import torch
        from vllm.v1.engine.async_llm import AsyncLLM

        if (
            str(torch.__version__) != self.torch_version
            or not torch.cuda.is_available()
        ):
            raise ValueError("frozen accelerator SDK unavailable")
        engine = factory(device=self.device)
        if type(engine) is not AsyncLLM:
            raise TypeError("factory must create the frozen private AsyncLLM engine")
        parallel = engine.vllm_config.parallel_config
        if any(
            getattr(parallel, key, None) != 1
            for key in (
                "tensor_parallel_size",
                "pipeline_parallel_size",
                "data_parallel_size",
            )
        ):
            raise ValueError("only one frozen engine/device topology is supported")
        if getattr(engine.vllm_config, "kv_transfer_config", None) is not None:
            raise ValueError("external KV connector cannot share owned barrier")
        return engine


def serve_vllm(connection, factory, launch_id):
    """Actual SDK pause Future/device RPC plus exhausted local generate task.

    Persistent engine/output-handler tasks are resident infrastructure. There
    is exactly one local generate coroutine, awaited to exhaustion before pause
    ACK. The private command channel is the only generation/resume ingress.
    """

    async def run():
        import os

        if await asyncio.to_thread(_receive, connection) != ("enrolled", launch_id):
            return
        engine = factory.load()
        worker = process_identity(os.getpid())
        try:

            async def idle(epoch, mode="wait"):
                # In the frozen implementation this Future resolves only after
                # _finish_pause collective synchronize_device completes.
                await engine.pause_generation(mode=mode, clear_cache=False)
                return SdkIdleAck(worker, launch_id, epoch, factory.capability_id)

            _send(connection, ("ready", await idle(0), None))
            while True:
                operation, epoch, payload = await asyncio.to_thread(
                    _receive, connection
                )
                if operation == "stop":
                    await idle(epoch, "abort")
                    return
                if operation == "barrier":
                    _send(connection, ("idle", await idle(epoch), None))
                elif operation == "infer":
                    await engine.resume_generation()
                    # Sampling parameters are built inside the same private
                    # worker. No public OpenAI server, engine handle or socket.
                    from vllm import SamplingParams

                    result = None
                    async for output in engine.generate(
                        payload["prompt"],
                        SamplingParams(**payload["sampling"]),
                        request_id=f"{launch_id}:{epoch}",
                    ):
                        result = [item.text for item in output.outputs]
                    _cpu_payload(result)
                    ack = await idle(epoch)
                    _send(connection, ("result", ack, result))
                else:
                    raise ValueError("unknown owned vLLM command")
        finally:
            # Device ownership is not released here. The supervisor still
            # requires empty complete containment before a physical handoff.
            engine.shutdown()

    try:
        asyncio.run(run())
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:  # noqa: BLE001 - never turn an SDK error into idle
        try:
            _send(connection, ("error", None, type(exc).__name__))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()
