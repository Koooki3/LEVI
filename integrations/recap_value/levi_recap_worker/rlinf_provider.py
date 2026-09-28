"""RLinf RECAP critic inference, faithful to ``compute_advantages.py``.

The model code is RLinf's own (vendored in ``rlinf/``, commit 807e5fd); this
module only replaces what RLinf gets from LeRobot and openpi:

- frames: each episode's videos decoded in order with PyAV (RGB24), turned
  into LeRobot's float CHW in [0, 1] (``uint8 / 255``) and back into uint8
  HWC by openpi's ``_parse_image`` (``(255 * x).astype(uint8)``);
- cameras: the manifest's ``views`` fill ``base_0_rgb`` /
  ``left_wrist_0_rgb`` / ``right_wrist_0_rgb`` the way LiberoInputs and
  FrankaEEInputs do (a missing camera is a zero image, its mask off for
  ``model_type`` pi0 / pi05 and on for pi0_fast);
- prompt: the frame's task from ``meta/tasks.jsonl`` (compute_advantages'
  ``KEY_MAPPINGS`` map LeRobot's ``task`` to ``prompt``).

The FR3 RECAP run (``env_type`` ``fr3_recap``, RLinf 807e5fdd plus local
FR3 patches) routes ``fr3_recap`` through the libero branch of
``build_input_transforms`` (InjectDefaultPrompt(None) + LiberoInputs) with
``observation.images.view1`` -> base and ``observation.images.hand`` -> left
wrist; its manifest preset (``levi recap import --preset fr3_recap``) states
exactly that, and ``action_dim`` / ``action_horizon`` are passed to the
config as its patched ``from_checkpoint`` does.

Everything after that — resize-with-pad to 224, [-1, 1], the ``Task: {p}.``
prompt, tokenisation and the two-stage forward — is RLinf's
``ValueCriticModel._prepare_observation_cpu`` and ``infer_batch``. State is
not an input of the value model.
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

RLINF_COMMIT = "807e5fd"
SLOTS = ("base_0_rgb", "left_wrist_0_rgb", "right_wrist_0_rgb")
# RLinf's openpi variants (checkpoint_utils.build_input_transforms). The
# variant decides the mask of a padded (absent) camera: LiberoInputs and
# FrankaEEInputs mask padding images for pi0 / pi05 but not for pi0-FAST.
MODEL_TYPES = ("pi0", "pi05", "pi0_fast")


def padding_mask(manifest: dict) -> bool:
    """The image mask RLinf gives a zero-padded camera slot."""
    model_type = str(manifest.get("model_type") or "pi05").lower()
    if model_type not in MODEL_TYPES:
        raise ValueError(f"model_type {model_type!r} is not one of {MODEL_TYPES}")
    return model_type == "pi0_fast"
# Unused by the value forward (the value comes from the expert's hidden state
# through ValueHead); tied to the embeddings in HF. Reported, not fatal.
UNUSED_KEYS = re.compile(r"(^|\.)lm_head\.weight$")


# ---------------------------------------------------------------- environment


def patch_applied() -> bool:
    """openpi's transformers_replace is in place (RLinf's installer copies it
    over transformers; setup.sh does the same)."""
    from transformers.models.gemma import modeling_gemma

    params = inspect.signature(modeling_gemma.GemmaRMSNorm.__init__).parameters
    try:
        from transformers.models.siglip import check  # noqa: F401
    except ImportError:
        return False
    return "cond_dim" in params


def environment() -> dict:
    import torch
    import transformers

    return {
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "patch_applied": patch_applied(),
        "rlinf_commit": RLINF_COMMIT,
        "cuda_built": torch.version.cuda,
    }


def require_patch() -> None:
    import transformers

    if transformers.__version__ != "4.53.2" or not patch_applied():
        raise RuntimeError(
            "The worker needs transformers 4.53.2 with openpi's transformers_replace "
            "patch; run integrations/recap_value/setup.sh"
        )


# ---------------------------------------------------------------- weights


def load_state_dict(path: Path) -> dict:
    import torch

    try:
        state = torch.load(str(path), map_location="cpu", weights_only=True, mmap=True)
    except Exception as exc:
        raise ValueError(
            f"{path} is not a plain tensor state dict readable with "
            f"weights_only=True: {exc}"
        ) from exc
    if not isinstance(state, dict) or not state:
        raise ValueError(f"{path} does not hold a state dict")
    return state


def strip_model_prefix(state: dict, model_keys: set) -> dict:
    """RLinf ``_strip_model_prefix``: drop a wrapper's ``model.`` prefix when
    no key matches the model as saved."""
    if len(model_keys & set(state)) == 0:
        stripped = {
            k.removeprefix("model."): v
            for k, v in state.items()
            if k.startswith("model.")
        }
        if len(set(stripped) & model_keys) > 0:
            return stripped
    return state


def infer_variant(state: dict) -> dict:
    """The value expert's size from its weight shapes (RLinf get_config)."""
    from .rlinf.configuration import get_config

    keys = list(state)
    prefix = None
    for key in keys:
        match = re.match(
            r"(.*experts\.value\.model\.)layers\.0\.self_attn\.q_proj\.weight$", key
        )
        if match:
            prefix = match.group(1)
            break
    if prefix is None:
        return {"inferred_variant": None, "reason": "no experts.value layers found"}
    q = state[prefix + "layers.0.self_attn.q_proj.weight"]
    k = state[prefix + "layers.0.self_attn.k_proj.weight"]
    gate = state[prefix + "layers.0.mlp.gate_proj.weight"]
    depth = len(
        {
            m.group(1)
            for key in keys
            if (m := re.match(re.escape(prefix) + r"layers\.(\d+)\.", key))
        }
    )
    width = int(q.shape[1])
    shape = {
        "width": width,
        "depth": depth,
        "mlp_dim": int(gate.shape[0]),
        "q_out": int(q.shape[0]),
        "kv_out": int(k.shape[0]),
    }
    match = None
    for variant in (
        "dummy",
        "gemma_1m",
        "gemma_50m",
        "gemma_100m",
        "gemma_150m",
        "gemma_300m",
        "gemma_2b",
    ):
        c = get_config(variant)
        if (
            c.width == width
            and c.depth == depth
            and c.mlp_dim == shape["mlp_dim"]
            and c.num_heads * c.head_dim == shape["q_out"]
            and c.num_kv_heads * c.head_dim == shape["kv_out"]
        ):
            match = variant
            break
    return {"inferred_variant": match, "expert_shape": shape}


def inspect_weights(path: Path) -> dict:
    state = load_state_dict(Path(path))
    dtypes: dict[str, int] = {}
    params = 0
    for value in state.values():
        dtypes[str(value.dtype)] = dtypes.get(str(value.dtype), 0) + 1
        params += int(value.numel())
    prefixed = all(k.startswith("model.") for k in state)
    probe = {k.removeprefix("model.") if prefixed else k: v for k, v in state.items()}
    return {
        "keys": len(state),
        "parameters": params,
        "dtypes": dtypes,
        "model_prefix": prefixed,
        **infer_variant(probe),
    }


# ---------------------------------------------------------------- model


def base_path(ckpt_dir: Path, value: str | None, what: str) -> str:
    if not value:
        raise ValueError(f"manifest base_models.{what} is not set")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ckpt_dir / path
    if not path.is_dir():
        raise ValueError(f"base_models.{what}: {path} is not a folder")
    return str(path)


def build_model(manifest: dict, ckpt_dir: Path):
    """RLinf ``get_model`` for inference: ValueCriticConfig from the
    manifest, SigLIP2 + Gemma3 from the base-model folders, bf16 with RLinf's
    fp32 exceptions (ValueExpert._apply_precision)."""
    from .rlinf.configuration import ValueCriticConfig
    from .rlinf.modeling_critic import ValueCriticModel

    base = manifest["base_models"]
    config = ValueCriticConfig(
        critic_expert_variant=manifest["critic_expert_variant"],
        num_bins=int(manifest["num_bins"]),
        v_min=float(manifest["v_min"]),
        v_max=float(manifest["v_max"]),
        value_dropout=0.0,
        siglip_path=base_path(ckpt_dir, base.get("siglip"), "siglip"),
        gemma3_path=base_path(ckpt_dir, base.get("gemma3"), "gemma3"),
        dtype=manifest["precision"],
        max_token_len=int(manifest["max_token_len"]),
        # Config-only in the value forward, but RLinf's from_checkpoint (as
        # patched in the FR3 run) passes them to get_model; keep the config equal.
        action_dim=int(manifest.get("action_dim") or 32),
        action_horizon=int(manifest.get("action_horizon") or 50),
    )
    return ValueCriticModel(config)


def load_weights(model, weights: Path) -> dict:
    """Strict load: every model key must come from the checkpoint and every
    checkpoint key must be used (only unused lm_head weights may be absent)."""
    state = load_state_dict(weights)
    model_keys = set(model.state_dict().keys())
    state = strip_model_prefix(state, model_keys)
    missing = sorted(model_keys - set(state))
    unexpected = sorted(set(state) - model_keys)
    tolerated = [k for k in missing if UNUSED_KEYS.search(k)]
    fatal = [k for k in missing if k not in tolerated]
    if fatal or unexpected:
        raise ValueError(
            f"checkpoint does not match the model built from the manifest "
            f"({len(fatal)} missing, {len(unexpected)} unexpected keys). "
            f"Missing: {fatal[:15]} Unexpected: {unexpected[:15]} — check "
            "critic_expert_variant and the base-model configs"
        )
    model.load_state_dict(state, strict=False)
    return {"keys": len(state), "missing_unused": tolerated}


def strict_report(ckpt_dir: Path) -> dict:
    """Build the model the manifest describes (CPU) and load the weights
    with the same strict rule as a job: RLinf's ``missing`` / ``unexpected``
    counts (the FR3 advantage job logged 0 / 0) plus the load report."""
    import json

    require_patch()
    ckpt_dir = Path(ckpt_dir)
    manifest = json.loads((ckpt_dir / "manifest.json").read_text())
    model = build_model(manifest, ckpt_dir)
    state = strip_model_prefix(
        load_state_dict(ckpt_dir / manifest["weights"]),
        set(model.state_dict().keys()),
    )
    missing, unexpected = model.load_state_dict(state, strict=False)
    report = {
        "missing": len(missing),
        "unexpected": len(unexpected),
        "missing_keys": list(missing)[:20],
        "unexpected_keys": list(unexpected)[:20],
        "model_parameters": int(sum(p.numel() for p in model.parameters())),
    }
    report["ok"] = report["missing"] == 0 and report["unexpected"] == 0
    return report


def tokenizer_for(manifest: dict, ckpt_dir: Path):
    from transformers import AutoTokenizer

    path = base_path(ckpt_dir, manifest["base_models"].get("tokenizer"), "tokenizer")
    # RLinf loads the Hub folder, which ships tokenizer.json. A folder with
    # only the SentencePiece model (tokenizer.model) would otherwise be
    # converted to a fast tokenizer, which needs protobuf; the slow
    # SentencePiece tokenizer gives the same ids without it. (LEVI change.)
    fast = (Path(path) / "tokenizer.json").is_file()
    return AutoTokenizer.from_pretrained(
        path, add_bos_token=True, local_files_only=True, use_fast=fast
    )


def resolve_device(requested: str) -> str:
    import torch

    if requested in ("auto", None):
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available to the worker")
    return requested


class Critic:
    """A loaded ValueCriticModel with RLinf's processor attached."""

    def __init__(self, model, tokenizer, manifest: dict, device: str):
        from .rlinf.processing import ValueProcessor

        self.manifest = manifest
        self.device = device
        self.processor = ValueProcessor(
            tokenizer=tokenizer, max_token_len=int(manifest["max_token_len"])
        )
        model.processor = self.processor
        model._device = device
        self.model = model.to(device)
        self.model.eval()

    def prepare(self, images: dict, prompt: str) -> dict:
        """One frame through RLinf's CPU preparation (``images`` holds uint8
        HWC arrays per filled slot)."""
        base = images["base_0_rgb"]
        pad_mask = np.bool_(padding_mask(self.manifest))
        slots, masks = {}, {}
        for slot in SLOTS:
            if images.get(slot) is not None:
                slots[slot], masks[slot] = images[slot], np.True_
            else:
                slots[slot], masks[slot] = np.zeros_like(base), pad_mask
        return self.model._prepare_observation_cpu(
            {"image": slots, "image_mask": masks, "prompt": prompt}, self.processor
        )

    def values(self, prepared: list[dict], batch_size: int) -> list[float]:
        results = self.model.infer_batch(
            prepared,
            batch_size=batch_size,
            pretransformed=True,
            already_cpu_prepared=True,
        )
        return [r["value"] for r in results]


def load_critic(manifest: dict, ckpt_dir: Path, device: str, tokenizer=None):
    import torch

    require_patch()
    torch.manual_seed(0)
    model = build_model(manifest, ckpt_dir)
    report = load_weights(model, ckpt_dir / manifest["weights"])
    tokenizer = (
        tokenizer if tokenizer is not None else tokenizer_for(manifest, ckpt_dir)
    )
    return Critic(model, tokenizer, manifest, device), report


# ---------------------------------------------------------------- frames


def lerobot_roundtrip(frame_hwc: np.ndarray) -> np.ndarray:
    """uint8 HWC → LeRobot float32 CHW in [0, 1] → openpi ``_parse_image``."""
    import torch

    chw = torch.from_numpy(frame_hwc).permute(2, 0, 1).to(torch.float32) / 255
    image = chw.numpy()
    image = (255 * image).astype(np.uint8)
    return np.ascontiguousarray(image.transpose(1, 2, 0))


def decode(path: Path):
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for frame in container.decode(stream):
            yield frame.to_ndarray(format="rgb24")


def episode_frames(plan: dict, episode: int, views: dict):
    """Yield (frame_index, task, {slot: uint8 HWC}) for one episode."""
    ds = plan["dataset"]
    chunk = int(ds["chunks_size"])
    root = Path(ds["root"])
    data = pq.read_table(
        root
        / ds["data_path"].format(episode_chunk=episode // chunk, episode_index=episode),
        columns=["frame_index", "task_index"],
    )
    frames = data.column("frame_index").to_numpy().astype(np.int64)
    task_index = data.column("task_index").to_numpy().astype(np.int64)
    if not np.array_equal(frames, np.arange(len(frames))):
        raise ValueError(f"episode {episode}: frame_index is not 0..n-1")
    tasks = {int(k): v for k, v in ds["tasks"].items()}
    decoders = {
        slot: decode(
            root
            / ds["video_path"].format(
                episode_chunk=episode // chunk, episode_index=episode, video_key=key
            )
        )
        for slot, key in views.items()
        if key
    }
    for i, frame_index in enumerate(frames):
        images = {}
        for slot, decoder in decoders.items():
            try:
                images[slot] = lerobot_roundtrip(next(decoder))
            except StopIteration:
                raise ValueError(
                    f"episode {episode}: {views[slot]} has only {i} frames for "
                    f"{len(frames)} rows"
                ) from None
        task = tasks.get(int(task_index[i]))
        if task is None:
            raise ValueError(f"task_index {task_index[i]} is not in meta/tasks.jsonl")
        yield int(frame_index), task, images


# ---------------------------------------------------------------- run


def _infer(critic: Critic, pool, pending: list, batch_size: int) -> list[float]:
    """CPU preparation in threads, then one batched forward."""
    prepared = list(pool.map(lambda item: critic.prepare(*item), pending))
    return critic.values(prepared, batch_size)


def run_episodes(critic: Critic, plan: dict, progress, batch_size: int) -> dict:
    views = critic.manifest["views"]
    out = {}
    done = 0
    workers = max(1, min(8, (os.cpu_count() or 2) // 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in plan["episodes"]:
            ep, n = int(item["episode_index"]), int(item["length"])
            # Static-filtered runs label only the kept rows; every frame is
            # still decoded (sequential decode), the rest are not inferred.
            keep = set(item["keep"]) if "keep" in item else None
            frame_ids, values, pending = [], [], []
            for position, (frame_index, task, images) in enumerate(
                episode_frames(plan, ep, views)
            ):
                if keep is not None and position not in keep:
                    continue
                frame_ids.append(frame_index)
                pending.append((images, task))
                if len(pending) >= batch_size:
                    values.extend(_infer(critic, pool, pending, batch_size))
                    pending = []
                    if progress is not None:
                        progress.values(done + len(values))
            if pending:
                values.extend(_infer(critic, pool, pending, batch_size))
            wanted = n if keep is None else len(keep)
            if len(values) != wanted:
                raise ValueError(
                    f"episode {ep}: {len(values)} values for {wanted} frames"
                )
            out[ep] = (np.asarray(frame_ids, np.int64), np.asarray(values, np.float32))
            done += wanted
            if progress is not None:
                progress.values(done)
    return out


def values(plan: dict, progress, tokenizer=None) -> tuple[dict, dict]:
    import torch
    import transformers

    manifest = plan["checkpoint"]["manifest"]
    ckpt_dir = Path(plan["checkpoint"]["dir"])
    device = resolve_device(plan.get("device", "auto"))
    batch_size = int(plan.get("batch_size", 32))
    started = time.time()
    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()
    critic, report = load_critic(manifest, ckpt_dir, device, tokenizer=tokenizer)
    loaded = time.time() - started
    out = run_episodes(critic, plan, progress, batch_size)
    provenance = {
        "provider": "rlinf",
        "rlinf_commit": RLINF_COMMIT,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "transformers_patch": "openpi transformers_replace (rlinf-openpi 0.1.1)",
        "device": device,
        "precision": manifest["precision"],
        "batch_size": batch_size,
        "decoder": "pyav rgb24, sequential, frame i = row frame_index i",
        "views": manifest["views"],
        "env_type": manifest.get("env_type"),
        "model_type": manifest.get("model_type") or "pi05",
        "action_dim": manifest.get("action_dim"),
        "action_horizon": manifest.get("action_horizon"),
        "max_token_len": manifest["max_token_len"],
        "critic_expert_variant": manifest["critic_expert_variant"],
        "weights_sha256": manifest.get("sha256"),
        "load": report,
        "load_seconds": round(loaded, 3),
    }
    if device.startswith("cuda"):
        provenance["peak_vram_mib"] = round(torch.cuda.max_memory_allocated() / 2**20)
    return out, provenance
