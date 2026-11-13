"""Render recorded-observation policy proposals and evidence-gate decisions.

No robot/simulator is run. Missing evidence remains unknown; filtered commands
are not substituted into the recorded future, and no success rate is inferred.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from levi.automatic.offline_worker import atomic_json
from levi.automatic.policy_replay import sha256, summarize_action_chunk
from levi.automatic.regrasp_gate import GateObservation, GateState, advance_regrasp

PHASES = {
    "pre_close": "首次闭爪前一帧",
    "first_close": "首次闭爪命令帧",
    "post_close_8": "闭爪后第 8 帧",
}
COLORS = ("#5ad5ef", "#ffcb6b", "#d28cff", "#93e3ae", "#ff8585")
BOUNDARY = "缺乏空爪／抓取几何证据，禁止执行，不等于抓取改善。"


def read_json(path: Path):
    return json.loads(path.read_text())


def _finite(value, field):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"invalid numeric {field}")
    return value


def analyse(predictions_path: Path, samples_path: Path) -> tuple[dict, dict, dict]:
    """Bind completed inference to source bytes before producing any page."""
    predictions_path, samples_path = predictions_path.resolve(), samples_path.resolve()
    prediction_doc = read_json(predictions_path)
    samples_doc = read_json(samples_path)
    runtime = read_json(predictions_path.parent / "manifest.json")
    if (
        prediction_doc.get("schema") != "levi.reset_policy.predictions.v1"
        or prediction_doc.get("physical_execution") is not False
        or prediction_doc.get("closed_loop") is not False
        or runtime.get("schema") != "levi.reset_policy.manifest.v1"
        or runtime.get("status") != "completed"
        or runtime.get("physical_execution") is not False
        or runtime.get("closed_loop") is not False
        or runtime.get("weights_modified") is not False
    ):
        raise ValueError("only completed offline inference can be reported")
    if (
        runtime.get("sample_manifest_sha256") != sha256(samples_path)
        or Path(runtime.get("sample_manifest", "")).resolve() != samples_path
    ):
        raise ValueError("sample manifest identity mismatch")
    samples = samples_doc.get("samples")
    predictions = prediction_doc.get("records")
    if (
        not isinstance(samples, list)
        or not samples
        or not isinstance(predictions, list)
        or not predictions
    ):
        raise ValueError("sample and prediction rows are required")
    by_id = {sample["sample_id"]: sample for sample in samples}
    if len(by_id) != len(samples):
        raise ValueError("duplicate sample identity")
    rows, identities, verified_inputs = [], set(), set()
    for record in predictions:
        sample_id, seed = record["sample_id"], record["seed"]
        if (
            not isinstance(sample_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sample_id)
            or sample_id not in by_id
            or type(seed) is not int
            or not 0 <= seed < 2**32
            or (sample_id, seed) in identities
        ):
            raise ValueError("invalid/repeated prediction identity")
        identities.add((sample_id, seed))
        sample = by_id[sample_id]
        if sample["phase"] not in PHASES or record.get("prompt") != sample["prompt"]:
            raise ValueError("sample phase/prompt mismatch")
        if record.get("source_sample") != sample:
            raise ValueError("model input provenance mismatch")
        if record.get("prompt_seen_in_training") is not sample["prompt_in_training"]:
            raise ValueError("training-prompt label mismatch")
        path = Path(sample["npz_path"]).resolve()
        if not path.is_relative_to(samples_path.parent):
            raise ValueError("NPZ input outside manifest directory")
        if sample_id not in verified_inputs:
            if sha256(path) != sample["npz_sha256"]:
                raise ValueError("NPZ input hash mismatch")
            with np.load(path, allow_pickle=False) as npz:
                state = np.asarray(npz["state7"])
                if state.shape != (7,) or not np.array_equal(
                    state, np.asarray(sample["state7"], dtype=np.float32)
                ):
                    raise ValueError("recorded state does not match NPZ")
                for key in ("image", "wrist_image"):
                    value = npz[key]
                    if (
                        value.dtype != np.uint8
                        or value.ndim != 3
                        or value.shape[-1] != 3
                    ):
                        raise ValueError("recorded image must be HWC uint8 RGB")
            verified_inputs.add(sample_id)
        actions = np.asarray(record["actions"], dtype=np.float64)
        if actions.shape != (10, 7) or not np.isfinite(actions).all():
            raise ValueError("expected finite 10x7 raw policy output")
        inference_ms = _finite(record["inference_ms"], "inference_ms")
        if inference_ms < 0:
            raise ValueError("negative inference duration")
        timestamp = _finite(sample["recorded_wall_time_s"], "recorded_wall_time_s")
        aperture = _finite(
            sample["measured_gripper_open_fraction"], "measured_gripper_open_fraction"
        )
        # This threshold describes aperture only; it never proves emptiness or
        # holding, and is not a reviewed physical controller setting.
        open_measured = aperture >= 0.9
        logical_ms = round(timestamp * 1000)
        run_id = f"recorded-reset-seed-{seed}"
        observation = GateObservation(
            run_id=run_id,
            sample_id=sample_id,
            policy_epoch=1,
            seq=sample["frame_index"],
            captured_ms=logical_ms,
            empty_verified=None,
            open_measured=open_measured,
            approach_verified=None,
            at_grasp_pose=None,
            holding_verified=None,
            source="recorded_measurement",
        )
        _, decision = advance_regrasp(
            GateState(run_id, sample_id, 1),
            observation,
            actions[0].tolist(),
            now_ms=logical_ms,
        )
        rows.append(
            {
                "sample_id": sample_id,
                "seed": seed,
                "phase": sample["phase"],
                "raw_actions": actions.tolist(),
                "raw_summary": summarize_action_chunk(actions, sample["state7"]),
                "inference_ms": inference_ms,
                "cold_start": record.get("cold_start") is True,
                "gate": decision,
                "executed_action": None,
                "gate_scope": "first_raw_action_only_independent_observation",
                "clock": {
                    "kind": "recorded_logical_replay_clock",
                    "now_ms": logical_ms,
                    "captured_ms": logical_ms,
                    "real_time_freshness_verified": False,
                },
                "aperture_diagnostic_threshold": 0.9,
            }
        )
    grouped = {}
    for phase in PHASES:
        phase_rows = [row for row in rows if row["phase"] == phase]
        proposed = sum(row["raw_summary"]["close_proposal_rows"] for row in phase_rows)
        total = sum(row["raw_summary"]["action_count"] for row in phase_rows)
        grouped[phase] = {
            "prediction_count": len(phase_rows),
            "proposed_action_rows": total,
            "proposed_close_rows": proposed,
            "proposed_close_fraction": proposed / total if total else None,
            "first_action_proposes_close": sum(
                row["raw_actions"][0][6] < 0.5 for row in phase_rows
            ),
            "all_rows_propose_close_chunks": sum(
                row["raw_summary"]["all_rows_propose_close"] for row in phase_rows
            ),
        }
    if runtime.get("successful_predictions") != len(rows) or runtime.get(
        "policy_calls"
    ) != len(rows):
        raise ValueError("runtime/prediction count mismatch")
    return (
        {
            "schema": "levi.reset_policy.review.v1",
            "physical_execution": False,
            "closed_loop": False,
            "weights_modified": False,
            "success_rate": None,
            "grasp_improvement_verified": False,
            "boundary": BOUNDARY,
            "sample_count": len(verified_inputs),
            "manifest_sample_count": len(samples),
            "prediction_count": len(rows),
            "seeds": sorted({row["seed"] for row in rows}),
            "by_phase": grouped,
            "gate_reasons": dict(Counter(row["gate"]["reason"] for row in rows)),
            "gate_allowed_count": sum(
                row["gate"]["model_proposal_allowed"] for row in rows
            ),
            "rows": rows,
            "predictions_sha256": sha256(predictions_path),
            "samples_sha256": sha256(samples_path),
            "runtime_manifest_sha256": sha256(
                predictions_path.parent / "manifest.json"
            ),
            "clock_note": "logical now equals saved observation timestamp; this does not attest current sensor freshness",
            "phase_note": "phases are recorded command transitions, not independently verified contact stages",
        },
        samples_doc,
        runtime,
    )


def _escape(value) -> str:
    return html.escape(str(value), quote=True)


def curve(rows: list[dict]) -> str:
    width, height = 480, 190
    left, right, top, bottom = 40, 462, 16, 155
    all_values = [value[6] for row in rows for value in row["raw_actions"]]
    low, high = min(-0.1, *all_values), max(1.1, *all_values)
    x = lambda index: left + (right - left) * index / 9
    y = lambda value: bottom - (value - low) / (high - low) * (bottom - top)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="模型原始夹爪动作曲线">'
    ]
    for value in (0, 0.5, 1):
        parts.append(
            f'<line x1="{left}" x2="{right}" y1="{y(value):.2f}" y2="{y(value):.2f}" stroke="#50647a" stroke-dasharray="4 4"/>'
        )
        parts.append(f'<text x="4" y="{y(value) + 4:.2f}">{value:g}</text>')
    for index, row in enumerate(sorted(rows, key=lambda row: row["seed"])):
        points = " ".join(
            f"{x(i):.2f},{y(action[6]):.2f}"
            for i, action in enumerate(row["raw_actions"])
        )
        color = COLORS[index % len(COLORS)]
        parts.append(
            f'<polyline points="{points}" stroke="{color}" fill="none" stroke-width="2.2"/>'
        )
        parts.append(
            f'<text x="{left + index * 115}" y="181" style="fill:{color}">seed {row["seed"]}</text>'
        )
    parts.append('<text x="359" y="181">动作行 0–9</text></svg>')
    return "".join(parts)


def _duration(value):
    return "未记录" if value is None else f"{float(value) / 1000:.3f} s"


def _percentage(value):
    return "—" if value is None else f"{value:.1%}"


def render_report(predictions: Path, samples: Path, output: Path) -> dict:
    import cv2

    analysis, samples_doc, runtime = analyse(predictions, samples)
    output = output.resolve()
    if output.exists():
        raise FileExistsError("report output already exists; use a new directory")
    output.mkdir(parents=True)
    image_root = output / "images"
    image_root.mkdir()
    by_id = {sample["sample_id"]: sample for sample in samples_doc["samples"]}
    grouped = defaultdict(list)
    for row in analysis["rows"]:
        grouped[row["sample_id"]].append(row)
    cards = []
    for sample_id, rows in grouped.items():
        sample = by_id[sample_id]
        if sha256(Path(sample["npz_path"])) != sample["npz_sha256"]:
            raise ValueError("input changed during report generation")
        with np.load(sample["npz_path"], allow_pickle=False) as data:
            for key, suffix in (("image", "side"), ("wrist_image", "wrist")):
                image = data[key]
                height, width = image.shape[:2]
                scale = min(1.0, 480 / width, 360 / height)
                thumbnail = cv2.resize(
                    image,
                    (round(width * scale), round(height * scale)),
                    interpolation=cv2.INTER_AREA,
                )
                if not cv2.imwrite(
                    str(image_root / f"{sample_id}_{suffix}.jpg"),
                    cv2.cvtColor(thumbnail, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 90],
                ):
                    raise ValueError("failed to save source thumbnail")
        seed_table = "".join(
            f"<tr><td>{row['seed']}</td><td>{row['raw_summary']['first_action_gripper_raw']:.4f}</td>"
            f"<td>{row['raw_summary']['close_proposal_rows']}/10</td>"
            f"<td>{_duration(row['inference_ms'])}</td><td>{_escape(row['gate']['reason'])}</td></tr>"
            for row in sorted(rows, key=lambda row: row["seed"])
        )
        evidence = {
            "source": sample["source"],
            "frame_index": sample["frame_index"],
            "recorded_wall_time_s": sample["recorded_wall_time_s"],
            "state7": sample["state7"],
            "npz_sha256": sample["npz_sha256"],
            "source_hashes": sample["source_hashes"],
            "image_mapping": sample["image_mapping"],
            "gate": rows[0]["gate"],
            "clock": rows[0]["clock"],
        }
        cards.append(f'''<article class="sample" data-phase="{_escape(sample["phase"])}" data-id="{_escape(sample_id.lower())}">
<div class="sample-head"><span class="phase">{PHASES[sample["phase"]]}</span><span>录制 t={sample["recorded_elapsed_s"]:.3f} s · 帧 {sample["frame_index"]}</span></div>
<h2>{_escape(sample["prompt"])}</h2><p class="mono muted">{_escape(sample_id)}</p>
<div class="images"><figure><img loading="lazy" src="images/{sample_id}_side.jpg" alt="侧相机录制帧"><figcaption>侧相机 · 原录制 RGB</figcaption></figure>
<figure><img loading="lazy" src="images/{sample_id}_wrist.jpg" alt="腕部相机录制帧"><figcaption>腕部相机 · 原录制 RGB</figcaption></figure></div>
<div class="measurements"><span>命令开度 <b>{sample["commanded_gripper_open_fraction"]:.3f}</b></span><span>实测开度 <b>{sample["measured_gripper_width_m"] * 1000:.2f} mm</b></span><span>训练指令：<b>{"见过" if sample["prompt_in_training"] else "未见过（原句保留）"}</b></span></div>
{curve(rows)}<p class="muted">原始输出 &lt;0.5 表示闭爪提议；曲线不代表实际执行或物体接触。</p>
<table><thead><tr><th>Seed</th><th>首动作夹爪</th><th>闭爪提议行</th><th>推理耗时</th><th>证据检查原因</th></tr></thead><tbody>{seed_table}</tbody></table>
<p class="blocked">未执行 · 缺少已验证空爪与抓取几何 · 持物状态未知</p>
<details><summary>查看来源、哈希与检查证据</summary><pre>{_escape(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre></details></article>''')
    phase_rows = "".join(
        f"<tr><td>{PHASES[phase]}</td><td>{row['prediction_count']}</td>"
        f"<td>{row['proposed_close_rows']}/{row['proposed_action_rows']}</td>"
        f"<td>{_percentage(row['proposed_close_fraction'])}</td>"
        f"<td>{row['first_action_proposes_close']}</td><td>{row['all_rows_propose_close_chunks']}</td></tr>"
        for phase, row in analysis["by_phase"].items()
    )
    html_text = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reset policy · 离线观测对照</title><style>
:root{{color-scheme:dark;font-family:system-ui,-apple-system,"Noto Sans SC",sans-serif;background:#0b111a;color:#e3ebf5}}body{{max-width:1500px;margin:auto;padding:28px}}h1{{font-size:32px;margin-bottom:8px}}h2{{font-size:18px;line-height:1.5}}a{{color:#74d8ed}}p{{line-height:1.7}}.muted,figcaption{{color:#9cacbf;font-size:13px}}.mono,pre{{font-family:ui-monospace,monospace;overflow-wrap:anywhere}}.banner,.metrics,.summary,.toolbar{{padding:18px;border:1px solid #33455c;border-radius:12px;margin:18px 0;background:#121d2b}}.banner{{border-color:#b28a42;background:#2a2217}}.metrics,.measurements,.sample-head{{display:flex;gap:24px;flex-wrap:wrap;justify-content:space-between}}.metrics b{{display:block;font-size:26px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,560px),1fr));gap:20px}}.sample{{padding:20px;border-radius:14px;background:#121c29;border:1px solid #2d3d51}}.sample[hidden]{{display:none}}.phase{{color:#81ddeb;font-weight:650}}.images{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}figure{{margin:0}}img{{width:100%;border-radius:7px;background:#070c11}}figcaption{{padding:6px 0}}svg{{display:block;width:100%;margin-top:14px;background:#0c1520;border-radius:8px}}svg text{{font-size:11px;fill:#a9bbcf}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:10px 7px;border-bottom:1px solid #304157;text-align:left}}.measurements{{font-size:13px;margin-top:12px}}.blocked{{font-size:13px;color:#f0c06c}}details{{margin-top:12px}}pre{{font-size:11px;white-space:pre-wrap;background:#0b131e;padding:14px;border-radius:7px}}select,input{{background:#0b131e;color:#e3ebf5;border:1px solid #45617b;border-radius:6px;padding:9px;max-width:100%}}.toolbar label{{margin-right:18px}}@media(max-width:600px){{body{{padding:14px}}h1{{font-size:25px}}th,td{{font-size:11px;padding:7px 3px}}}}
</style><header><p class="phase">真实 checkpoint 推理 · 录制观测 · 无硬件执行</p><h1>Reset policy 离线观测对照</h1><p>检查原始模型是否提议闭爪，以及 re-grasp harness 是否会因缺少证据而拒绝执行。没有生成受控动作后的新画面或新轨迹。</p></header>
<div class="banner"><strong>{BOUNDARY}</strong><p>“未放行”不是优化成功率。闭爪命令和很小的实测开度都不能证明持物。这里使用录制时钟进行逻辑回放，不证明当前传感器新鲜度。</p></div>
<div class="metrics"><span>观测样本<b>{analysis["sample_count"]} / {analysis["manifest_sample_count"]}</b></span><span>真实模型调用<b>{analysis["prediction_count"]}</b></span><span>固定 seed<b>{_escape(", ".join(map(str, analysis["seeds"])))}</b></span><span>物理执行<b>0</b></span></div>
<section class="summary"><h2>按录制命令切换阶段汇总</h2><p class="muted">阶段由历史开闭命令定义，不是独立验证的接触阶段。“闭爪比例”仅统计模型提议的 action 行。</p><table><thead><tr><th>观测阶段</th><th>动作块数</th><th>闭爪提议 / 全部行</th><th>比例</th><th>首动作闭爪块</th><th>整块闭爪</th></tr></thead><tbody>{phase_rows}</tbody></table></section>
<section class="summary"><h2>运行成本与证据范围</h2><p>权重加载：{_duration(runtime.get("load_ms"))}；首调用（含冷启动）：{_duration(runtime.get("cold_inference_ms"))}；暖调用中位数：{_duration(runtime.get("warm_median_ms"))}；暖调用最大值：{_duration(runtime.get("warm_max_ms"))}。</p><p class="muted">这些数值只表示本次机器与资源条件下的离线推理耗时。没有在线控制或闭环成功率。LLM 调用：{_escape(runtime.get("llm_calls", "未记录"))}；token：未提供（不按 0 计）。</p><p>模型：{_escape(runtime.get("model", "未记录"))} · checkpoint step {_escape(runtime.get("checkpoint_step", "未记录"))} · 权重未修改。</p><p><a href="analysis.json">原始提议＋证据检查结果</a> · <a href="predictions.json">原始模型输出</a> · <a href="policy_manifest.json">模型与耗时记录</a> · <a href="samples.json">输入来源与哈希</a></p></section>
<div class="toolbar"><label>观测阶段 <select id="phase"><option value="all">全部</option>{"".join(f'<option value="{phase}">{label}</option>' for phase, label in PHASES.items())}</select></label><label>筛选样本 <input id="search" placeholder="eggplant / watermelon / demo_0000"></label><span id="count"></span></div>
<main class="cards">{"".join(cards)}</main><footer><p class="muted">保留全部模型输出和未知状态。没有独立空爪、抓取位和物体随动证据时，harness 不发开爪／闭爪技能，也不放行整个模型动作。</p></footer>
<script>const phase=document.querySelector('#phase'),search=document.querySelector('#search'),cards=[...document.querySelectorAll('.sample')];function filter(){{let n=0;for(const c of cards){{const show=(phase.value==='all'||c.dataset.phase===phase.value)&&c.dataset.id.includes(search.value.trim().toLowerCase());c.hidden=!show;if(show)n++;}}document.querySelector('#count').textContent=n+' 个样本';}}phase.addEventListener('change',filter);search.addEventListener('input',filter);filter();</script></html>"""
    atomic_json(output / "analysis.json", analysis)
    shutil.copyfile(predictions, output / "predictions.json")
    shutil.copyfile(
        predictions.parent / "manifest.json", output / "policy_manifest.json"
    )
    shutil.copyfile(samples, output / "samples.json")
    (output / "index.html").write_text(html_text)
    return analysis


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = render_report(args.predictions, args.samples, args.output)
    print(
        f"review page: {args.output / 'index.html'} ({result['prediction_count']} proposals, no physical execution)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
