"""RECAP value dataset (π*0.6 / RLinf RECAP / LeRobot RECAP proposal).

There is no official PI specification (openpi has not released RECAP). The
export is a LeRobot v2.1 dataset that satisfies the three concrete consumers
at once — see docs/RECAP.md:

- RLinf RECAP (examples/offline_rl/advantage_labeling/recap, pinned below):
  per-frame ``is_success`` read at each episode's last frame, the
  ``meta/returns.parquet`` sidecar (``returns_{tag}.parquet`` when tagged)
  with ``episode_index, frame_index, return, reward, prompt``, and flat
  ``return``/``reward`` entries in ``meta/stats.json`` (its value normalizer
  reads their min/max). Running RLinf's own compute_returns.py on this export
  reproduces the sidecar exactly.
- LeRobot RECAP proposal (huggingface/lerobot#3245): ``meta/episode_labels.csv``.
- The paper: r_t = -1 per step, terminal 0 on success / C_fail on failure,
  per-task maximum episode length recorded for the (-1, 0) normalization.

One row per executed policy step is assumed by the per-step reward, so this
target defaults to ``timing=retime`` without static-frame filtering.
"""

import csv
import json
import os
import shutil
from pathlib import Path
from typing import ClassVar, Literal

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field

from ...catalog import atomic
from .. import dataset, media
from ..options import Options
from ..report import InputReport, Solution, TargetCompatibility
from .base import TargetOptions
from .lerobot_v21 import LeRobotV21

RLINF_COMMIT = "db66ac56d1aa4a9c8441c4026e4212b21811970d"
RECAP_COLUMNS = ("is_success", "next.reward", "next.done")


class RecapOptions(TargetOptions):
    # "rollout": outcomes come from labels; "sft": demonstrations, all
    # treated as successes (RLinf's dataset types).
    dataset_type: Literal["rollout", "sft"] = "rollout"
    failure_reward: float = Field(-300.0, le=0, ge=-1e6)
    gamma: float = Field(1.0, gt=0, le=1)
    tag: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


def episode_rewards(n: int, success: bool, gamma: float, failure_reward: float):
    """RLinf ``compute_returns_for_episode``: -1 per step, terminal 0 on
    success or ``failure_reward`` on failure; G_t = r_t + gamma * G_{t+1}."""
    rewards = np.full(n, -1.0, dtype=np.float32)
    rewards[-1] = 0.0 if success else failure_reward
    returns = np.zeros(n, dtype=np.float32)
    returns[-1] = rewards[-1]
    for t in range(n - 2, -1, -1):
        returns[t] = rewards[t] + gamma * returns[t + 1]
    return returns, rewards


def _success(row: dict, settings: RecapOptions) -> bool:
    if settings.dataset_type == "sft":
        return True
    outcome = row.get("levi_outcome")
    if outcome not in ("success", "failure"):
        raise ValueError(
            f"Episode {row['episode_index']} has no success/failure label; "
            "label it, exclude it, or export as sft demonstrations"
        )
    return outcome == "success"


class RecapValue(LeRobotV21):
    id = "recap_value"
    label = "RECAP value dataset (π*0.6)"
    description = (
        "LeRobot v2.1 plus per-step rewards, terminal success flags and the "
        "returns sidecar used to train a RECAP value function (RLinf format)."
    )
    evidence = "fixture"
    options_model = RecapOptions
    defaults: ClassVar[dict] = {"timing": "retime", "filter_static": False}
    inputs = ("robot_capture", "image_sequence", "lerobot")
    ignored_warnings = ()

    def check(self, report: InputReport, options: Options) -> TargetCompatibility:
        settings = self.target_options(options.target_options)
        if report.format == "lerobot":
            failed = report.failed
            dropped = [r for r in report.warned if r.id == "frame_selection"]
            result = TargetCompatibility(
                target=self.id,
                label=self.label,
                status="unsupported" if failed else "supported",
                reasons=[f"{r.label}: {r.detail}" for r in failed + dropped],
                defaults={},
            )
        else:
            result = super().check(report, options)
            result.defaults = self.defaults
        if result.status == "unsupported" and report.format not in self.inputs:
            return result
        outcomes = report.summary.get("outcomes", {})
        unlabeled = [e.source_id for e in report.episodes if e.outcome is None]
        if settings.dataset_type == "rollout" and unlabeled:
            result.status = "unsupported"
            result.reasons.insert(
                0,
                f"{len(unlabeled)} of {len(report.episodes)} episodes have no "
                "success/failure label; RECAP rewards need one per episode",
            )
            if len(unlabeled) < len(report.episodes):
                result.solutions.append(
                    Solution(
                        id="exclude_unlabeled",
                        label=f"Exclude the {len(unlabeled)} unlabeled episode(s)",
                        options={
                            "exclude_demos": sorted(
                                set(options.exclude_demos) | set(unlabeled)
                            )
                        },
                    )
                )
            result.solutions.append(
                Solution(
                    id="label_outcomes",
                    label="Label episode outcomes in LEVI",
                    link="label",
                )
            )
            result.solutions.append(
                Solution(
                    id="export_sft",
                    label="Export as demonstrations (every episode a success)",
                    options={
                        "target_options": {
                            **settings.model_dump(),
                            "dataset_type": "sft",
                        }
                    },
                )
            )
            return result
        warnings = []
        if settings.dataset_type == "rollout" and (
            outcomes.get("success", 0) == 0 or outcomes.get("failure", 0) == 0
        ):
            warnings.append(
                "All labelled episodes share one outcome; the value function "
                "cannot learn to separate success from failure"
            )
        if len(report.summary.get("tasks", {})) <= 1:
            warnings.append(
                "Single task: per-task return normalization is trivial (fine for a "
                "first reproduction; mix tasks for the multi-task setting)"
            )
        warnings.append(
            "No human-intervention data: RECAP's forced-improvement indicator "
            "for corrections cannot be set"
        )
        if report.format != "lerobot" and (
            options.timing != "retime" or options.filter_static
        ):
            warnings.append(
                "Frames will be dropped (resampling or static filtering): rewards "
                "will no longer be one per executed step. Use timing=retime without filtering."
            )
        if result.status == "supported":
            result.status = "warnings"
        result.reasons.extend(warnings)
        return result

    def episode_columns(self, n, episode, settings):
        success = _success(episode, settings)
        _returns, rewards = episode_rewards(
            n, success, settings.gamma, settings.failure_reward
        )
        is_success = np.zeros(n, dtype=bool)
        is_success[-1] = success
        done = np.zeros(n, dtype=bool)
        done[-1] = True
        scalar = lambda dtype: {"dtype": dtype, "shape": [1], "names": None}
        return {
            "is_success": (is_success, scalar("bool")),
            "next.reward": (rewards, scalar("float32")),
            "next.done": (done, scalar("bool")),
        }

    def finalize(
        self, root: Path, episodes: list[dict], info: dict, settings: RecapOptions
    ):
        ep_col, frame_col, ret_col, rew_col, prompts = [], [], [], [], []
        max_length: dict[str, int] = {}
        for row in episodes:
            n = row["length"]
            task = row["tasks"][0]
            returns, rewards = episode_rewards(
                n, _success(row, settings), settings.gamma, settings.failure_reward
            )
            ep_col.append(np.full(n, row["episode_index"], dtype=np.int64))
            frame_col.append(np.arange(n, dtype=np.int64))
            ret_col.append(returns)
            rew_col.append(rewards)
            prompts += [task] * n
            max_length[task] = max(max_length.get(task, 0), n)
        returns = np.concatenate(ret_col)
        rewards = np.concatenate(rew_col)
        sidecar = (
            f"returns_{settings.tag}.parquet" if settings.tag else "returns.parquet"
        )
        pq.write_table(
            pa.table(
                {
                    "episode_index": pa.array(np.concatenate(ep_col)),
                    "frame_index": pa.array(np.concatenate(frame_col)),
                    "return": pa.array(returns),
                    "reward": pa.array(rewards),
                    "prompt": pa.array(prompts, type=pa.string()),
                }
            ),
            root / "meta" / sidecar,
        )
        stats = json.loads((root / "meta/stats.json").read_text())

        def flat(a):
            a64 = a.astype(np.float64)
            return {
                "mean": float(a64.mean()),
                "std": float(a64.std()),
                "min": float(a.min()),
                "max": float(a.max()),
            }

        # RLinf's format (flat floats); its value normalizer reads min/max.
        stats["return"] = flat(returns)
        stats["reward"] = flat(rewards)
        atomic(root / "meta/stats.json", stats)
        with (root / "meta/episode_labels.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["episode_index", "success"])
            for row in episodes:
                writer.writerow([row["episode_index"], int(_success(row, settings))])
        counts = {"success": 0, "failure": 0}
        for row in episodes:
            counts["success" if _success(row, settings) else "failure"] += 1
        atomic(
            root / "meta/levi_recap.json",
            {
                "schema": "levi.recap.v1",
                "dataset_type": settings.dataset_type,
                "gamma": settings.gamma,
                "failure_reward": settings.failure_reward,
                "tag": settings.tag,
                "reward": "-1 per step; last step 0 on success, failure_reward on failure",
                "return": "G_t = r_t + gamma * G_{t+1} (backward from the last step)",
                "columns": {
                    "is_success": "True only on the last frame of a successful episode",
                    "next.reward": "per-step reward above",
                    "next.done": "True on each episode's last frame",
                },
                "returns_sidecar": f"meta/{sidecar}",
                "episode_labels": "meta/episode_labels.csv",
                "outcomes": counts,
                "label_sources": {
                    src: sum(
                        1
                        for r in episodes
                        if r.get("levi_outcome_source", "metadata") == src
                    )
                    for src in {
                        r.get("levi_outcome_source", "metadata") for r in episodes
                    }
                },
                "task_max_episode_length": max_length,
                "normalization": (
                    "π*0.6: per task, value = return / (max_episode_length + |failure_reward|) "
                    "lies in (-1, 0); RLinf instead min-max normalizes with "
                    "meta/stats.json return.min/max"
                ),
                "suggested_value_bins": 201,
                "fps": info["fps"],
                "consumers": {
                    "rlinf": {
                        "repo": "https://github.com/RLinf/RLinf",
                        "commit": RLINF_COMMIT,
                        "compute_returns": "examples/offline_rl/advantage_labeling/recap/process/compute_returns.py",
                        "suggested_repack_keys": _repack(info),
                    },
                    "lerobot_pr_3245": "https://github.com/huggingface/lerobot/pull/3245",
                    "paper": "https://arxiv.org/abs/2511.14759",
                },
            },
        )

    # --- from an existing LeRobot v2.x dataset ------------------------------

    def from_dataset(self, source: Path, target: Path, options: Options, progress=None):
        """Same frames and videos, new RECAP columns: parquet rewritten, videos
        hard-linked (copied across filesystems), metadata extended."""
        settings = self.target_options(options.target_options)
        staging = target.parent / f".{target.name}.partial"
        if target.exists() or staging.exists():
            raise ValueError(f"Output directory already exists: {target}")
        info = json.loads((source / "meta/info.json").read_text())
        rows = dataset.read_jsonl(source / "meta/episodes.jsonl")
        stats_rows = {
            r["episode_index"]: r
            for r in (
                dataset.read_jsonl(source / "meta/episodes_stats.jsonl")
                if (source / "meta/episodes_stats.jsonl").exists()
                else []
            )
        }
        known = {str(r["episode_index"]) for r in rows}
        unknown = sorted(set(options.exclude_demos) - known)
        if unknown:
            raise ValueError(f"Unknown excluded episodes: {unknown}")
        excluded = {int(x) for x in options.exclude_demos}
        kept = [r for r in rows if r["episode_index"] not in excluded]
        if not kept:
            raise ValueError("No episodes selected")
        labels = {int(k): v for k, v in options.outcome_labels.items()}
        staging.mkdir(parents=True)
        try:
            (staging / "meta").mkdir()
            if progress:
                progress.stage("Convert episodes", len(kept))
            features = {
                k: v for k, v in info["features"].items() if k not in RECAP_COLUMNS
            }
            chunk = info["chunks_size"]
            new_rows, epstats, measured, provenance_map = [], [], {}, {}
            offset = 0
            for new, row in enumerate(kept):
                old = row["episode_index"]
                data = pq.read_table(
                    source
                    / info["data_path"].format(
                        episode_chunk=old // chunk, episode_index=old
                    )
                )
                data = data.drop_columns(
                    [c for c in RECAP_COLUMNS if c in data.column_names]
                )
                n = data.num_rows
                row = {**row, "episode_index": new}
                if old in labels:
                    row["levi_outcome"] = labels[old]
                    row["levi_outcome_source"] = "human"
                row.update(self.episode_fields(row, settings))
                extra = self.episode_columns(n, row, settings)
                for name, values in (
                    ("episode_index", np.full(n, new, dtype=np.int64)),
                    ("index", np.arange(offset, offset + n, dtype=np.int64)),
                ):
                    data = data.set_column(
                        data.schema.get_field_index(name), name, pa.array(values)
                    )
                for key, (values, feature) in extra.items():
                    data = data.append_column(key, pa.array(values))
                    features[key] = feature
                path = staging / info["data_path"].format(
                    episode_chunk=new // chunk, episode_index=new
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(data, path, compression="snappy")
                stat = dict(stats_rows.get(old, {}).get("stats", {}))
                for key in RECAP_COLUMNS:
                    stat.pop(key, None)
                for key, (values, _f) in extra.items():
                    stat[key] = dataset.stats(
                        np.asarray(values, dtype=np.float64).reshape(n, -1)
                    )
                stat["episode_index"] = dataset.stats(np.full((n, 1), new))
                stat["index"] = dataset.stats(
                    np.arange(offset, offset + n).reshape(-1, 1)
                )
                epstats.append({"episode_index": new, "stats": stat})
                for key, feature in info["features"].items():
                    if feature.get("dtype") != "video":
                        continue
                    rel = info["video_path"].format(
                        episode_chunk=old // chunk, episode_index=old, video_key=key
                    )
                    out_rel = info["video_path"].format(
                        episode_chunk=new // chunk, episode_index=new, video_key=key
                    )
                    dst = staging / out_rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(source / rel, dst)
                    except OSError:
                        shutil.copy2(source / rel, dst)
                    # Byte-identical files of an already-validated dataset:
                    # container metadata suffices, no re-decode.
                    probe = media.probe(dst)
                    measured[out_rel] = {**probe, "frames": probe["declared_frames"]}
                new_rows.append(row)
                provenance_map[old] = new
                offset += n
                if progress:
                    progress.advance(f"episode {old}")
            info = {
                **info,
                "features": features,
                "total_episodes": len(new_rows),
                "total_frames": offset,
                "total_videos": len(new_rows)
                * sum(1 for f in features.values() if f.get("dtype") == "video"),
                "total_chunks": (len(new_rows) + chunk - 1) // chunk,
                "splits": {"train": f"0:{len(new_rows)}"},
            }
            atomic(staging / "meta/info.json", info)
            dataset.jsonl(staging / "meta/episodes.jsonl", new_rows)
            dataset.jsonl(staging / "meta/episodes_stats.jsonl", epstats)
            used = {t for r in new_rows for t in r.get("tasks", [])}
            tasks = [t for t in dataset.read_jsonl(source / "meta/tasks.jsonl")]
            if not used <= {t["task"] for t in tasks}:
                raise ValueError("Episode tasks missing from tasks.jsonl")
            shutil.copy2(source / "meta/tasks.jsonl", staging / "meta/tasks.jsonl")
            aggregated = (
                dataset.aggregate(epstats)
                if epstats and all(e["stats"] for e in epstats)
                else {}
            )
            atomic(staging / "meta/stats.json", aggregated)
            provenance = source / "meta/levi_provenance.jsonl"
            if provenance.exists():
                ledger = []
                for item in dataset.read_jsonl(provenance):
                    if item["episode_index"] in provenance_map:
                        ledger.append(
                            {
                                **item,
                                "episode_index": provenance_map[item["episode_index"]],
                            }
                        )
                dataset.jsonl(staging / "meta/levi_provenance.jsonl", ledger)
            conversion = {}
            if (source / "meta/levi_conversion.json").exists():
                conversion = json.loads(
                    (source / "meta/levi_conversion.json").read_text()
                )
            atomic(
                staging / "meta/levi_conversion.json",
                {
                    **conversion,
                    "target": self.id,
                    "derived_from": str(source),
                    "excluded_episodes": sorted(excluded),
                    "target_options": settings.model_dump(),
                },
            )
            self.finalize(staging, new_rows, info, settings)
            if progress:
                progress.stage("Validate", 1)
            validation = dataset.validate(staging, measured=measured)
            atomic(staging / "meta/levi_validation.json", validation)
            if not validation["ok"]:
                raise ValueError(
                    "RECAP export failed validation: "
                    + "; ".join(validation["failures"][:10])
                )
            if progress:
                progress.stage("Publish", 1)
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if progress:
            progress.finish()
        return {
            "ok": True,
            "dataset_path": str(target),
            "validation": validation,
            "target": self.id,
            "fps": info["fps"],
        }


def _repack(info: dict) -> dict:
    cameras = [k for k, f in info["features"].items() if f.get("dtype") == "video"]
    wrist = next((c for c in cameras if "hand" in c or "wrist" in c), None)
    base = next((c for c in cameras if c != wrist), cameras[0] if cameras else None)
    keys = {
        "observation/image": base,
        "observation/state": "observation.state",
        "actions": "action",
        "prompt": "prompt",
    }
    if wrist:
        keys["observation/wrist_image"] = wrist
    return keys
