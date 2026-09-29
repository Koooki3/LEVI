"""Walk the pool roots and write ``<workspace>/pool/index.parquet``.

One row per episode (see ``COLUMNS`` and docs/TRAINING_POOL.md#index):

- **Formats.** Raw captures (a folder with ``end_effector_pose.csv`` — its
  ``demo_NNNN`` name is the standard; ``… copy``, ``_failure`` and task folders
  nested in task folders are listed as ``nonstandard``), LeRobot v2.x
  (``meta/info.json``), DROID raw (``trajectory.h5`` + ``metadata_*.json``).
  Folders of pickle / npz / npy files are listed as sources that cannot be
  exported. LeRobot v3 datasets are listed without episodes.
- **Categories** from ``rules.py``: path rules (archive), format rules
  (external), LEVI provenance (``levi``: LEVI outputs and copies inside a LEVI
  workspace), then the data's own ``data_source`` / ``control_mode``.
- **Fingerprints.** A raw episode: md5 of ``frames.csv`` (else
  ``end_effector_pose.csv``) plus every video's name and size. A LeRobot
  episode converted from a raw episode the pool indexed
  (``rollout_source_demo``, ``meta/processed_demos.json``, or LEVI's
  ``source_demo`` + ``levi_conversion.json``) carries that episode's
  fingerprint; otherwise the md5 of its state and action arrays. Episodes
  with one fingerprint are copies of one another; the canonical one is the
  original collection or rollout folder.
- **Held-out** (``heldout.py``), **human outcome labels** (``labels.py``) and
  the robot's own flag are joined in; a label or a held-out mark on one copy
  applies to every copy.

Re-scans reuse every episode whose files did not change (size + mtime).
"""

import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ..conversion import raw
from ..conversion.progress import Progress
from . import heldout, labels, settings
from . import rules as rules_mod

SCHEMA = "levi.pool.index.v1"
STAGES = ["Walk", "Raw episodes", "Datasets", "Labels", "Held-out", "Index"]
COLUMNS = [
    "key",
    "source",
    "source_path",
    "root",
    "format",
    "episode",
    "episode_index",
    "task",
    "task_raw",
    "frames",
    "fps",
    "cameras",
    "state_dim",
    "action_dim",
    "category",
    "category_reason",
    "data_source",
    "control_mode",
    "policy",
    "date",
    "robot_flag",
    "human_label",
    "human_label_from",
    "label_conflict",
    "outcome",
    "outcome_source",
    "nonstandard",
    "nonstandard_reason",
    "exportable",
    "not_exportable_reason",
    "fingerprint",
    "fingerprint_kind",
    "derived_from",
    "canonical",
    "canonical_key",
    "copies",
    "heldout",
    "heldout_set",
    "heldout_id",
    "in_levi_workspace",
    "video_bytes",
    "video_sha256",
    "stat_sig",
]
# Facts that depend only on an episode's own files, reused while unchanged.
RC_FACTS = (
    "frames",
    "fps",
    "cameras",
    "task_raw",
    "data_source",
    "control_mode",
    "policy",
    "date",
    "robot_flag",
    "fingerprint",
    "video_bytes",
    "video_sha256",
)
VIDEO_SUFFIXES = (".mp4", ".avi", ".mkv")


def index_path() -> Path:
    return settings.pool_dir() / "index.parquet"


def sources_path() -> Path:
    return settings.pool_dir() / "sources.json"


def scan_path() -> Path:
    return settings.pool_dir() / "scan.json"


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(temp, path)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _sig(folder: Path, extra: list[Path] = ()) -> str:
    rows = []
    try:
        for entry in os.scandir(folder):
            if entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                rows.append((entry.name, st.st_size, st.st_mtime_ns))
    except OSError:
        pass
    for path in extra:
        try:
            st = path.stat()
            rows.append((str(path), st.st_size, st.st_mtime_ns))
        except OSError:
            pass
    return _md5(json.dumps(sorted(rows)).encode())


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


# ------------------------------------------------------------------ walk


def walk(roots: list[Path], rules: dict, progress: Progress | None = None) -> dict:
    """Every raw episode, LeRobot dataset, DROID episode, record-only data
    folder and LEVI workspace under the roots (skip rules applied)."""
    found = {"rc": [], "lerobot": [], "droid": [], "unsupported": {}, "workspaces": []}
    seen: set[str] = set()
    suffixes = rules["unsupported_suffixes"]
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            key = str(here)
            if key in seen:
                dirnames[:] = []
                continue
            seen.add(key)
            rel = here.relative_to(root).as_posix()
            if labels.is_workspace(here):
                found["workspaces"].append(here)
            keep = []
            for name in dirnames:
                child_rel = name if rel == "." else f"{rel}/{name}"
                if rules_mod.skipped(child_rel, name, rules):
                    continue
                if (here / name).is_symlink():
                    continue
                keep.append(name)
            dirnames[:] = sorted(keep)
            files = set(filenames)
            if "meta" in dirnames and (here / "meta/info.json").is_file():
                found["lerobot"].append(here)
                dirnames[:] = []
                continue
            if "end_effector_pose.csv" in files:
                found["rc"].append(here)
                dirnames[:] = []
                continue
            if "trajectory.h5" in files and any(
                f.startswith("metadata_") and f.endswith(".json") for f in files
            ):
                found["droid"].append(here)
                dirnames[:] = []
                continue
            data = [f for f in filenames if Path(f).suffix in suffixes]
            if data:
                size = 0
                kinds = Counter()
                for name in data:
                    try:
                        size += (here / name).stat().st_size
                    except OSError:
                        continue
                    kinds[suffixes[Path(name).suffix]] += 1
                found["unsupported"][key] = {
                    "bytes": size,
                    "files": len(data),
                    "kinds": dict(kinds),
                }
            if progress:
                progress.advance(rel)
    return found


# ------------------------------------------------------------------ helpers


class Context:
    def __init__(self, roots, rules, workspaces, registered):
        self.roots = roots
        self.rules = rules
        self.workspaces = {str(w) for w in workspaces}
        self.registered = registered

    def root_of(self, path: Path) -> Path:
        return settings.inside_any(path, self.roots) or path.anchor

    def relative(self, path: Path) -> str:
        root = self.root_of(path)
        rel = Path(path).relative_to(root).as_posix()
        return root.name if rel == "." else rel

    def source_id(self, path: Path) -> str:
        root = self.root_of(path)
        rel = self.relative(path)
        # Two roots may hold folders with one relative name.
        return rel if len(self.roots) == 1 else f"{root.name}/{rel}"

    def in_workspace(self, path: Path) -> bool:
        path = Path(path)
        return any(str(p) in self.workspaces for p in path.parents)


def _category(ctx: Context, path: Path, fmt: str, facts: dict, levi: bool):
    rules = ctx.rules
    by_path = rules_mod.path_category(ctx.relative(path), rules)
    if by_path:
        return by_path, "path_rule"
    if fmt in rules["format_categories"]:
        return rules["format_categories"][fmt], "format_rule"
    if levi:
        return "levi", "levi_output"
    if ctx.in_workspace(path):
        return "levi", "levi_workspace_copy"
    source = facts.get("data_source")
    if source in rules["rollout_data_sources"]:
        return "rollout", f"data_source={source}"
    if source in rules["human_data_sources"]:
        return "human", f"data_source={source}"
    if source:
        return "external", f"unknown data_source={source}"
    if fmt == "robot_capture":
        mode = facts.get("control_mode")
        if mode in rules["teleop_control_modes"]:
            return "human", f"control_mode={mode}"
        return "human", "no data_source"
    return None, None


def _policy(meta: dict) -> str | None:
    policy = meta.get("policy")
    if isinstance(policy, dict):
        checkpoint = policy.get("checkpoint_dir")
        if checkpoint:
            return Path(str(checkpoint)).name
        return policy.get("config")
    if isinstance(policy, str):
        return policy
    return None


def _videos(folder: Path) -> list[tuple[str, int]]:
    out = []
    for entry in os.scandir(folder):
        if entry.is_file() and Path(entry.name).suffix in VIDEO_SUFFIXES:
            out.append((entry.name, entry.stat().st_size))
    return sorted(out)


def rc_facts(demo: Path) -> dict:
    """Everything about a raw episode that depends only on its own files."""
    meta = _read_json(demo / "metadata.json") or {}
    table = demo / "frames.csv"
    if not table.is_file():
        table = demo / "end_effector_pose.csv"
    data = table.read_bytes()
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    frames = max(0, lines - 1)  # minus the header
    videos = _videos(demo)
    fingerprint = _md5(
        ("rc|" + _md5(data) + "|" + "|".join(f"{n}:{s}" for n, s in videos)).encode()
    )
    text = meta.get("task_description")
    if not text:
        path = demo.parent / "task_description.txt"
        text = (
            path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        ) or demo.parent.name
    rate = meta.get("nominal_freq_hz") or meta.get("collection_freq_hz")
    return {
        "frames": int(meta.get("frame_count") or frames),
        "fps": float(rate) if isinstance(rate, (int, float)) else None,
        "cameras": json.dumps([Path(n).stem for n, _ in videos]),
        "task_raw": str(text),
        "data_source": meta.get("data_source"),
        "control_mode": meta.get("control_mode"),
        "policy": _policy(meta),
        "date": str(meta.get("created_at") or "")[:10] or None,
        "robot_flag": raw.demo_outcome(demo),
        "fingerprint": fingerprint,
        "video_bytes": int(sum(s for _, s in videos)),
        "video_sha256": "{}",
    }


def _rc_source(ctx: Context, demo: Path, task_dirs: set[Path]):
    """(source folder, nonstandard reason) of a raw episode."""
    for folder in [demo.parent, *demo.parent.parents]:
        if str(folder) in ctx.registered:
            return folder, None
        if folder in ctx.roots:
            break
    task = demo.parent
    if task in ctx.roots:
        return task, None
    if task.parent in task_dirs:
        return task.parent.parent, "nested_task_folder"
    if task.parent in ctx.roots or str(task.parent) in ctx.workspaces:
        return task, None
    return task.parent, None


# ------------------------------------------------------------------ rows


def _rc_rows(ctx, demos, previous, progress) -> list[dict]:
    task_dirs = {d.parent for d in demos}
    if progress:
        progress.stage("Raw episodes", len(demos))

    def one(demo: Path) -> dict:
        key = str(demo)
        sig = _sig(demo, [demo.parent / "task_description.txt"])
        old = previous.get(key)
        if old and old.get("stat_sig") == sig and old.get("fingerprint_kind") == "raw":
            facts = {k: old.get(k) for k in RC_FACTS}
        else:
            facts = rc_facts(demo)
        source, nested = _rc_source(ctx, demo, task_dirs)
        reason = nested or (
            None
            if rules_mod.standard_demo(demo.name, ctx.rules)
            else "nonstandard_folder_name"
        )
        category, why = _category(ctx, demo, "robot_capture", facts, False)
        return {
            **facts,
            "key": key,
            "source": ctx.source_id(source),
            "source_path": str(source),
            "root": str(ctx.root_of(demo)),
            "format": "robot_capture",
            "episode": demo.relative_to(source).as_posix(),
            "episode_index": -1,
            "task": rules_mod.normalize_task(facts["task_raw"]),
            "state_dim": None,
            "action_dim": None,
            "category": category,
            "category_reason": why,
            "nonstandard": reason is not None,
            "nonstandard_reason": reason,
            "exportable": reason is None,
            "not_exportable_reason": "nonstandard" if reason else None,
            "fingerprint_kind": "raw",
            "derived_from": None,
            "in_levi_workspace": ctx.in_workspace(demo),
            "stat_sig": sig,
        }

    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for row in pool.map(one, demos):
            rows.append(row)
            if progress:
                progress.advance(row["episode"])
    return rows


def _content_hash(path: Path) -> str:
    try:
        table = pq.read_table(path)
        names = [c for c in ("observation.state", "action") if c in table.column_names]
        if names:
            digest = hashlib.md5(usedforsecurity=False)
            for name in names:
                values = np.asarray(table[name].to_pylist(), dtype=np.float32)
                digest.update(name.encode())
                digest.update(values.tobytes())
            return digest.hexdigest()
    except Exception:  # noqa: BLE001 -- fall back to the file bytes
        pass
    return _md5(path.read_bytes())


def _lerobot_rows(
    ctx, dataset: Path, rc_keys: dict, previous, aliases: dict
) -> tuple[list, dict]:
    """Rows of one LeRobot dataset; ``aliases`` collects raw episodes found
    to be derived from another raw episode (key -> original key)."""
    info = _read_json(dataset / "meta/info.json") or {}
    version = str(info.get("codebase_version") or "?")
    features = info.get("features") or {}
    markers = [m for m in ctx.rules["levi_markers"] if (dataset / m).is_file()]
    base = {
        "path": str(dataset),
        "format": "lerobot",
        "version": version,
        "levi_markers": markers,
    }
    if not version.startswith("v2"):
        return [], {
            **base,
            "episodes": info.get("total_episodes"),
            "exportable": False,
            "reason": f"LeRobot {version}: pool exports read v2.x datasets",
        }
    sig = _sig(dataset / "meta", [dataset / m for m in markers])
    rows_meta = _read_jsonl(dataset / "meta/episodes.jsonl")
    tasks = {
        r.get("task_index"): r.get("task")
        for r in _read_jsonl(dataset / "meta/tasks.jsonl")
    }
    processed = _read_json(dataset / "meta/processed_demos.json")
    if not (isinstance(processed, list) and len(processed) == len(rows_meta)):
        processed = None
    conversion = _read_json(dataset / "meta/levi_conversion.json") or {}
    conv_source = conversion.get("source") or conversion.get("derived_from")
    cameras = [k for k, f in features.items() if f.get("dtype") == "video"]
    shape = lambda k: (features.get(k, {}).get("shape") or [None])[0]  # noqa: E731
    chunk = int(info.get("chunks_size") or 1000)
    data_path = info.get("data_path") or ""

    def one(item):
        position, row = item
        ep = int(row.get("episode_index", position))
        key = f"{dataset}#{ep}"
        old = previous.get(key)
        task_raw = (row.get("tasks") or [None])[0] or tasks.get(row.get("task_index"))
        link = None
        candidates = [row.get("rollout_source_demo")]
        if processed:
            candidates.append(processed[position])
        if conv_source and row.get("source_demo"):
            candidates.append(str(Path(conv_source) / row["source_demo"]))
        linked = [
            str(Path(c)) for c in candidates if c and str(Path(c)) in rc_keys
        ]
        # Several indexed raw folders may stand behind one converted episode
        # (the original rollout and a filtered copy of it): the first is the
        # original, the others are derived from it.
        link = linked[0] if linked else None
        for other in dict.fromkeys(linked[1:]):
            if rc_keys[other]["fingerprint"] != rc_keys[link]["fingerprint"]:
                aliases[other] = link
        if link:
            fingerprint, kind = rc_keys[link]["fingerprint"], "derived"
        elif old and old.get("stat_sig") == sig and old.get("fingerprint_kind") == "content":
            fingerprint, kind = old["fingerprint"], "content"
        else:
            part = dataset / data_path.format(
                episode_chunk=ep // chunk, episode_index=ep
            )
            fingerprint, kind = (
                (_content_hash(part), "content") if part.is_file() else (None, None)
            )
        flag = row.get("levi_outcome")
        if flag not in ("success", "failure"):
            flag = None
        data_source = row.get("data_source")
        if flag is None and isinstance(row.get("is_success"), bool):
            if data_source in ctx.rules["rollout_data_sources"]:
                flag = "success" if row["is_success"] else "failure"
        facts = {"data_source": data_source}
        category, why = _category(ctx, dataset, "lerobot", facts, bool(markers))
        if category is None and "levi_outcome" in row:
            category, why = "rollout", "levi_outcome"
        if category is None and link:
            category, why = rc_keys[link]["category"], "converted_from_raw"
        if category is None:
            category = ctx.rules["lerobot_default_category"]
            why = "default: LeRobot dataset without provenance"
        return {
            "key": key,
            "source": ctx.source_id(dataset),
            "source_path": str(dataset),
            "root": str(ctx.root_of(dataset)),
            "format": "lerobot",
            "episode": str(ep),
            "episode_index": ep,
            "task": rules_mod.normalize_task(task_raw),
            "task_raw": task_raw,
            "frames": int(row.get("length") or 0),
            "fps": float(info["fps"]) if info.get("fps") else None,
            "cameras": json.dumps(cameras),
            "state_dim": shape("observation.state"),
            "action_dim": shape("action"),
            "category": category,
            "category_reason": why,
            "data_source": data_source,
            "control_mode": None,
            "policy": row.get("rollout_policy") or None,
            "date": None,
            "robot_flag": flag,
            "nonstandard": False,
            "nonstandard_reason": None,
            "exportable": True,
            "not_exportable_reason": None,
            "fingerprint": fingerprint,
            "fingerprint_kind": kind,
            "derived_from": link,
            "in_levi_workspace": ctx.in_workspace(dataset),
            "video_bytes": None,
            "video_sha256": "{}",
            "stat_sig": sig,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, enumerate(rows_meta)))
    return rows, {**base, "episodes": len(rows), "exportable": True, "reason": None}


def _droid_rows(ctx, demos, previous) -> list[dict]:
    rows = []
    for demo in demos:
        key = str(demo)
        meta_path = next(demo.glob("metadata_*.json"))
        meta = _read_json(meta_path) or {}
        try:
            h5 = (demo / "trajectory.h5").stat().st_size
        except OSError:
            h5 = 0
        success = meta.get("success")
        category, why = _category(ctx, demo, "droid_raw", {}, False)
        task = (meta.get("current_task") or "").strip() or "Unspecified task"
        rows.append(
            {
                "key": key,
                "source": ctx.source_id(demo.parent),
                "source_path": str(demo.parent),
                "root": str(ctx.root_of(demo)),
                "format": "droid_raw",
                "episode": demo.name,
                "episode_index": -1,
                "task": rules_mod.normalize_task(task),
                "task_raw": task,
                "frames": int(meta.get("trajectory_length") or 0),
                "fps": 15.0,
                "cameras": json.dumps(["wrist", "exterior_1", "exterior_2"]),
                "state_dim": None,
                "action_dim": None,
                "category": category,
                "category_reason": why,
                "data_source": None,
                "control_mode": None,
                "policy": None,
                "date": None,
                "robot_flag": (
                    ("success" if success else "failure")
                    if isinstance(success, bool)
                    else None
                ),
                "nonstandard": False,
                "nonstandard_reason": None,
                "exportable": False,
                "not_exportable_reason": "DROID raw has no training conversion "
                "(it needs an action mapping)",
                "fingerprint": _md5(
                    b"droid|" + meta_path.read_bytes() + str(h5).encode()
                ),
                "fingerprint_kind": "droid",
                "derived_from": None,
                "in_levi_workspace": ctx.in_workspace(demo),
                "video_bytes": None,
                "video_sha256": "{}",
                "stat_sig": _sig(demo),
            }
        )
    return rows


# ------------------------------------------------------------------ joins


def _labels(rows: list[dict], workspaces: list[Path]) -> dict:
    found: dict[str, dict] = {}
    for workspace in workspaces:
        for key, value in labels.workspace_labels(workspace).items():
            found.setdefault(key, value)
    groups: dict[str, set] = defaultdict(set)
    direct = {}
    for row in rows:
        label = found.get(row["key"])
        if label:
            direct[row["key"]] = label
            if row["fingerprint"]:
                groups[row["fingerprint"]].add(label["outcome"])
    conflicts = 0
    for row in rows:
        label = direct.get(row["key"])
        group = groups.get(row["fingerprint"]) if row["fingerprint"] else None
        row["label_conflict"] = bool(group and len(group) > 1)
        if row["label_conflict"]:
            conflicts += 1
            row["human_label"] = None
            row["human_label_from"] = None
        elif label:
            row["human_label"] = label["outcome"]
            row["human_label_from"] = f"{label['workspace']}:{label['dataset']}"
        elif group:
            row["human_label"] = next(iter(group))
            row["human_label_from"] = "copy"
        else:
            row["human_label"] = None
            row["human_label_from"] = None
        if row["human_label"]:
            row["outcome"], row["outcome_source"] = row["human_label"], "human"
        elif row["robot_flag"]:
            row["outcome"], row["outcome_source"] = row["robot_flag"], "robot_flag"
        else:
            row["outcome"], row["outcome_source"] = None, None
    return {
        "labelled_episodes": len(direct),
        "workspaces": [str(w) for w in workspaces],
        "conflicts": conflicts,
    }


def _video_sha(row: dict, names) -> dict:
    cached = json.loads(row.get("video_sha256") or "{}")
    changed = False
    for name in names:
        if name not in cached:
            path = Path(row["key"]) / name
            if path.is_file():
                cached[name] = heldout.sha256(path)
                changed = True
    if changed:
        row["video_sha256"] = json.dumps(cached, sort_keys=True)
    return cached


def _heldout(rows: list[dict], roots: list[Path], progress) -> dict:
    for row in rows:
        row["heldout"], row["heldout_set"], row["heldout_id"] = False, None, None
    entries = heldout.load(settings.heldout_files())
    report = {
        "lists": [str(p) for p in settings.heldout_files()],
        "entries": len(entries),
        "matched_by_path": 0,
        "matched_by_sha256": 0,
        "sha256_mismatch": [],
        "unmatched": [],
    }
    if not entries:
        return report
    raw_rows = [r for r in rows if r["format"] == "robot_capture"]
    by_key = {r["key"]: r for r in raw_rows}
    by_frames: dict[int, list] = defaultdict(list)
    for r in raw_rows:
        by_frames[int(r["frames"] or -1)].append(r)
    if progress:
        progress.stage("Held-out", len(entries))
    held: dict[str, tuple] = {}
    originals: list[tuple[dict, dict]] = []
    for entry in entries:
        path = Path(entry["path"])
        candidates = [path] if path.is_absolute() else [r / path for r in roots]
        match = next((by_key[str(c)] for c in candidates if str(c) in by_key), None)
        if match:
            report["matched_by_path"] += 1
            if entry["sha256"]:
                shas = _video_sha(match, entry["sha256"])
                bad = [n for n, v in entry["sha256"].items() if shas.get(n) != v]
                if bad:
                    report["sha256_mismatch"].append(
                        {"id": entry["id"], "path": match["key"], "videos": bad}
                    )
            originals.append((entry, match))
        elif entry["sha256"]:
            name, want = next(iter(entry["sha256"].items()))
            pool = (
                by_frames.get(int(entry["frame_count"]), [])
                if entry["frame_count"]
                else raw_rows
            )
            hits = [r for r in pool if _video_sha(r, [name]).get(name) == want]
            if hits:
                report["matched_by_sha256"] += 1
                originals += [(entry, r) for r in hits]
            else:
                report["unmatched"].append(entry["id"])
        else:
            report["unmatched"].append(entry["id"])
        if progress:
            progress.advance(entry["id"])
    for entry, row in originals:
        held[row["fingerprint"]] = (entry["set"], entry["id"])
    # Copies re-encoded or re-written byte for byte but with another
    # frames.csv: same video name, size and sha256.
    sizes = {}
    for entry, row in originals:
        for name, want in entry["sha256"].items():
            try:
                size = (Path(row["key"]) / name).stat().st_size
            except OSError:
                continue
            sizes[(name, size)] = (want, entry)
    for r in raw_rows:
        if r["fingerprint"] in held:
            continue
        for video in sorted(Path(r["key"]).glob("*.mp4")):
            probe = (video.name, _size(video))
            if probe in sizes:
                want, entry = sizes[probe]
                if _video_sha(r, [video.name]).get(video.name) == want:
                    held[r["fingerprint"]] = (entry["set"], entry["id"])
                    break
    count = 0
    for row in rows:
        mark = held.get(row["fingerprint"]) if row["fingerprint"] else None
        if mark:
            row["heldout"], (row["heldout_set"], row["heldout_id"]) = True, mark
            count += 1
    report["heldout_episodes"] = count
    report["heldout_originals"] = len({r["key"] for _, r in originals})
    report["heldout_copies"] = count - report["heldout_originals"]
    return report


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _rank(row: dict) -> tuple:
    return (
        row["category"] == "archive",
        row["category"] == "levi" or bool(row["in_levi_workspace"]),
        row["format"] != "robot_capture",
        bool(row["nonstandard"]),
        len(Path(row["source_path"]).parts),
        row["key"],
    )


def _dedup(rows: list[dict]) -> dict:
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        if row["fingerprint"]:
            groups[row["fingerprint"]].append(row)
        else:
            row.update(canonical=True, canonical_key=row["key"], copies=0)
    duplicates = 0
    for members in groups.values():
        members.sort(key=_rank)
        head = members[0]["key"]
        for i, row in enumerate(members):
            row.update(canonical=i == 0, canonical_key=head, copies=len(members) - 1)
        duplicates += len(members) - 1
    return {
        "fingerprints": len(groups),
        "duplicate_episodes": duplicates,
        "groups_with_copies": sum(1 for m in groups.values() if len(m) > 1),
    }


def _sources(ctx, rows, extra_sources, unsupported, dataset_paths) -> list[dict]:
    by_source: dict[str, list] = defaultdict(list)
    for row in rows:
        by_source[row["source_path"]].append(row)
    canonical_source = {r["key"]: r["source"] for r in rows}
    out = []
    for path, members in sorted(by_source.items()):
        categories = Counter(r["category"] for r in members)
        copy_of = Counter(
            canonical_source.get(r["canonical_key"])
            for r in members
            if not r["canonical"]
        )
        first = members[0]
        extra = extra_sources.get(path, {})
        out.append(
            {
                "id": first["source"],
                "path": path,
                "root": first["root"],
                "format": first["format"],
                **({"version": extra["version"]} if extra.get("version") else {}),
                "category": categories.most_common(1)[0][0],
                "categories": dict(categories),
                "episodes": len(members),
                "nonstandard": sum(1 for r in members if r["nonstandard"]),
                "frames": int(sum(int(r["frames"] or 0) for r in members)),
                "tasks": len({r["task"] for r in members}),
                "canonical": sum(1 for r in members if r["canonical"]),
                "copies": len(members) - sum(1 for r in members if r["canonical"]),
                "copy_of": {k: v for k, v in copy_of.most_common() if k},
                "heldout": sum(1 for r in members if r["heldout"]),
                "labelled": sum(1 for r in members if r["human_label"]),
                "exportable": any(r["exportable"] for r in members),
                "reason": next(
                    (r["not_exportable_reason"] for r in members if not r["exportable"]),
                    None,
                )
                if not any(r["exportable"] for r in members)
                else None,
                "in_levi_workspace": bool(first["in_levi_workspace"]),
                "levi_markers": extra.get("levi_markers", []),
                "cameras": json.loads(first["cameras"] or "[]"),
                "fps": first["fps"],
            }
        )
    for path, extra in extra_sources.items():
        if path in by_source:
            continue
        out.append(
            {
                "id": ctx.source_id(Path(path)),
                "path": path,
                "root": str(ctx.root_of(Path(path))),
                "format": extra["format"],
                "version": extra.get("version"),
                "category": rules_mod.path_category(
                    ctx.relative(Path(path)), ctx.rules
                ),
                "episodes": extra.get("episodes") or 0,
                "exportable": False,
                "reason": extra.get("reason"),
            }
        )
    # Record-only data folders: the top-most folder holding such files.
    datasets = [Path(p) for p in dataset_paths]
    kept: dict[str, dict] = {}
    for folder in sorted(unsupported):
        path = Path(folder)
        if any(path == d or path.is_relative_to(d) for d in datasets):
            continue
        owner = next((k for k in kept if path.is_relative_to(Path(k))), None)
        value = unsupported[folder]
        if owner:
            kept[owner]["bytes"] += value["bytes"]
            kept[owner]["files"] += value["files"]
            for kind, n in value["kinds"].items():
                kept[owner]["kinds"][kind] = kept[owner]["kinds"].get(kind, 0) + n
        else:
            kept[folder] = {**value, "kinds": dict(value["kinds"])}
    for folder, value in kept.items():
        if value["bytes"] < ctx.rules["unsupported_min_bytes"]:
            continue
        path = Path(folder)
        kinds = value["kinds"]
        out.append(
            {
                "id": ctx.source_id(path),
                "path": folder,
                "root": str(ctx.root_of(path)),
                "format": "+".join(sorted(kinds)),
                "category": rules_mod.path_category(ctx.relative(path), ctx.rules),
                "episodes": None,
                "files": value["files"],
                "bytes": value["bytes"],
                "exportable": False,
                "reason": "format not supported yet: listed, not exported",
            }
        )
    return sorted(out, key=lambda s: s["id"])


# ------------------------------------------------------------------ scan


def load_rows() -> list[dict]:
    path = index_path()
    if not path.is_file():
        return []
    frame = pq.read_table(path).to_pandas()
    frame = frame.astype(object).where(pd.notna(frame), None)
    return frame.to_dict("records")


def scan(progress_path: Path | None = None, rehash: bool = False) -> dict:
    """Walk the pool roots and rewrite the index; returns the scan summary."""
    started = time.time()
    roots = settings.require_enabled()
    pool = settings.pool_dir()
    rules = rules_mod.load(pool)
    progress = Progress(progress_path, STAGES)
    previous = {} if rehash else {r["key"]: r for r in load_rows()}
    progress.stage("Walk")
    found = walk(roots, rules, progress)
    workspaces = sorted(
        set(found["workspaces"])
        | ({settings.workspace()} if labels.is_workspace(settings.workspace()) else set())
    )
    registered = labels.registered_paths(workspaces)
    ctx = Context(roots, rules, workspaces, registered)
    rows = _rc_rows(ctx, found["rc"], previous, progress)
    rc_keys = {r["key"]: r for r in rows}
    progress.stage("Datasets", len(found["lerobot"]) + 1)
    extra_sources = {}
    aliases: dict[str, str] = {}
    for dataset in found["lerobot"]:
        more, source = _lerobot_rows(ctx, dataset, rc_keys, previous, aliases)
        rows += more
        extra_sources[str(dataset)] = source
        progress.advance(ctx.relative(dataset))
    for key, original in aliases.items():
        rc_keys[key].update(
            fingerprint=rc_keys[original]["fingerprint"],
            fingerprint_kind="derived",
            derived_from=original,
        )
    rows += _droid_rows(ctx, found["droid"], previous)
    progress.advance("DROID")
    progress.stage("Labels", 1)
    label_report = _labels(rows, workspaces)
    heldout_report = _heldout(rows, roots, progress)
    dedup_report = _dedup(rows)
    progress.stage("Index", 1)
    dataset_paths = {r["source_path"] for r in rows} | set(extra_sources)
    sources = _sources(ctx, rows, extra_sources, found["unsupported"], dataset_paths)
    frame = pd.DataFrame(rows, columns=COLUMNS)
    pool.mkdir(parents=True, exist_ok=True)
    temp = pool / f".index.{os.getpid()}.parquet"
    frame.to_parquet(temp, index=False)
    os.replace(temp, index_path())
    _atomic_json(sources_path(), {"schema": SCHEMA, "sources": sources})
    summary = {
        "schema": SCHEMA,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seconds": round(time.time() - started, 1),
        "roots": [str(r) for r in roots],
        "episodes": len(rows),
        "reused": sum(
            1 for r in rows if previous.get(r["key"], {}).get("stat_sig") == r["stat_sig"]
        ),
        "sources": len(sources),
        "formats": dict(Counter(r["format"] for r in rows)),
        "categories": dict(Counter(r["category"] for r in rows)),
        "canonical_categories": dict(
            Counter(r["category"] for r in rows if r["canonical"])
        ),
        "nonstandard": sum(1 for r in rows if r["nonstandard"]),
        "tasks": len({r["task"] for r in rows}),
        "dedup": dedup_report,
        "heldout": heldout_report,
        "labels": label_report,
        "unsupported_sources": sum(
            1 for s in sources if not s["exportable"] and s["format"] not in (
                "robot_capture",
                "lerobot",
                "droid_raw",
            )
        ),
    }
    _atomic_json(scan_path(), summary)
    progress.finish()
    return summary
