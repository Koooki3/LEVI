"""Code must not list the whole of /proc (root AGENTS.md execution boundary, user 2026-10-14).

Listing /proc enumerates every process on a machine that several people share, and the
next step is usually reading another person's /proc/<pid> (fd, cwd, cmdline). Ownership is
decided by our own registry: the units this run started (their cgroup.procs) and our own
process tree. A directory listing of /proc itself, or of a /proc root handed in for tests,
fails this test unless it is on KNOWN below, which is the list of places still to fix
(L2-BOUNDARY-PROC-SCAN): an entry is removed when its fix lands, and none may be added.
Two rules, so the common spellings are caught however they are used:
- a string literal that is exactly "/proc" or "/proc/", or starts with "/proc/" and holds a
  wildcard (``*``, ``?``, ``[``), wherever it appears (``pathlib.Path("/proc")``, a module-level
  ``PROC = Path("/proc")``, ``glob.glob("/proc/[0-9]*")``, a ``proc_root="/proc"`` default);
- a directory listing of a /proc root handed in for tests (a ``proc``/``proc_root`` name).
Reading one given pid's entries (f"/proc/{pid}/fd") is not matched here.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ["levi"]
LISTING = {"iterdir", "scandir", "listdir", "glob", "rglob", "walk"}

# (file, enclosing function) of every whole-/proc listing that existed on 2026-10-14.
KNOWN = {
    ("levi/children.py", "_members"),
    ("levi/install.py", "_processes"),
    ("levi/live/gpumgr.py", "_group"),
    ("levi/live/gpumgr.py", "_group_alive"),
    ("levi/live/resources.py", "tree"),
    ("levi/live/service_control.py", "<module>"),
    ("levi/live/service_control.py", "socket_owners"),
    ("levi/setup/probes.py", "<module>"),
    ("levi/setup/probes.py", "_pids"),
    ("levi/setup/probes.py", "_robot_processes"),
    ("levi/setup/probes.py", "listening"),
}


def _proc_root(node: ast.AST, cls: str | None) -> bool:
    """True when ``node`` names /proc itself (not one pid's directory under it)."""
    if isinstance(node, ast.JoinedStr):  # f"/proc/{pid}/fd": one given pid
        return False
    text = ast.unparse(node)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rstrip("/") == "/proc"
    if text in {'Path("/proc")', "Path('/proc')"}:
        return True
    tail = text.rsplit(".", 1)[-1]
    if tail in {"proc", "proc_root"} or text.endswith("proc.root"):
        return True
    return bool(cls and "proc" in cls.lower() and text in {"self.root", "self._root"})


GLOB = set("*?[")


def _proc_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value
        return v in {"/proc", "/proc/"} or (
            v.startswith("/proc/") and bool(GLOB & set(v))
        )
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if (
            isinstance(head, ast.Constant)
            and isinstance(head.value, str)
            and head.value.startswith("/proc/")
        ):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            return bool(GLOB & set(text))
    return False


def _is_proc_path(node: ast.AST) -> bool:
    """``Path("/proc")`` or ``pathlib.Path("/proc")`` (the left side of ``/ str(pid)``)."""
    return (
        isinstance(node, ast.Call)
        and ast.unparse(node.func) in {"Path", "pathlib.Path"}
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in {"/proc", "/proc/"}
    )


def scan(text: str, rel: str) -> list[tuple[str, str, int]]:
    """Every whole-/proc use in one module's source, as (file, enclosing function, line)."""
    out: list[tuple[str, str, int]] = []

    def visit(node, cls=None, fn=None):
        if isinstance(node, ast.ClassDef):
            cls = node.name
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if _proc_literal(node):
            out.append((rel, fn or "<module>", node.lineno))
            return
        if isinstance(node, ast.JoinedStr):  # f"/proc/{pid}/...": one given pid
            return
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and _is_proc_path(node.left)
        ):
            visit(node.right, cls, fn)  # Path("/proc") / str(pid): one given pid
            return
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in LISTING:
                targets = list(node.args[:1])
                if isinstance(f, ast.Attribute) and name in {
                    "iterdir",
                    "glob",
                    "rglob",
                }:
                    targets.append(f.value)
                if any(_proc_root(t, cls) for t in targets):
                    out.append((rel, fn or "<module>", node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, cls, fn)

    visit(ast.parse(text))
    return out


def findings():
    out = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            out += scan(
                path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
            )
    return out


def _known(rel: str, fn: str) -> bool:
    return (rel, fn) in KNOWN


def test_no_new_whole_proc_listing():
    new = [
        f"{rel}:{line} in {fn}" for rel, fn, line in findings() if not _known(rel, fn)
    ]
    assert not new, (
        "code lists the whole of /proc (other people's processes): "
        + ", ".join(new)
        + "; read only the cgroup.procs of a unit this run started and our own process tree "
        "(root AGENTS.md execution boundary, L2-BOUNDARY-PROC-SCAN)"
    )


def test_every_known_entry_still_exists():
    """A fixed place must leave KNOWN, so the list only shrinks."""
    seen = {(rel, fn) for rel, fn, _ in findings()}
    stale = sorted(k for k in KNOWN if k not in seen)
    assert not stale, f"remove these fixed entries from KNOWN: {stale}"


def test_the_check_catches_the_forms_it_names():
    for src in (
        'os.scandir("/proc")',
        'Path("/proc").iterdir()',
        "os.listdir(proc_root)",
        "self.proc.root.iterdir()",
    ):
        call = ast.parse(src, mode="eval").body
        targets = list(call.args[:1])
        if isinstance(call.func, ast.Attribute) and call.func.attr == "iterdir":
            targets.append(call.func.value)
        assert any(_proc_root(t, None) for t in targets), src
    for src in (
        'os.scandir(f"/proc/{pid}/fd")',
        'os.listdir(f"/proc/{pid}/fd")',
        "root.iterdir()",
    ):
        call = ast.parse(src, mode="eval").body
        targets = list(call.args[:1]) + (
            [call.func.value]
            if isinstance(call.func, ast.Attribute) and call.func.attr == "iterdir"
            else []
        )
        assert not any(_proc_root(t, None) for t in targets), src


@pytest.mark.parametrize(
    "src",
    [
        'import pathlib\n\ndef f():\n    return list(pathlib.Path("/proc").iterdir())\n',
        'import glob\n\ndef f():\n    return glob.glob("/proc/[0-9]*")\n',
        'from pathlib import Path\nPROC = Path("/proc")\n\ndef f():\n    return list(PROC.iterdir())\n',
        'import os\n\ndef f():\n    return os.listdir("/proc/")\n',
        'def f(proc_root="/proc"):\n    return proc_root\n',
    ],
)
def test_the_spellings_reviewers_found_are_caught(src):
    assert scan(src, "x.py"), src


@pytest.mark.parametrize(
    "src",
    [
        'def f(pid):\n    return open(f"/proc/{pid}/stat").read()\n',
        'from pathlib import Path\n\ndef f(pid):\n    return (Path("/proc") / str(pid) / "fd").iterdir()\n',
        'def f():\n    return open("/proc/self/status").read()\n',
    ],
)
def test_one_given_pid_is_not_a_whole_proc_use(src):
    assert not scan(src, "x.py"), src
