"""Pre-test of the rlinf provider without a real checkpoint or downloads.

Builds the value model from local configs (``tiny``: small SigLIP / Gemma3
and the ``gemma_1m`` expert; ``full``: the published sizes of
SigLIP2-so400m-patch14-224, Gemma3-270M and the ``gemma_100m`` expert,
randomly initialised, for timing and memory), saves random weights as a
``full_weights.pt`` with a ``model.`` prefix, reloads them strictly into a
freshly initialised model, writes a synthetic two-camera view (h264
640x480, 10 fps) and runs the same plan path as a real job. Checks shapes,
V in [v_min, v_max], run-to-run determinism and CPU/GPU agreement.

The tokenizer here is a character-level stand-in (``SelftestTokenizer``) —
for this selftest only; real runs load the manifest's Gemma3 tokenizer.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

TINY = {
    "siglip": {
        "hidden_size": 64,
        "intermediate_size": 128,
        "num_hidden_layers": 2,
        "num_attention_heads": 2,
        "image_size": 224,
        "patch_size": 14,
    },
    "gemma3": {
        "vocab_size": 512,
        "hidden_size": 64,
        "intermediate_size": 128,
        "num_hidden_layers": 4,
        "num_attention_heads": 1,
        "num_key_value_heads": 1,
        "head_dim": 256,
        "query_pre_attn_scalar": 256,
        "sliding_window": 512,
        "max_position_embeddings": 2048,
    },
    "variant": "gemma_1m",
}
# Published sizes (google/siglip2-so400m-patch14-224 vision tower,
# google/gemma-3-270m); the real run reads these from the base-model folders.
FULL = {
    "siglip": {
        "hidden_size": 1152,
        "intermediate_size": 4304,
        "num_hidden_layers": 27,
        "num_attention_heads": 16,
        "image_size": 224,
        "patch_size": 14,
    },
    "gemma3": {
        "vocab_size": 262144,
        "hidden_size": 640,
        "intermediate_size": 2048,
        "num_hidden_layers": 18,
        "num_attention_heads": 4,
        "num_key_value_heads": 1,
        "head_dim": 256,
        "query_pre_attn_scalar": 256,
        "sliding_window": 512,
        "max_position_embeddings": 32768,
        "rope_theta": 1_000_000.0,
        "rope_local_base_freq": 10_000.0,
    },
    "variant": "gemma_100m",
}


class SelftestTokenizer:
    """SELFTEST ONLY: a character-level stand-in for the Gemma3 tokenizer
    (BOS id 2, then one id per character). Never used for a real run."""

    def __init__(self, vocab_size: int):
        self.vocab_size = vocab_size

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        ids = [3 + (ord(c) % (self.vocab_size - 3)) for c in text]
        return ([2] if add_special_tokens else []) + ids


def write_video(path: Path, frames: int, seed: int, fps: int = 10) -> None:
    import av

    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, size=(480, 640, 3), dtype=np.uint8)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = 640, 480, "yuv420p"
        for i in range(frames):
            image = np.roll(base, shift=8 * i, axis=1)
            for packet in stream.encode(
                av.VideoFrame.from_ndarray(image, format="rgb24")
            ):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def write_view(root: Path, lengths: list[int]) -> dict:
    """A LeRobot v2.1 view with two cameras, like a raw capture's view."""
    info = {
        "codebase_version": "v2.1",
        "fps": 10,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "observation.images.view1": {"dtype": "video"},
            "observation.images.hand": {"dtype": "video"},
        },
    }
    (root / "meta").mkdir(parents=True, exist_ok=True)
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "Stack the plates."}) + "\n"
    )
    for ep, n in enumerate(lengths):
        path = root / f"data/chunk-000/episode_{ep:06d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "frame_index": np.arange(n, dtype=np.int64),
                    "timestamp": np.arange(n, dtype=np.float32) / 10,
                    "task_index": np.zeros(n, dtype=np.int64),
                    "episode_index": np.full(n, ep, dtype=np.int64),
                }
            ),
            path,
        )
        for camera, seed in (("view1", ep), ("hand", 100 + ep)):
            write_video(
                root
                / f"videos/chunk-000/observation.images.{camera}/episode_{ep:06d}.mp4",
                n,
                seed,
            )
    return info


def build_checkpoint(work: Path, spec: dict) -> dict:
    import torch
    from transformers import Gemma3TextConfig, SiglipVisionConfig

    from .rlinf_provider import build_model

    ckpt = work / "checkpoint"
    SiglipVisionConfig(**spec["siglip"]).save_pretrained(ckpt / "siglip")
    Gemma3TextConfig(**spec["gemma3"]).save_pretrained(ckpt / "gemma3")
    manifest = {
        "name": "selftest",
        "provider": "rlinf",
        "weights": "full_weights.pt",
        "critic_expert_variant": spec["variant"],
        "num_bins": 201,
        "v_min": -1.0,
        "v_max": 0.0,
        "max_token_len": 200,
        "precision": "bfloat16",
        "views": {
            "base_0_rgb": "observation.images.view1",
            "left_wrist_0_rgb": "observation.images.hand",
            "right_wrist_0_rgb": None,
        },
        "base_models": {"siglip": "siglip", "gemma3": "gemma3", "tokenizer": None},
        "failure_reward": -300.0,
    }
    torch.manual_seed(1234)
    model = build_model(manifest, ckpt)
    # As a wrapper (FSDP/DDP) saves it: every key under "model.".
    state = {f"model.{k}": v for k, v in model.state_dict().items()}
    torch.save(state, ckpt / "full_weights.pt")
    reference = {k: v.detach().clone() for k, v in model.state_dict().items()}
    return {"dir": ckpt, "manifest": manifest, "reference": reference}


def _plan(ckpt: dict, root: Path, lengths: list[int], device: str, batch: int) -> dict:
    return {
        "provider": "rlinf",
        "checkpoint": {
            "name": "selftest",
            "dir": str(ckpt["dir"]),
            "manifest": ckpt["manifest"],
        },
        "dataset": {
            "name": "selftest",
            "root": str(root),
            "fps": 10.0,
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "chunks_size": 1000,
            "tasks": {"0": "Stack the plates."},
        },
        "episodes": [
            {"episode_index": ep, "length": n, "success": ep == 0}
            for ep, n in enumerate(lengths)
        ],
        "batch_size": batch,
        "device": device,
    }


def run(
    size: str = "tiny", devices: str = "both", frames: int = 0, workdir=None
) -> dict:
    import torch

    from . import rlinf_provider

    spec = TINY if size == "tiny" else FULL
    own = workdir is None
    work = (
        Path(tempfile.mkdtemp(prefix="levi-recap-selftest-")) if own else Path(workdir)
    )
    work.mkdir(parents=True, exist_ok=True)
    report: dict = {"size": size, "variant": spec["variant"], "ok": False, "checks": {}}
    try:
        rlinf_provider.require_patch()
        started = time.time()
        ckpt = build_checkpoint(work, spec)
        report["build_seconds"] = round(time.time() - started, 2)
        # Strict reload into a differently initialised model.
        torch.manual_seed(99)
        fresh = rlinf_provider.build_model(ckpt["manifest"], ckpt["dir"])
        load = rlinf_provider.load_weights(fresh, ckpt["dir"] / "full_weights.pt")
        same = all(
            torch.equal(v, ckpt["reference"][k]) for k, v in fresh.state_dict().items()
        )
        report["checks"]["strict_reload_identical"] = same
        report["load"] = load
        inspected = rlinf_provider.inspect_weights(ckpt["dir"] / "full_weights.pt")
        report["checks"]["variant_inferred"] = (
            inspected["inferred_variant"] == spec["variant"]
        )
        del fresh, ckpt["reference"]
        lengths = [13, 7]
        view = work / "view"
        write_view(view, lengths)
        tokenizer = SelftestTokenizer(spec["gemma3"]["vocab_size"])
        wanted = ["cuda", "cpu"] if devices == "both" else [devices]
        if "cuda" in wanted and not torch.cuda.is_available():
            report["checks"]["cuda_available"] = False
            wanted = [d for d in wanted if d != "cuda"]
        results = {}
        for device in wanted:
            timings = []
            outputs = []
            for _ in range(2):
                t0 = time.time()
                out, provenance = rlinf_provider.values(
                    _plan(ckpt, view, lengths, device, 4), None, tokenizer=tokenizer
                )
                timings.append(round(time.time() - t0, 2))
                outputs.append(out)
            values = np.concatenate([outputs[0][ep][1] for ep in sorted(outputs[0])])
            results[device] = values
            report[device] = {
                "seconds_per_run": timings,
                "peak_vram_mib": provenance.get("peak_vram_mib"),
                "values_min": float(values.min()),
                "values_max": float(values.max()),
            }
            report["checks"][f"{device}_shapes"] = [
                len(outputs[0][ep][1]) for ep in sorted(outputs[0])
            ] == lengths
            report["checks"][f"{device}_in_range"] = bool(
                values.min() >= -1.0 and values.max() <= 0.0
            )
            report["checks"][f"{device}_deterministic"] = all(
                np.array_equal(outputs[0][ep][1], outputs[1][ep][1])
                for ep in outputs[0]
            )
        if "cuda" in results and "cpu" in results:
            diff = float(np.max(np.abs(results["cuda"] - results["cpu"])))
            report["cuda_cpu_max_abs_diff"] = diff
            report["checks"]["cuda_cpu_agree"] = diff < 0.05
        if frames and "cuda" in wanted:
            long_view = work / "long"
            write_view(long_view, [frames])
            plan = _plan(ckpt, long_view, [frames], "cuda", 32)
            t0 = time.time()
            _out, provenance = rlinf_provider.values(plan, None, tokenizer=tokenizer)
            elapsed = time.time() - t0
            report["throughput"] = {
                "frames": frames,
                "seconds": round(elapsed, 2),
                "load_seconds": provenance["load_seconds"],
                "frames_per_second": round(
                    frames / max(elapsed - provenance["load_seconds"], 1e-6), 1
                ),
                "peak_vram_mib": provenance.get("peak_vram_mib"),
                "batch_size": 32,
            }
        report["ok"] = all(report["checks"].values())
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)
    return report
