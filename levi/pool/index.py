"""Read-only queries over the pool index (CLI, API and recipes share them)."""

import json
from collections import Counter, defaultdict

import pandas as pd
import pyarrow.parquet as pq

from . import embodiment, exclusions, scanner

OLDER = (
    "The pool index is from an older LEVI (no {columns} column): scan again "
    "(Scan now on the page, or `levi pool scan`) / "
    "训练池索引由旧版 LEVI 写成（缺少 {columns} 列）：请重新扫描"
    "（页面上点“立即扫描”，或运行 `levi pool scan`）"
)


def frame() -> pd.DataFrame:
    path = scanner.index_path()
    if not path.is_file():
        raise ValueError("The pool has not been scanned yet: run `levi pool scan`")
    df = pd.read_parquet(path)
    missing = [c for c in ("policy_method", *embodiment.COLUMNS) if c not in df.columns]
    if missing:
        raise ValueError(OLDER.format(columns=", ".join(missing)))
    return mark_excluded(df)


def removed_groups(keys=None) -> tuple[set, set]:
    """``(keys, groups)`` of the episodes a person removed on a live page
    (``exclusions.py``), read now: the removed episodes' keys and the groups
    they belong to. ``keys`` is ``{key: group}`` of the index (read when not
    given); without an index only the keys are known."""
    removed = exclusions.current(summary().get("workspaces"))
    if not removed:
        return set(), set()
    if keys is None:
        path = scanner.index_path()
        if path.is_file():
            frame = pd.read_parquet(path, columns=["key", "group"])
            keys = dict(zip(frame.key, frame.group, strict=True))
        else:
            keys = {}
    return set(removed), {keys[k] for k in removed if k in keys}


def mark_excluded(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``excluded``: the episode, or a copy of it, was removed on a live
    workspace's page (``exclusions.py``; always read fresh, no scan needed)."""
    removed = exclusions.current(summary().get("workspaces"))
    df = df.copy()
    if not removed:
        df["excluded"] = False
        return df
    groups = set(df.loc[df.key.isin(list(removed)), "group"])
    df["excluded"] = df.key.isin(list(removed)) | df.group.isin(groups)
    return df


def heldout_view() -> pd.DataFrame | None:
    """``key``, ``group`` and ``heldout`` of the index, whatever else it holds
    (an index from an older LEVI has them too), or ``None`` when no scan has
    written one. A damaged file raises: the held-out check never passes by
    silence."""
    path = scanner.index_path()
    if not path.is_file():
        return None
    return pd.read_parquet(path, columns=["key", "group", "heldout"])


def embodiment_view() -> pd.DataFrame | None:
    """``key``, ``source`` and ``gripper`` of the index (``None`` before the
    first scan). An index from an older LEVI lacks ``gripper``: that raises, an
    export cannot check its plan against it."""
    path = scanner.index_path()
    if not path.is_file():
        return None
    if "gripper" not in pq.read_schema(path).names:
        raise ValueError(OLDER.format(columns="gripper"))
    return pd.read_parquet(path, columns=["key", "source", "gripper"])


def summary() -> dict:
    try:
        return json.loads(scanner.scan_path().read_text())
    except (OSError, ValueError):
        return {}


def sources(category: str | None = None, show_archive: bool = False) -> list[dict]:
    try:
        items = json.loads(scanner.sources_path().read_text())["sources"]
    except (OSError, ValueError, KeyError):
        raise ValueError(
            "The pool has not been scanned yet: run `levi pool scan`"
        ) from None
    if category:
        items = [s for s in items if s.get("category") == category]
    elif not show_archive:
        items = [s for s in items if s.get("category") != "archive"]
    return items


def _filter(
    df: pd.DataFrame,
    *,
    categories=None,
    sources=None,
    tasks=None,
    search=None,
    formats=None,
    outcome=None,
    policies=None,
    policy_models=None,
    policy_checkpoints=None,
    policy_methods=None,
    robots=None,
    grippers=None,
    date_from=None,
    date_to=None,
    show_heldout=False,
    show_copies=False,
    show_nonstandard=True,
    show_archive=False,
    show_excluded=False,
) -> pd.DataFrame:
    if categories:
        df = df[df.category.isin(categories)]
    elif not show_archive:
        df = df[df.category != "archive"]
    if sources:
        df = df[df.source.isin(sources) | df.source_path.isin(sources)]
    if tasks:
        df = df[df.task.isin([t.strip().lower() for t in tasks])]
    if search:
        df = df[df.task.str.contains(search.lower(), regex=False, na=False)]
    if formats:
        df = df[df.format.isin(formats)]
    if outcome == "robot_flag_success":
        df = df[df.robot_flag == "success"]
    elif outcome == "verified_success":
        # A human label first, then the robot's flag (as in recipes).
        df = df[df.human_label.fillna(df.robot_flag) == "success"]
    elif outcome == "human_verified_success":
        df = df[df.human_label == "success"]
    elif outcome:
        df = df[df.outcome == outcome]
    if policies:
        df = df[df.policy.isin(policies)]
    if policy_models:
        df = df[df.policy_model.isin(policy_models)]
    if policy_checkpoints:
        df = df[df.policy_checkpoint.isin(policy_checkpoints)]
    if policy_methods:
        df = df[df.policy_method.isin(policy_methods)]
    if robots:
        df = df[df.robot.fillna("unknown").isin(robots)]
    if grippers:
        df = df[df.gripper.fillna("unknown").isin(grippers)]
    if date_from:
        df = df[df.date.fillna("") >= date_from]
    if date_to:
        df = df[df.date.fillna("9999") <= date_to]
    if not show_heldout:
        df = df[~df.heldout.astype(bool)]
    if not show_excluded and "excluded" in df.columns:
        # Removed on a live page: not listed, counted or exported.
        df = df[~df.excluded.astype(bool)]
    if not show_copies:
        df = df[df.canonical.astype(bool)]
    if not show_nonstandard:
        df = df[~df.nonstandard.astype(bool)]
    return df


def episodes(limit: int = 200, offset: int = 0, **filters) -> dict:
    df = _filter(frame(), **filters)
    order = df.sort_values(["task", "source", "episode"])
    page = order.iloc[offset : offset + limit]
    rows = page.astype(object).where(pd.notna(page), None).to_dict("records")
    for row in rows:
        row["cameras"] = json.loads(row.get("cameras") or "[]")
        row.pop("video_sha256", None)
        row.pop("stat_sig", None)
        row["embodiment_evidence"] = json.loads(row.get("embodiment_evidence") or "{}")
    return {"total": len(df), "offset": offset, "episodes": rows}


def facets(**filters) -> dict:
    """Counts for the page's facets over the rows the visibility toggles
    (``show_heldout``, ``show_copies``, ``show_archive``) and ``categories``
    leave: categories, sources, formats, policies (the old single field: the
    checkpoint), policy_models, policy_checkpoints, policy_methods, robots, grippers, outcomes,
    dates, plus how
    many held-out episodes and copies the toggles hide."""
    full = frame()
    toggles = {
        k: filters.get(k)
        for k in ("show_heldout", "show_copies", "show_archive")
        if k in filters
    }
    df = _filter(full, **toggles)
    scoped = _filter(full, categories=filters.get("categories"), **toggles)
    dates = sorted(d for d in scoped.date.dropna().unique() if d)
    return {
        "episodes": len(df),
        "categories": dict(Counter(df.category)),
        "sources": [
            {"source": s, "path": p, "episodes": int(n)}
            for (s, p), n in scoped.groupby(["source", "source_path"])
            .size()
            .sort_values(ascending=False)
            .items()
        ],
        "formats": dict(Counter(scoped.format)),
        "policies": dict(Counter(scoped.policy.dropna())),
        "policy_models": dict(Counter(scoped.policy_model.dropna())),
        "policy_checkpoints": dict(Counter(scoped.policy_checkpoint.dropna())),
        "policy_methods": dict(Counter(scoped.policy_method.dropna())),
        "robots": dict(Counter(scoped.robot.fillna("unknown"))),
        "grippers": dict(Counter(scoped.gripper.fillna("unknown"))),
        "outcomes": {
            "success": int((scoped.outcome == "success").sum()),
            "failure": int((scoped.outcome == "failure").sum()),
            "robot_flag_success": int((scoped.robot_flag == "success").sum()),
            "verified_success": int(
                (scoped.human_label.fillna(scoped.robot_flag) == "success").sum()
            ),
            "human_verified_success": int((scoped.human_label == "success").sum()),
        },
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "hidden_heldout": int(
            len(_filter(full, **{**toggles, "show_heldout": True})) - len(df)
        )
        if not toggles.get("show_heldout")
        else 0,
        "hidden_copies": int(
            len(_filter(full, **{**toggles, "show_copies": True})) - len(df)
        )
        if not toggles.get("show_copies")
        else 0,
        "archive": int((full.category == "archive").sum()),
        # Removed on a live page (never listed, counted or exported).
        "removed_in_live": int(full.excluded.astype(bool).sum()),
    }


def tasks(**filters) -> list[dict]:
    """Per normalized task: episodes and frames per category, outcomes,
    sources and the original spellings."""
    df = _filter(frame(), **filters)
    out = []
    by_task = defaultdict(list)
    for row in df[
        [
            "task",
            "task_raw",
            "category",
            "frames",
            "outcome",
            "source",
            "policy_method",
            "gripper",
        ]
    ].itertuples(index=False):
        by_task[row.task].append(row)
    for task, members in sorted(by_task.items()):
        outcomes = Counter(m.outcome for m in members if m.outcome)
        decided = outcomes.get("success", 0) + outcomes.get("failure", 0)
        out.append(
            {
                "task": task,
                "spellings": sorted({str(m.task_raw) for m in members if m.task_raw}),
                "episodes": len(members),
                "frames": int(sum(int(m.frames or 0) for m in members)),
                "categories": dict(Counter(m.category for m in members)),
                "policy_methods": dict(
                    Counter(m.policy_method for m in members if m.policy_method)
                ),
                "grippers": dict(Counter(m.gripper or "unknown" for m in members)),
                "success": outcomes.get("success", 0),
                "failure": outcomes.get("failure", 0),
                "success_rate": round(outcomes.get("success", 0) / decided, 3)
                if decided
                else None,
                "sources": sorted({m.source for m in members}),
            }
        )
    return out
