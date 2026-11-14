"""What the three arm adapters share: reading, the round declaration, the
blindness guard and the write of one record.

Rules (the same for every adapter):

- **Read only the paths the caller names.** Nothing is listed, globbed or found
  by name. A path inside a gold working folder, the ``levi-hub/gold`` subtree or
  a folder named for a frozen or held-out set is refused (``blind_reason``), as
  input and as output: an adapter turns a run's own output into a record, it has
  no business with gold.
- **The round is declared, not inferred.** ``task``, ``model``, ``decoding``,
  ``scoring``, ``split`` and the problem set come from one declaration file
  that all three arms of a round share, so the records cannot differ in them.
  ``split`` is taken as written; a value that is not ``tune``, ``confirm``,
  ``other`` or ``frozen`` is refused. Nothing here guesses a split from a path
  or a name. ``frozen`` (a frozen-set round) is a declaration-only value:
  ``compare.SPLITS`` has no such split, so the record is written as
  ``split="confirm"`` and is held to exactly the confirm rules.
- **Unknown is ``None``, never 0.** A field an artifact does not carry is left
  ``None`` and, where that is surprising, named on the notes the caller prints.
  A field that is present but malformed is an error, not a quiet ``None``.
- **A confirm or frozen round yields aggregates only.** A declared ``frozen``
  becomes ``confirm`` in the record (``FROZEN_SPLIT``), and the finished record
  is checked with ``ArmRecord.confirm_violations`` before anything is written.
  A frozen round declared as ``other`` is not caught by this (the declaration is
  taken as written); what stops it reading blind outputs is the path guard,
  which does not depend on the split.
- **Messages name fields, not content.** An artifact may belong to a confirm
  round; an error says which field is wrong and how, never what it held.
- **The output is new.** It is written to ``<out>.partial`` first and then
  linked to its name, so an existing file is never replaced and a refused or
  failed run leaves nothing behind.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from ..compare import (
    SPLITS,
    WALL_BASES,
    ArmRecord,
    RecordError,
    _canonical,
    _normalize,
    _show,
    episode_set_hash,
)

# A round on a frozen set is declared ``frozen``; compare.SPLITS has no such
# value (its public interface stays as it is), so the record carries the split
# whose rules are the strictest one that exists, ``confirm``.
FROZEN_SPLIT = "frozen"
DECLARED_SPLITS = (*SPLITS, FROZEN_SPLIT)
ROUND_SCHEMA = "levi.eval.arm_round.v1"
SCORES_SCHEMA = "levi.eval.arm_scores.v1"
MAX_BYTES = 64 * 1024 * 1024
# Path components of the gold working folders that the coordinator must not
# read (the workspace's blindness rule); an adapter refuses them as input and
# as output. This is the exact-name part of the guard; ``blind_reason`` adds
# the subtree and the word rules.
BLIND_PARTS = frozenset(
    {"levi-gold-work", "screws-frozen", "generic-frozen", "generic-heldout"}
)
# The whole ``levi-hub/gold`` subtree (every frozen set's gold, whatever the
# name of the folder inside it): two adjacent path components.
BLIND_SUBTREE = ("levi-hub", "gold")
# A directory whose name has a *word* ``frozen`` or ``heldout`` (words are the
# runs of letters and digits between separators; a digit tail is allowed, so
# ``frozen60`` and ``heldout40`` count), a name that ends in ``frozen60``
# (``ScrewsFrozen60``) or the two words ``held`` ``out`` side by side. Matching
# is on whole components and ignores case, never on a substring of a word:
# ``unfrozen`` passes, ``frozen-yogurt`` is refused (the guard only refuses, so
# an unlucky name is renamed, not argued with).
_BLIND_WORD = re.compile(r"(frozen|heldout)(\d\w*)?")
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
ROUND_FIELDS = (
    "schema",
    "task",
    "model",
    "decoding",
    "scoring",
    "split",
    "episodes",
    "episode_set_hash",
    "episode_count",
)
# What a scorer's aggregate output may add to a record.
SCORE_FIELDS = (
    "quality",
    "scored_count",
    "quality_ci",
    "quality_ci_method",
    "quality_delta_ci_vs_teacher",
    "quality_delta_ci_method",
    "agreement_with_teacher",
    "per_episode",
)


class AdapterError(ValueError):
    """An input is malformed or a rule of the adapters would be broken. The
    message never repeats the content of an artifact."""


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate key in an object")
        out[key] = value
    return out


def loads(text: str, what: str):
    """Strict JSON: no NaN or Infinity, no duplicate keys."""
    try:
        return json.loads(text, parse_constant=_no_constant, object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as exc:
        raise AdapterError(f"{what} is not valid JSON ({type(exc).__name__})") from None


def _blind_directory(name: str) -> bool:
    words = [w for w in _NOT_ALNUM.split(name.casefold()) if w]
    if any(_BLIND_WORD.fullmatch(w) or w.endswith("frozen60") for w in words):
        return True
    return any(a == "held" and b == "out" for a, b in pairwise(words))


def blind_reason(path) -> str | None:
    """Why ``path`` lies in a gold, frozen or held-out place (None = it does
    not): the component that matched. Both the path as written and its
    ``resolve()``d form are looked at, so a symlink into such a folder, a
    ``..`` or a relative spelling does not get past. The exact names of
    ``BLIND_PARTS`` match any component; the ``levi-hub/gold`` pair matches
    anywhere in the sequence; the frozen/held-out word rule is applied to
    directory components (the file name itself is not a folder, so a record
    called ``round3-frozen.rec.json`` in a safe folder is fine)."""
    p = Path(path)
    for parts in (p.parts, p.resolve().parts):
        folded = tuple(x.casefold() for x in parts)
        for name in folded:
            if name in BLIND_PARTS:
                return name
        if BLIND_SUBTREE in pairwise(folded):
            return "/".join(BLIND_SUBTREE)
        for name in parts[:-1]:
            if _blind_directory(name):
                return name
    return None


def check_path(path, what: str) -> Path:
    """The path as a ``Path``, refused if it lies in a gold, frozen or
    held-out place (``blind_reason``), whether it is read or written."""
    p = Path(path)
    hit = blind_reason(p)
    if hit:
        raise AdapterError(
            f"{what}: the path lies in a gold working folder, a frozen or a "
            f"held-out place ({hit}); an arm record is built from a run's own "
            "output, never from gold"
        )
    return p


def read_text(path, what: str) -> str:
    p = check_path(path, what)
    try:
        if p.stat().st_size > MAX_BYTES:
            raise AdapterError(f"{what} is larger than {MAX_BYTES} bytes")
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AdapterError(f"{what} cannot be read ({type(exc).__name__})") from None


def read_json(path, what: str):
    return loads(read_text(path, what), what)


def read_jsonl(path, what: str) -> list:
    rows = []
    for number, line in enumerate(read_text(path, what).splitlines(), 1):
        if line.strip():
            rows.append(loads(line, f"{what} line {number}"))
    return rows


# ------------------------------------------------------------------- numbers


def number(value, name: str, *, positive=False, lo=None, hi=None) -> float | None:
    """A finite number or ``None``; anything else is an error."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterError(f"{name} must be a number or null, got {_show(value)}")
    if not math.isfinite(value):
        raise AdapterError(f"{name} must be finite")
    if positive and value <= 0:
        raise AdapterError(f"{name} must be > 0, got {_show(value)}")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise AdapterError(f"{name} must be in [{lo}, {hi}], got {_show(value)}")
    return value


def count(value, name: str) -> int | None:
    """A non-negative integer or ``None``."""
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise AdapterError(f"{name} must be a non-negative integer or null")
    return value


def dig(value, path: str):
    """``(found, value)`` of a dotted path into nested dicts. A key that
    contains a dot cannot be named; the keys of the result files do not."""
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def episode_ids(value, name: str) -> list:
    if not isinstance(value, list) or not value:
        raise AdapterError(f"{name} must be a non-empty list of episode ids")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise AdapterError(f"{name} holds an id that is not an int or a string")
    return value


# ---------------------------------------------------------------- the round


@dataclass(frozen=True)
class Round:
    """The declaration all arms of a round share."""

    task: str
    model: str
    decoding: dict
    scoring: dict
    split: str
    episode_set_hash: str
    episode_count: int
    episode_set: tuple | None

    def check_problems(self, ids, what: str) -> None:
        """The artifact ran exactly the declared problems (compared by hash
        and count; the ids are never shown)."""
        ids = episode_ids(ids, f"{what} episodes")
        if episode_set_hash(ids) != self.episode_set_hash:
            raise AdapterError(
                f"{what} ran a different problem set than the round declares "
                f"(episode_set_hash differs; {len({str(i) for i in ids})} "
                f"distinct problems against {self.episode_count} declared)"
            )

    def check_decoding(self, config, what: str) -> None:
        """A key that the run's own config and the declared decoding share must
        agree; keys only one side has cannot be compared and are not."""
        if not isinstance(config, dict):
            return
        for key in sorted(set(config) & set(self.decoding), key=str):
            if _canonical(_normalize(config[key])) != _canonical(
                _normalize(self.decoding[key])
            ):
                raise AdapterError(
                    f"{what} config.{key} differs from the declared decoding.{key}"
                )


def load_round(path) -> Round:
    row = read_json(path, "the round declaration")
    if not isinstance(row, dict):
        raise AdapterError("the round declaration must be a JSON object")
    if row.get("schema", ROUND_SCHEMA) != ROUND_SCHEMA:
        raise AdapterError(f"the round declaration's schema must be {ROUND_SCHEMA}")
    unknown = sorted(set(row) - set(ROUND_FIELDS), key=str)
    if unknown:
        raise AdapterError(
            f"the round declaration has unknown fields {[_show(k) for k in unknown]}"
        )
    missing = [
        n for n in ("task", "model", "decoding", "scoring", "split") if n not in row
    ]
    if missing:
        raise AdapterError(f"the round declaration lacks {missing}")
    if row["split"] not in DECLARED_SPLITS:
        raise AdapterError(
            f"split must be one of {DECLARED_SPLITS}, got {_show(row['split'])}; "
            "it is taken from the declaration and never inferred"
        )
    ids, digest, total = (
        row.get("episodes"),
        row.get("episode_set_hash"),
        row.get("episode_count"),
    )
    if ids is not None:
        ids = episode_ids(ids, "episodes")
        computed = episode_set_hash(ids)
        unique = len({str(i) for i in ids})
        if digest is not None and digest != computed:
            raise AdapterError("episode_set_hash does not match episodes")
        if total is not None and total != unique:
            raise AdapterError("episode_count does not match episodes")
        digest, total = computed, unique
    if (
        not isinstance(digest, str)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < 1
    ):
        raise AdapterError(
            "the round declaration needs episodes, or episode_set_hash with episode_count"
        )
    return Round(
        task=row["task"],
        model=row["model"],
        decoding=row["decoding"],
        scoring=row["scoring"],
        split="confirm" if row["split"] == FROZEN_SPLIT else row["split"],
        episode_set_hash=digest,
        episode_count=total,
        episode_set=tuple(sorted({str(i) for i in ids})) if ids is not None else None,
    )


def load_scores(path) -> dict:
    """A scorer's aggregate output: only the whitelisted fields."""
    row = read_json(path, "the scores file")
    if not isinstance(row, dict):
        raise AdapterError("the scores file must be a JSON object")
    if row.get("schema", SCORES_SCHEMA) != SCORES_SCHEMA:
        raise AdapterError(f"the scores file's schema must be {SCORES_SCHEMA}")
    row = {k: v for k, v in row.items() if k != "schema"}
    unknown = sorted(set(row) - set(SCORE_FIELDS), key=str)
    if unknown:
        raise AdapterError(
            f"the scores file has fields that are not scores {[_show(k) for k in unknown]}"
        )
    return row


# ---------------------------------------------------------------- the record


def check_basis(basis: str | None) -> str:
    if basis not in WALL_BASES:
        raise AdapterError(
            f"wall_s_basis must be one of {WALL_BASES}, got {_show(basis)}: say "
            "whether this arm's time includes queueing"
        )
    return basis


def quality_fields(
    result, quality_path, count_path, scores: dict | None, notes: list[str]
) -> dict:
    """``quality`` and ``scored_count`` (and the other score fields) from a
    path into the run's result and/or a scorer's file; both for the same field
    is an error."""
    out = dict(scores or {})
    if quality_path:
        if "quality" in out:
            raise AdapterError(
                "quality is given by --quality-path and by the scores file"
            )
        found, value = dig(result, quality_path)
        out["quality"] = number(value, "quality")
        if not found or out["quality"] is None:
            notes.append(
                "quality: the path names nothing in the result, so it is unknown (null)"
            )
        if out["quality"] is not None and "scored_count" not in out:
            if count_path is None:
                head, _, _ = quality_path.rpartition(".")
                count_path = f"{head}.episodes" if head else "episodes"
            found, value = dig(result, count_path)
            out["scored_count"] = count(value if found else None, "scored_count")
    elif count_path:
        raise AdapterError("--scored-count-path needs --quality-path")
    return out


def build(round_: Round, arm: str, notes: list[str], **fields) -> dict:
    """The validated record as a dict; refuses anything ``ArmRecord`` refuses
    and, for a confirm round, anything a confirm table would."""
    if fields.get("quality") is not None and fields.get("scored_count") is None:
        raise AdapterError(
            "a quality without scored_count: say over how many problems it was "
            "computed (--scored-count-path, or scored_count in the scores file)"
        )
    if fields.get("wall_s") is None:
        fields["wall_s_basis"] = None
    ids = {}
    if round_.episode_set is not None:
        ids["episode_set"] = round_.episode_set
    try:
        record = ArmRecord(
            arm=arm,
            task=round_.task,
            model=round_.model,
            decoding=round_.decoding,
            scoring=round_.scoring,
            split=round_.split,
            episode_set_hash=round_.episode_set_hash,
            episode_count=round_.episode_count,
            **ids,
            **fields,
        )
    except RecordError as exc:
        raise AdapterError(f"the record is not valid: {exc}") from None
    if record.split == "confirm":
        problems = record.confirm_violations()
        if problems:
            raise AdapterError(
                "a confirm record may hold aggregate numbers and short codes "
                "only: " + "; ".join(problems)
            )
    row = record.to_dict()
    try:
        again = ArmRecord.from_dict(row)
    except RecordError as exc:  # pragma: no cover - a guard, not a path
        raise AdapterError(f"the record does not read back: {exc}") from None
    if again != record:  # pragma: no cover
        raise AdapterError("the record does not read back as written")
    if record.scored_count is not None and record.scored_count < record.episode_count:
        notes.append(
            f"scored_count {record.scored_count} is below episode_count "
            f"{record.episode_count}: compare.py will refuse a table with this "
            "record (a blocked or failed problem stays in the denominator)"
        )
    return row


def write(row: dict, out) -> Path:
    """Write ``row`` as JSON to a path that does not exist yet: first to
    ``<out>.partial`` (created exclusively), then linked to its name. Nothing is
    replaced, and the partial file is removed whatever happens."""
    path = check_path(out, "the output path")
    if os.path.lexists(path):
        raise AdapterError("the output path already exists; it is never overwritten")
    if not path.parent.is_dir():
        raise AdapterError("the output directory does not exist")
    partial = path.with_name(path.name + ".partial")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise AdapterError("the output's .partial file exists (another run?)") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(row, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(partial, path)
        except FileExistsError:
            raise AdapterError(
                "the output path already exists; it is never overwritten"
            ) from None
    finally:
        partial.unlink(missing_ok=True)
    return path
