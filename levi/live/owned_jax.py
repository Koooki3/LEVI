"""JAX dependency fence, explicitly NOT a whole-backend idle provider.

Existing openpi infer returns NumPy output dependencies; that does not prove
all device work finished. This executable fence is useful to a future reviewed
standard-infer closure, but emits no SdkIdleAck, BackendAck or RuntimeObservation.
The current installed SDK exposes per-array and side-effect fences, not a
verified whole-client device barrier. No model, device or HTTP starts at import.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class JaxDependencyProof:
    array_count: int
    output_dependencies_ready: bool
    side_effects_ready: bool
    backend_idle: bool = False


class JaxDependencyFence:
    def __init__(self, *, jax_version, jaxlib_version):
        if not jax_version or not jaxlib_version:
            raise ValueError("exact reviewed JAX and jaxlib versions required")
        self.jax_version, self.jaxlib_version = jax_version, jaxlib_version

    def wait(self, output, retained):
        import importlib.metadata

        if any(
            importlib.metadata.version(name) != expected
            for name, expected in (
                ("jax", self.jax_version),
                ("jaxlib", self.jaxlib_version),
            )
        ):
            raise ValueError("installed JAX SDK changed")
        import jax

        # A trusted standard-infer provider would also retain policy._rng,
        # input/device arrays and any model/cache state here. This dependency
        # proof cannot vouch for untracked dispatch or other ingress.
        arrays = list(jax.live_arrays())
        jax.block_until_ready((output, retained, arrays))
        jax.effects_barrier()
        return JaxDependencyProof(len(arrays), True, True)

    def backend_idle(self):
        raise ValueError(
            "complete exclusive JAX dispatch closure is not frozen; backend idle unavailable"
        )
