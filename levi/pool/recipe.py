"""Recipes: named, saved selections over the pool index, and their preview.

A recipe picks episodes by category, source, format, task (an ordered list:
the export follows it), date and policy (model, checkpoint, how it was run), then applies, in order:

1. held-out episodes out — always, whatever the recipe says;
2. episodes the recipe names in ``exclude`` out;
3. nonstandard folders out (unless ``include_nonstandard``) and episodes of
   formats the pool cannot export out;
4. duplicates out: of the episodes left with one fingerprint, the canonical
   one (or, when the canonical copy is not selected, the best-ranked one)
   stays;
5. the outcome filter: ``all``, ``robot_flag_success`` (the robot's flag),
   ``verified_success`` (a human label first, then the robot's flag) or
   ``human_verified_success`` (a human label only). An episode whose human
   labels disagree (``label_conflict``) is out of every verified outcome and
   of RECAP exports;
6. per task, in the task order, the episodes that go in: ``count`` of them
   (else ``per_task_cap``, else all), with a ``success_ratio`` and a
   ``strategy`` (``quality``, ``random``, ``first``; see ``select.py``), drawn
   with ``seed``. An episode an earlier task picked is not picked again.

Every episode left out is listed with its reason.
"""

import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import index, recap_signal, settings
from . import select as picker
from . import timing as timing_mod
from .rules import CATEGORIES, normalize_task

NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
DATE = r"^\d{4}-\d{2}-\d{2}$"


class TaskEntry(BaseModel):
    """One task of a recipe: how many episodes it contributes and how they
    are picked. ``count`` None: the recipe's ``per_task_cap``, else all.
    ``success_ratio`` None: keep the natural share of successes.

    A bare string in a saved recipe is the old form (the task text; the
    global cap drew a seeded random sample): it loads as
    ``strategy="random"`` so the same episodes are picked as before."""

    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=1000)
    count: int | None = Field(None, ge=1, le=10_000_000)
    success_ratio: float | None = Field(None, ge=0, le=1)
    strategy: Literal["quality", "random", "first"] = "quality"

    @field_validator("task")
    @classmethod
    def _task(cls, value):
        value = normalize_task(value)
        if not value:
            raise ValueError("Tasks must be nonempty")
        return value


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    description: str = Field("", max_length=2000)
    categories: list[Literal[CATEGORIES]] = Field(default_factory=list)  # type: ignore[valid-type]
    sources: list[str] = Field(default_factory=list, max_length=1000)
    formats: list[Literal["robot_capture", "lerobot", "droid_raw"]] = Field(
        default_factory=list
    )
    tasks: list[TaskEntry] = Field(default_factory=list, max_length=5000)
    outcome: Literal[
        "all", "robot_flag_success", "verified_success", "human_verified_success"
    ] = "all"
    per_task_cap: int | None = Field(None, ge=1, le=10_000_000)
    seed: int = 0
    date_from: str | None = Field(None, pattern=DATE)
    date_to: str | None = Field(None, pattern=DATE)
    # The old single field: a rollout's checkpoint name (kept for saved recipes).
    policies: list[str] = Field(default_factory=list)
    policy_models: list[str] = Field(default_factory=list)
    policy_checkpoints: list[str] = Field(default_factory=list)
    policy_methods: list[
        Literal["direct", "dsrl", "rlt", "sfe", "student", "other", "unknown"]
    ] = Field(default_factory=list)
    include_nonstandard: bool = False
    # A task taken from raw captures and from a LeRobot source that is not
    # linked to them may be one recording twice: refused unless sources are
    # named or this is set.
    allow_unlinked_sources: bool = False
    exclude: list[str] = Field(default_factory=list, max_length=100000)
    # Normalized task -> the text written into the export (default: the
    # normalized task itself).
    task_text: dict[str, str] = Field(default_factory=dict)

    @field_validator("tasks", mode="before")
    @classmethod
    def _legacy_tasks(cls, value):
        if not isinstance(value, list):
            return value
        return [
            {"task": t, "strategy": "random"} if isinstance(t, str) else t
            for t in value
        ]

    @field_validator("tasks")
    @classmethod
    def _tasks(cls, value):
        names = [t.task for t in value]
        if len(set(names)) != len(names):
            raise ValueError("A task is listed twice")
        return value

    @property
    def entries(self) -> list[TaskEntry]:
        """The task entries (a bare string set by ``model_copy(update=...)``,
        which skips validation, is the old form too)."""
        return [
            t if isinstance(t, TaskEntry) else TaskEntry(task=t, strategy="random")
            for t in self.tasks
        ]

    @property
    def task_names(self) -> list[str]:
        return [t.task for t in self.entries]

    def entry(self, task: str) -> TaskEntry:
        """The task's entry; a task the recipe does not list (no task list
        means every task) is the old form: random under the global cap."""
        for item in self.entries:
            if item.task == task:
                return item
        return TaskEntry(task=task, strategy="random")

    def task_count(self, task: str) -> int | None:
        return self.entry(task).count or self.per_task_cap

    @field_validator("task_text")
    @classmethod
    def _task_text(cls, value):
        out = {}
        for key, text in value.items():
            text = text.strip()
            if not text:
                continue
            if len(text) > 300 or any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise ValueError("task_text must be one line of at most 300 characters")
            out[normalize_task(key)] = text
        return out


SPEC_KEYS = ("count", "success", "strategy")


def parse_task_spec(text: str) -> "str | dict":
    """A ``--task`` value: ``text`` (the old bare form) or
    ``text:count=50,success=0.6,strategy=quality``.

    ``count``: an integer or ``all``; ``success``: the target share of
    successes as 0..1 or ``60%`` (``natural``: keep the task's own share);
    ``strategy``: ``quality`` (default), ``random`` or ``first``. The options
    follow the last colon and are recognised only when every part is a
    known ``key=value``, so a task text may contain colons and commas."""
    head, sep, tail = text.rpartition(":")
    parts = [p.strip() for p in tail.split(",")] if sep else []
    if not parts or not all(
        "=" in p and p.split("=", 1)[0].strip() in SPEC_KEYS for p in parts
    ):
        return text
    entry: dict = {"task": head}
    for part in parts:
        key, value = (x.strip() for x in part.split("=", 1))
        low = value.lower()
        if key == "count":
            entry["count"] = None if low == "all" else int(value)
        elif key == "success":
            if low == "natural":
                entry["success_ratio"] = None
            elif low.endswith("%"):
                entry["success_ratio"] = float(low[:-1]) / 100
            else:
                entry["success_ratio"] = float(value)
        else:
            entry["strategy"] = low
    return entry


def recipes_dir():
    return settings.pool_dir() / "recipes"


def save(recipe: Recipe) -> dict:
    folder = recipes_dir()
    folder.mkdir(parents=True, exist_ok=True)
    value = {**recipe.model_dump(), "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    path = folder / f"{recipe.name}.json"
    temp = folder / f".{recipe.name}.{os.getpid()}.tmp"
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(temp, path)
    return value


def load(name: str) -> Recipe:
    if not re.fullmatch(NAME, name):
        raise ValueError("Invalid recipe name")
    path = recipes_dir() / f"{name}.json"
    if not path.is_file():
        raise KeyError(name)
    value = json.loads(path.read_text())
    value.pop("saved_at", None)
    return Recipe.model_validate(value)


def listing() -> list[dict]:
    folder = recipes_dir()
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            value = json.loads(path.read_text())
            saved_at = value.pop("saved_at", None)
            out.append(
                {**Recipe.model_validate(value).model_dump(), "saved_at": saved_at}
            )
        except (OSError, ValueError):
            continue
    return out


def delete(name: str) -> bool:
    if not re.fullmatch(NAME, name):
        raise ValueError("Invalid recipe name")
    path = recipes_dir() / f"{name}.json"
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


def _natural(text: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(text))]


def _rank(row: dict) -> tuple:
    return (
        not row["canonical"],
        row["category"] == "levi" or bool(row["in_levi_workspace"]),
        row["format"] != "robot_capture",
        bool(row.get("filtered")),
        str(row["source_path"]).count("/"),
        row["key"],
    )


@dataclass
class Selection:
    """What a recipe selects: the ordered episodes (each annotated with
    ``sel_stratum``, ``quality_score`` and ``selection_reason``), every
    episode left out with its reason, and per task the report of how many
    were asked for, available and picked."""

    chosen: list[dict]
    excluded: list[dict]
    tasks: dict[str, dict] = field(default_factory=dict)


def select(
    recipe: Recipe,
    df: pd.DataFrame | None = None,
    target: str | None = None,
    human_as_success: bool = False,
):
    """Ordered episodes of a recipe, and every excluded one with its reason
    (``select_detailed`` also returns the per-task report)."""
    result = select_detailed(recipe, df, target, human_as_success)
    return result.chosen, result.excluded


def select_detailed(
    recipe: Recipe,
    df: pd.DataFrame | None = None,
    target: str | None = None,
    human_as_success: bool = False,
) -> Selection:
    """See the module docstring. ``target`` adds the export format's own
    requirement: ``raw_capture`` copies raw captures only, ``recap_value``
    needs an outcome per episode (``human_as_success``: a human demonstration
    without one counts as a success)."""
    df = index.frame() if df is None else df
    df = index._filter(
        df,
        categories=recipe.categories or None,
        sources=recipe.sources or None,
        tasks=recipe.task_names or None,
        formats=recipe.formats or None,
        policies=recipe.policies or None,
        policy_models=recipe.policy_models or None,
        policy_checkpoints=recipe.policy_checkpoints or None,
        policy_methods=recipe.policy_methods or None,
        date_from=recipe.date_from,
        date_to=recipe.date_to,
        show_heldout=True,
        show_copies=True,
        show_nonstandard=True,
        show_archive=bool(recipe.categories),
    )
    rows = df.astype(object).where(pd.notna(df), None).to_dict("records")
    excluded: list[dict] = []

    def out(row, reason, **extra):
        excluded.append(
            {
                "key": row["key"],
                "source": row["source"],
                "task": row["task"],
                "reason": reason,
                **extra,
            }
        )

    kept = []
    skip = set(recipe.exclude)
    for row in rows:
        if row["heldout"]:
            out(row, "heldout", heldout_id=row["heldout_id"])
        elif row["key"] in skip:
            out(row, "excluded_by_recipe")
        elif row["nonstandard"] and not recipe.include_nonstandard:
            out(row, "nonstandard", detail=row["nonstandard_reason"])
        elif not row["exportable"] and not row["nonstandard"]:
            out(row, "unsupported", detail=row["not_exportable_reason"])
        elif target == "raw_capture" and row["format"] != "robot_capture":
            out(row, "not_a_raw_capture")
        else:
            kept.append(row)
    groups = defaultdict(list)
    for row in kept:
        groups[row.get("group") or row["key"]].append(row)
    unique = []
    for members in groups.values():
        members.sort(key=_rank)
        unique.append(members[0])
        for row in members[1:]:
            out(row, "duplicate", kept=members[0]["key"])
    passed = []
    verified = recipe.outcome in ("verified_success", "human_verified_success")
    for row in unique:
        if (verified or target == "recap_value") and row.get("label_conflict"):
            out(row, "label_conflict")
        elif recipe.outcome == "robot_flag_success" and row["robot_flag"] != "success":
            out(row, "outcome_filter", outcome=row["robot_flag"])
        elif (
            recipe.outcome == "verified_success"
            and (row["human_label"] or row["robot_flag"]) != "success"
        ):
            out(row, "outcome_filter", outcome=row["human_label"] or row["robot_flag"])
        elif recipe.outcome == "human_verified_success" and (
            row["human_label"] != "success"
        ):
            out(row, "outcome_filter", outcome=row["human_label"])
        elif target == "recap_value" and row["outcome"] not in ("success", "failure"):
            if human_as_success and row["category"] == "human":
                passed.append(
                    {**row, "outcome": "success", "outcome_source": "sft_demonstration"}
                )
            else:
                out(row, "no_outcome")
        else:
            passed.append(row)
    task_order = recipe.task_names or sorted({r["task"] for r in passed})
    position = {t: i for i, t in enumerate(task_order)}
    source_order = {s: i for i, s in enumerate(recipe.sources)}

    def order_key(r):
        return (
            source_order.get(
                r["source"], source_order.get(r["source_path"], len(source_order))
            ),
            r["source"],
            _natural(r["episode"]),
        )

    by_task = defaultdict(list)
    for row in passed:
        by_task[row["task"]].append(row)
    recap = None
    chosen: list[dict] = []
    reports: dict[str, dict] = {}
    taken: set = set()
    for task in task_order:
        members = by_task.get(task, [])
        entry = recipe.entry(task)
        count = recipe.task_count(task)
        if entry.strategy == "quality" and members and recap is None:
            recap = recap_signal.load()
        picked, report = picker.choose(
            members,
            task=task,
            count=count,
            success_ratio=entry.success_ratio,
            strategy=entry.strategy,
            seed=recipe.seed,
            order_key=order_key,
            recap=recap,
            taken=taken,
        )
        reports[task] = report
        keep = {r["key"] for r in picked}
        reason = "per_task_cap" if entry.count is None else "not_selected"
        for row in members:
            if row["key"] in keep:
                continue
            if row["key"] in taken or (row.get("group") or row["key"]) in taken:
                out(row, "already_in_composition")
            else:
                out(row, reason)
        for row in picked:
            taken.add(row["key"])
            taken.add(row.get("group") or row["key"])
        chosen += picked
    chosen.sort(key=lambda r: (position[r["task"]], *order_key(r)))
    return Selection(chosen, excluded, reports)


def find_warnings(recipe: Recipe, chosen: list[dict], df=None) -> list[dict]:
    """What the selection rests on that a person should know. ``blocking``
    ones stop an export (plan) until fixed or lifted explicitly."""
    out: list[dict] = []
    scan = (index.summary().get("heldout") or {}) if index.summary() else {}
    lists = [str(p) for p in settings.heldout_files()]
    if not lists and not settings.heldout_disabled():
        out.append(
            {
                "code": "heldout_unconfigured",
                "blocking": True,
                "message": "No held-out list is configured (LEVI_POOL_HELDOUT); "
                "exports are refused until it is set, or set to `none`",
            }
        )
    elif index.summary() and scan.get("lists", []) != lists:
        out.append(
            {
                "code": "heldout_lists_changed",
                "blocking": True,
                "message": "The held-out lists changed since the last scan; "
                "scan again so the index marks them",
                "scanned_with": scan.get("lists", []),
                "now": lists,
            }
        )
    if scan.get("unmatched"):
        out.append(
            {
                "code": "heldout_unmatched",
                "blocking": False,
                "message": f"{len(scan['unmatched'])} held-out entries match no "
                "indexed episode (moved, renamed or not under the pool roots)",
                "ids": scan["unmatched"][:20],
            }
        )
    full = index.frame() if df is None else df
    linked = set(full.loc[full.format == "robot_capture", "group"])
    raw_tasks, loose = defaultdict(set), defaultdict(set)
    for row in chosen:
        if row["format"] == "robot_capture":
            raw_tasks[row["task"]].add(row["source"])
        elif row["format"] == "lerobot" and row.get("group") not in linked:
            loose[row["task"]].add(row["source"])
    both = sorted(set(raw_tasks) & set(loose))
    if both:
        lifted = bool(recipe.sources or recipe.allow_unlinked_sources)
        out.append(
            {
                "code": "possible_unlinked_conversion",
                "blocking": not lifted,
                "message": f"{len(both)} task(s) are taken from raw captures and "
                "from a LeRobot dataset with no link to them; it may be the same "
                "recordings twice. Name the sources, or allow it explicitly",
                "tasks": both[:20],
                "raw_sources": sorted({s for t in both for s in raw_tasks[t]})[:10],
                "lerobot_sources": sorted({s for t in both for s in loose[t]})[:10],
            }
        )
    fallback = sum(
        1
        for r in chosen
        if r["outcome_source"] == "robot_flag" and recipe.outcome == "verified_success"
    )
    if fallback:
        out.append(
            {
                "code": "outcome_from_robot_flag",
                "blocking": False,
                "message": f"{fallback} episode(s) count as verified only through "
                "the operator's key press (no human label); use the human-labelled "
                "outcome to leave them out",
                "episodes": fallback,
            }
        )
    return out


def _task_view(row: dict) -> dict:
    return {
        k: row.get(k)
        for k in (
            "key",
            "source",
            "source_path",
            "format",
            "episode",
            "episode_index",
            "frames",
            "category",
            "outcome",
            "outcome_source",
            "human_label",
            "robot_flag",
            "policy_label",
            "policy_method",
            "date",
            "quality_score",
            "sel_stratum",
            "selection_reason",
            "selection_parts",
        )
    }


def selected_episodes(
    recipe: Recipe,
    task: str,
    target: str | None = None,
    df=None,
    human_as_success: bool = False,
) -> dict:
    """The episodes the recipe picks for one task, with score, stratum and
    reason: what preview counts and export writes."""
    result = select_detailed(recipe, df, target, human_as_success)
    if task not in result.tasks:
        raise KeyError(task)
    return {
        "task": task,
        "report": result.tasks[task],
        "episodes": [_task_view(r) for r in result.chosen if r["task"] == task],
    }


def suggest(
    recipe: Recipe,
    task: str,
    target: str | None = None,
    df=None,
    human_as_success: bool = False,
) -> dict:
    """What adding ``task`` (last in the order) to the composition offers:
    its availability under the recipe's filters and the episodes the earlier
    tasks already use, and a default count that keeps the composition
    balanced (the median of the earlier tasks' counts, 100 when none)."""
    task = normalize_task(task)
    base = recipe.model_copy(
        update={"tasks": [t for t in recipe.entries if t.task != task]}
    )
    counts = []
    if base.tasks:
        before = select_detailed(base, df, target, human_as_success)
        counts = [r["selected"] for r in before.tasks.values() if r["selected"]]
    probe = base.model_copy(update={"tasks": [*base.entries, TaskEntry(task=task)]})
    report = select_detailed(probe, df, target, human_as_success).tasks[task]
    return {
        **{
            k: report[k]
            for k in ("available", "successes", "failures", "unknown", "already_used")
        },
        "task": task,
        "suggested_count": picker.suggest_count(counts, report["available"]),
        "earlier_counts": counts,
    }


def preview(
    recipe: Recipe,
    target: str | None = None,
    df=None,
    human_as_success: bool = False,
    fps: float | None = None,
    timing: str | None = None,
) -> dict:
    """Counts for a recipe. With ``fps`` (and, for the formats that have a
    time axis, ``timing``; default: the format's own) the warnings include how
    the chosen raw captures meet that export rate (levi/pool/timing.py)."""
    result = select_detailed(recipe, df, target, human_as_success)
    chosen, excluded = result.chosen, result.excluded
    reasons = Counter(e["reason"] for e in excluded)
    per_task = defaultdict(lambda: {"episodes": 0, "frames": 0, "sources": Counter()})
    for row in chosen:
        item = per_task[row["task"]]
        item["episodes"] += 1
        item["frames"] += int(row["frames"] or 0)
        item["sources"][row["source"]] += 1
    order = [t for t in (recipe.task_names or result.tasks) if t in result.tasks]
    missing = [t for t in recipe.task_names if t not in per_task]
    return {
        "recipe": recipe.model_dump(),
        "target": target,
        "episodes": len(chosen),
        "frames": int(sum(int(r["frames"] or 0) for r in chosen)),
        "tasks": [
            {
                "task": t,
                "text": recipe.task_text.get(t, t),
                "episodes": per_task[t]["episodes"],
                "frames": per_task[t]["frames"],
                "sources": dict(per_task[t]["sources"]),
                **result.tasks[t],
            }
            for t in order
        ],
        "task_reports": list(result.tasks.values()),
        "mix": picker.mix(chosen),
        "tasks_without_episodes": missing,
        "categories": dict(Counter(r["category"] for r in chosen)),
        "formats": dict(Counter(r["format"] for r in chosen)),
        "policy_methods": dict(
            Counter(r["policy_method"] for r in chosen if r.get("policy_method"))
        ),
        "policy_models": dict(
            Counter(r["policy_model"] for r in chosen if r.get("policy_model"))
        ),
        "outcomes": dict(Counter(r["outcome"] or "none" for r in chosen)),
        "outcome_sources": dict(Counter(r["outcome_source"] or "none" for r in chosen)),
        "human_as_success": human_as_success,
        "warnings": find_warnings(recipe, chosen, df)
        + (
            timing_mod.warnings(
                chosen, fps, timing or timing_mod.default_timing(target)
            )
            if fps and timing_mod.has_timing(target)
            else []
        ),
        "excluded": dict(reasons),
        "excluded_label_conflicts": reasons.get("label_conflict", 0),
        "excluded_heldout": reasons.get("heldout", 0),
        "excluded_duplicates": reasons.get("duplicate", 0),
        "excluded_nonstandard": reasons.get("nonstandard", 0),
        "excluded_unsupported": reasons.get("unsupported", 0)
        + reasons.get("not_a_raw_capture", 0),
    }
