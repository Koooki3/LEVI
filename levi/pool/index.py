"""Read-only queries over the pool index (CLI, API and recipes share them)."""

import json
from collections import Counter, defaultdict

import pandas as pd

from . import scanner


def frame() -> pd.DataFrame:
    path = scanner.index_path()
    if not path.is_file():
        raise ValueError("The pool has not been scanned yet: run `levi pool scan`")
    return pd.read_parquet(path)


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
    date_from=None,
    date_to=None,
    show_heldout=False,
    show_copies=False,
    show_nonstandard=True,
    show_archive=False,
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
    if date_from:
        df = df[df.date.fillna("") >= date_from]
    if date_to:
        df = df[df.date.fillna("9999") <= date_to]
    if not show_heldout:
        df = df[~df.heldout.astype(bool)]
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
    return {"total": len(df), "offset": offset, "episodes": rows}


def facets(**filters) -> dict:
    """Counts for the page's facets over the rows the visibility toggles
    (``show_heldout``, ``show_copies``, ``show_archive``) and ``categories``
    leave: categories, sources, formats, policies, outcomes, dates, plus how
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
    }


def tasks(**filters) -> list[dict]:
    """Per normalized task: episodes and frames per category, outcomes,
    sources and the original spellings."""
    df = _filter(frame(), **filters)
    out = []
    by_task = defaultdict(list)
    for row in df[
        ["task", "task_raw", "category", "frames", "outcome", "source"]
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
                "success": outcomes.get("success", 0),
                "failure": outcomes.get("failure", 0),
                "success_rate": round(outcomes.get("success", 0) / decided, 3)
                if decided
                else None,
                "sources": sorted({m.source for m in members}),
            }
        )
    return out
