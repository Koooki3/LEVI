"""Recorded-observation reset_r1 inference in an isolated, network-denied process.

This saves real model proposals. It does not execute them or turn a recorded
video into a closed-loop experiment. No weights or original transforms change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
TOKENIZER_URI = "gs://big_vision/paligemma_tokenizer.model"
SEEDS = (17, 31, 47)


def validate_memory_fraction(value: float) -> float:
    if (
        type(value) not in (float, int)
        or not math.isfinite(value)
        or not 0 < value <= 0.45
    ):
        raise ValueError("GPU memory fraction must be finite and in (0, 0.45]")
    return float(value)


class MeasuredPolicy:
    """Preserve the original infer call and expose only this process's allocator stats."""

    def __init__(self, policy, jax_module):
        self.policy = policy
        self.jax = jax_module

    def infer(self, observation, *, noise):
        return self.policy.infer(observation, noise=noise)

    def offline_memory_stats(self):
        result = []
        for device in self.jax.local_devices():
            stats = device.memory_stats()
            result.append(
                {
                    "device_id": device.id,
                    "platform": device.platform,
                    "stats": None
                    if stats is None
                    else {
                        str(key): int(value)
                        for key, value in stats.items()
                        if isinstance(value, int)
                    },
                }
            )
        return result


def own_memory_stats(policy) -> dict:
    reader = getattr(policy, "offline_memory_stats", None)
    if not callable(reader):
        return {
            "available": False,
            "reason": "allocator_stats_unavailable",
            "devices": [],
        }
    try:
        devices = reader()
        return {"available": True, "devices": devices}
    except (RuntimeError, ValueError, TypeError) as error:
        return {"available": False, "reason": type(error).__name__, "devices": []}


def peak_allocator_bytes(measurements: list[dict], key: str) -> int | None:
    values = [
        device["stats"][key]
        for measurement in measurements
        for device in measurement.get("devices", [])
        if isinstance(device.get("stats"), dict) and key in device["stats"]
    ]
    return max(values) if values else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text())


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x") as stream:
        json.dump(value, stream, allow_nan=False, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def progress(output: Path, stage: str, **fields) -> None:
    event = {"at": datetime.now(UTC).isoformat(), "stage": stage, **fields}
    with (output / "progress.jsonl").open("a") as stream:
        stream.write(json.dumps(event, allow_nan=False) + "\n")
        stream.flush()
    print(json.dumps(event, allow_nan=False), flush=True)


def deny_network() -> None:
    def denied(*_args, **_kwargs):
        raise PermissionError("offline policy inference forbids network access")

    def audit(event, args):
        # Orbax uses asyncio's unnamed, in-process AF_UNIX stream socketpair
        # for event-loop wakeups. It needs neither bind, connect nor a path.
        if event == "socket.__new__" and args[1:] == (
            socket.AF_UNIX,
            socket.SOCK_STREAM,
            0,
        ):
            return
        if event.startswith("socket."):
            denied()

    sys.addaudithook(audit)
    # Keep the class intact: ssl subclasses socket.socket during import.
    # INET creation, DNS and every connect/bind/sendto/sendmsg remain denied.
    # In particular, allowing an AF_UNIX constructor does not permit a path
    # connection to a local service or a datagram sent without connect().
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.bind = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied


def local_download_resolver(tokenizer: Path, checkpoint: Path, output: Path):
    """Resolve only pinned local roots; never call the cache chmod/downloader."""
    tokenizer, checkpoint, output = (
        p.resolve() for p in (tokenizer, checkpoint, output)
    )
    if not tokenizer.is_file():
        raise FileNotFoundError("local tokenizer is missing")

    def resolve(url, *, force_download=False, **_kwargs):
        if force_download:
            raise PermissionError("downloads are disabled")
        if str(url) == TOKENIZER_URI:
            return tokenizer
        if urlparse(str(url)).scheme:
            raise PermissionError("remote model assets are disabled")
        path = Path(url).resolve()
        if not (path.is_relative_to(checkpoint) or path.is_relative_to(output)):
            raise PermissionError("asset path outside checkpoint/output")
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    return resolve


def normalized_prompt(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("nonempty recorded prompt is required")
    text = text.strip().replace("_", " ").replace("\n", " ")
    if text.lower().startswith("reset:"):
        text = text[len("reset:") :].strip()
    return "Reset: " + text


def model_identity(checkpoint: Path, snapshot: Path, tokenizer: Path) -> dict:
    checkpoint, snapshot, tokenizer = (
        p.resolve() for p in (checkpoint, snapshot, tokenizer)
    )
    best = read_json(checkpoint / "best.json")
    if best.get("step") != 20000:
        raise ValueError("expected reset_r1 step 20000")
    norm = checkpoint / "assets/reset_r1/norm_stats.json"
    if sha256(norm) != best["norm_stats_sha256"]:
        raise ValueError("checkpoint normalization hash mismatch")
    prompts = sorted(best["metrics"]["val_task_mse"])
    if not prompts or any(not p.startswith("Reset: ") for p in prompts):
        raise ValueError("checkpoint prompt inventory is invalid")
    if (
        not (snapshot / "scripts/train.py").is_file()
        or not (checkpoint / "params").is_dir()
    ):
        raise FileNotFoundError("incomplete checkpoint or source snapshot")
    sources = [
        checkpoint / "best.json",
        norm,
        checkpoint / "checkpoint.sha256",
        checkpoint / "runtime/train_reset_r1.py",
        checkpoint / "runtime/reset_r1_data.py",
        tokenizer,
        Path(__file__).resolve(),
        *sorted((snapshot / "src/openpi").rglob("*.py")),
    ]
    if not (snapshot / "src/openpi/models/pi0.py").is_file():
        raise FileNotFoundError("missing policy source snapshot")
    param_files = sorted(p for p in (checkpoint / "params").rglob("*") if p.is_file())
    return {
        "checkpoint": str(checkpoint),
        "checkpoint_step": best["step"],
        "model": "reset_r1",
        "config": "reset_r1_pi05",
        "source_snapshot": str(snapshot),
        "source_sha256": {str(path): sha256(path) for path in sources},
        "trained_prompts": prompts,
        "parameter_file_count": len(param_files),
        "parameter_file_bytes": sum(p.stat().st_size for p in param_files),
        "parameter_bytes_rehashed": False,
        "weight_identity_note": "checkpoint checksum manifest recorded; array shards are read by loader, not rehashed here",
        "contract": {
            "input": "dual uint8 HWC RGB plus physical xyz/euler/commanded-gripper state7",
            "gripper": "1=open, 0=closed; command channel, not measured aperture or holding",
            "transforms": "original reset loader: pad32, lift rx, rx-minus-pi, delta6, wrap, quantile norm",
            "output": "10x7 physical absolute actions, original continuous rx branch",
            "action_horizon": 10,
            "model_action_dim": 32,
            "discrete_state_input": True,
            "noise": "explicit seeded float32 normal (10,32)",
        },
    }


def load_samples(manifest: Path, limit: int | None) -> list[dict]:
    manifest = manifest.resolve()
    data = read_json(manifest)
    samples = data.get("samples") if isinstance(data, dict) else data
    if not isinstance(samples, list) or not samples or len(samples) > 1000:
        raise ValueError("expected 1..1000 recorded samples")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be positive")
    selected, seen = [], set()
    for sample in samples[:limit]:
        if not isinstance(sample, dict) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", sample.get("sample_id", "")
        ):
            raise ValueError("invalid sample identity")
        if sample["sample_id"] in seen:
            raise ValueError("duplicate sample identity")
        seen.add(sample["sample_id"])
        normalized_prompt(sample.get("prompt"))
        path = Path(sample["npz_path"]).resolve()
        if not path.is_relative_to(manifest.parent) or not path.is_file():
            raise ValueError("sample is not inside its manifest directory")
        if sha256(path) != sample.get("npz_sha256"):
            raise ValueError("recorded sample hash mismatch")
        selected.append({**sample, "npz_path": str(path)})
    return selected


def native_observation(sample: dict):
    import numpy as np

    path = Path(sample["npz_path"])
    if sha256(path) != sample["npz_sha256"]:
        raise ValueError("sample changed during inference")
    with np.load(path, allow_pickle=False) as data:
        image, wrist, state = (
            data[k].copy() for k in ("image", "wrist_image", "state7")
        )
    for value in (image, wrist):
        if value.dtype != np.uint8 or value.ndim != 3 or value.shape[-1] != 3:
            raise ValueError("recorded image must be uint8 HWC RGB")
    if state.shape != (7,) or not np.isfinite(state).all() or not 0 <= state[6] <= 1:
        raise ValueError("recorded state must be finite xyz/euler/commanded-gripper")
    return {
        "observation.images.view1": image,
        "observation.images.hand": wrist,
        "observation.state": state.astype(np.float32),
        "prompt": normalized_prompt(sample["prompt"]),
    }


def load_policy(checkpoint: Path, snapshot: Path, tokenizer: Path, output: Path):
    # Only called in the isolated CLI process, never in the web server.
    sys.path[:0] = [str(checkpoint / "runtime"), str(snapshot / "src"), str(snapshot)]
    from openpi.shared import download

    module_path = Path(download.__file__).resolve()
    if not module_path.is_relative_to(snapshot.resolve()):
        raise RuntimeError("OpenPI import did not use the pinned source snapshot")
    download.maybe_download = local_download_resolver(tokenizer, checkpoint, output)
    from train_reset_r1 import load_reset_r1_policy

    policy = load_reset_r1_policy(
        run_dir=output / "loader", openpi_root=snapshot, checkpoint_dir=checkpoint
    )
    import jax

    return MeasuredPolicy(policy, jax)


def run_inference(
    *,
    output: Path,
    manifest: Path,
    checkpoint: Path,
    snapshot: Path,
    tokenizer: Path,
    limit: int | None,
    seeds: tuple[int, ...],
    gpu_memory_fraction: float = 0.30,
    loader=load_policy,
) -> dict:
    import numpy as np

    gpu_memory_fraction = validate_memory_fraction(gpu_memory_fraction)
    if (
        not seeds
        or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise ValueError("distinct uint32 seeds are required")
    samples = load_samples(manifest, limit)
    identity = model_identity(checkpoint, snapshot, tokenizer)
    output.mkdir(parents=True, exist_ok=True)
    # An interrupted attempt is kept for diagnosis; never silently reuse it.
    with (output / "inference.claim.json").open("x") as stream:
        json.dump({"sample_manifest": str(manifest.resolve()), "seeds": seeds}, stream)
    if (output / "reports/predictions.json").exists():
        raise FileExistsError("prediction output already exists")
    manifest_hash = sha256(manifest)
    report = {
        "schema": "levi.reset_policy.predictions.v1",
        "checkpoint": str(checkpoint.resolve()),
        "physical_execution": False,
        "closed_loop": False,
        "historical_exact_replay": False,
        "records": [],
    }
    runtime = {
        "schema": "levi.reset_policy.manifest.v1",
        **identity,
        "physical_execution": False,
        "closed_loop": False,
        "historical_exact_replay": False,
        "weights_modified": False,
        "sample_manifest": str(manifest.resolve()),
        "sample_manifest_sha256": manifest_hash,
        "seeds": list(seeds),
        "status": "loading",
        "policy_calls": 0,
        "llm_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
        "gpu_memory_fraction": gpu_memory_fraction,
        "gpu_allocator": "bfc",
        "gpu_preallocate": False,
        "gpu_memory_scope": "fraction limits the ordinary BFC pool; CUDA overhead/other allocators remain outside this bound",
        "gpu_memory_stats_source": "current process jax local_devices memory_stats only",
        "limits": [
            "recorded observations do not change in response to proposed actions",
            "no grasp success, geometry accuracy or physical improvement metric",
            "original prompts retained; unseen training strings explicitly labeled",
        ],
    }
    write_json(output / "reports/manifest.json", runtime)
    progress(output, "loading", samples=len(samples), seeds=list(seeds))
    try:
        started = time.monotonic()
        policy = loader(checkpoint, snapshot, tokenizer, output)
        runtime["load_ms"] = round((time.monotonic() - started) * 1000, 3)
        memory_measurements = [own_memory_stats(policy)]
        runtime["memory_after_load"] = memory_measurements[0]
        progress(output, "loaded", load_ms=runtime["load_ms"])
        trained = {normalized_prompt(p) for p in identity["trained_prompts"]}
        for sample in samples:
            observation = native_observation(sample)
            for seed in seeds:
                noise = (
                    np.random.default_rng(seed)
                    .standard_normal((10, 32))
                    .astype(np.float32)
                )
                runtime["policy_calls"] += 1
                progress(
                    output,
                    "inference_started",
                    sample_id=sample["sample_id"],
                    seed=seed,
                    policy_calls=runtime["policy_calls"],
                )
                before = time.monotonic()
                answer = policy.infer(observation, noise=noise)
                actions = np.asarray(answer["actions"])
                elapsed_ms = (time.monotonic() - before) * 1000
                if actions.shape != (10, 7) or not np.isfinite(actions).all():
                    raise ValueError("invalid policy action chunk")
                row = {
                    "sample_id": sample["sample_id"],
                    "seed": seed,
                    "prompt": sample["prompt"],
                    "effective_prompt": observation["prompt"],
                    "prompt_seen_in_training": observation["prompt"] in trained,
                    "actions": actions.tolist(),
                    "inference_ms": round(elapsed_ms, 3),
                    "policy_timing_ms": answer.get("policy_timing", {}).get("infer_ms"),
                    "cold_start": not report["records"],
                    "source_sample": sample,
                }
                report["records"].append(row)
                row["device_memory"] = own_memory_stats(policy)
                memory_measurements.append(row["device_memory"])
                write_json(output / "reports/predictions.partial.json", report)
                runtime["successful_predictions"] = len(report["records"])
                progress(
                    output,
                    "inference_complete",
                    sample_id=row["sample_id"],
                    seed=seed,
                    inference_ms=row["inference_ms"],
                    cold_start=row["cold_start"],
                    completed=runtime["successful_predictions"],
                    total=len(samples) * len(seeds),
                )
        if sha256(manifest) != manifest_hash:
            raise ValueError("sample manifest changed during inference")
        if (
            model_identity(checkpoint, snapshot, tokenizer)["source_sha256"]
            != identity["source_sha256"]
        ):
            raise ValueError("model source/normalization changed during inference")
        warm = [
            row["inference_ms"] for row in report["records"] if not row["cold_start"]
        ]
        runtime.update(
            status="completed",
            cold_inference_ms=report["records"][0]["inference_ms"],
            warm_median_ms=statistics.median(warm) if warm else None,
            warm_max_ms=max(warm) if warm else None,
            peak_bytes_in_use=peak_allocator_bytes(
                memory_measurements, "peak_bytes_in_use"
            ),
            peak_bytes_reserved=peak_allocator_bytes(
                memory_measurements, "peak_bytes_reserved"
            ),
        )
        write_json(output / "reports/predictions.json", report)
        write_json(output / "reports/manifest.json", runtime)
        progress(
            output,
            "completed",
            policy_calls=runtime["policy_calls"],
            warm_median_ms=runtime["warm_median_ms"],
        )
        return report
    except Exception as error:
        runtime.update(
            status="failed", error_type=type(error).__name__, error=str(error)
        )
        write_json(output / "reports/manifest.json", runtime)
        progress(output, "failed", error_type=type(error).__name__)
        raise


def isolated_environment(
    output: Path, *, gpu: bool, gpu_memory_fraction: float = 0.30
) -> dict[str, str]:
    gpu_memory_fraction = validate_memory_fraction(gpu_memory_fraction)
    cache = output / "cache"
    locations = {
        "TMPDIR": cache / "tmp",
        "XDG_CACHE_HOME": cache / "xdg",
        "HF_HOME": cache / "hf",
        "HF_HUB_CACHE": cache / "hf/hub",
        "HF_DATASETS_CACHE": cache / "hf/datasets",
        "TORCH_HOME": cache / "torch",
        "OPENPI_DATA_HOME": cache / "openpi",
        "JAX_COMPILATION_CACHE_DIR": cache / "jax",
        "CUDA_CACHE_PATH": cache / "cuda",
        "MPLCONFIGDIR": cache / "matplotlib",
        "TRITON_CACHE_DIR": cache / "triton",
    }
    for path in locations.values():
        path.mkdir(parents=True, exist_ok=True)
    return {
        **{key: str(path) for key, path in locations.items()},
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "WANDB_MODE": "disabled",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "CUDA_VISIBLE_DEVICES": "0" if gpu else "",
        "JAX_PLATFORMS": "cuda" if gpu else "cpu",
        "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
        "XLA_PYTHON_CLIENT_MEM_FRACTION": str(gpu_memory_fraction),
        "XLA_PYTHON_CLIENT_ALLOCATOR": "bfc",
        "TF_FORCE_UNIFIED_MEMORY": "false",
        "LEVI_OFFLINE_POLICY_CHILD": "1",
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--infer", action="store_true")
    parser.add_argument(
        "--output", "--output-root", dest="output", type=Path, required=True
    )
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--samples", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--openpi-root", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(SEEDS))
    parser.add_argument("--gpu-memory-fraction", type=float, default=0.30)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if args.prepare_only and args.source_root is None:
        parser.error("--prepare-only requires --source-root")
    if args.infer and any(
        p is None for p in (args.checkpoint, args.openpi_root, args.tokenizer)
    ):
        parser.error("--infer requires --checkpoint --openpi-root --tokenizer")
    readonly = (
        {"--source-root": args.source_root}
        if args.prepare_only
        else {
            "--checkpoint": args.checkpoint,
            "--openpi-root": args.openpi_root,
            "--tokenizer": args.tokenizer,
        }
    )
    # Reject aliases and both containment directions before creating caches or
    # replacing this process. Prepared samples may intentionally live in output.
    for option, path in readonly.items():
        path = path.resolve()
        if output.is_relative_to(path) or path.is_relative_to(output):
            parser.error(f"--output must be disjoint from {option}")
    if os.environ.get("LEVI_OFFLINE_POLICY_CHILD") != "1":
        environment = isolated_environment(
            output, gpu=args.infer, gpu_memory_fraction=args.gpu_memory_fraction
        )
        os.execve(
            sys.executable,
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), *argv],
            environment,
        )
    deny_network()
    if args.prepare_only:
        sys.path.insert(0, str(ROOT))
        from levi.automatic.policy_replay import prepare_samples

        samples = prepare_samples(output, source_root=args.source_root)
        print(
            json.dumps(
                {"prepared": len(samples), "manifest": str(output / "samples.json")}
            ),
            flush=True,
        )
        return 0
    run_inference(
        output=output,
        manifest=(args.samples or output / "samples.json").resolve(),
        checkpoint=args.checkpoint.resolve(),
        snapshot=args.openpi_root.resolve(),
        tokenizer=args.tokenizer.resolve(),
        limit=args.limit,
        seeds=tuple(args.seeds),
        gpu_memory_fraction=args.gpu_memory_fraction,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
