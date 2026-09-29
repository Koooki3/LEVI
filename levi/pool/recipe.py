"""Recipes: named, saved selections over the pool index, and their preview.

A recipe picks episodes by category, source, format, task (an ordered list:
the export follows it), date and policy, then applies, in order:

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
6. ``per_task_cap`` episodes per task, drawn with ``seed``.

Every episode left out is listed with its reason.
"""

import json
import os
import random
import re
import time
from collections import Counter, defaultdict
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import index, settings
from .rules import CATEGORIES, normalize_task

NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
DATE = r"^\d{4}-\d{2}-\d{2}$"


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    description: str = Field("", max_length=2000)
    categories: list[Literal[CATEGORIES]] = Field(default_factory=list)  # type: ignore[valid-type]
    sources: list[str] = Field(default_factory=list, max_length=1000)
    formats: list[Literal["robot_capture", "lerobot", "droid_raw"]] = Field(
        default_factory=list
    )
    tasks: list[str] = Field(default_factory=list, max_length=5000)
    outcome: Literal[
        "all", "robot_flag_success", "verified_success", "human_verified_success"
    ] = "all"
    per_task_cap: int | None = Field(None, ge=1, le=10_000_000)
    seed: int = 0
    date_from: str | None = Field(None, pattern=DATE)
    date_to: str | None = Field(None, pattern=DATE)
    policies: list[str] = Field(default_factory=list)
    include_nonstandard: bool = False
    # A task taken from raw captures and from a LeRobot source that is not
    # linked to them may be one recording twice: refused unless sources are
    # named or this is set.
    allow_unlinked_sources: bool = False
    exclude: list[str] = Field(default_factory=list, max_length=100000)
    # Normalized task -> the text written into the export (default: the
    # normalized task itself).
    task_text: dict[str, str] = Field(default_factory=dict)

    @field_validator("tasks")
    @classmethod
    def _tasks(cls, value):
        normalized = [normalize_task(t) for t in value]
        if any(not t for t in normalized):
            raise ValueError("Tasks must be nonempty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("A task is listed twice")
        return normalized

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
            out.append(json.loads(path.read_text()))
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


def select(
    recipe: Recipe,
    df: pd.DataFrame | None = None,
    target: str | None = None,
    human_as_success: bool = False,
):
    """Ordered episodes of a recipe, and every excluded one with its reason.

    ``target`` adds the export format's own requirement: ``raw_capture`` copies
    raw captures only, ``recap_value`` needs an outcome per episode
    (``human_as_success``: a human demonstration without one counts as a
    success)."""
    df = index.frame() if df is None else df
    df = index._filter(
        df,
        categories=recipe.categories or None,
        sources=recipe.sources or None,
        tasks=recipe.tasks or None,
        formats=recipe.formats or None,
        policies=recipe.policies or None,
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
    task_order = recipe.tasks or sorted({r["task"] for r in passed})
    position = {t: i for i, t in enumerate(task_order)}
    source_order = {s: i for i, s in enumerate(recipe.sources)}
    by_task = defaultdict(list)
    for row in passed:
        by_task[row["task"]].append(row)
    chosen = []
    for task in task_order:
        members = sorted(by_task.get(task, []), key=lambda r: r["key"])
        if recipe.per_task_cap and len(members) > recipe.per_task_cap:
            rng = random.Random(f"{recipe.seed}:{task}")
            keep = {r["key"] for r in rng.sample(members, recipe.per_task_cap)}
            for row in members:
                if row["key"] not in keep:
                    out(row, "per_task_cap")
            members = [r for r in members if r["key"] in keep]
        chosen += members
    chosen.sort(
        key=lambda r: (
            position[r["task"]],
            source_order.get(
                r["source"], source_order.get(r["source_path"], len(source_order))
            ),
            r["source"],
            _natural(r["episode"]),
        )
    )
    return chosen, excluded


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


def preview(
    recipe: Recipe,
    target: str | None = None,
    df=None,
    human_as_success: bool = False,
) -> dict:
    chosen, excluded = select(recipe, df, target, human_as_success)
    reasons = Counter(e["reason"] for e in excluded)
    per_task = defaultdict(lambda: {"episodes": 0, "frames": 0, "sources": Counter()})
    for row in chosen:
        item = per_task[row["task"]]
        item["episodes"] += 1
        item["frames"] += int(row["frames"] or 0)
        item["sources"][row["source"]] += 1
    order = list(dict.fromkeys(r["task"] for r in chosen))
    missing = [t for t in recipe.tasks if t not in per_task]
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
            }
            for t in order
        ],
        "tasks_without_episodes": missing,
        "categories": dict(Counter(r["category"] for r in chosen)),
        "formats": dict(Counter(r["format"] for r in chosen)),
        "outcomes": dict(Counter(r["outcome"] or "none" for r in chosen)),
        "outcome_sources": dict(Counter(r["outcome_source"] or "none" for r in chosen)),
        "human_as_success": human_as_success,
        "warnings": find_warnings(recipe, chosen, df),
        "excluded": dict(reasons),
        "excluded_label_conflicts": reasons.get("label_conflict", 0),
        "excluded_heldout": reasons.get("heldout", 0),
        "excluded_duplicates": reasons.get("duplicate", 0),
        "excluded_nonstandard": reasons.get("nonstandard", 0),
        "excluded_unsupported": reasons.get("unsupported", 0)
        + reasons.get("not_a_raw_capture", 0),
    }
