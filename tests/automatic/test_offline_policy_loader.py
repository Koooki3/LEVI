"""Loader admission and durable real-policy protocol, using a CPU-only stub."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import offline_reset_policy as runner


@pytest.fixture
def fixture(tmp_path):
    checkpoint, snapshot, inputs, output = (
        tmp_path / p for p in ("checkpoint", "snapshot", "inputs", "out")
    )
    for path in (
        checkpoint / "params",
        checkpoint / "runtime",
        checkpoint / "assets/reset_r1",
        snapshot / "scripts",
        snapshot / "src/openpi/models",
        inputs,
    ):
        path.mkdir(parents=True)
    norm = checkpoint / "assets/reset_r1/norm_stats.json"
    norm.write_text("{}")
    (checkpoint / "best.json").write_text(
        json.dumps(
            {
                "step": 20000,
                "norm_stats_sha256": runner.sha256(norm),
                "metrics": {"val_task_mse": {"Reset: recorded task": 0.1}},
            }
        )
    )
    for path in (
        checkpoint / "runtime/train_reset_r1.py",
        checkpoint / "runtime/reset_r1_data.py",
        checkpoint / "checkpoint.sha256",
        checkpoint / "params/weights",
        snapshot / "scripts/train.py",
        snapshot / "src/openpi/models/pi0.py",
    ):
        path.write_text("fixture")
    tokenizer = tmp_path / "tokenizer.model"
    tokenizer.write_text("tokenizer fixture")
    samples = []
    for index, prompt in enumerate(("Reset: recorded task", "Reset: unseen task")):
        path = inputs / f"sample_{index}.npz"
        np.savez(
            path,
            image=np.zeros((4, 5, 3), np.uint8),
            wrist_image=np.zeros((4, 5, 3), np.uint8),
            state7=np.asarray([0.3, 0, 0.1, 3.14, 0, 0, 1], np.float32),
        )
        samples.append(
            {
                "sample_id": f"sample_{index}",
                "npz_path": str(path),
                "npz_sha256": runner.sha256(path),
                "prompt": prompt,
                "holding": "unknown",
                "grasp_readiness": "unknown",
            }
        )
    manifest = inputs / "samples.json"
    manifest.write_text(
        json.dumps({"schema": "levi.reset_policy.samples.v1", "samples": samples})
    )
    return {
        "checkpoint": checkpoint,
        "snapshot": snapshot,
        "tokenizer": tokenizer,
        "manifest": manifest,
        "output": output,
        "limit": None,
        "seeds": (17, 31, 47),
    }


class FakePolicy:
    def __init__(self):
        self.calls = []

    def infer(self, observation, *, noise):
        self.calls.append((observation, noise.copy()))
        assert noise.shape == (10, 32) and noise.dtype == np.float32
        return {"actions": noise[:, :7], "policy_timing": {"infer_ms": 0.1}}


def test_seeded_chunks_preserved_prompt_identity_and_cold_warm_distinction(fixture):
    policy = FakePolicy()
    result = runner.run_inference(**fixture, loader=lambda *_: policy)
    assert len(result["records"]) == 6
    assert result["physical_execution"] is False and result["closed_loop"] is False
    assert result["historical_exact_replay"] is False
    assert [r["cold_start"] for r in result["records"]] == [
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert [r["prompt_seen_in_training"] for r in result["records"]] == [True] * 3 + [
        False
    ] * 3
    assert result["records"][3]["prompt"] == "Reset: unseen task"
    for row, (_, noise) in zip(result["records"], policy.calls, strict=True):
        np.testing.assert_array_equal(row["actions"], noise[:, :7])
        assert row["source_sample"]["holding"] == "unknown"
    np.testing.assert_array_equal(policy.calls[0][1], policy.calls[3][1])
    runtime = runner.read_json(fixture["output"] / "reports/manifest.json")
    assert runtime["status"] == "completed" and runtime["policy_calls"] == 6
    assert runtime["successful_predictions"] == 6
    assert runtime["weights_modified"] is False
    assert runtime["input_tokens"] is None and runtime["output_tokens"] is None


def test_completed_output_is_never_overwritten(fixture):
    runner.run_inference(**fixture, loader=lambda *_: FakePolicy())
    path = fixture["output"] / "reports/predictions.json"
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        runner.run_inference(
            **fixture, loader=lambda *_: pytest.fail("must not load twice")
        )
    assert path.read_bytes() == before


def test_invalid_model_output_preserves_partial_and_failure(fixture):
    policy = FakePolicy()

    def infer(obs, *, noise):
        result = FakePolicy.infer(policy, obs, noise=noise)
        if len(policy.calls) == 2:
            result["actions"][0, 0] = np.nan
        return result

    policy.infer = infer
    with pytest.raises(ValueError, match="invalid policy action"):
        runner.run_inference(**fixture, loader=lambda *_: policy)
    assert not (fixture["output"] / "reports/predictions.json").exists()
    partial = runner.read_json(fixture["output"] / "reports/predictions.partial.json")
    assert len(partial["records"]) == 1
    runtime = runner.read_json(fixture["output"] / "reports/manifest.json")
    assert runtime["status"] == "failed" and runtime["policy_calls"] == 2


@pytest.mark.parametrize("bad", ["hash", "duplicate", "escape", "prompt"])
def test_bad_input_refused_before_loading(fixture, bad):
    data = runner.read_json(fixture["manifest"])
    if bad == "hash":
        data["samples"][0]["npz_sha256"] = "wrong"
    elif bad == "duplicate":
        data["samples"][1]["sample_id"] = data["samples"][0]["sample_id"]
    elif bad == "escape":
        data["samples"][0]["npz_path"] = str(fixture["tokenizer"])
    else:
        data["samples"][0]["prompt"] = ""
    fixture["manifest"].write_text(json.dumps(data))
    with pytest.raises(ValueError):
        runner.run_inference(**fixture, loader=lambda *_: pytest.fail("must not load"))
    assert not fixture["output"].exists()


def test_normalization_identity_required(fixture):
    (fixture["checkpoint"] / "assets/reset_r1/norm_stats.json").write_text("changed")
    with pytest.raises(ValueError, match="normalization hash"):
        runner.run_inference(**fixture, loader=lambda *_: pytest.fail("must not load"))


def test_download_mapping_is_local_only_without_cache_mutations(fixture):
    before = fixture["tokenizer"].stat().st_mode
    resolver = runner.local_download_resolver(
        fixture["tokenizer"], fixture["checkpoint"], fixture["output"]
    )
    assert resolver(runner.TOKENIZER_URI) == fixture["tokenizer"]
    assert resolver(fixture["checkpoint"]) == fixture["checkpoint"]
    for url in (
        "https://example.invalid/model",
        "gs://other/asset",
        str(fixture["manifest"]),
    ):
        with pytest.raises(PermissionError):
            resolver(url)
    with pytest.raises(PermissionError):
        resolver(runner.TOKENIZER_URI, force_download=True)
    assert fixture["tokenizer"].stat().st_mode == before
    assert not fixture["output"].exists()


def test_environment_does_not_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "not-a-real-token")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    env = runner.isolated_environment(tmp_path / "out", gpu=True)
    assert "HF_TOKEN" not in env and "OPENAI_API_KEY" not in env and "HOME" not in env
    assert env["CUDA_VISIBLE_DEVICES"] == "0" and env["HF_HUB_OFFLINE"] == "1"
    assert env["XLA_PYTHON_CLIENT_PREALLOCATE"] == "false"
    assert env["XLA_PYTHON_CLIENT_MEM_FRACTION"] == "0.3"
    assert env["XLA_PYTHON_CLIENT_ALLOCATOR"] == "bfc"
    assert env["TF_FORCE_UNIFIED_MEMORY"] == "false"
    assert Path(env["TMPDIR"]).is_relative_to(tmp_path / "out")


@pytest.mark.parametrize(
    "readonly_option", ["--source-root", "--checkpoint", "--openpi-root", "--tokenizer"]
)
@pytest.mark.parametrize("overlap", ["same", "nested", "parent", "symlink"])
def test_cli_rejects_readonly_overlap_before_any_writes(
    tmp_path, readonly_option, overlap
):
    inputs = {
        "--source-root": tmp_path / "recordings" / "source",
        "--checkpoint": tmp_path / "model" / "checkpoint",
        "--openpi-root": tmp_path / "runtime" / "snapshot",
        "--tokenizer": tmp_path / "tokenizer" / "tokenizer.model",
    }
    for option, path in inputs.items():
        if option == "--tokenizer":
            path.parent.mkdir()
            path.write_bytes(b"readonly tokenizer")
        else:
            path.mkdir(parents=True)
            (path / "sentinel").write_bytes(b"readonly input")
    readonly = inputs[readonly_option]
    if overlap == "same":
        output = readonly
    elif overlap == "nested":
        output = readonly / "generated"
    elif overlap == "parent":
        output = readonly.parent
    else:
        output = tmp_path / "output_alias"
        output.symlink_to(readonly.parent, target_is_directory=True)
    mode = "--prepare-only" if readonly_option == "--source-root" else "--infer"
    options = (
        ["--source-root"]
        if mode == "--prepare-only"
        else ["--checkpoint", "--openpi-root", "--tokenizer"]
    )
    before = {
        str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(Path(runner.__file__).resolve()),
            mode,
            "--output",
            str(output),
            *[value for option in options for value in (option, str(inputs[option]))],
        ],
        cwd=tmp_path,
        env={},
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2, result.stderr.decode()
    assert f"--output must be disjoint from {readonly_option}" in result.stderr.decode()
    after = {
        str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }
    assert after == before


def test_cli_accepts_prepared_samples_in_output(fixture, monkeypatch):
    class IsolationReached(Exception):
        pass

    def reached(output, **_kwargs):
        assert output == fixture["output"]
        raise IsolationReached

    monkeypatch.delenv("LEVI_OFFLINE_POLICY_CHILD", raising=False)
    monkeypatch.setattr(runner, "isolated_environment", reached)
    with pytest.raises(IsolationReached):
        runner.main(
            [
                "--infer",
                "--output",
                str(fixture["output"]),
                "--samples",
                str(fixture["output"] / "samples.json"),
                "--checkpoint",
                str(fixture["checkpoint"]),
                "--openpi-root",
                str(fixture["snapshot"]),
                "--tokenizer",
                str(fixture["tokenizer"]),
            ]
        )
    assert not fixture["output"].exists()


def test_custom_memory_fraction_is_kept_in_environment_and_report(fixture):
    env = runner.isolated_environment(
        fixture["output"], gpu=True, gpu_memory_fraction=0.25
    )
    assert env["XLA_PYTHON_CLIENT_MEM_FRACTION"] == "0.25"
    runner.run_inference(
        **fixture, gpu_memory_fraction=0.25, loader=lambda *_: FakePolicy()
    )
    manifest = runner.read_json(fixture["output"] / "reports/manifest.json")
    assert manifest["gpu_memory_fraction"] == 0.25
    assert manifest["gpu_allocator"] == "bfc" and manifest["gpu_preallocate"] is False
    assert manifest["peak_bytes_in_use"] is None


@pytest.mark.parametrize(
    "fraction", [0, -0.1, 0.450001, float("nan"), float("inf"), True]
)
def test_invalid_fraction_refused_before_output_creation(tmp_path, fraction):
    output = tmp_path / "never_created"
    with pytest.raises(ValueError, match="GPU memory fraction"):
        runner.isolated_environment(output, gpu=True, gpu_memory_fraction=fraction)
    assert not output.exists()


def test_only_current_policy_device_allocator_peaks_are_recorded(fixture):
    device = SimpleNamespace(
        id=0,
        platform="gpu",
        memory_stats=lambda: {
            "bytes_in_use": 12,
            "peak_bytes_in_use": 40,
            "peak_bytes_reserved": 64,
        },
    )
    jax_stub = SimpleNamespace(local_devices=lambda: [device])
    wrapper = runner.MeasuredPolicy(FakePolicy(), jax_stub)
    result = runner.run_inference(**fixture, loader=lambda *_: wrapper)
    assert (
        result["records"][0]["device_memory"]["devices"][0]["stats"][
            "peak_bytes_in_use"
        ]
        == 40
    )
    manifest = runner.read_json(fixture["output"] / "reports/manifest.json")
    assert manifest["peak_bytes_in_use"] == 40
    assert manifest["peak_bytes_reserved"] == 64


def test_socket_guard_allows_asyncio_self_wakeup_but_blocks_external_io():
    code = """
import socket
from scripts.offline_reset_policy import deny_network
deny_network()
import ssl
assert issubclass(ssl.SSLSocket, socket.socket)
import asyncio
async def check_event_loop():
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    loop.call_soon_threadsafe(done.set_result, 42)
    assert await done == 42
asyncio.run(check_event_loop())
first, second = socket.socketpair()
try:
    first.sendall(b"x")
    assert second.recv(1) == b"x"
finally:
    first.close()
    second.close()
unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
for call in (lambda: socket.socket(socket.AF_INET), lambda: socket.socket(socket.AF_INET6),
             lambda: socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM),
             lambda: socket.getaddrinfo("localhost",1),
             lambda: socket.create_connection(("127.0.0.1",1)),
             lambda: unix.connect("/nonexistent-offline-service"),
             lambda: unix.connect_ex("/nonexistent-offline-service"),
             lambda: unix.bind("/nonexistent-offline-service"),
             lambda: unix.sendto(b"x", "/nonexistent-offline-service"),
             lambda: unix.sendmsg([b"x"], [], 0, "/nonexistent-offline-service")):
    try: call()
    except PermissionError: pass
    else: raise AssertionError("network guard bypassed")
unix.close()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stderr.decode()
    import socket

    assert isinstance(socket.socket, type)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("recorded_task", "Reset: recorded task"),
        ("reset: recorded task", "Reset: recorded task"),
        ("Reset: recorded task", "Reset: recorded task"),
    ],
)
def test_prompt_prefix_matches_original_wrapper(text, expected):
    assert runner.normalized_prompt(text) == expected
