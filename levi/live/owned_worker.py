"""Private child entrypoint; the supervisor alone supplies its inherited pipe.

No model/SDK import occurs before the dedicated cgroup enrollment gate. This
entrypoint has no public socket, service configuration or external PID attach.
"""

import os
import sys
from multiprocessing.connection import Connection

from levi.live.owned_runtime import FrozenTorchFactory, _receive, _serve
from levi.live.owned_vllm import FrozenVllmFactory, serve_vllm


def main():
    # A worker started without the pinned interpreter flags may have read
    # stale bytecode; refuse before reading the bootstrap or loading any SDK.
    if not sys.flags.dont_write_bytecode or sys.pycache_prefix != os.devnull:
        raise ValueError("owned worker requires the pinned no-bytecode interpreter")
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise ValueError("private inherited owned pipe descriptor required")
    connection = Connection(int(sys.argv[1]))
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
