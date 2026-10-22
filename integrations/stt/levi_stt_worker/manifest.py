"""``MANIFEST.sha256`` of a model folder: written once by the installer,
verified by the worker before it loads a single weight.

The format is ``sha256sum``'s (``<hex>  <relative path>``), so the file can
also be checked by hand with ``sha256sum -c``. Verification is strict: every
listed file must match, no file may be missing, and no unlisted file may sit
in the folder (a stray ``.py``, tokenizer or config would otherwise be
picked up by the loader unchecked).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .common import EXIT_MANIFEST, MANIFEST_NAME
from .errors import StartupError

_LINE = re.compile(r"([0-9a-f]{64})  (\S.*)")


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _listed_files(folder: Path) -> list[Path]:
    """Every entry under ``folder`` except the manifest. Anything that is not a
    plain file or directory (a symlink, a socket) is returned too, so the
    caller can refuse it."""
    found = []
    for path in sorted(folder.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        if path.relative_to(folder).as_posix() == MANIFEST_NAME:
            continue
        found.append(path)
    return found


def write_manifest(folder: Path) -> dict[str, str]:
    """Hash every file of ``folder`` and write the manifest atomically."""
    entries: dict[str, str] = {}
    for path in _listed_files(folder):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{path.relative_to(folder)} is not a regular file")
        entries[path.relative_to(folder).as_posix()] = file_sha256(path)
    if not entries:
        raise ValueError("the model folder is empty")
    text = "".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items()))
    temp = folder / (MANIFEST_NAME + ".partial")
    temp.write_text(text)
    temp.replace(folder / MANIFEST_NAME)
    return entries


def read_manifest(folder: Path) -> dict[str, str]:
    path = folder / MANIFEST_NAME
    try:
        lines = path.read_text().splitlines()
    except OSError:
        raise StartupError(
            "manifest_mismatch",
            f"{MANIFEST_NAME} is missing or unreadable.",
            EXIT_MANIFEST,
        ) from None
    except UnicodeDecodeError:
        raise StartupError(
            "manifest_mismatch", f"{MANIFEST_NAME} is not valid text.", EXIT_MANIFEST
        ) from None
    entries: dict[str, str] = {}
    for number, line in enumerate(lines, 1):
        match = _LINE.fullmatch(line)
        name = match.group(2) if match else ""
        parts = name.split("/")
        if (
            not match
            or name.startswith("/")
            or ".." in parts
            or "" in parts
            or name in entries
        ):
            raise StartupError(
                "manifest_mismatch",
                f"{MANIFEST_NAME} line {number} is malformed.",
                EXIT_MANIFEST,
            )
        entries[name] = match.group(1)
    if not entries:
        raise StartupError(
            "manifest_mismatch", f"{MANIFEST_NAME} lists no files.", EXIT_MANIFEST
        )
    return entries


def verify_manifest(folder: Path) -> dict[str, str]:
    """Return the verified ``{relative path: sha256}`` or raise StartupError.

    A missing folder is ``model_missing`` (the installer was never run); every
    other disagreement is ``manifest_mismatch``. Messages name the relative
    file only, never a filesystem path.
    """
    if not folder.is_dir():
        raise StartupError(
            "model_missing", "The speech model folder is not installed.", EXIT_MANIFEST
        )
    listed = read_manifest(folder)
    present = {
        path.relative_to(folder).as_posix(): path for path in _listed_files(folder)
    }
    for name in sorted(present.keys() - listed.keys()):
        raise StartupError(
            "manifest_mismatch",
            f"{name} is not listed in {MANIFEST_NAME}.",
            EXIT_MANIFEST,
        )
    for name in sorted(listed):
        path = present.get(name)
        if path is None:
            raise StartupError(
                "manifest_mismatch", f"{name} is listed but missing.", EXIT_MANIFEST
            )
        if path.is_symlink() or not path.is_file():
            raise StartupError(
                "manifest_mismatch", f"{name} is not a regular file.", EXIT_MANIFEST
            )
        try:
            actual = file_sha256(path)
        except OSError:
            raise StartupError(
                "manifest_mismatch", f"{name} cannot be read.", EXIT_MANIFEST
            ) from None
        if actual != listed[name]:
            raise StartupError(
                "manifest_mismatch",
                f"{name} does not match {MANIFEST_NAME}.",
                EXIT_MANIFEST,
            )
    return listed
