"""Carry annotations from a source dataset into one converted from it.

Typical case: a raw capture was annotated through its browsing view, then
converted (possibly resampled/filtered, some demos excluded). Episodes are
matched by ``source_demo`` — never by index — and frames through the
provenance ledgers (``meta/levi_provenance.jsonl``): each side records which
raw capture row every frame came from (``source_positions``; older LEVI
conversions only ``source_frame_ids``, which serve the same purpose).

- Language atoms: moved to the output frame nearest in the raw capture;
  ``timestamp``/``to`` rewritten from the output's own frame times, so event
  atoms stay snapped to frames. Persistent atoms that collapse onto one frame
  are reported.
- Outcome labels: copied per episode.
- SAM3 masks: kept only on frames the output retained exactly (a mask is
  pixel-accurate for its frame; it is never moved to a neighbour), published
  as a new revision (``model.provider = carry_over``).

Nothing is overwritten: an output episode that already has annotations keeps
them and the conflict is reported. Everything dropped is counted in
``meta/levi_annotation_carryover.json`` inside the output dataset.
"""

import json
from pathlib import Path
from typing import Any

import numpy as np

from .. import catalog
from . import outcomes
from .schema import ObjectAnnotation
from .sidecar import SidecarStore

# Mirrors backend/app.py PERSISTENT_STYLES (everything else is an event).
PERSISTENT_STYLES = {"task_aug", "subtask", "plan", "memory", "motion"}


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _frames(root: Path) -> tuple[dict[str, dict], float]:
    """source_demo → {"episode": index, "keys": raw-row keys per frame}."""
    info = json.loads((root / "meta/info.json").read_text())
    ledger = _jsonl(root / "meta/levi_provenance.jsonl")
    result = {}
    for item in ledger:
        keys = item.get("source_positions")
        kind = "positions"
        if keys is None:
            keys, kind = item.get("source_frame_ids"), "frame_ids"
        if keys is None or "source_demo" not in item:
            continue
        result[item["source_demo"]] = {
            "episode": item["episode_index"],
            "keys": np.asarray(keys),
            "kind": kind,
        }
    return result, float(info["fps"])


def _nearest(keys: np.ndarray, value) -> int:
    j = int(np.clip(np.searchsorted(keys, value), 0, len(keys) - 1))
    if j > 0 and abs(keys[j - 1] - value) <= abs(keys[j] - value):
        j -= 1
    return j


def carry_over(
    source_name: str, source_root: Path, output: Path, output_name: str
) -> dict:
    state = catalog.STATE
    src, src_fps = _frames(source_root)
    dst, dst_fps = _frames(output)
    report: dict[str, Any] = {
        "schema": "levi.annotation_carryover.v1",
        "source": source_name,
        "source_root": str(source_root),
        "matched_episodes": 0,
        "unmatched_source_episodes": [],
        "language": {"atoms": 0, "episodes": 0, "collapsed": 0, "conflicts": []},
        "outcomes": {"labels": 0, "conflicts": []},
        "sam3": {"masks": 0, "dropped_frames": 0, "revision": None},
    }
    if not src or not dst:
        report["skipped"] = "No provenance ledger on one side; nothing can be matched"
        return _save(output, report)
    pairs = {}  # source episode → (output episode, map)
    for demo, s in src.items():
        d = dst.get(demo)
        if d is None or s["kind"] != d["kind"]:
            report["unmatched_source_episodes"].append(s["episode"])
            continue
        pairs[s["episode"]] = (d["episode"], s["keys"], d["keys"])
    report["matched_episodes"] = len(pairs)

    def to_output(ep: int, t: float) -> int:
        _, s_keys, d_keys = pairs[ep]
        i = int(np.clip(round(t * src_fps), 0, len(s_keys) - 1))
        return _nearest(d_keys, s_keys[i])

    # Language atoms
    src_dir = state / "annotations" / source_name
    dst_dir = state / "annotations" / output_name
    for path in sorted(src_dir.glob("episode_*.json")):
        ep = int(path.stem.split("_")[1])
        if ep not in pairs:
            continue
        out_ep = pairs[ep][0]
        target = dst_dir / f"episode_{out_ep:06d}.json"
        if target.exists():
            report["language"]["conflicts"].append(out_ep)
            continue
        atoms = json.loads(path.read_text()).get("atoms", [])
        moved, seen = [], set()
        for atom in atoms:
            atom = dict(atom)
            j = to_output(ep, float(atom["timestamp"]))
            atom["timestamp"] = j / dst_fps
            if atom.get("to") is not None:
                atom["to"] = to_output(ep, float(atom["to"])) / dst_fps
            if atom.get("style") in PERSISTENT_STYLES:
                key = (atom.get("style"), atom.get("role"), j)
                if key in seen:
                    report["language"]["collapsed"] += 1
                seen.add(key)
            moved.append(atom)
        catalog.atomic(target, {"episode_index": out_ep, "atoms": moved})
        report["language"]["atoms"] += len(moved)
        report["language"]["episodes"] += 1

    # Outcome labels
    labels = outcomes.read_labels(src_dir)
    existing = outcomes.read_labels(dst_dir)
    for ep, label in labels.items():
        if ep not in pairs:
            continue
        out_ep = pairs[ep][0]
        if out_ep in existing:
            report["outcomes"]["conflicts"].append(out_ep)
            continue
        outcomes.write_label(dst_dir, out_ep, label["outcome"])
        report["outcomes"]["labels"] += 1

    # SAM3 masks
    source_store_root = state / "object_annotations" / source_name
    if (source_store_root / "current.json").exists():
        store = SidecarStore(source_store_root)
        revision = store.current_revision()
        rows = []
        cameras = {
            k
            for k, f in json.loads((output / "meta/info.json").read_text())[
                "features"
            ].items()
            if f.get("dtype") == "video"
        }
        for raw in store.read_annotations(revision):
            row = SidecarStore._from_mask_row(raw)
            ep = row["episode_index"]
            if ep not in pairs or row["camera_key"] not in cameras:
                report["sam3"]["dropped_frames"] += 1
                continue
            out_ep, s_keys, d_keys = pairs[ep]
            key = (
                s_keys[row["frame_index"]] if row["frame_index"] < len(s_keys) else None
            )
            hits = np.flatnonzero(d_keys == key) if key is not None else []
            if len(hits) == 0:
                report["sam3"]["dropped_frames"] += 1
                continue
            j = int(hits[0])
            rows.append(
                ObjectAnnotation.model_validate(
                    {
                        **row,
                        "episode_index": out_ep,
                        "frame_index": j,
                        "timestamp": j / dst_fps,
                    }
                )
            )
        if rows:
            out_store = SidecarStore(
                state / "object_annotations" / output_name,
                identity={"local_path": str(output), "fps": dst_fps},
            )
            if out_store.current_revision():
                report["sam3"]["conflict"] = "output already has SAM3 annotations"
            else:
                info = out_store.publish(
                    rows,
                    model={
                        "provider": "carry_over",
                        "source": source_name,
                        "source_revision": revision,
                    },
                )
                report["sam3"]["revision"] = info["revision_id"]
                report["sam3"]["masks"] = len(rows)
    return _save(output, report)


def _save(output: Path, report: dict) -> dict:
    report["ok"] = True
    catalog.atomic(output / "meta/levi_annotation_carryover.json", report)
    return report


def rekey_view(name: str, old_view: Path, new_view: Path) -> dict:
    """A rebuilt view (the raw capture gained or lost demos) renumbers
    episodes; move the raw dataset's own language and outcome files to the
    new numbers, by ``source_demo``. Files of demos no longer present are
    moved aside to ``orphaned/`` (never deleted)."""
    old = {
        r["episode_index"]: r.get("source_demo")
        for r in _jsonl(old_view / "meta/episodes.jsonl")
    }
    new = {
        r.get("source_demo"): r["episode_index"]
        for r in _jsonl(new_view / "meta/episodes.jsonl")
    }
    folder = catalog.STATE / "annotations" / name
    moved = orphaned = 0
    for sub in (folder, folder / "outcomes"):
        if not sub.is_dir():
            continue
        staged = []
        for path in sorted(sub.glob("episode_*.json")):
            ep = int(path.stem.split("_")[1])
            target_ep = new.get(old.get(ep))
            temp = path.with_name(f".rekey.{path.name}")
            path.rename(temp)
            staged.append((temp, ep, target_ep))
        for temp, ep, target_ep in staged:
            if target_ep is None:
                (sub / "orphaned").mkdir(exist_ok=True)
                temp.rename(sub / "orphaned" / f"episode_{ep:06d}.json")
                orphaned += 1
                continue
            value = json.loads(temp.read_text())
            value["episode_index"] = target_ep
            catalog.atomic(sub / f"episode_{target_ep:06d}.json", value)
            temp.unlink()
            moved += 1
    return {"moved": moved, "orphaned": orphaned}
