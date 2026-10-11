"""Owned private-IPC vLLM SDK adapter; no HTTP or external-server attach.

Installed synchronized-pause implementation and the model factory must be
independently frozen. A private single-engine AsyncLLM is created ONLY inside
the launcher-created contained worker. Missing code/version/containment fails
closed. Existing RuntimeOwnership remains the sole execution scheduler.
"""

import asyncio
from dataclasses import dataclass, field

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
    sdk_source_hashes: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)

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
        from levi.live.owned_source import frozen_distribution_bundle

        declared = dict(self.sdk_source_hashes)
        if any(
            declared.get("vllm." + name[:-3].replace("/", ".")) != sha
            for name, sha in self.barrier_hashes
        ):
            raise ValueError(
                "barrier source must belong to the complete frozen SDK manifest"
            )
        torch_bundle = frozen_distribution_bundle(
            "torch", "torch", self.torch_version, self.torch_source_hashes
        )
        sdk_bundle = frozen_distribution_bundle(
            "vllm",
            "vllm",
            self.vllm_version,
            self.sdk_source_hashes,
            allowed_bundles=(torch_bundle,),
        )
        torch_bundle.allowed_bundles = (sdk_bundle,)
        bundles = (torch_bundle, sdk_bundle)
        try:
            for bundle in bundles:
                bundle.install()
            torch = torch_bundle.import_module("torch")
            sdk = sdk_bundle.import_module("vllm")
            async_module = sdk_bundle.import_module("vllm.v1.engine.async_llm")
            factory = self.reviewed_factory(allowed_bundles=bundles)
            if (
                str(torch.__version__) != self.torch_version
                or not torch.cuda.is_available()
            ):
                raise ValueError("frozen accelerator SDK unavailable")
            torch_bundle.require_function(torch.cuda.synchronize)
            engine = factory(device=self.device, sdk=sdk)
            if type(engine) is not async_module.AsyncLLM:
                raise TypeError(
                    "factory must create the frozen private AsyncLLM engine"
                )
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
            engine._owned_sdk_bundles = bundles
            self.require_engine(engine)
            return engine
        except BaseException:
            for bundle in bundles:
                bundle.close()
            raise

    def require_engine(self, engine):
        from levi.live.owned_source import FrozenSourceBundle

        bundles = getattr(engine, "_owned_sdk_bundles", ())
        if len(bundles) != 2 or any(
            type(bundle) is not FrozenSourceBundle for bundle in bundles
        ):
            raise ValueError("actual engine SDK source binding required")
        for bundle in bundles:
            bundle.require_loaded()
        for name in ("pause_generation", "resume_generation", "generate", "shutdown"):
            function = getattr(engine, name)
            bundles[1].require_function(function)


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
                factory.require_engine(engine)
                await engine.pause_generation(mode=mode, clear_cache=False)
                factory.require_engine(engine)
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
                    factory.require_engine(engine)
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
            factory.require_engine(engine)
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
