"""``python -m levi.pool run-job <plan>``: the pool worker process;
anything else is ``levi pool`` (see ``cli.py``)."""

import sys
from pathlib import Path

from ..paths import configure

if __name__ == "__main__":
    configure()
    if len(sys.argv) == 3 and sys.argv[1] == "run-job":
        from .jobs import worker

        raise SystemExit(worker(Path(sys.argv[2])))
    from .cli import main

    raise SystemExit(main(sys.argv[1:]))
