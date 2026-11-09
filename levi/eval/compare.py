"""Teacher / LEVI / direct comparison of one round: up to four arms, one table.

Every round is run several ways on the same problems, with the same decoding
and the same scoring (``levi.eval.arm_record.v2``):

- ``teacher``: the external agent (the source of the paradigm and the
  experience entries). Its answer is a reference, not the gold standard;
- ``prior_levi``: the local VLM through the LEVI harness as it was before
  LEVI 2.0 (arm B of the three-arm experiment);
- ``levi2``: the local VLM through the guided LEVI 2.0 harness (arm C);
- ``direct``: the same local VLM called directly, without LEVI (arm A).

``arm_record.v1`` files are still read: ``local_direct`` becomes ``direct`` and
``local_harness`` becomes ``prior_levi`` (it predates LEVI 2.0; see
``LEGACY_ARMS``). The headline arm is ``levi2`` if the round has it, otherwise
``prior_levi``.

The headline is whether the headline arm reaches at least the teacher's
quality while being faster and spending fewer external tokens, so the table
carries quality per second and quality per external token, not accuracy alone.
"At least the teacher's quality" is a non-inferiority claim, judged on an
interval, not on two point estimates (a confirm round has about six problems,
and one flipped problem moves a rate by 0.14 to 0.17): ``scoring`` states the
direction (``higher_is_better``) and the margin δ
(``non_inferiority_margin``) before the round, the scorer who can see the
problems one by one supplies the paired difference interval
(``quality_delta_ci_vs_teacher``, student minus teacher), and the answer is yes
if its worse end is still within δ, no if even its better end is beyond δ,
undetermined otherwise. The point estimates stay in the table as a side line.

Rules this module enforces:

- **Same problems, same conditions.** Records whose task, episode-set hash,
  decoding or scoring differ are refused, with the differences named. So is a
  round with two records for one arm, or with records of different splits.
- **Unknown is not zero.** ``wall_s``, ``quality``, ``local_tokens`` and each
  external token field may be ``None`` (not measured); the table prints
  ``未知`` and every ratio or difference that needs it is ``未知``, not a
  number made from zero. A true zero (a local arm that spent no external
  tokens) is written as ``0`` by the caller.
- **Confirm stays blind, and the record says so.** Every record names its
  ``split`` (``tune``, ``confirm`` or ``other``). A record whose split is
  ``confirm`` is held to the confirm rules; no flag a caller may forget
  switches them on. In a confirm table only aggregate numbers and short
  codes (``CODE``: at most 64 characters of ``A-Za-z0-9_.:+-``) can appear:
  a record with per-episode scores, an explicit episode list, a ``decoding`` or
  ``scoring`` that is not a short flat dict of codes and numbers, or free text
  in ``task``, ``model``, ``quality_ci_method``, ``blocked`` or
  ``external_tokens_source`` is refused, and so is a table that mixes splits.
  Unknown fields are refused in every mode. The refusal message never repeats
  the content it refused. What the guard cannot do is judge intent: a caller
  who writes an episode-specific word as a "code" gets it through, but only a
  few of 64 characters per field, never a score list or a sentence.
- **Time is compared like with like.** ``wall_s_basis`` says whether a time
  includes queueing (``includes_queue``: a sub-agent's duration does) or not
  (``excludes_queue``); a difference of times, and "faster", are only given
  between arms of the same stated basis, otherwise they are unknown.
- **The direct arm is part of the round.** Without ``direct`` the
  table is built but its headline is marked incomplete and is never "yes".
- **External tokens are four separate fields** (``input_new``, ``output``,
  ``cache_read``, ``cache_write``, as in ``levi.aeri.external_report.v1``), and
  there are two totals side by side, because "fewer external tokens" can flip
  with the choice: ``fresh`` = ``input_new + output + cache_write`` (what the
  external model newly read or wrote; ``cache_read`` is cheap re-reading of a
  cached prefix) and ``all`` = the four. Each has its own quality per 1,000
  tokens and difference, and is unknown as soon as one of its fields is. The
  headline answers "fewer external tokens" under both (yes only if both say yes,
  no only if both say no, undetermined if they disagree, and the table says it
  was the basis), or under one basis that was registered before the round
  (``verdict_basis="fresh"`` or ``"all"``, written in the table).

Records come from ``ArmRecord`` (JSON via ``to_dict`` / ``from_dict``);
``levi.eval.tokens`` produces the teacher's ``external_tokens`` from session
files. Nothing here reads datasets, calls a model, uses a GPU or opens a port.

    python -m levi.eval.compare round_teacher.json round_harness.json round_direct.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .tokens import FIELDS as TOKEN_FIELDS
from .tokens import MAX_COUNT

ARM_SCHEMA = "levi.eval.arm_record.v2"
ARM_SCHEMA_V1 = "levi.eval.arm_record.v1"
TABLE_SCHEMA = "levi.eval.compare.v1"
# teacher: the external agents (reference arm T); prior_levi: the LEVI harness
# before LEVI 2.0 (arm B); levi2: the guided LEVI 2.0 harness (arm C); direct:
# the same local model called without LEVI (arm A).
ARMS = ("teacher", "prior_levi", "levi2", "direct")
LOCAL_ARMS = ("prior_levi", "levi2", "direct")
# v1 arm names. ``local_harness`` was "the local VLM through LEVI's harness" at
# a time when no LEVI 2.0 existed, so its records are the prior LEVI, not LEVI
# 2.0: mapping it to ``levi2`` would credit an old result to the new method.
LEGACY_ARMS = {"local_harness": "prior_levi", "local_direct": "direct"}
SPLITS = ("tune", "confirm", "other")
REQUIRED = ("arm", "task", "model", "decoding", "scoring", "split")
# input_new + output + cache_write; cache_read is a column of its own.
FRESH_BASIS = ("input_new", "output", "cache_write")
ALL_BASIS = TOKEN_FIELDS
BASES = {"fresh": FRESH_BASIS, "all": ALL_BASIS}
VERDICT_BASES = ("both", "fresh", "all")
WALL_BASES = ("includes_queue", "excludes_queue")
UNKNOWN = "未知"
NOT_APPLICABLE = "不适用"
NONE_SELF = "—"
NO_ARM = "无此臂"
# What a free-text field may hold in confirm mode: a short reason code.
CODE = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")
MAX_TEXT = 200
# A confirm record's decoding / scoring: a flat dict of codes and numbers,
# at most this many entries and this many characters once written as JSON.
MAX_FLAT_ITEMS = 16
MAX_FLAT_CHARS = 256


class RecordError(ValueError):
    """An arm record is malformed (wrong type, out of range, unknown field)."""


class CompareError(ValueError):
    """The records cannot be put in one table. ``problems`` lists every reason."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


def _canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def episode_set_hash(episodes: Iterable) -> str:
    """Order-independent sha256 of the episode ids (an int and its string are
    the same episode). Two rounds ran the same problems iff this is equal."""
    ids = []
    for episode in episodes:
        if isinstance(episode, bool) or not isinstance(episode, (int, str)):
            raise RecordError(
                f"episode id must be an int or a string, got {_show(episode)}"
            )
        ids.append(str(episode))
    return hashlib.sha256(_canonical(sorted(set(ids))).encode()).hexdigest()


# ---------------------------------------------------------------- validation


def _show(value) -> str:
    """A value for an error message. A record may carry confirm content, and
    the message goes to people who must not see it: numbers and short codes are
    repeated, anything else is described by its kind and size."""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return (
            repr(value)
            if CODE.fullmatch(value)
            else f"a string of {len(value)} characters"
        )
    if isinstance(value, (list, tuple, dict, set)):
        return f"a {type(value).__name__} of {len(value)} items"
    return f"a {type(value).__name__}"


def _real(value, name, *, lo=None, hi=None, positive=False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecordError(f"{name} must be a number, got {_show(value)}")
    value = float(value)
    if not math.isfinite(value):
        raise RecordError(f"{name} must be finite, got {_show(value)}")
    if positive and value <= 0:
        raise RecordError(f"{name} must be > 0, got {_show(value)}")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise RecordError(f"{name} must be in [{lo}, {hi}], got {_show(value)}")
    return value


def _count(value, name) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise RecordError(
            f"{name} must be an integer in [0, 2^53-1], got {_show(value)}"
        )
    return value


def _text(value, name) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise RecordError(
            f"{name} must be a non-empty string of at most {MAX_TEXT} characters"
        )
    if "\n" in value or "\r" in value:
        raise RecordError(f"{name} must be one line")
    return value.strip()


def _normalize(value):
    """Plain JSON with whole floats written as ints, so ``0`` and ``0.0`` (and
    ``1`` and ``1.0``) are one value: two arms that declared the same decoding
    in two spellings must not be refused as different."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _json_copy(value, name):
    try:
        return _normalize(json.loads(_canonical(value)))
    except (TypeError, ValueError, RecursionError) as exc:
        raise RecordError(f"{name} must be plain JSON") from exc


_ABSENT = "<absent>"


def _first_difference(a, b, path: str = ""):
    """(path, a's value, b's value) of the first place two JSON values differ,
    keys visited in sorted order; ``None`` if they are equal. A key one side
    lacks has the value ``_ABSENT``."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            where = f"{path}.{key}" if path else str(key)
            if key not in a or key not in b:
                return where, a.get(key, _ABSENT), b.get(key, _ABSENT)
            found = _first_difference(a[key], b[key], where)
            if found:
                return found
        return None
    return None if _canonical(a) == _canonical(b) else (path, a, b)


def _not_flat_codes(value) -> str | None:
    """Why ``value`` is not allowed in a confirm record (None = allowed): a
    code, or a short flat dict whose keys are codes and whose values are codes,
    numbers or booleans. Nesting, free text and length are what a sentence or
    a score list would need to ride along."""
    if isinstance(value, str):
        return None if CODE.fullmatch(value) else "not a code"
    if not isinstance(value, dict):
        return "neither a code nor a dict"
    if len(value) > MAX_FLAT_ITEMS:
        return f"more than {MAX_FLAT_ITEMS} entries"
    if len(_canonical(value)) > MAX_FLAT_CHARS:
        return f"longer than {MAX_FLAT_CHARS} characters"
    for key, item in value.items():
        if not CODE.fullmatch(key):
            return "a key that is not a code"
        if isinstance(item, str):
            if not CODE.fullmatch(item):
                return "a value that is not a code"
        elif not isinstance(item, (bool, int, float)):
            return "a nested or empty value"
    return None


@dataclass(frozen=True)
class ArmRecord:
    """One arm of one round. ``None`` in a measured field means not measured
    (unknown), never zero.

    ``split`` says which data the arm ran on (``tune``, ``confirm`` or
    ``other``); a ``confirm`` record is held to the confirm rules in every table
    (see the module docstring).

    ``scoring`` must state ``higher_is_better`` (bool) and
    ``non_inferiority_margin`` (δ, a number >= 0, declared before the round).
    ``quality`` is the primary metric against the gold standard (the metric is
    named in ``scoring``); ``quality_ci`` its interval.
    ``quality_delta_ci_vs_teacher`` is the interval of the paired difference
    (this arm's quality minus the teacher's, same problems), computed by
    whoever can see the problems one by one; the table only reads it (not on
    the teacher arm), and ``quality_delta_ci_method`` says how (level, method).
    ``agreement_with_teacher`` is the share of the teacher's answers this arm
    reproduces (not set on the teacher arm).

    ``wall_s`` is wall-clock seconds; ``wall_s_basis`` says whether it includes
    queueing (``includes_queue``: a sub-agent's duration does) or not
    (``excludes_queue``), ``None`` = not stated.

    ``external_tokens`` has the four fields of ``levi.aeri.external_report.v1``;
    ``None`` for the whole dict or one field means unknown, ``0`` means measured
    zero. ``external_tokens_exact`` says whether those counts are complete:
    ``False`` = lower bounds (a transcript with lines set aside, sub-agents
    missing, ...; ``levi.eval.tokens.Usage.exact``), ``True`` = exact, ``None``
    = not stated. A lower bound is marked in the table and never decides "fewer
    external tokens" for the student arm.

    The problems are given either as ``episode_set`` (the ids) or as
    ``episode_set_hash`` plus ``episode_count``; if both are given they must
    agree. ``scored_count`` is how many of them ``quality`` was computed over:
    it must equal ``episode_count`` in any table (a problem the arm was blocked
    on or failed stays in the denominator, scored as a failure; an arm that
    dropped it would be measured on an easier set). ``per_episode`` (episode
    id -> score) is the one per-item field allowed outside a confirm record.
    """

    arm: str
    task: str
    model: str
    decoding: dict
    scoring: dict
    split: str
    episode_set_hash: str | None = None
    episode_count: int | None = None
    episode_set: tuple | None = None
    scored_count: int | None = None
    quality: float | None = None
    quality_ci: tuple[float, float] | None = None
    quality_ci_method: str | None = None
    quality_delta_ci_vs_teacher: tuple[float, float] | None = None
    quality_delta_ci_method: str | None = None
    agreement_with_teacher: float | None = None
    wall_s: float | None = None
    wall_s_basis: str | None = None
    local_tokens: int | None = None
    external_tokens: dict | None = None
    external_tokens_exact: bool | None = None
    external_tokens_source: str | None = None
    blocked: str | None = None
    per_episode: dict | None = None

    def __post_init__(self):
        def put(name, value):
            object.__setattr__(self, name, value)

        if isinstance(self.arm, str) and self.arm in LEGACY_ARMS:
            put("arm", LEGACY_ARMS[self.arm])
        if self.arm not in ARMS:
            raise RecordError(f"arm must be one of {ARMS}, got {_show(self.arm)}")
        if self.split not in SPLITS:
            raise RecordError(f"split must be one of {SPLITS}, got {_show(self.split)}")
        for name in ("task", "model"):
            if getattr(self, name) is None:
                raise RecordError(f"{name} is required")
            put(name, _text(getattr(self, name), name))
        if not isinstance(self.decoding, dict) or not self.decoding:
            raise RecordError(
                "decoding must be a non-empty dict: the round's declared decoding "
                "(an empty one would match any other and compare nothing)"
            )
        put("decoding", _json_copy(self.decoding, "decoding"))
        scoring = _json_copy(self.scoring, "scoring")
        if not isinstance(scoring, dict):
            raise RecordError(
                "scoring must be a dict that states higher_is_better and "
                "non_inferiority_margin (the direction of the metric and the "
                "margin δ declared before the round)"
            )
        if not isinstance(scoring.get("higher_is_better"), bool):
            raise RecordError("scoring.higher_is_better must be true or false")
        margin = scoring.get("non_inferiority_margin")
        if (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or margin < 0
        ):
            raise RecordError("scoring.non_inferiority_margin must be a number >= 0")
        put("scoring", scoring)

        ids, count, digest = self.episode_set, self.episode_count, self.episode_set_hash
        if ids is not None:
            computed = episode_set_hash(ids)
            unique = len({str(e) for e in ids})
            if digest is not None and digest != computed:
                raise RecordError("episode_set_hash does not match episode_set")
            if count is not None and count != unique:
                raise RecordError("episode_count does not match episode_set")
            digest, count = computed, unique
            put("episode_set", tuple(sorted({str(e) for e in ids})))
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RecordError(
                "episode_set_hash (64 hex digits) or episode_set is required"
            )
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise RecordError("episode_count (>= 1) or episode_set is required")
        put("episode_set_hash", digest)
        put("episode_count", count)

        scored = _count(self.scored_count, "scored_count")
        if scored is not None and scored > count:
            raise RecordError("scored_count is larger than episode_count")
        put("scored_count", scored)
        put("quality", _real(self.quality, "quality"))
        if self.quality_ci is not None:
            ci = self.quality_ci
            if not isinstance(ci, (list, tuple)) or len(ci) != 2:
                raise RecordError("quality_ci must be [low, high]")
            low, high = _real(ci[0], "quality_ci[0]"), _real(ci[1], "quality_ci[1]")
            if low is None or high is None or low > high:
                raise RecordError("quality_ci must be [low, high] with low <= high")
            put("quality_ci", (low, high))
        put("quality_ci_method", _text(self.quality_ci_method, "quality_ci_method"))
        if self.quality_delta_ci_vs_teacher is not None:
            if self.arm == "teacher":
                raise RecordError("the teacher arm has no quality_delta_ci_vs_teacher")
            ci = self.quality_delta_ci_vs_teacher
            if self.quality is None:
                raise RecordError(
                    "quality_delta_ci_vs_teacher is the interval of a difference "
                    "of qualities: it needs this arm's quality"
                )
            if not isinstance(ci, (list, tuple)) or len(ci) != 2:
                raise RecordError("quality_delta_ci_vs_teacher must be [low, high]")
            low = _real(ci[0], "quality_delta_ci_vs_teacher[0]")
            high = _real(ci[1], "quality_delta_ci_vs_teacher[1]")
            if low is None or high is None or low > high:
                raise RecordError(
                    "quality_delta_ci_vs_teacher must be [low, high] with low <= high"
                )
            put("quality_delta_ci_vs_teacher", (low, high))
        elif self.quality_delta_ci_method is not None:
            raise RecordError(
                "quality_delta_ci_method needs quality_delta_ci_vs_teacher"
            )
        put(
            "quality_delta_ci_method",
            _text(self.quality_delta_ci_method, "quality_delta_ci_method"),
        )
        agreement = _real(
            self.agreement_with_teacher, "agreement_with_teacher", lo=0, hi=1
        )
        if agreement is not None and self.arm == "teacher":
            raise RecordError("the teacher arm has no agreement_with_teacher")
        put("agreement_with_teacher", agreement)
        put("wall_s", _real(self.wall_s, "wall_s", positive=True))
        if self.wall_s_basis is not None:
            if self.wall_s_basis not in WALL_BASES:
                raise RecordError(
                    f"wall_s_basis must be one of {WALL_BASES}, "
                    f"got {_show(self.wall_s_basis)}"
                )
            if self.wall_s is None:
                raise RecordError("wall_s_basis needs wall_s")
        put("local_tokens", _count(self.local_tokens, "local_tokens"))

        external = self.external_tokens
        if external is not None:
            if not isinstance(external, dict) or set(external) - set(TOKEN_FIELDS):
                raise RecordError(
                    f"external_tokens has the fields {TOKEN_FIELDS} only, "
                    f"got {_show(external)}"
                )
            external = {
                name: _count(external.get(name), f"external_tokens.{name}")
                for name in TOKEN_FIELDS
            }
        put("external_tokens", external)
        exact = self.external_tokens_exact
        if exact is not None:
            if not isinstance(exact, bool):
                raise RecordError("external_tokens_exact must be true, false or null")
            if external is None:
                raise RecordError("external_tokens_exact needs external_tokens")
        put(
            "external_tokens_source",
            _text(self.external_tokens_source, "external_tokens_source"),
        )
        put("blocked", _text(self.blocked, "blocked"))

        if self.per_episode is not None:
            if not isinstance(self.per_episode, dict):
                raise RecordError("per_episode must be a dict of episode id -> score")
            put(
                "per_episode",
                {
                    str(k): _real(v, f"per_episode[{k}]")
                    for k, v in sorted(
                        self.per_episode.items(), key=lambda kv: str(kv[0])
                    )
                },
            )

    def external(self, name: str) -> int | None:
        """One external token field; ``None`` is unknown."""
        return None if self.external_tokens is None else self.external_tokens[name]

    def confirm_violations(self) -> list[str]:
        """What keeps this record out of a confirm table (empty = allowed).
        The messages name fields, never their content."""
        found = []
        if self.per_episode is not None:
            found.append("per_episode scores")
        if self.episode_set is not None:
            found.append(
                "an explicit episode_set (use episode_set_hash and episode_count)"
            )
        for name in (
            "task",
            "model",
            "quality_ci_method",
            "quality_delta_ci_method",
            "blocked",
            "external_tokens_source",
        ):
            value = getattr(self, name)
            if value is not None and not CODE.fullmatch(value):
                found.append(
                    f"{name} text (only a short code like 'gpu_busy' is allowed)"
                )
        for name in ("decoding", "scoring"):
            why = _not_flat_codes(getattr(self, name))
            if why:
                found.append(
                    f"{name} that is not a short flat dict of codes and numbers ({why})"
                )
        return found

    def to_dict(self) -> dict:
        row = {"schema": ARM_SCHEMA}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            row[name] = list(value) if isinstance(value, tuple) else value
        return row

    @classmethod
    def from_dict(cls, row: dict) -> ArmRecord:
        if not isinstance(row, dict):
            raise RecordError(
                f"an arm record is a JSON object, got {type(row).__name__}"
            )
        row = dict(row)
        schema = row.pop("schema", ARM_SCHEMA)
        if schema not in (ARM_SCHEMA, ARM_SCHEMA_V1):
            raise RecordError(
                f"unsupported schema {_show(schema)}, expected {ARM_SCHEMA} "
                f"(or {ARM_SCHEMA_V1}, read with its arm names mapped)"
            )
        arm = row.get("arm")
        if schema == ARM_SCHEMA_V1:
            if arm in ("levi2", "prior_levi", "direct"):
                raise RecordError(f"arm {_show(arm)} does not exist in {ARM_SCHEMA_V1}")
        elif isinstance(arm, str) and arm in LEGACY_ARMS:
            raise RecordError(
                f"arm {_show(arm)} is a v1 name; a {ARM_SCHEMA} record uses "
                f"{LEGACY_ARMS[arm]!r}"
            )
        unknown = sorted(set(row) - set(cls.__dataclass_fields__), key=str)
        if unknown:
            shown = [_show(name) for name in unknown]
            raise RecordError(
                f"unknown fields {shown}: a record holds only the fields of {schema}"
            )
        missing = [n for n in REQUIRED if n not in row]
        if missing:
            raise RecordError(f"missing fields {missing}")
        if row.get("episode_set") is not None:
            if not isinstance(row["episode_set"], (list, tuple)):
                raise RecordError("episode_set must be a list of episode ids")
            row["episode_set"] = tuple(row["episode_set"])
        return cls(**row)


# ---------------------------------------------------------------- the table


def _total(record: ArmRecord, basis) -> int | None:
    values = [record.external(name) for name in basis]
    return None if any(v is None for v in values) else sum(values)


def _ratio(numerator, denominator):
    """(value, status): status is ``unknown`` or ``undefined`` when no number."""
    if numerator is None or denominator is None:
        return None, "unknown"
    if denominator == 0:
        return None, "undefined"
    return numerator / denominator, "ok"


def _delta(mine, reference):
    return None if mine is None or reference is None else mine - reference


def _is_bound(row: dict) -> bool:
    """The row's external token counts are lower bounds (stated, not guessed)."""
    return row["external_tokens_exact"] is False


def _fewer(student: dict, teacher: dict, key: str) -> bool | None:
    """Does the student arm spend fewer external tokens than the teacher arm?

    A lower bound is not a count. If the student's is one, its true total may
    be anything above it, so the answer is unknown whatever the numbers say. If
    the teacher's is one, the student is certainly lower when it is below that
    bound, and otherwise nothing can be said (the teacher may have used more)."""
    mine, reference = student[key], teacher[key]
    if mine is None or reference is None or _is_bound(student):
        return None
    if _is_bound(teacher):
        return True if mine < reference else None
    return mine < reference


def _row(record: ArmRecord, confirm: bool) -> dict:
    totals = {name: _total(record, basis) for name, basis in BASES.items()}
    per_second, second_status = _ratio(record.quality, record.wall_s)
    per_token = {name: _ratio(record.quality, t) for name, t in totals.items()}
    if not record.scoring["higher_is_better"]:
        # An error per second or per token does not grow with merit.
        per_second, second_status = None, "undefined"
        per_token = dict.fromkeys(BASES, (None, "undefined"))
    # (column, value, why it has no value)
    columns = [
        ("quality", record.quality, "unknown"),
        ("wall_s", record.wall_s, "unknown"),
        ("local_tokens", record.local_tokens, "unknown"),
        *[
            (f"external_tokens.{n}", record.external(n), "unknown")
            for n in TOKEN_FIELDS
        ],
        *[(f"external_tokens_total_{n}", t, "unknown") for n, t in totals.items()],
        ("quality_per_second", per_second, second_status),
        *[
            (f"quality_per_external_token_{n}", value, status)
            for n, (value, status) in per_token.items()
        ],
    ]
    row = {
        "arm": record.arm,
        "model": record.model,
        "quality": record.quality,
        "quality_ci": list(record.quality_ci) if record.quality_ci else None,
        "quality_ci_method": record.quality_ci_method,
        "quality_delta_ci_vs_teacher": (
            list(record.quality_delta_ci_vs_teacher)
            if record.quality_delta_ci_vs_teacher
            else None
        ),
        "quality_delta_ci_method": record.quality_delta_ci_method,
        "agreement_with_teacher": record.agreement_with_teacher,
        "wall_s": record.wall_s,
        "wall_s_basis": record.wall_s_basis,
        "local_tokens": record.local_tokens,
        "external_tokens": {name: record.external(name) for name in TOKEN_FIELDS},
        **{f"external_tokens_total_{n}": t for n, t in totals.items()},
        "external_tokens_exact": record.external_tokens_exact,
        "external_tokens_source": record.external_tokens_source,
        "quality_per_second": per_second,
        **{f"quality_per_external_token_{n}": v for n, (v, _) in per_token.items()},
        "blocked": record.blocked,
    }
    if not confirm:
        row["per_episode"] = record.per_episode
    row["unknown"] = [n for n, v, why in columns if v is None and why == "unknown"]
    row["undefined"] = [n for n, v, why in columns if v is None and why == "undefined"]
    return row


def _wall_problem(row: dict, reference: dict) -> str | None:
    """Why two measured times cannot be compared (None = they can, or one is
    not measured at all): a time with and one without queueing are not the same
    quantity, and an unstated basis cannot be told."""
    if row["wall_s"] is None or reference["wall_s"] is None:
        return None
    if row["wall_s_basis"] is None or reference["wall_s_basis"] is None:
        return "unstated"
    return None if row["wall_s_basis"] == reference["wall_s_basis"] else "different"


def _vs(row: dict, reference: dict | None, key: str) -> dict | None:
    """Differences ``row - reference``; ``None`` if the round has no such arm."""
    if reference is None:
        return None
    if reference is row:
        return {"self": True}
    out = {
        "quality_delta": _delta(row["quality"], reference["quality"]),
        "wall_s_delta": (
            None
            if _wall_problem(row, reference)
            else _delta(row["wall_s"], reference["wall_s"])
        ),
        **{
            f"external_tokens_delta_{n}": _delta(
                row[f"external_tokens_total_{n}"],
                reference[f"external_tokens_total_{n}"],
            )
            for n in BASES
        },
    }
    for name, value in out.items():
        if value is None:
            row["unknown"].append(f"{key}.{name}")
    out["wall_s_problem"] = _wall_problem(row, reference)
    # A difference with a lower bound in it is itself only a bound.
    out["external_tokens_delta_is_bound"] = _is_bound(row) or _is_bound(reference)
    # The paired interval exists only against the teacher (the scorer pairs the
    # problems arm by arm).
    out["quality_delta_ci"] = (
        row["quality_delta_ci_vs_teacher"] if key == "vs_teacher" else None
    )
    return out


def _non_inferior(interval, higher_is_better: bool, margin: float) -> str:
    """Is the student non-inferior to the teacher, from the paired interval of
    (student - teacher)? ``yes`` if the worse end is within the margin, ``no``
    if even the better end is beyond it, ``undetermined`` if the interval
    covers the margin, ``unknown`` if there is no interval. For a metric where
    smaller is better (an error) the interval is turned around first."""
    if interval is None:
        return "unknown"
    low, high = interval
    if not higher_is_better:
        low, high = -high, -low
    if low >= -margin:
        return "yes"
    if high < -margin:
        return "no"
    return "undetermined"


def _point_not_worse(mine, reference, higher_is_better: bool):
    if mine is None or reference is None:
        return None
    return mine >= reference if higher_is_better else mine <= reference


def _faster(student: dict, teacher: dict) -> bool | None:
    if _wall_problem(student, teacher) or None in (
        student["wall_s"],
        teacher["wall_s"],
    ):
        return None
    return student["wall_s"] < teacher["wall_s"]


def _verdict(
    student: dict,
    teacher: dict | None,
    scoring: dict,
    verdict_basis: str = "both",
    complete: bool = True,
) -> dict | None:
    """The round's headline: the harness arm against the teacher arm.

    Quality is a non-inferiority call on the paired difference interval against
    the margin declared in ``scoring``; the point estimates are a side line that
    never enters ``all``. Time and external tokens are measured totals, compared
    as numbers (wall time only when both are of the same basis)."""
    if teacher is None or student is None:
        return None
    higher, margin = scoring["higher_is_better"], scoring["non_inferiority_margin"]
    interval = student["quality_delta_ci_vs_teacher"]
    if student["quality"] is None or teacher["quality"] is None:
        interval = None  # an interval of a difference needs both qualities
    status = _non_inferior(interval, higher, margin)
    checks = {
        "quality_non_inferior": {"yes": True, "no": False}.get(status),
        "quality_status": status,
        "quality_point_not_worse": _point_not_worse(
            student["quality"], teacher["quality"], higher
        ),
        "faster": _faster(student, teacher),
        "faster_problem": _wall_problem(student, teacher),
        "complete": complete,
    }
    fewer = {n: _fewer(student, teacher, f"external_tokens_total_{n}") for n in BASES}
    checks["fewer_external_tokens_fresh"] = fewer["fresh"]
    checks["fewer_external_tokens_all"] = fewer["all"]
    if verdict_basis == "both":
        # Both bases must agree: a claim that one choice of denominator can
        # reverse is not made.
        answers = list(fewer.values())
        checks["fewer_external_tokens"] = (
            answers[0] if answers[0] is not None and answers[0] == answers[1] else None
        )
        checks["fewer_external_tokens_basis_dependent"] = (
            None not in answers and answers[0] != answers[1]
        )
    else:
        checks["fewer_external_tokens"] = fewer[verdict_basis]
        checks["fewer_external_tokens_basis_dependent"] = False
    checks["fewer_external_tokens_basis"] = verdict_basis
    needed = [
        checks["quality_non_inferior"],
        checks["faster"],
        checks["fewer_external_tokens"],
    ]
    checks["all"] = False if False in needed else None if None in needed else True
    if not complete and checks["all"]:
        checks["all"] = None  # a round without the direct arm is not a round
    checks["non_inferiority"] = {
        "higher_is_better": higher,
        "margin": margin,
        "delta_ci": student["quality_delta_ci_vs_teacher"],
        "method": student["quality_delta_ci_method"],
    }
    return checks


@dataclass(frozen=True)
class Table:
    conditions: dict
    rows: list[dict]
    missing_arms: list[str]
    main_metric: dict | None
    main_metric_arm: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema": TABLE_SCHEMA,
            "conditions": self.conditions,
            "rows": self.rows,
            "missing_arms": self.missing_arms,
            "main_metric": self.main_metric,
            "main_metric_arm": self.main_metric_arm,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)

    def to_markdown(self) -> str:
        return _markdown(self)


def compare(
    records: Iterable[ArmRecord],
    *,
    assert_confirm: bool = False,
    verdict_basis: str = "both",
) -> Table:
    """One table for one round. Raises ``CompareError`` if the records were not
    run on the same problems under the same conditions (or break the confirm
    rules); the message names every difference.

    Whether this is a confirm table is not a choice of the caller: it is
    decided by the records' ``split``. ``assert_confirm=True`` only adds the
    demand that every record is a ``confirm`` one (the command line's
    ``--confirm``), so a round that was meant to be confirm cannot pass as
    another split.

    ``verdict_basis`` is how "fewer external tokens" is decided: ``"both"``
    (default: the ``fresh`` and the ``all`` total must agree) or the one basis
    registered before the round, ``"fresh"`` or ``"all"``. Both totals are in
    the table either way."""
    records = list(records)
    problems = []
    if not records:
        raise CompareError("no records to compare")
    for record in records:
        if not isinstance(record, ArmRecord):
            raise CompareError(f"not an ArmRecord: {type(record).__name__}")
    if verdict_basis not in VERDICT_BASES:
        raise CompareError(
            f"verdict_basis must be one of {VERDICT_BASES}, got {_show(verdict_basis)}"
        )

    arms = [r.arm for r in records]
    problems += [f"two records for arm {arm}" for arm in ARMS if arms.count(arm) > 1]
    splits = {r.split for r in records}
    if len(splits) > 1:
        shown = ", ".join(f"{r.arm}={r.split}" for r in records)
        problems.append(
            f"records come from different splits ({shown}); one table holds one split"
        )
    if assert_confirm:
        problems += [
            f"a confirm round was asserted but arm {r.arm} is split={r.split}"
            for r in records
            if r.split != "confirm"
        ]
    confirm = splits == {"confirm"}
    for record in records:
        if record.split == "confirm":
            problems += [
                f"confirm record refused: {violation} (arm {record.arm}); "
                "only aggregate scores and short codes are allowed"
                for violation in record.confirm_violations()
            ]
    if problems:
        # Before any difference is shown: a refused record's content must not
        # come back in the message.
        raise CompareError(problems)
    labels = [
        r.arm if arms.count(r.arm) == 1 else f"{r.arm}#{i + 1}"
        for i, r in enumerate(records)
    ]
    for name, getter in (
        ("task", lambda r: r.task),
        ("episode_set_hash", lambda r: r.episode_set_hash),
        ("episode_count", lambda r: r.episode_count),
        ("decoding", lambda r: r.decoding),
        ("scoring", lambda r: r.scoring),
    ):
        values = [getter(r) for r in records]
        if len({_canonical(v) for v in values}) > 1:
            problems.append(_difference(name, labels, values))
    local = [r for r in records if r.arm in LOCAL_ARMS]
    models = {r.model for r in local}
    if len(models) > 1:
        shown = ", ".join(f"{r.arm}={_short(r.model, 40)}" for r in local)
        problems.append(
            f"the local arms ran different models ({shown}); the direct arm is "
            "the same model without LEVI, otherwise LEVI is credited with a "
            "model change"
        )
    for record, label in zip(records, labels, strict=True):
        if record.quality is None:
            continue
        if record.scored_count is None:
            problems.append(
                f"{label} has a quality but no scored_count: say over how many "
                "problems it was computed"
            )
        elif record.scored_count != record.episode_count:
            problems.append(
                f"{label} scored {record.scored_count} of {record.episode_count} "
                "problems; a score over fewer problems than the round has is not "
                "comparable (a blocked or failed problem stays in the denominator)"
            )
    teacher_quality = next((r.quality for r in records if r.arm == "teacher"), None)
    for record, label in zip(records, labels, strict=True):
        ci = record.quality_delta_ci_vs_teacher
        if ci and record.quality is not None and teacher_quality is not None:
            point = record.quality - teacher_quality
            if not ci[0] - 1e-9 <= point <= ci[1] + 1e-9:
                problems.append(
                    f"{label}: the paired interval [{ci[0]:.3f}, {ci[1]:.3f}] does "
                    f"not contain its own point difference {point:+.3f} "
                    "(this arm's quality minus the teacher's); the sign or the "
                    "arm order is probably reversed"
                )
    if problems:
        raise CompareError(problems)

    ordered = sorted(records, key=lambda r: ARMS.index(r.arm))
    rows = [_row(r, confirm) for r in ordered]
    by_arm = {row["arm"]: row for row in rows}
    teacher, direct = by_arm.get("teacher"), by_arm.get("direct")
    # The arm the headline is about: LEVI 2.0, or (a v1 round) the prior LEVI.
    harness = by_arm.get("levi2") or by_arm.get("prior_levi")
    for row in rows:
        row["vs_teacher"] = _vs(row, teacher, "vs_teacher")
        row["vs_direct"] = _vs(row, direct, "vs_direct")
        row["vs_prior_levi"] = _vs(row, by_arm.get("prior_levi"), "vs_prior_levi")
    first = ordered[0]
    return Table(
        conditions={
            "task": first.task,
            "episode_set_hash": first.episode_set_hash,
            "episode_count": first.episode_count,
            "decoding": first.decoding,
            "scoring": first.scoring,
            "quality_direction": (
                "higher_is_better"
                if first.scoring["higher_is_better"]
                else "lower_is_better"
            ),
            "non_inferiority_margin": first.scoring["non_inferiority_margin"],
            "split": first.split,
            "confirm": confirm,
            "external_token_bases": {n: list(b) for n, b in BASES.items()},
            "verdict_basis": verdict_basis,
        },
        rows=rows,
        missing_arms=[a for a in ARMS if a not in by_arm],
        main_metric=(
            _verdict(harness, teacher, first.scoring, verdict_basis, direct is not None)
            if harness and teacher
            else None
        ),
        main_metric_arm=harness["arm"] if harness else None,
    )


def _short(value, limit: int = 24) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit // 2] + "…"


def _value(value) -> str:
    return _ABSENT if value == _ABSENT else _short(_canonical(value))


def _difference(name: str, labels: list[str], values: list) -> str:
    """The message for a condition that differs between arms. For a dict it
    names the first key that differs (each other arm against the first)."""
    tail = "; a round needs the same problems, decoding and scoring in every arm"
    if all(isinstance(v, dict) for v in values):
        found = []
        for label, value in zip(labels[1:], values[1:], strict=True):
            diff = _first_difference(values[0], value)
            if diff:
                path, mine, theirs = diff
                found.append(
                    f"at {path!r} ({labels[0]}={_value(mine)}, {label}={_value(theirs)})"
                )
        return f"{name} differs " + "; ".join(found) + tail
    shown = ", ".join(f"{a}={_short(v)}" for a, v in zip(labels, values, strict=True))
    return f"{name} differs ({shown}){tail}"


# ---------------------------------------------------------------- Markdown


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _num(value, fmt) -> str:
    return UNKNOWN if value is None else format(value, fmt)


def _tri(value) -> str:
    return UNKNOWN if value is None else "是" if value else "否"


def _faster_line(verdict: dict) -> str:
    problem = verdict["faster_problem"]
    note = f"（两臂耗时{WALL_PROBLEMS[problem]}，不比较）" if problem else ""
    return f"- 耗时更低：{_tri(verdict['faster'])}{note}"


def _fewer_line(verdict: dict) -> str:
    basis = verdict["fewer_external_tokens_basis"]
    how = (
        "两个口径要一致才算"
        if basis == "both"
        else f"按事先登记的口径：{'新增' if basis == 'fresh' else '全量'}"
    )
    if verdict["fewer_external_tokens_basis_dependent"]:
        how += "；两个口径给出相反的答案，结论随口径翻转"
    return (
        f"- 外部 token 更少：{_tri(verdict['fewer_external_tokens'])}"
        f"（新增口径 {_tri(verdict['fewer_external_tokens_fresh'])}，"
        f"全量口径 {_tri(verdict['fewer_external_tokens_all'])}；{how}）"
    )


def _status_cell(status: str) -> str:
    return {
        "yes": "是",
        "no": "否",
        "undetermined": "未定（区间跨过界，样本不足以判）",
        "unknown": f"{UNKNOWN}（没有配对差区间）",
    }[status]


def _ratio_cell(row: dict, name: str, fmt: str, scale: float = 1.0) -> str:
    value = row[name]
    if value is not None:
        # Per external token with a lower-bound denominator is an upper bound.
        per_token = name.startswith("quality_per_external_token")
        mark = "≤" if per_token and _is_bound(row) else ""
        return mark + format(value * scale, fmt)
    return NOT_APPLICABLE if name in row["undefined"] else UNKNOWN


def _token_cell(row: dict, value) -> str:
    """An external token count; a stated lower bound is written ``≥N``."""
    text = _num(value, ",d")
    return "≥" + text if value is not None and _is_bound(row) else text


def _exactness(row: dict) -> str:
    if row["external_tokens_exact"] is None:
        return (
            NONE_SELF
            if all(v is None for v in row["external_tokens"].values())
            else "未声明"
        )
    return "下限" if _is_bound(row) else "精确"


WALL_NAMES = {"includes_queue": "含排队", "excludes_queue": "不含排队"}
WALL_PROBLEMS = {"unstated": "口径未说明", "different": "口径不同"}


def _wall_cell(row: dict) -> str:
    if row["wall_s"] is None:
        return UNKNOWN
    basis = WALL_NAMES.get(row["wall_s_basis"], "口径未说明")
    return f"{row['wall_s']:,.1f}（{basis}）"


def _vs_cells(row: dict, key: str) -> list[str]:
    """Quality, wall time, then the external token difference on each basis."""
    vs = row[key]
    if vs is None:
        return [NO_ARM] * 4
    if vs.get("self"):
        return [NONE_SELF] * 4
    tokens = []
    for name in BASES:
        value = vs[f"external_tokens_delta_{name}"]
        text = _num(value, "+,d")
        tokens.append(
            text + "（含下限）"
            if value is not None and vs["external_tokens_delta_is_bound"]
            else text
        )
    wall = _num(vs["wall_s_delta"], "+,.1f")
    if vs["wall_s_problem"]:
        wall += f"（{WALL_PROBLEMS[vs['wall_s_problem']]}）"
    return [_num(vs["quality_delta"], "+.3f"), wall, *tokens]


def _ci_cell(row: dict) -> str:
    vs = row["vs_teacher"]
    if vs is None:
        return NO_ARM
    if vs.get("self"):
        return NONE_SELF
    ci = vs["quality_delta_ci"]
    return UNKNOWN if ci is None else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def _markdown(table: Table) -> str:
    c, rows = table.conditions, table.rows
    scale = 1000.0
    out = [
        f"## 教师–学生三组对比 · {_cell(c['task'])}",
        "",
        f"- 题目集：{c['episode_count']} 个 episode，哈希 `{c['episode_set_hash'][:12]}`",
        f"- 解码：`{_canonical(c['decoding'])}`",
        f"- 评分：`{_canonical(c['scoring'])}`",
    ]
    direction = (
        "越大越好" if c["quality_direction"] == "higher_is_better" else "越小越好"
    )
    out.append(
        f"- 质量方向：{direction}；非劣效界 δ = {c['non_inferiority_margin']:g}（事先声明）"
    )
    out.append(f"- 数据划分：{c['split']}")
    if c["confirm"]:
        out.append("- 模式：confirm，表中只有聚合分数，没有逐条内容")
    if table.missing_arms:
        out.append(f"- 本轮缺少：{', '.join(table.missing_arms)}")
    out += ["", "### 质量与速度", ""]
    out += _table(
        [
            "臂",
            "模型",
            "质量（对金标准）",
            "区间",
            "与教师一致率",
            "墙钟 (s)",
            "每秒质量",
            "每千外部 token 质量（新增口径）",
            "每千外部 token 质量（全量口径）",
            "阻塞",
        ],
        [
            [
                r["arm"],
                r["model"],
                _num(r["quality"], ".3f"),
                (
                    f"[{r['quality_ci'][0]:.3f}, {r['quality_ci'][1]:.3f}]"
                    + (f" {r['quality_ci_method']}" if r["quality_ci_method"] else "")
                    if r["quality_ci"]
                    else NONE_SELF
                ),
                NONE_SELF
                if r["arm"] == "teacher"
                else _num(r["agreement_with_teacher"], ".3f"),
                _wall_cell(r),
                _ratio_cell(r, "quality_per_second", ".3g"),
                _ratio_cell(r, "quality_per_external_token_fresh", ".3g", scale),
                _ratio_cell(r, "quality_per_external_token_all", ".3g", scale),
                r["blocked"] or NONE_SELF,
            ]
            for r in rows
        ],
    )
    fresh = " + ".join(c["external_token_bases"]["fresh"])
    out += [
        "",
        "### token（本地与外部分开记；外部合计并列两个口径）",
        "",
        f"- 新增口径 = {fresh}",
        f"- 全量口径 = {' + '.join(c['external_token_bases']['all'])}（含 cache_read）",
        "",
    ]
    out += _table(
        [
            "臂",
            "本地 token",
            "外部 input_new",
            "外部 output",
            "外部 cache_read",
            "外部 cache_write",
            "外部合计（新增口径）",
            "外部合计（全量口径）",
            "计数",
            "外部 token 来源",
        ],
        [
            [
                r["arm"],
                _num(r["local_tokens"], ",d"),
                *[_token_cell(r, r["external_tokens"][n]) for n in TOKEN_FIELDS],
                _token_cell(r, r["external_tokens_total_fresh"]),
                _token_cell(r, r["external_tokens_total_all"]),
                _exactness(r),
                r["external_tokens_source"] or NONE_SELF,
            ]
            for r in rows
        ],
    )
    out += ["", "### 相对差（本臂 − 参照臂；时间、token 为负表示更省）", ""]
    out += _table(
        [
            "臂",
            "对教师 Δ质量",
            "对教师 Δ质量配对区间",
            "对教师 Δ墙钟 (s)",
            "对教师 Δ外部 token（新增口径）",
            "对教师 Δ外部 token（全量口径）",
            "对直接调用 Δ质量",
            "对直接调用 Δ墙钟 (s)",
            "对直接调用 Δ外部 token（新增口径）",
            "对直接调用 Δ外部 token（全量口径）",
        ],
        [
            [
                r["arm"],
                *_vs_cells(r, "vs_teacher")[:1],
                _ci_cell(r),
                *_vs_cells(r, "vs_teacher")[1:],
                *_vs_cells(r, "vs_direct"),
            ]
            for r in rows
        ],
    )
    verdict = table.main_metric
    if verdict:
        rule = verdict["non_inferiority"]
        better = "大" if rule["higher_is_better"] else "小"
        bound = f"{rule['margin']:g}"
        ci = rule["delta_ci"]
        shown = f"[{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "没有"
        method = f"（{_cell(rule['method'])}）" if rule["method"] else ""
        gap = "" if verdict["complete"] else "；不完整：缺 direct（一轮要三组）"
        out += [
            "",
            f"### 主指标：本地 + harness 对教师（质量按非劣效，耗时和外部 token 比合计{gap}）",
            "",
            (
                f"- 质量不低于教师（越{better}越好；配对差区间 {shown}{method}，"
                f"界 δ = {bound}）：{_status_cell(verdict['quality_status'])}"
            ),
            f"- 质量点估计不劣于教师（辅助，不进结论）：{_tri(verdict['quality_point_not_worse'])}",
            _faster_line(verdict),
            _fewer_line(verdict),
            (
                f"- 三项全满足：{_tri(verdict['all'])}"
                if verdict["complete"]
                else "- 三项全满足：不完整（缺 direct，不能判；此行不是“是”）"
            ),
        ]
    out += [
        "",
        (
            f"注：`{UNKNOWN}` = 没有测量，不是 0；依赖它的每秒质量、每外部 token 质量和相对差也是"
            f"`{UNKNOWN}`。`{NOT_APPLICABLE}` = 分母为 0（例如本臂外部 token 真为 0），"
            "或质量越小越好（误差除以秒或 token 不是越大越好的比值）。"
            "每千外部 token 质量 = 质量 ÷ 外部合计 × 1000（新增、全量各算一次）。"
            "`≥` = 外部 token 只是下限"
            "（计数不完整），带 `≤` 的每千 token 质量是上限，含下限的差也只是下限；"
            "学生臂是下限时，“外部 token 更少”写未知。"
        ),
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------- command line


def _load(path: str) -> list[ArmRecord]:
    value = json.loads(Path(path).read_text())
    if isinstance(value, dict) and "records" in value:
        value = value["records"]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise RecordError(
            f"{path}: expected a record, a list of records or {{'records': [...]}}"
        )
    return [ArmRecord.from_dict(row) for row in value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m levi.eval.compare",
        description=(
            "Put the arm records of one teacher / local+harness / local-direct round "
            "in one table. Reads only the files named; writes to stdout."
        ),
    )
    parser.add_argument(
        "records", nargs="+", help="JSON files: a record, a list, or {'records': [...]}"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "assert that every record is split=confirm. The confirm rules follow "
            "each record's split with or without this flag"
        ),
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--verdict-basis",
        choices=VERDICT_BASES,
        default="both",
        help=(
            "how 'fewer external tokens' is decided: both totals must agree "
            "(default), or the one basis registered before the round; "
            "both totals are in the table either way"
        ),
    )
    args = parser.parse_args(argv)
    try:
        records = [r for path in args.records for r in _load(path)]
        table = compare(
            records,
            assert_confirm=args.confirm,
            verdict_basis=args.verdict_basis,
        )
    except (OSError, ValueError) as exc:  # CompareError is a ValueError
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(table.to_json() if args.format == "json" else table.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
