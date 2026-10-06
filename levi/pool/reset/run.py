"""The reset part of an export run: decide, then write.

``analyze`` looks at every forward episode the export holds and returns one
``Plan`` each: reversible (whole, whole with its falls cut out, or from its last
safe hold on), completed by a recorded stretch, or left out with a reason.
``write`` turns the plans into episodes. Nothing here touches the sources.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...conversion import media
from . import analysis as analysis_mod
from . import bridge as bridge_mod
from . import build
from . import contract as contract_mod
from .schema import ResetOptions


@dataclass
class Located:
    """A planned episode found in its LeRobot form."""

    source: build.Source
    converted: bool
    source_fps: float | None
    stats: dict
    width: np.ndarray | None = None
    already_reset: bool = False


@dataclass
class Plan:
    ep: dict
    located: Located
    text: str  # the reset instruction
    analysis: analysis_mod.Analysis
    generation: str = "reversed_source"
    keep: list[int] = field(default_factory=list)
    record: Located | None = None
    record_rows: int | None = None
    join: dict | None = None
    excluded: dict | None = None  # the export's exclusion row when not exported

    @property
    def ok(self) -> bool:
        return self.excluded is None


def forward_ok(ep: dict) -> tuple[bool, str | None]:
    """A reset undoes a task that was done: a failed forward episode is not
    reversed, and a policy rollout nobody labelled is not either (a
    demonstration, a human recording without an outcome, counts)."""
    outcome = ep.get("outcome")
    if outcome == "success":
        return True, None
    if outcome == "failure":
        return False, "reset_forward_failed"
    if ep.get("category") == "human":
        return True, None
    return False, "reset_forward_unlabeled"


def raw_width(demo: Path, positions) -> np.ndarray | None:
    """The measured finger width of a raw capture at the rows the conversion
    kept (None when the capture has none)."""
    try:
        import pandas as pd

        grip = pd.read_csv(Path(demo) / "gripper_state.csv")
        values = pd.to_numeric(grip["gripper_width"], errors="raise").to_numpy(float)
        rows = np.asarray(positions, dtype=int)
        return values[rows] if len(rows) and rows.max() < len(values) else None
    except (OSError, ValueError, KeyError):
        return None


def _exclusion(ep: dict, reason: str, detail: str | None) -> dict:
    return {
        "key": ep["key"],
        "source": ep["source"],
        "task": ep["task"],
        "reason": reason,
        "detail": [detail] if detail else [],
    }


def _camera_source_key(source: build.Source, output_key: str) -> str | None:
    for src_key, out_key in source.mapping.items():
        if out_key == output_key:
            return src_key
    return None


def analyze(
    kept: list[dict],
    locate,
    records: dict[str, dict],
    opts: ResetOptions,
    texts: dict[str, str],
    *,
    reviewer=None,
    progress=None,
) -> tuple[list[Plan], list[dict]]:
    """(plans, capture requests): one plan per forward episode."""
    contract = contract_mod.resolve(opts.action_contract)
    links = {b.source: b.record for b in opts.bridges}
    plans: list[Plan] = []
    requests: list[dict] = []
    fps = None
    for ep in kept:
        located = locate(ep)
        src = located.source.load()
        fps = fps or (src.info.get("fps"))
        text = opts.reset_text(texts.get(ep["task"], ep["task"]))
        ok, why = (True, None)
        if opts.require_forward_success:
            ok, why = forward_ok(ep)
        if not ok:
            plans.append(
                Plan(
                    ep,
                    located,
                    text,
                    analysis_mod.Analysis(ep["key"]),
                    excluded=_exclusion(ep, why, f"outcome {ep.get('outcome')!r}"),
                )
            )
            continue
        release_key = _camera_source_key(src, opts.release_camera)
        ev = analysis_mod.Evidence(
            key=ep["key"],
            state=src.state,
            action=src.action,
            width=located.width,
            release_video=src.video_path(release_key) if release_key else None,
            already_reset=located.already_reset,
        )
        found = analysis_mod.analyze(ev, contract, opts, reviewer=reviewer)
        plan = Plan(
            ep, located, text, found, generation=found.generation, keep=found.keep
        )
        if found.eligible:
            plans.append(plan)
        else:
            plan.excluded = _exclusion(ep, found.reason, found.detail)
            link = links.get(ep["key"])
            if link and found.bridgeable:
                _join(plan, ev, records, link, opts, contract)
            elif found.bridgeable:
                requests.append(
                    bridge_mod.capture_request(
                        ep["key"],
                        texts.get(ep["task"], ep["task"]),
                        text,
                        src.state[-1],
                        src.state[found.bridge_anchor],
                        found.bridge_anchor,
                        found.detail or found.reason,
                        None
                        if ev.width is None
                        else float(ev.width[max(0, found.bridge_anchor)]),
                        list(src.mapping.values()),
                        float(src.info.get("fps") or 0),
                    )
                )
            plans.append(plan)
        if progress:
            progress.advance(ep["key"])
    return plans, requests


def _join(plan: Plan, ev, records, link, opts, contract) -> None:
    ep, found = plan.ep, plan.analysis
    located = records.get(link)
    if located is None:
        plan.excluded = _exclusion(
            ep, "reset_bridge_missing", f"recorded episode {link} was not exported"
        )
        return
    rec = located.source.load()
    try:
        contract_mod.check(contract, rec.state, rec.action)
    except contract_mod.ContractProblem as exc:
        plan.excluded = _exclusion(ep, "reset_bridge_contract", str(exc))
        return
    src = plan.located.source
    join = bridge_mod.find_join(
        rec.state,
        located.width,
        src.state[-1],
        src.state[found.bridge_anchor],
        contract.gripper_index,
        contract.gripper_open_value,
    )
    detail = join.record()
    if join.ok:
        release_key = _camera_source_key(rec, opts.release_camera)
        if ev.release_video is None or release_key is None:
            join = bridge_mod.Join(
                False, join.row, "bridge_visual_unchecked", join.metrics
            )
        else:
            try:
                a = media.frames_at(ev.release_video, [found.bridge_anchor])
                b = media.frames_at(rec.video_path(release_key), [join.row])
                same, m = bridge_mod.visual_join(a[found.bridge_anchor], b[join.row])
            except (KeyError, ValueError, OSError):
                same, m = False, {}
            detail["visual"] = m
            if not same:
                join = bridge_mod.Join(
                    False, join.row, "bridge_visual_mismatch", join.metrics
                )
    plan.join = {**detail, "record": link, "ok": join.ok, "reason": join.reason}
    if not join.ok:
        plan.excluded = _exclusion(
            ep,
            "reset_" + str(join.reason).removeprefix("reset_"),
            json.dumps(detail, default=str)[:300],
        )
        return
    plan.excluded = None
    plan.generation = "recorded_bridge"
    plan.keep = found.anchor_keep
    plan.record = located
    plan.record_rows = join.row + 1


def exported(plans: list[Plan]) -> list[Plan]:
    return [p for p in plans if p.ok]


def write(
    ctx,
    plans: list[Plan],
    opts: ResetOptions,
    *,
    new0: int,
    offset0: int,
    task_index: dict[str, int],
    fps: float,
    staging: Path,
    data_path: str,
    video_path: str,
) -> list[dict]:
    """Write the exported plans as episodes ``new0, new0 + 1, …``. One dict
    per episode: the metadata row, its statistics, provenance and the pieces
    of the export record."""
    contract = contract_mod.resolve(opts.action_contract)
    out = []
    new, offset = new0, offset0
    for plan in exported(plans):
        src = plan.located.source
        built = build.write(
            ctx=ctx,
            source=src,
            keep=plan.keep,
            edits=plan.analysis.edits,
            gripper=contract.gripper_index,
            semantics=plan.analysis.semantics,
            record=plan.record.source if plan.record else None,
            record_rows=plan.record_rows,
            new=new,
            offset=offset,
            task_index=task_index[plan.text],
            fps=fps,
            staging=staging,
            data_path=data_path,
            video_path=video_path,
        )
        n = built["rows"]
        meta = {
            "scope": plan.analysis.scope,
            "generation": plan.generation,
            "source_pool_key": plan.ep["key"],
            "source_episode": src.old,
            "forward_task": plan.ep["task"],
            "reset_task": plan.text,
            "contract": contract.ref,
            "bridge_record": plan.join and plan.join.get("record"),
        }
        out.append(
            {
                "plan": plan,
                "built": built,
                "episode": new,
                "length": n,
                "row": {
                    "episode_index": new,
                    "tasks": [plan.text],
                    "length": n,
                    "pool_key": plan.ep["key"],
                    "pool_source": plan.ep["source"],
                    "pool_fingerprint": plan.ep["fingerprint"],
                    "levi_reset": meta,
                },
                "record": {
                    **meta,
                    "analysis": plan.analysis.record(),
                    "join": plan.join,
                    "temporal_map": built["temporal_map"],
                    "frames": n,
                },
            }
        )
        new += 1
        offset += n
    return out
