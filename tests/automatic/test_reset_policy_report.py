"""A report may describe proposals and unknown evidence, never fake success."""

import json

import numpy as np
import pytest

from levi.automatic.policy_replay import sha256
from scripts import render_reset_policy_report as report


def fixture(tmp_path, *, prompt="Reset: unseen"):
    source = tmp_path / "inputs"
    source.mkdir()
    samples, records = [], []
    for index, phase in enumerate(("pre_close", "first_close")):
        path = source / f"sample_{index}.npz"
        state = np.array([0.3, 0, 0.2, 3.0, 0, 0, 1 - index], np.float32)
        np.savez_compressed(
            path,
            image=np.full((16, 20, 3), 120, np.uint8),
            wrist_image=np.full((16, 20, 3), 180, np.uint8),
            state7=state,
        )
        sample = {
            "sample_id": f"sample_{index}",
            "npz_path": str(path),
            "npz_sha256": sha256(path),
            "prompt": prompt,
            "phase": phase,
            "source": "synthetic-test-recording",
            "frame_index": 12 + index,
            "recorded_wall_time_s": 1000 + index / 10,
            "recorded_elapsed_s": 1 + index / 10,
            "state7": state.tolist(),
            "measured_gripper_open_fraction": 1.0 if index == 0 else 0.01,
            "measured_gripper_width_m": 0.08 if index == 0 else 0.0008,
            "commanded_gripper_open_fraction": float(state[6]),
            "prompt_in_training": False,
            "source_hashes": {"gripper_state.csv": "test-hash"},
            "image_mapping": {},
        }
        samples.append(sample)
        for seed in (17, 31):
            actions = np.tile(
                [0.3, 0, 0.2, 3.0, 0, 0, 0.2 if seed == 17 else 0.8], (10, 1)
            )
            records.append(
                {
                    "sample_id": sample["sample_id"],
                    "seed": seed,
                    "prompt": prompt,
                    "prompt_seen_in_training": False,
                    "actions": actions.tolist(),
                    "inference_ms": 100.0,
                    "cold_start": not records,
                    "source_sample": sample,
                }
            )
    samples_path = source / "samples.json"
    samples_path.write_text(
        json.dumps({"schema": "levi.reset_policy.samples.v1", "samples": samples})
    )
    model = tmp_path / "model"
    model.mkdir()
    predictions = model / "predictions.json"
    predictions.write_text(
        json.dumps(
            {
                "schema": "levi.reset_policy.predictions.v1",
                "physical_execution": False,
                "closed_loop": False,
                "records": records,
            }
        )
    )
    manifest = {
        "schema": "levi.reset_policy.manifest.v1",
        "status": "completed",
        "physical_execution": False,
        "closed_loop": False,
        "weights_modified": False,
        "sample_manifest": str(samples_path),
        "sample_manifest_sha256": sha256(samples_path),
        "successful_predictions": 4,
        "policy_calls": 4,
        "cold_inference_ms": 100,
        "warm_median_ms": 100,
        "warm_max_ms": 100,
        "load_ms": 50,
        "model": "fixture-reset",
        "checkpoint_step": 20000,
        "llm_calls": 0,
    }
    (model / "manifest.json").write_text(json.dumps(manifest))
    return predictions, samples_path


def test_gate_keeps_missing_facts_unknown_for_open_and_closed_aperture(tmp_path):
    predictions, samples = fixture(tmp_path)
    analysis, _, _ = report.analyse(predictions, samples)
    assert analysis["success_rate"] is None
    assert analysis["gate_allowed_count"] == 0
    assert analysis["grasp_improvement_verified"] is False
    assert analysis["gate_reasons"] == {"empty_not_verified": 4}
    assert analysis["by_phase"]["pre_close"]["proposed_close_fraction"] == 0.5
    for row in analysis["rows"]:
        assert row["executed_action"] is None
        assert row["gate"]["model_proposal_allowed"] is False
        assert row["gate"]["required_skill"] is None
        assert row["gate"]["evidence"]["empty_verified"] is None
        assert row["gate"]["evidence"]["holding_verified"] is None
        assert row["clock"]["real_time_freshness_verified"] is False
    assert analysis["rows"][0]["gate"]["evidence"]["open_measured"] is True
    assert analysis["rows"][2]["gate"]["evidence"]["open_measured"] is False


def test_report_has_local_images_raw_outputs_and_explicit_limitations(tmp_path):
    predictions, samples = fixture(tmp_path)
    output = tmp_path / "report"
    result = report.render_report(predictions, samples, output)
    assert result["prediction_count"] == 4
    page = (output / "index.html").read_text()
    assert report.BOUNDARY in page
    assert "未见过（原句保留）" in page
    assert "不是优化成功率" in page
    assert "逻辑回放" in page
    assert "命令开度" in page and "实测开度" in page
    assert "http://" not in page and "https://" not in page
    assert len(list((output / "images").glob("*.jpg"))) == 4
    assert (output / "predictions.json").read_bytes() == predictions.read_bytes()
    stored = json.loads((output / "analysis.json").read_text())
    assert (
        stored["rows"][0]["raw_actions"]
        == json.loads(predictions.read_text())["records"][0]["actions"]
    )
    assert stored["physical_execution"] is False
    with pytest.raises(FileExistsError):
        report.render_report(predictions, samples, output)


def test_page_escapes_saved_prompt_instead_of_injecting_markup(tmp_path):
    predictions, samples = fixture(
        tmp_path, prompt='Reset: <img src=x onerror="alert(1)">'
    )
    output = tmp_path / "report"
    report.render_report(predictions, samples, output)
    page = (output / "index.html").read_text()
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "failed"),
        ("physical_execution", True),
        ("closed_loop", True),
        ("weights_modified", True),
        ("sample_manifest_sha256", "wrong"),
        ("successful_predictions", 3),
    ],
)
def test_failed_or_mismatched_runtime_cannot_be_published_as_completed(
    tmp_path, field, value
):
    predictions, samples = fixture(tmp_path)
    path = predictions.parent / "manifest.json"
    runtime = json.loads(path.read_text())
    runtime[field] = value
    path.write_text(json.dumps(runtime))
    with pytest.raises(ValueError):
        report.render_report(predictions, samples, tmp_path / "report")
    assert not (tmp_path / "report").exists()


def test_changed_input_bytes_are_refused_before_rendering(tmp_path):
    predictions, samples = fixture(tmp_path)
    path = samples.parent / "sample_0.npz"
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        report.analyse(predictions, samples)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "wrong_prompt", "wrong_provenance", "nonfinite", "bad_shape"],
)
def test_prediction_rows_are_bound_and_numeric(tmp_path, mutation):
    predictions, samples = fixture(tmp_path)
    doc = json.loads(predictions.read_text())
    first = doc["records"][0]
    if mutation == "duplicate":
        doc["records"][1] = first
    elif mutation == "wrong_prompt":
        first["prompt"] = "other"
    elif mutation == "wrong_provenance":
        first["source_sample"]["frame_index"] = 99
    elif mutation == "nonfinite":
        first["actions"][0][6] = float("nan")
    else:
        first["actions"] = [[0] * 7]
    predictions.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        report.analyse(predictions, samples)
