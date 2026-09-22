"""Subtask-annotation quality and efficiency, measured one way for everyone.

A person's recorded session and an agent's full-dataset task are both
measured from the same thing -- the subtask atoms in the dataset's
annotation files -- with the same functions, so their records compare line
by line. Nothing here calls a model; every number is recomputable.

A subtask atom covers ``[timestamp, to)``; without ``to`` it runs until the
next subtask starts, or to the episode's last frame (as the timeline draws
it). Frame ``i`` sits at ``i / fps``.
"""

import json
import re
from itertools import pairwise
from pathlib import Path
from statistics import mean, median

# Gaps shorter than this between intervals are not gaps anyone could label.
GAP_SECONDS = 0.2
# An episode this well covered counts as fully covered.
FULL = 0.95
WORD = re.compile(r"[A-Za-z]+|[一-鿿]")


def episode_meta(root: Path) -> tuple[dict[int, int], float]:
    """Frames per episode and fps, from a LeRobot v2.1 dataset or a raw view."""
    root = Path(root)
    info = json.loads((root / "meta/info.json").read_text())
    lengths = {}
    for line in (root / "meta/episodes.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            lengths[int(row["episode_index"])] = int(row["length"])
    return lengths, float(info["fps"])


def read_atoms(annotations_dir: Path, episode: int) -> list[dict] | None:
    try:
        value = json.loads(
            (Path(annotations_dir) / f"episode_{episode:06d}.json").read_text()
        )
    except (OSError, ValueError):
        return None
    return value.get("atoms") or []


def segments(atoms: list[dict], last: float) -> list[dict]:
    """Subtask intervals of one episode, in time order."""
    rows = sorted(
        (a for a in atoms if a.get("style") == "subtask"),
        key=lambda a: float(a.get("timestamp") or 0),
    )
    out = []
    for i, atom in enumerate(rows):
        start = float(atom.get("timestamp") or 0)
        if atom.get("to") is not None:
            end = float(atom["to"])
        elif i + 1 < len(rows):
            end = float(rows[i + 1].get("timestamp") or 0)
        else:
            end = last
        levi = atom.get("levi") or {}
        out.append(
            {
                "subtask": levi.get("subtask_id"),
                "outcome": levi.get("outcome"),
                "content": (atom.get("content") or "").strip(),
                "start": start,
                "end": end,
                "by": ((levi.get("origin") or {}).get("kind")) or "human",
            }
        )
    return out


def _union(intervals, lo, hi):
    spans = sorted((max(lo, a), min(hi, b)) for a, b in intervals if b > a)
    total, cursor = 0.0, lo
    for a, b in spans:
        if b <= cursor:
            continue
        total += b - max(a, cursor)
        cursor = b
    return total


def _gaps(intervals, lo, hi):
    gaps, cursor = [], lo
    for a, b in sorted(intervals):
        if a - cursor > GAP_SECONDS:
            gaps.append(round(a - cursor, 2))
        cursor = max(cursor, b)
    if hi - cursor > GAP_SECONDS:
        gaps.append(round(hi - cursor, 2))
    return gaps


def episode_metrics(segs: list[dict], frames: int, fps: float, vocab: set[str]):
    last = (frames - 1) / fps if frames > 1 else 0.0
    spans = [(s["start"], s["end"]) for s in segs]
    covered = _union(spans, 0.0, last)
    times = [i / fps for i in range(frames)]
    covered_frames = sum(
        1
        for t in times
        if any(
            a <= t < b or (abs(t - last) < 1e-6 and b >= last - 1e-6) for a, b in spans
        )
    )
    ordered = sorted(segs, key=lambda s: s["start"])
    overlaps = sum(1 for x, y in pairwise(ordered) if y["start"] < x["end"] - 1e-6)
    invalid = [
        s
        for s in segs
        if s["end"] <= s["start"] or s["start"] < -1e-6 or s["end"] > last + 1e-3
    ]
    unknown_ids = [
        s for s in segs if s["subtask"] and vocab and s["subtask"] not in vocab
    ]
    return {
        "duration": last,
        "frames": frames,
        "segments": len(segs),
        "covered_seconds": covered,
        "coverage_seconds": covered / last if last else 0.0,
        "coverage_frames": covered_frames / frames if frames else 0.0,
        "gaps": _gaps(spans, 0.0, last),
        "overlaps": overlaps,
        "invalid": len(invalid),
        "unknown_ids": len(unknown_ids),
    }


def summarize(episodes: dict[int, dict], vocab: set[str]) -> dict:
    """``episodes``: index -> {"frames", "fps", "segments"} for the episodes
    the record is about. Returns the shared metric block."""
    per = {
        ep: episode_metrics(row["segments"], row["frames"], row["fps"], vocab)
        for ep, row in sorted(episodes.items())
    }
    segs = [s for row in episodes.values() for s in row["segments"]]
    n = len(segs)
    duration = sum(m["duration"] for m in per.values())
    frames = sum(m["frames"] for m in per.values())
    contents = [s["content"] for s in segs if s["content"]]
    words = [WORD.findall(c.lower()) for c in contents]
    tokens = [w for ws in words for w in ws]
    labels = {s["subtask"] or s["content"].lower() for s in segs}
    outcomes = {}
    for s in segs:
        outcomes[s["outcome"] or "none"] = outcomes.get(s["outcome"] or "none", 0) + 1
    coverage = [m["coverage_seconds"] for m in per.values()]
    valid = n - sum(m["invalid"] + m["unknown_ids"] for m in per.values())
    return {
        "episodes": len(per),
        "segments": n,
        "segments_per_episode": n / len(per) if per else 0.0,
        "video_seconds": duration,
        "coverage": {
            "seconds": sum(m["covered_seconds"] for m in per.values()) / duration
            if duration
            else 0.0,
            "frames": sum(m["coverage_frames"] * m["frames"] for m in per.values())
            / frames
            if frames
            else 0.0,
            "episode_mean": mean(coverage) if coverage else 0.0,
            "episode_min": min(coverage) if coverage else 0.0,
            "episode_median": median(coverage) if coverage else 0.0,
            "fully_covered": sum(c >= FULL for c in coverage),
            "gaps": sum(len(m["gaps"]) for m in per.values()),
            "gap_seconds": round(sum(sum(m["gaps"]) for m in per.values()), 2),
        },
        "semantics": {
            "distinct_labels": len(labels),
            "with_subtask_id": sum(1 for s in segs if s["subtask"]) / n if n else 0.0,
            "vocabulary_used": len(
                {s["subtask"] for s in segs if s["subtask"] in vocab}
            )
            if vocab
            else None,
            "vocabulary_size": len(vocab) or None,
            "with_outcome": sum(1 for s in segs if s["outcome"]) / n if n else 0.0,
            "outcomes": outcomes,
            "with_description": len(contents) / n if n else 0.0,
            "words_per_description": len(tokens) / len(contents) if contents else 0.0,
            "distinct_descriptions": len(set(contents)) / len(contents)
            if contents
            else 0.0,
            "type_token_ratio": len(set(tokens)) / len(tokens) if tokens else 0.0,
            "label_only": sum(
                1
                for s in segs
                if s["content"]
                and s["subtask"]
                and s["content"].lower() == s["subtask"]
            )
            / n
            if n
            else 0.0,
        },
        "structure": {
            "valid_share": valid / n if n else 0.0,
            "invalid_intervals": sum(m["invalid"] for m in per.values()),
            "overlaps": sum(m["overlaps"] for m in per.values()),
            "unknown_subtask_ids": sum(m["unknown_ids"] for m in per.values()),
        },
        "per_episode": {
            f"episode_{ep:06d}": {
                "segments": m["segments"],
                "coverage_seconds": round(m["coverage_seconds"], 3),
                "gaps": len(m["gaps"]),
                "overlaps": m["overlaps"],
            }
            for ep, m in per.items()
        },
    }


def agreement(reference: dict[int, list], candidate: dict[int, list]) -> dict | None:
    """Interval-by-interval agreement on the episodes both annotated
    (``levi.harness.grading``: same subtask and IoU >= 0.3)."""
    from levi.harness import grading

    common = sorted(set(reference) & set(candidate))
    if not common:
        return None

    def rows(segs):
        return [
            {
                "subtask": s["subtask"] or s["content"].lower(),
                "start": s["start"],
                "end": s["end"],
                "outcome": s["outcome"],
                "content": s["content"],
            }
            for s in segs
        ]

    graded = grading.grade(
        {
            "task": "agreement",
            "episodes": {f"episode_{e:06d}": rows(reference[e]) for e in common},
        },
        {f"episode_{e:06d}": rows(candidate[e]) for e in common},
    )
    return {"episodes": len(common), **graded["overall"]}
