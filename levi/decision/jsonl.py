"""Append-only JSON-lines helpers shared by the three decision ledgers (queue, token
ledger, confirm claims): a fresh line before every append, and an explicit reading-side
message for a line that a crash cut short.

A crash can leave the last line without its newline. Appending straight after it would
glue the next row onto the cut one and ruin both; so every appender calls
``start_on_fresh_line`` (under its lock) first, which writes the missing newline and
nothing else. A cut line stays in the file as a line of its own, and readers refuse the
file naming it (``line_problem``) instead of reading past it. A last line that is
complete JSON but lacks only its newline is a whole row and is read as one.
"""

from __future__ import annotations

import os


def start_on_fresh_line(handle) -> bool:
    """For an append handle that is locked and open for writing: write one ``\\n`` when
    the file is not empty and does not end in one. True when a newline was added."""
    fd = handle.fileno()
    size = os.fstat(fd).st_size
    if size and os.pread(fd, 1, size - 1) != b"\n":
        handle.write("\n")
        return True
    return False


def line_problem(path, n: int, text: str, what: str) -> str:
    """The message for line ``n`` (1-based) of ``text`` read from ``path`` that is not a
    ``what``: names a cut-short last line as such."""
    if text and not text.endswith("\n") and n == len(text.split("\n")):
        return (
            f"line {n} of {path} is not a {what}: it is the last line and the file "
            "does not end in a newline, so a write was cut short (remove that line "
            "or restore the file)"
        )
    return f"line {n} of {path} is not a {what}"
