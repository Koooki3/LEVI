"""Teacher / harness / direct comparison of one round: three arms, one table.

Every round of the teacher-student method is run three ways on the same
problems, with the same decoding and the same scoring:

- ``teacher``: the external agent (the source of the paradigm and the
  experience entries). Its answer is a reference, not the gold standard;
- ``local_harness``: the local VLM through LEVI's harness;
- ``local_direct``: the same local VLM called directly, without LEVI (R13).

The headline is whether ``local_harness`` reaches at least the teacher's
quality while being faster and spending fewer external tokens, so the table
carries quality per second and quality per external token, not accuracy alone.

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
- **External tokens are four separate fields** (``input_new``, ``output``,
  ``cache_read``, ``cache_write``, as in ``levi.aeri.external_report.v1``).
  Quality per external token needs one denominator; the default is
  ``input_new + output + cache_write`` (what the external model newly read or
  wrote; ``cache_read`` is cheap re-reading of a cached prefix and stays a
  column of its own, the convention of the lab's experiment ledger). It is
  unknown as soon as one of those fields is. The basis is printed with the
  table and can be changed with ``token_basis``.

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

ARM_SCHEMA = "levi.eval.arm_record.v1"
TABLE_SCHEMA = "levi.eval.compare.v1"
ARMS = ("teacher", "local_harness", "local_direct")
SPLITS = ("tune", "confirm", "other")
REQUIRED = ("arm", "task", "model", "decoding", "scoring", "split")
# input_new + output + cache_write; cache_read is a column of its own.
FRESH_BASIS = ("input_new", "output", "cache_write")
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


def _json_copy(value, name):
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise RecordError(f"{name} must be plain JSON") from exc


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
    """One arm of one round.

    ``split`` says which data the arm ran on (``tune``, ``confirm`` or
    ``other``); a ``confirm`` record is held to the confirm rules in every table
    (see the module docstring).

    ``quality`` is the primary metric against the gold standard (the metric is
    named in ``scoring``); ``quality_ci`` its interval. ``None`` means not
    measured. ``agreement_with_teacher`` is the share of the teacher's answers
    this arm reproduces (not set on the teacher arm). ``external_tokens`` has
    the four fields of ``levi.aeri.external_report.v1``; ``None`` for the whole
    dict or one field means unknown, ``0`` means measured zero.

    The problems are given either as ``episode_set`` (the ids) or as
    ``episode_set_hash`` plus ``episode_count``; if both are given they must
    agree. ``per_episode`` (episode id -> score) is the one per-item field
    allowed outside confirm mode.
    """

    arm: str
    task: str
    model: str
    decoding: dict
    scoring: dict | str
    split: str
    episode_set_hash: str | None = None
    episode_count: int | None = None
    episode_set: tuple | None = None
    quality: float | None = None
    quality_ci: tuple[float, float] | None = None
    quality_ci_method: str | None = None
    agreement_with_teacher: float | None = None
    wall_s: float | None = None
    local_tokens: int | None = None
    external_tokens: dict | None = None
    external_tokens_source: str | None = None
    blocked: str | None = None
    per_episode: dict | None = None

    def __post_init__(self):
        def put(name, value):
            object.__setattr__(self, name, value)

        if self.arm not in ARMS:
            raise RecordError(f"arm must be one of {ARMS}, got {_show(self.arm)}")
        if self.split not in SPLITS:
            raise RecordError(f"split must be one of {SPLITS}, got {_show(self.split)}")
        for name in ("task", "model"):
            if getattr(self, name) is None:
                raise RecordError(f"{name} is required")
            put(name, _text(getattr(self, name), name))
        if not isinstance(self.decoding, dict):
            raise RecordError("decoding must be a dict (the round's declared decoding)")
        put("decoding", _json_copy(self.decoding, "decoding"))
        if not isinstance(self.scoring, (dict, str)) or not self.scoring:
            raise RecordError("scoring must be a non-empty dict or string")
        put("scoring", _json_copy(self.scoring, "scoring"))

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
        agreement = _real(
            self.agreement_with_teacher, "agreement_with_teacher", lo=0, hi=1
        )
        if agreement is not None and self.arm == "teacher":
            raise RecordError("the teacher arm has no agreement_with_teacher")
        put("agreement_with_teacher", agreement)
        put("wall_s", _real(self.wall_s, "wall_s", positive=True))
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
        if schema != ARM_SCHEMA:
            raise RecordError(
                f"unsupported schema {_show(schema)}, expected {ARM_SCHEMA}"
            )
        unknown = sorted(set(row) - set(cls.__dataclass_fields__), key=str)
        if unknown:
            shown = [_show(name) for name in unknown]
            raise RecordError(
                f"unknown fields {shown}: a record holds only the fields of {ARM_SCHEMA}"
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


def _row(record: ArmRecord, basis, confirm: bool) -> dict:
    total = _total(record, basis)
    per_second, second_status = _ratio(record.quality, record.wall_s)
    per_token, token_status = _ratio(record.quality, total)
    # (column, value, why it has no value)
    columns = [
        ("quality", record.quality, "unknown"),
        ("wall_s", record.wall_s, "unknown"),
        ("local_tokens", record.local_tokens, "unknown"),
        *[
            (f"external_tokens.{n}", record.external(n), "unknown")
            for n in TOKEN_FIELDS
        ],
        ("external_tokens_total", total, "unknown"),
        ("quality_per_second", per_second, second_status),
        ("quality_per_external_token", per_token, token_status),
    ]
    row = {
        "arm": record.arm,
        "model": record.model,
        "quality": record.quality,
        "quality_ci": list(record.quality_ci) if record.quality_ci else None,
        "quality_ci_method": record.quality_ci_method,
        "agreement_with_teacher": record.agreement_with_teacher,
        "wall_s": record.wall_s,
        "local_tokens": record.local_tokens,
        "external_tokens": {name: record.external(name) for name in TOKEN_FIELDS},
        "external_tokens_total": total,
        "external_tokens_source": record.external_tokens_source,
        "quality_per_second": per_second,
        "quality_per_external_token": per_token,
        "blocked": record.blocked,
    }
    if not confirm:
        row["per_episode"] = record.per_episode
    row["unknown"] = [n for n, v, why in columns if v is None and why == "unknown"]
    row["undefined"] = [n for n, v, why in columns if v is None and why == "undefined"]
    return row


def _vs(row: dict, reference: dict | None, key: str) -> dict | None:
    """Differences ``row - reference``; ``None`` if the round has no such arm."""
    if reference is None:
        return None
    if reference is row:
        return {"self": True}
    out = {
        "quality_delta": _delta(row["quality"], reference["quality"]),
        "wall_s_delta": _delta(row["wall_s"], reference["wall_s"]),
        "external_tokens_delta": _delta(
            row["external_tokens_total"], reference["external_tokens_total"]
        ),
    }
    for name, value in out.items():
        if value is None:
            row["unknown"].append(f"{key}.{name}")
    return out


def _verdict(student: dict, teacher: dict | None) -> dict | None:
    """The round's headline: the harness arm against the teacher arm, point
    estimates only (read the intervals before believing a small difference)."""
    if teacher is None or student is None:
        return None
    checks = {
        "quality_not_lower": _holds(student["quality"], teacher["quality"], ">="),
        "faster": _holds(student["wall_s"], teacher["wall_s"], "<"),
        "fewer_external_tokens": _holds(
            student["external_tokens_total"], teacher["external_tokens_total"], "<"
        ),
    }
    values = list(checks.values())
    checks["all"] = False if False in values else None if None in values else True
    checks["point_estimates_only"] = True
    return checks


def _holds(mine, reference, op):
    if mine is None or reference is None:
        return None
    return mine >= reference if op == ">=" else mine < reference


@dataclass(frozen=True)
class Table:
    conditions: dict
    rows: list[dict]
    missing_arms: list[str]
    main_metric: dict | None

    def to_dict(self) -> dict:
        return {
            "schema": TABLE_SCHEMA,
            "conditions": self.conditions,
            "rows": self.rows,
            "missing_arms": self.missing_arms,
            "main_metric": self.main_metric,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)

    def to_markdown(self) -> str:
        return _markdown(self)


def compare(
    records: Iterable[ArmRecord],
    *,
    assert_confirm: bool = False,
    token_basis: Iterable[str] = FRESH_BASIS,
) -> Table:
    """One table for one round. Raises ``CompareError`` if the records were not
    run on the same problems under the same conditions (or break the confirm
    rules); the message names every difference.

    Whether this is a confirm table is not a choice of the caller: it is
    decided by the records' ``split``. ``assert_confirm=True`` only adds the
    demand that every record is a ``confirm`` one (the command line's
    ``--confirm``), so a round that was meant to be confirm cannot pass as
    another split."""
    records = list(records)
    problems = []
    if not records:
        raise CompareError("no records to compare")
    for record in records:
        if not isinstance(record, ArmRecord):
            raise CompareError(f"not an ArmRecord: {type(record).__name__}")
    basis = tuple(token_basis)
    if not basis or len(set(basis)) != len(basis) or set(basis) - set(TOKEN_FIELDS):
        raise CompareError(
            f"token_basis must be distinct fields among {TOKEN_FIELDS}, got {basis}"
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
        ("decoding", lambda r: _canonical(r.decoding)),
        ("scoring", lambda r: _canonical(r.scoring)),
    ):
        values = [getter(r) for r in records]
        if len(set(values)) > 1:
            shown = ", ".join(
                f"{a}={_short(v)}" for a, v in zip(labels, values, strict=True)
            )
            problems.append(
                f"{name} differs ({shown}); a round needs the same problems, "
                "decoding and scoring in every arm"
            )
    if problems:
        raise CompareError(problems)

    ordered = sorted(records, key=lambda r: ARMS.index(r.arm))
    rows = [_row(r, basis, confirm) for r in ordered]
    by_arm = {row["arm"]: row for row in rows}
    teacher, harness, direct = (by_arm.get(a) for a in ARMS)
    for row in rows:
        row["vs_teacher"] = _vs(row, teacher, "vs_teacher")
        row["vs_direct"] = _vs(row, direct, "vs_direct")
    first = ordered[0]
    return Table(
        conditions={
            "task": first.task,
            "episode_set_hash": first.episode_set_hash,
            "episode_count": first.episode_count,
            "decoding": first.decoding,
            "scoring": first.scoring,
            "split": first.split,
            "confirm": confirm,
            "external_token_basis": list(basis),
        },
        rows=rows,
        missing_arms=[a for a in ARMS if a not in by_arm],
        main_metric=_verdict(harness, teacher) if harness and teacher else None,
    )


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= 24 else text[:12] + "…"


# ---------------------------------------------------------------- Markdown


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _num(value, fmt) -> str:
    return UNKNOWN if value is None else format(value, fmt)


def _tri(value) -> str:
    return UNKNOWN if value is None else "是" if value else "否"


def _ratio_cell(row: dict, name: str, fmt: str, scale: float = 1.0) -> str:
    value = row[name]
    if value is not None:
        return format(value * scale, fmt)
    return NOT_APPLICABLE if name in row["undefined"] else UNKNOWN


def _vs_cells(row: dict, key: str) -> list[str]:
    vs = row[key]
    if vs is None:
        return [NO_ARM] * 3
    if vs.get("self"):
        return [NONE_SELF] * 3
    return [
        _num(vs["quality_delta"], "+.3f"),
        _num(vs["wall_s_delta"], "+,.1f"),
        _num(vs["external_tokens_delta"], "+,d"),
    ]


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
            "每千外部 token 质量",
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
                _num(r["wall_s"], ",.1f"),
                _ratio_cell(r, "quality_per_second", ".3g"),
                _ratio_cell(r, "quality_per_external_token", ".3g", scale),
                r["blocked"] or NONE_SELF,
            ]
            for r in rows
        ],
    )
    basis = " + ".join(c["external_token_basis"])
    out += [
        "",
        f"### token（本地与外部分开记；外部口径合计 = {basis}）",
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
            "外部合计（口径）",
            "外部 token 来源",
        ],
        [
            [
                r["arm"],
                _num(r["local_tokens"], ",d"),
                *[_num(r["external_tokens"][n], ",d") for n in TOKEN_FIELDS],
                _num(r["external_tokens_total"], ",d"),
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
            "对教师 Δ墙钟 (s)",
            "对教师 Δ外部 token",
            "对直接调用 Δ质量",
            "对直接调用 Δ墙钟 (s)",
            "对直接调用 Δ外部 token",
        ],
        [
            [r["arm"], *_vs_cells(r, "vs_teacher"), *_vs_cells(r, "vs_direct")]
            for r in rows
        ],
    )
    verdict = table.main_metric
    if verdict:
        out += [
            "",
            "### 主指标：本地 + harness 对教师（只比点估计，小差异先看区间）",
            "",
            f"- 质量不低于教师：{_tri(verdict['quality_not_lower'])}",
            f"- 耗时更低：{_tri(verdict['faster'])}",
            f"- 外部 token 更少：{_tri(verdict['fewer_external_tokens'])}",
            f"- 三项全满足：{_tri(verdict['all'])}",
        ]
    out += [
        "",
        (
            f"注：`{UNKNOWN}` = 没有测量，不是 0；依赖它的每秒质量、每外部 token 质量和相对差也是"
            f"`{UNKNOWN}`。`{NOT_APPLICABLE}` = 分母为 0（例如本臂外部 token 真为 0）。"
            "每千外部 token 质量 = 质量 ÷ 外部合计 × 1000。"
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
        "--basis",
        default=",".join(FRESH_BASIS),
        help="external token fields summed for the per-token ratio (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        records = [r for path in args.records for r in _load(path)]
        table = compare(
            records,
            assert_confirm=args.confirm,
            token_basis=[b.strip() for b in args.basis.split(",") if b.strip()],
        )
    except (OSError, ValueError) as exc:  # CompareError is a ValueError
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(table.to_json() if args.format == "json" else table.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
