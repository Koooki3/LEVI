"""Task text corrections: a reviewed, versioned table that says an episode's
recording does another task than its text claims (docs/TRAINING_POOL.md,
"Task text corrections").

Nothing here changes a source: a correction is a row in the pool's own files,
and an export applies it only when the recipe names its version
(``task_corrections``) **and** a person approved it.

Layout under ``<workspace>/pool/task_corrections/``:

- ``<version>.jsonl``: the proposals, one per line, written once by
  ``import_file`` (an agent or a script may propose; a proposal carries no
  decision). Each names an episode (``episode``: a path relative to a pool
  root, or absolute; optionally its ``fingerprint``), the text it carries now
  (``task_from``, a guard: when the index says otherwise the proposal is
  ``stale`` and never applied), the corrected text (``task_to``) and where the
  proposal comes from (``source``, ``evidence``, ``confidence``, ``proposed_by``).
- ``<version>.json``: when and from what file it was imported, the count and
  the sha256 of the proposals file.
- ``<version>.reviews.jsonl``: a person's decisions, appended and never
  rewritten (``approved`` / ``rejected``, who, when, note). The latest decision
  on a proposal is its status; with none it is ``proposed``.

Reviews are a person's action: ``review`` is only called by the
``/api/levi/pool/corrections/{version}/review`` route, which refuses an
agent's credential and needs the UI token (the CLI's ``approve`` and
``reject`` go through the running service with the person's key).

A correction states what the *recording* shows, so it applies to every copy
of it (the index's group), the same way a human outcome label does.

``copy_candidates`` is the other half of this module: groups of copies whose
members carry different task texts (the export keeps the canonical member,
never a folder named like a copy when an original exists) and copy-named
folders with no original whose text differs from their task folder. They are
listed for a person to decide, never resolved silently.
"""

import fcntl
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import settings
from .rules import normalize_task

SCHEMA = "levi.pool.task_corrections.v1"
FOLDER = "task_corrections"
VERSION = r"[a-z0-9][a-z0-9._-]{0,63}"
ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
RESERVED = {"copies"}
DECISIONS = ("approved", "rejected")
STATUSES = ("proposed", *DECISIONS)
MAX_ENTRIES = 100_000


def _line(value: str, name: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be one line of at most {limit} characters")
    return value


class Proposal(BaseModel):
    """One proposed correction, as imported (no decision in it)."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID)
    episode: str = Field(min_length=1, max_length=1000)
    fingerprint: str | None = Field(None, max_length=128)
    task_from: str = Field(min_length=1, max_length=300)
    task_to: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=200)
    evidence: str = Field("", max_length=4000)
    evidence_files: list[str] = Field(default_factory=list, max_length=20)
    confidence: str = Field("", max_length=40)
    review_batch: str = Field("", max_length=40)
    proposed_by: str = Field(min_length=1, max_length=120)
    proposed_at: str = Field("", max_length=40)
    note: str = Field("", max_length=2000)

    @field_validator("task_from", "task_to")
    @classmethod
    def _text(cls, value):
        value = _line(value, "task text", 300)
        if not value:
            raise ValueError("task text must be nonempty")
        return value

    @field_validator("source", "proposed_by", "confidence", "review_batch")
    @classmethod
    def _short(cls, value):
        return _line(value, "this field", 200)

    @field_validator("episode")
    @classmethod
    def _episode(cls, value):
        value = value.strip()
        if "\x00" in value or ".." in Path(value).parts:
            raise ValueError("episode must be a plain path without '..'")
        return value


class Review(BaseModel):
    """A person's decision on some proposals of one version."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=120)
    # The sha256 of the version's proposals file the person looked at
    # (``show`` / ``list`` print it): a decision binds to that content.
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ids: list[str] = Field(default_factory=list, max_length=MAX_ENTRIES)
    batch: str | None = Field(None, max_length=40)
    all: bool = False
    exclude: list[str] = Field(default_factory=list, max_length=MAX_ENTRIES)
    note: str = Field("", max_length=2000)

    @field_validator("reviewer")
    @classmethod
    def _reviewer(cls, value):
        value = _line(value, "reviewer", 120)
        if not value:
            raise ValueError("reviewer must name the person")
        return value


# ------------------------------------------------------------------ storage


def folder() -> Path:
    return settings.pool_dir() / FOLDER


def check_version(version: str) -> str:
    if not re.fullmatch(VERSION, version or "") or version in RESERVED:
        raise ValueError(
            f"{version!r} is not a correction version name "
            "(lower case letters, digits, '.', '_', '-'; at most 64)"
        )
    return version


def _paths(version: str) -> dict[str, Path]:
    check_version(version)
    base = folder()
    return {
        "proposals": base / f"{version}.jsonl",
        "manifest": base / f"{version}.json",
        "reviews": base / f"{version}.reviews.jsonl",
        "lock": base / f".{version}.lock",
    }


@contextmanager
def _locked(version: str):
    path = _paths(version)["lock"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_rows(text: str, name: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"{name}: line {n} is not JSON: {exc}") from None
    if not all(isinstance(r, dict) for r in rows):
        raise ValueError(f"{name}: every proposal must be a JSON object")
    return rows


def parse(rows: list[dict], name: str = "proposals") -> list[Proposal]:
    """Validate proposals. A row that carries a decision is refused: only a
    person reviews, and only through ``review``."""
    out, seen = [], set()
    if len(rows) > MAX_ENTRIES:
        raise ValueError(f"{name}: more than {MAX_ENTRIES} proposals")
    for n, row in enumerate(rows, 1):
        decided = {k for k in ("status", "reviewed_by", "reviewed_at") if row.get(k)}
        if row.get("status") in (None, "", "proposed"):
            decided.discard("status")
        if decided:
            raise ValueError(
                f"{name}: proposal {n} carries a review ({', '.join(sorted(decided))}); "
                "an import only proposes, a person approves or rejects "
                "(levi pool corrections approve|reject)"
            )
        row = {
            k: v
            for k, v in row.items()
            if k not in ("status", "reviewed_by", "reviewed_at", "version")
        }
        row.setdefault("id", f"{n:05d}")
        try:
            item = Proposal.model_validate(row)
        except ValidationError as exc:
            raise ValueError(f"{name}: proposal {n}: {exc}") from None
        if item.id in seen:
            raise ValueError(f"{name}: id {item.id!r} is listed twice")
        seen.add(item.id)
        if normalize_task(item.task_from) == normalize_task(item.task_to):
            raise ValueError(f"{name}: proposal {item.id} does not change the task")
        out.append(item)
    if not out:
        raise ValueError(f"{name}: no proposal")
    return out


def import_file(path, version: str) -> dict:
    """Write the proposals of ``path`` (JSONL, or a JSON list) as ``version``.
    A version is written once: any file of it already there (proposals,
    manifest or reviews, even a reviews file left from a deleted version)
    refuses the import. A change is a new version, so a person's decisions
    always refer to the proposals they saw."""
    path = Path(path)
    items = parse(_read_rows(path.read_text(encoding="utf-8"), path.name), path.name)
    files = _paths(version)
    with _locked(version):
        present = [
            files[k].name
            for k in ("proposals", "manifest", "reviews")
            if files[k].exists()
        ]
        if present:
            raise ValueError(
                f"Correction version {version!r} exists ({', '.join(present)}); "
                "a version is written once (import under a new version name)"
            )
        stamp = _now()
        lines = []
        for item in items:
            row = item.model_dump()
            row["proposed_at"] = row["proposed_at"] or stamp
            lines.append(json.dumps({"version": version, **row}, ensure_ascii=False))
        files["proposals"].parent.mkdir(parents=True, exist_ok=True)
        temp = files["proposals"].with_name(files["proposals"].name + ".tmp")
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temp.replace(files["proposals"])
        manifest = {
            "schema": SCHEMA,
            "version": version,
            "imported_at": stamp,
            "imported_from": str(path.resolve()),
            "proposals": len(items),
            "sha256": _sha256(files["proposals"]),
        }
        temp = files["manifest"].with_name(files["manifest"].name + ".tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
        temp.replace(files["manifest"])
    return manifest


def versions() -> list[str]:
    base = folder()
    if not base.is_dir():
        return []
    return sorted(
        p.name[: -len(".jsonl")]
        for p in base.glob("*.jsonl")
        if not p.name.endswith(".reviews.jsonl") and re.fullmatch(VERSION, p.stem)
    )


def proposals(version: str) -> list[dict]:
    path = _paths(version)["proposals"]
    if not path.is_file():
        raise KeyError(f"task correction version {version}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def manifest(version: str) -> dict:
    try:
        return json.loads(_paths(version)["manifest"].read_text())
    except (OSError, ValueError):
        return {}


def _reviews(version: str) -> tuple[list[dict], bool]:
    """The decisions, and whether the last line was cut short (a write that
    did not finish: skipped, and reported as ``reviews_truncated``). A
    damaged line before the last one raises: that is not an unfinished
    append, and guessing past it could change a decision."""
    path = _paths(version)["reviews"]
    if not path.is_file():
        return [], False
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    # A complete file ends with a newline, so the last piece is empty.
    tail = lines.pop()
    rows = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise ValueError(
                f"{path.name}: line {n} is damaged; the decisions after it "
                "cannot be trusted (restore the file)"
            ) from None
    truncated = bool(tail.strip())
    if truncated:
        try:  # a last line without its newline may still be whole
            rows.append(json.loads(tail))
            truncated = False
        except ValueError:
            pass
    return rows, truncated


def reviews_truncated(version: str) -> bool:
    return _reviews(version)[1]


def checked_sha256(version: str) -> str:
    """The sha256 of the version's proposals file, which must equal the one
    the import recorded; anything else (an edited file, a missing manifest)
    raises: decisions were made on what the manifest names."""
    path = _paths(version)["proposals"]
    if not path.is_file():
        raise KeyError(f"task correction version {version}")
    want = manifest(version).get("sha256")
    have = _sha256(path)
    if not want or have != want:
        raise ValueError(
            f"Task correction version {version!r} was changed after its import "
            f"(proposals sha256 {have[:12]}…, manifest {str(want)[:12]}…); "
            "nothing of it is applied: import the change as a new version"
        )
    return have


def _binds(decision: dict, proposal: dict, sha: str | None) -> bool:
    """A decision counts only for the content it was made on: the version's
    proposals file (sha256) and this proposal's ``task_to``."""
    return (
        sha is not None
        and decision.get("sha256") == sha
        and decision.get("task_to") == proposal.get("task_to")
    )


def entries(version: str) -> list[dict]:
    """Proposals with their current status: the latest decision made on this
    content (the manifest's sha256 and the proposal's ``task_to``); a decision
    on other content is ignored."""
    rows = proposals(version)
    sha = manifest(version).get("sha256")
    by_id = {r["id"]: r for r in rows}
    latest: dict[str, dict] = {}
    for row in _reviews(version)[0]:
        proposal = by_id.get(row.get("id"))
        if proposal is not None and _binds(row, proposal, sha):
            latest[row["id"]] = row
    out = []
    for row in rows:
        decision = latest.get(row["id"])
        out.append(
            {
                **row,
                "status": decision["decision"] if decision else "proposed",
                "reviewed_by": decision["reviewer"] if decision else None,
                "reviewed_at": decision["at"] if decision else None,
                "review_note": decision.get("note") if decision else None,
            }
        )
    return out


def review(version: str, decision: Review, *, principal: str) -> dict:
    """Record a person's decision. Callers: the review route only (it has
    checked that a person, not an agent, asks)."""
    with _locked(version):
        rows = proposals(version)
        sha = checked_sha256(version)
        if decision.sha256 != sha:
            raise ValueError(
                f"The proposals of {version!r} have sha256 {sha}, not the "
                f"{decision.sha256} named in the decision: look at "
                f"`levi pool corrections show {version}` and decide on what it shows"
            )
        task_to = {r["id"]: r["task_to"] for r in rows}
        known = set(task_to)
        if decision.all:
            wanted = [r["id"] for r in rows]
        elif decision.batch is not None:
            wanted = [r["id"] for r in rows if r.get("review_batch") == decision.batch]
            if not wanted:
                raise ValueError(
                    f"No proposal of {version} is in batch {decision.batch!r}"
                )
        else:
            wanted = list(dict.fromkeys(decision.ids))
        if not wanted:
            raise ValueError("Name the proposals: ids, a batch or all")
        unknown = [i for i in [*wanted, *decision.exclude] if i not in known]
        if unknown:
            raise ValueError(f"Unknown proposal id(s) in {version}: {unknown[:10]}")
        skip = set(decision.exclude)
        wanted = [i for i in wanted if i not in skip]
        stamp = _now()
        path = _paths(version)["reviews"]
        lines = "".join(
            json.dumps(
                {
                    "id": item,
                    "decision": decision.decision,
                    "reviewer": decision.reviewer,
                    "principal": principal,
                    "sha256": sha,
                    "task_to": task_to[item],
                    "at": stamp,
                    "note": decision.note,
                },
                ensure_ascii=False,
            )
            + "\n"
            for item in wanted
        )
        data = path.read_bytes() if path.is_file() else b""
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n") + 1
            try:
                json.loads(data[cut:])
            except ValueError:
                # An append cut short (never a whole decision): drop the
                # fragment, so the new lines are not glued onto it.
                with path.open("r+b") as handle:
                    handle.truncate(cut)
            else:
                # A whole last line that lost only its newline: end it.
                lines = "\n" + lines
        # One write and an fsync. A crash can still leave part of it: the
        # whole lines written before the cut count (a partial review of the
        # ids asked for), and an unfinished last line is skipped.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            data = lines.encode("utf-8")
            written = os.write(fd, data)
            if written != len(data):
                raise OSError(f"short write to {path.name}")
            os.fsync(fd)
        finally:
            os.close(fd)
    return {
        "version": version,
        "decision": decision.decision,
        "reviewer": decision.reviewer,
        "changed": len(wanted),
        "left_out": sorted(skip),
        "counts": dict(Counter(e["status"] for e in entries(version))),
    }


# ------------------------------------------------------------------ matching


def _candidates(episode: str) -> list[str]:
    path = Path(episode)
    if path.is_absolute():
        return [str(path)]
    return [str(root / path) for root in settings.pool_roots()]


def resolve(rows: list[dict], df: pd.DataFrame) -> list[dict]:
    """Each proposal against the index: the matched episode ``key`` and
    ``group``, and ``match``: ``ok``, ``stale`` (the index's text is not
    ``task_from``), ``ambiguous`` (a relative path found under more than one
    pool root) or ``unmatched``."""
    by_key = {k: i for i, k in enumerate(df.key)}
    resolved: dict[str, int] = {}

    def by_real_path(candidate: str):
        # Built on the first miss only: resolving every key costs a stat each.
        if not resolved:
            for i, key in enumerate(df.key):
                try:
                    resolved.setdefault(str(Path(key).resolve()), i)
                except OSError:
                    pass
        try:
            return resolved.get(str(Path(candidate).resolve()))
        except OSError:
            return None

    by_fp = defaultdict(list)
    if "fingerprint" in df.columns:
        for i, fp in enumerate(df.fingerprint):
            if fp:
                by_fp[fp].append(i)
    keys, groups, tasks = list(df.key), list(df.group), list(df.task)
    out = []
    for row in rows:
        hits = []
        for candidate in _candidates(row["episode"]):
            found = by_key.get(candidate)
            if found is None:
                found = by_real_path(candidate)
            if found is not None and found not in hits:
                hits.append(found)
        if len(hits) > 1:
            # A relative path found under two pool roots: which one the
            # proposal meant is not known, so it is not applied.
            out.append(
                {
                    **row,
                    "match": "ambiguous",
                    "key": None,
                    "group": None,
                    "candidates": [keys[i] for i in hits],
                }
            )
            continue
        hit = hits[0] if hits else None
        if hit is None and row.get("fingerprint"):
            found = by_fp.get(row["fingerprint"]) or []
            if len({groups[i] for i in found}) == 1:
                hit = found[0]
        if hit is None:
            out.append({**row, "match": "unmatched", "key": None, "group": None})
            continue
        now = tasks[hit]
        match = "ok" if now == normalize_task(row["task_from"]) else "stale"
        out.append(
            {
                **row,
                "match": match,
                "key": keys[hit],
                "group": groups[hit] or keys[hit],
                "task_now": now,
            }
        )
    return out


def _index() -> pd.DataFrame | None:
    from . import scanner

    path = scanner.index_path()
    if not path.is_file():
        return None
    return pd.read_parquet(path, columns=["key", "group", "task", "fingerprint"])


def _viewed(version: str) -> tuple[list[dict], bool]:
    """``entries`` for a person to read, and whether the proposals file no
    longer matches its manifest. Then no decision holds (nothing of the
    version is applied): every status reads ``invalid``, never ``approved``."""
    rows = entries(version)
    try:
        checked_sha256(version)
    except ValueError:
        return [{**r, "status": "invalid", "decision": r["status"]} for r in rows], True
    return rows, False


def show(version: str, status: str | None = None, batch: str | None = None) -> dict:
    rows, changed = _viewed(version)
    df = _index()
    if df is not None:
        rows = resolve(rows, df)
    if status:
        rows = [r for r in rows if r["status"] == status]
    if batch is not None:
        rows = [r for r in rows if r.get("review_batch") == batch]
    return {
        "version": version,
        "sha256": manifest(version).get("sha256"),
        "changed_after_import": changed,
        "reviews_truncated": reviews_truncated(version),
        "manifest": manifest(version),
        "counts": dict(Counter(r["status"] for r in rows)),
        "match": dict(Counter(r.get("match", "not_scanned") for r in rows)),
        "entries": rows,
    }


def listing() -> list[dict]:
    out = []
    df = _index()
    for version in versions():
        rows, changed = _viewed(version)
        item = {
            "version": version,
            "imported_at": manifest(version).get("imported_at"),
            "sha256": manifest(version).get("sha256"),
            "changed_after_import": changed,
            "reviews_truncated": reviews_truncated(version),
            "proposals": len(rows),
            "status": dict(Counter(r["status"] for r in rows)),
            "batches": sorted({r.get("review_batch") or "" for r in rows} - {""}),
        }
        if df is not None:
            item["match"] = dict(Counter(r["match"] for r in resolve(rows, df)))
        out.append(item)
    return out


# ------------------------------------------------------------------ applying


def applicable(names: list[str], df: pd.DataFrame) -> tuple[dict, list[dict]]:
    """``(by group, problems)`` for the versions a recipe names: the approved,
    matched corrections per group (the first version listed wins when two
    agree), and every reason one is not applied (``stale``, ``unmatched``,
    ``conflict`` when two approved corrections of one recording disagree).
    An unknown version raises."""
    chosen: dict[str, dict] = {}
    problems: list[dict] = []
    for version in names:
        try:
            checked_sha256(version)
            rows = entries(version)
        except KeyError:
            raise ValueError(
                f"No task correction version {version!r} in this pool "
                "(levi pool corrections list)"
            ) from None
        for row in resolve([r for r in rows if r["status"] == "approved"], df):
            ref = f"{version}:{row['id']}"
            if row["match"] != "ok":
                problems.append({"correction": ref, "problem": row["match"]})
                continue
            have = chosen.get(row["group"])
            if have is None:
                chosen[row["group"]] = {**row, "ref": ref}
            elif normalize_task(have["task_to"]) != normalize_task(row["task_to"]):
                problems.append(
                    {
                        "correction": ref,
                        "problem": "conflict",
                        "with": have["ref"],
                        "group": row["group"],
                    }
                )
    return chosen, problems


def apply(df: pd.DataFrame, names: list[str]) -> tuple[pd.DataFrame, dict]:
    """The index with the approved corrections of ``names`` applied to every
    member of each corrected group (``task``, ``task_raw``), and
    ``task_original`` / ``task_correction`` recording what changed; plus a
    summary. Nothing else changes; without names the frame is returned as is."""
    if not names:
        return df, {}
    chosen, problems = applicable(names, df)
    df = df.copy()
    df["task_original"] = None
    df["task_correction"] = None
    if chosen:
        groups = df.group.where(df.group.notna(), df.key)
        for group, row in chosen.items():
            hit = groups == group
            df.loc[hit, "task_original"] = df.loc[hit, "task_raw"]
            df.loc[hit, "task_correction"] = row["ref"]
            df.loc[hit, "task_raw"] = row["task_to"]
            df.loc[hit, "task"] = normalize_task(row["task_to"])
    summary = {
        "versions": [
            {
                "version": v,
                "sha256": manifest(v).get("sha256"),
                "status": dict(Counter(e["status"] for e in entries(v))),
            }
            for v in names
        ],
        "applied_groups": len(chosen),
        "not_applied": problems,
    }
    return df, summary


def record(version_refs: list[str]) -> dict[str, dict]:
    """``version:id`` -> the proposal with its current decision (for the
    export record and the run-time check)."""
    out = {}
    wanted = defaultdict(set)
    for ref in version_refs:
        version, _, item = ref.partition(":")
        wanted[version].add(item)
    for version, ids in wanted.items():
        try:
            checked_sha256(version)
            rows = entries(version)
        except (KeyError, ValueError):
            continue
        for row in rows:
            if row["id"] in ids:
                out[f"{version}:{row['id']}"] = row
    return out


def verify(episodes: list[dict], recipe: dict | None = None) -> None:
    """At run time: every correction a plan applied is still approved and
    says the same (a decision reversed after planning stops the export), and
    the versions the recipe names hold no two approved corrections of one
    recording that disagree (one approved after planning stops it too)."""
    names = (recipe or {}).get("task_corrections") or []
    if names:
        df = _index()
        if df is not None:
            _, problems = applicable(names, df)
            clash = [p for p in problems if p["problem"] == "conflict"]
            if clash:
                raise ValueError(
                    f"{len(clash)} approved task correction(s) disagree with "
                    "another one for the same recording (e.g. "
                    f"{clash[0]['correction']} and {clash[0]['with']}); reject "
                    "one of them and plan again"
                )
    refs = {e["task_correction"] for e in episodes if e.get("task_correction")}
    if not refs:
        return
    for version in sorted({r.partition(":")[0] for r in refs}):
        try:
            checked_sha256(version)
        except KeyError:
            raise ValueError(
                f"Task correction version {version!r} is gone (plan again)"
            ) from None
    now = record(sorted(refs))
    wrong = []
    for e in episodes:
        ref = e.get("task_correction")
        if not ref:
            continue
        row = now.get(ref)
        if (
            row is None
            or row["status"] != "approved"
            or normalize_task(row["task_to"]) != e["task"]
        ):
            wrong.append(f"{e['key']} ({ref}: {row and row['status']})")
    if wrong:
        raise ValueError(
            f"{len(wrong)} planned task correction(s) are no longer approved as "
            "planned (plan again): " + "; ".join(wrong[:5])
        )


def export_record(episodes: list[dict], recipe: dict) -> dict:
    """What ``pool_export.json`` keeps under ``task_corrections``."""
    names = recipe.get("task_corrections") or []
    refs = sorted({e["task_correction"] for e in episodes if e.get("task_correction")})
    rows = record(refs)
    return {
        "versions": [
            {"version": v, "sha256": manifest(v).get("sha256")} for v in names
        ],
        "applied": [
            {
                "correction": ref,
                "episodes": [
                    e["key"] for e in episodes if e.get("task_correction") == ref
                ],
                "task_from": rows.get(ref, {}).get("task_from"),
                "task_to": rows.get(ref, {}).get("task_to"),
                "source": rows.get(ref, {}).get("source"),
                "confidence": rows.get(ref, {}).get("confidence"),
                "reviewed_by": rows.get(ref, {}).get("reviewed_by"),
                "reviewed_at": rows.get(ref, {}).get("reviewed_at"),
            }
            for ref in refs
        ],
        "episodes_corrected": sum(1 for e in episodes if e.get("task_correction")),
    }


# ------------------------------------------------------------------ copies


def copy_candidates(df: pd.DataFrame | None = None) -> list[dict]:
    """What a person must decide about copies, never resolved silently:

    - ``group_text_differs``: copies of one recording (one group) whose task
      texts differ. The export keeps the canonical member (a folder with a
      standard name before one named like a copy) and its text, and warns;
      a correction decides the text instead.
    - ``nonstandard_text_differs``: a raw capture in a non-standard folder
      (``demo_0022 copy``, ``demo_0038_failure``) with no standard copy in the pool, whose text is
      not its task folder's name: which of the two is right is not known.
      Non-standard folders stay out of exports unless a recipe includes them.
    """
    if df is None:
        from . import index

        df = index.frame()
    rows = df.astype(object).where(pd.notna(df), None).to_dict("records")
    groups = defaultdict(list)
    for row in rows:
        groups[row.get("group") or row["key"]].append(row)
    out = []
    for group, members in sorted(groups.items()):
        texts = {m["task"] for m in members}
        if len(members) > 1 and len(texts) > 1:
            head = next((m for m in members if m.get("canonical")), members[0])
            out.append(
                {
                    "kind": "group_text_differs",
                    "group": group,
                    "kept": head["key"],
                    "kept_task": head["task"],
                    "members": [
                        {
                            "key": m["key"],
                            "task_raw": m.get("task_raw"),
                            "nonstandard": bool(m.get("nonstandard")),
                            "canonical": bool(m.get("canonical")),
                        }
                        for m in members
                    ],
                }
            )
            continue
        if len(members) == 1:
            m = members[0]
            if (
                m.get("format") == "robot_capture"
                and m.get("nonstandard_reason") == "nonstandard_folder_name"
            ):
                folder_task = normalize_task(Path(m["key"]).parent.name)
                if folder_task != m["task"]:
                    out.append(
                        {
                            "kind": "nonstandard_text_differs",
                            "group": group,
                            "key": m["key"],
                            "task_raw": m.get("task_raw"),
                            "folder_task": folder_task,
                        }
                    )
    return out


def conflict_warning(chosen: list[dict], df: pd.DataFrame) -> dict | None:
    """A non-blocking warning: picked episodes whose copies carry another
    task text (and no applied correction decided it)."""
    groups = defaultdict(set)
    for key, group, task in zip(df.key, df.group, df.task, strict=True):
        groups[group or key].add(task)
    hits = [
        r["key"]
        for r in chosen
        if not r.get("task_correction")
        and len(groups.get(r.get("group") or r["key"], ())) > 1
    ]
    if not hits:
        return None
    return {
        "code": "copy_task_conflict",
        "blocking": False,
        "count": len(hits),
        "episodes": hits[:50],
        "message": (
            f"{len(hits)} picked episode(s) have copies carrying another task text; "
            "the canonical copy's text is used. List them with "
            "`levi pool corrections copies`, and settle them with a reviewed "
            "correction / "
            f"{len(hits)} 个选中片段的副本带着不同的任务文本，现用规范副本的文本；"
            "用 `levi pool corrections copies` 列出，经人审核的订正可以定下文本"
        ),
    }
