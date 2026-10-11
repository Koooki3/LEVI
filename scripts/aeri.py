"""AERI thin entry: check/execute use the operator FR3 core; other commands
use the same automatic CLI as the page. No hardware import on dispatch/help.
Run from an installed LEVI environment or with the checkout on PYTHONPATH.
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("check", "execute"):
        from levi.automatic import fr3_run

        command, *rest = argv
        return fr3_run.main([*rest, "--" + command])
    from levi.automatic import cli

    if not argv or argv == ["--help"] or argv == ["-h"]:
        print(
            "Standalone AERI / 独立自动测评: check|execute --config JOB --binding BINDING"
        )
        print(
            "execute retains operator-terminal and immutable-digest approval; HTTP real launch remains refused."
        )
        print(
            "Other commands use the shared automatic CLI below. / 其余指令复用下方统一 CLI。"
        )
        argv = ["--help"]
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
