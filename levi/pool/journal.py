"""The durable journal of an unfinished export.

Two files in the ``.<name>.partial`` folder (removed when the export
finishes):

- ``resume.json``: the plan's hash, the options, the target and counters
  (resumes, interruptions), replaced atomically;
- ``resume.units.jsonl``: append-only, one line per finished unit (a
  converted part of raw episodes, a merged video, a copied episode) with the
  size and sha256 of every file it produced. A line is complete when it ends
  with a newline; a torn last line is ignored.

A resume trusts nothing it cannot check: the plan hash must match the
frozen plan, and every unit is verified on disk (the file exists, its size
matches and, up to ``HASH_LIMIT``, its sha256) before it is skipped; a unit
that fails is done again.
"""

import hashlib
import json
import os
import time
from pathlib import Path

from ..catalog import atomic

SCHEMA = "levi.pool.resume.v1"
HEADER = "resume.json"
UNITS = "resume.units.jsonl"
HASH_LIMIT = 64 * 1024 * 1024
PLAN_KEYS = (
    "format",
    "options",
    "target",
    "sources",
    "episodes",
    "excluded",
    "recipe",
    "heldout_lists",
    "heldout_disabled",
    "pool_roots",
)


class ResumeRefused(ValueError):
    """The unfinished export cannot be trusted; run it again instead."""


def plan_hash(job: dict) -> str:
    frozen = {k: job.get(k) for k in PLAN_KEYS}
    text = json.dumps(frozen, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(root: Path, relative: str, sha: str | None = None) -> dict:
    """Size and sha256 of ``root/relative`` (``sha`` when already known)."""
    path = Path(root) / relative
    return {"size": path.stat().st_size, "sha256": sha or sha256_of(path)}


class Journal:
    def __init__(self, partial: Path, header: dict, resumed: bool = False):
        self.partial = Path(partial)
        self.header = header
        self.resumed = resumed
        self.units: dict[str, dict] = {}
        self.new_units = 0

    # ------------------------------------------------------------- open

    @classmethod
    def create(cls, partial: Path, job: dict) -> "Journal":
        header = {
            "schema": SCHEMA,
            "job_id": job.get("id"),
            "plan_hash": plan_hash(job),
            "target": job.get("target"),
            "format": job.get("format") or (job.get("options") or {}).get("format"),
            "options": job.get("options"),
            "created_at": time.time(),
            "updated_at": time.time(),
            "resumes": 0,
            "interruptions": 0,
            "state": "running",
        }
        journal = cls(partial, header)
        journal._write_header()
        (Path(partial) / UNITS).write_text("")
        return journal

    @staticmethod
    def check(partial: Path, job: dict) -> dict:
        """The journal header of an unfinished export, if this plan may
        continue it (nothing is written)."""
        partial = Path(partial)
        try:
            header = json.loads((partial / HEADER).read_text())
        except (OSError, ValueError):
            raise ResumeRefused(
                f"{partial} has no readable resume journal; run the export again"
            ) from None
        if header.get("schema") != SCHEMA:
            raise ResumeRefused("The resume journal is from another LEVI version")
        if header.get("plan_hash") != plan_hash(job):
            raise ResumeRefused(
                "The plan changed since the export started (options, episodes or "
                "target differ); run the export again"
            )
        if header.get("target") != job.get("target"):
            raise ResumeRefused("The export directory is not the one that was planned")
        return header

    @classmethod
    def read(cls, partial: Path) -> "Journal":
        """The journal as it stands (units loaded, nothing written)."""
        partial = Path(partial)
        journal = cls(partial, json.loads((partial / HEADER).read_text()))
        journal._load_units()
        return journal

    @classmethod
    def open(cls, partial: Path, job: dict) -> "Journal":
        """The journal of an unfinished export, ready to continue it."""
        partial = Path(partial)
        header = cls.check(partial, job)
        journal = cls(partial, header, resumed=True)
        journal._load_units()
        journal.header["resumes"] = int(header.get("resumes") or 0) + 1
        journal.header["state"] = "running"
        journal._write_header()
        return journal

    @staticmethod
    def exists(partial: Path) -> bool:
        return (Path(partial) / HEADER).is_file()

    def _load_units(self) -> None:
        path = self.partial / UNITS
        if not path.is_file():
            return
        raw = path.read_bytes()
        # The last segment is empty, or a line the crash tore.
        for line in raw.split(b"\n")[:-1]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("unit"):
                self.units[row["unit"]] = row

    # ------------------------------------------------------------ write

    def _write_header(self) -> None:
        self.header["updated_at"] = time.time()
        atomic(self.partial / HEADER, self.header)

    def record(self, unit: str, **fields) -> dict:
        row = {"unit": unit, "at": time.time(), **fields}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with (self.partial / UNITS).open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self.units[unit] = row
        self.new_units += 1
        return row

    def note_stopped(self, state: str, reason: str) -> None:
        """Interrupted or failed: the partial stays, and says why."""
        if not (self.partial / HEADER).is_file():
            return
        if state == "interrupted":
            self.header["interruptions"] = (
                int(self.header.get("interruptions") or 0) + 1
            )
        self.header.update(state=state, reason=reason, stopped_at=time.time())
        self._write_header()

    # ------------------------------------------------------------- read

    @property
    def resumes(self) -> int:
        return int(self.header.get("resumes") or 0)

    @property
    def interruptions(self) -> int:
        return int(self.header.get("interruptions") or 0)

    def done(self, unit: str) -> dict | None:
        return self.units.get(unit)

    def prefixed(self, prefix: str) -> list[dict]:
        return [r for u, r in self.units.items() if u.startswith(prefix)]

    def verified(self, files: dict[str, dict], root: Path | None = None) -> bool:
        """Every recorded file exists with its recorded size (and sha256, up
        to ``HASH_LIMIT`` bytes)."""
        base = Path(root) if root else self.partial
        for relative, record in files.items():
            path = base / relative
            try:
                size = path.stat().st_size
            except OSError:
                return False
            if size != record.get("size"):
                return False
            sha = record.get("sha256")
            if sha and size <= HASH_LIMIT and sha256_of(path) != sha:
                return False
        return True

    def has_units(self) -> bool:
        return bool(self.units)

    # ---------------------------------------------------------- discard

    def discard(self) -> None:
        for name in (HEADER, UNITS):
            (self.partial / name).unlink(missing_ok=True)
