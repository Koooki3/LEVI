"""Private child entrypoint; the supervisor alone supplies its inherited pipe.

No model/SDK import occurs before the dedicated cgroup enrollment gate. This
entrypoint has no public socket, service configuration or external PID attach.
"""

import importlib.machinery
import os
import sys
from multiprocessing.connection import Connection
from pathlib import Path

from levi.live.owned_runtime import FrozenTorchFactory, _receive, _serve
from levi.live.owned_vllm import FrozenVllmFactory, serve_vllm


def _require_origin(root):
    """Every loaded levi module is ordinary source under the pinned root."""
    package = Path(root).resolve(strict=True) / "levi"
    for name, module in tuple(sys.modules.items()):
        if name != "levi" and not name.startswith("levi."):
            continue
        spec = getattr(module, "__spec__", None)
        origin = getattr(spec, "origin", None)
        if (
            type(getattr(spec, "loader", None))
            is not importlib.machinery.SourceFileLoader
            or origin is None
            or getattr(module, "__file__", None) != origin
            or not Path(origin).resolve().is_relative_to(package)
        ):
            raise ValueError("owned worker levi module origin differs from its root")


def main():
    # A worker started without the pinned interpreter flags may have read
    # stale bytecode or a cwd/PYTHONPATH shadow; refuse before reading the
    # bootstrap or loading any SDK.
    if (
        not sys.flags.isolated
        or not sys.flags.dont_write_bytecode
        or sys.pycache_prefix != os.devnull
    ):
        raise ValueError(
            "owned worker requires the pinned isolated no-bytecode interpreter"
        )
    if len(sys.argv) != 3 or not sys.argv[2].isdigit():
        raise ValueError("private inherited owned pipe descriptor required")
    _require_origin(sys.argv[1])
    connection = Connection(int(sys.argv[2]))
    operation, factory, launch_id = _receive(connection)
    if operation != "bootstrap" or type(factory) not in (
        FrozenTorchFactory,
        FrozenVllmFactory,
    ):
        raise ValueError("frozen owned backend bootstrap required")
    if type(factory) is FrozenVllmFactory:
        serve_vllm(connection, factory, launch_id)
    else:
        _serve(connection, factory, launch_id)


if __name__ == "__main__":
    main()
