"""Dump the frozen FR3 RECAP pipeline's per-frame preparation (no model).

Runs compute_advantages' own LeRobot loading (pyav) → build_obs →
from_checkpoint's input transforms → ValueCriticModel._prepare_observation_cpu
from the frozen sources, on a few frames per episode, and saves the tensors
for tests/test_fr3_preprocessing.py. Needs an RLinf environment (lerobot
0.3.x, openpi deps, transformers 4.53.2), not this worker's:

    python tools/frozen_prep.py --frozen-src SRC --data DATA_PARENT \\
        --tokenizer GEMMA_DIR --samples '{"rollouts_recap": [0, 1]}' --out prep.npz
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen-src", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--samples", required=True, help='{"dataset": [episode, …]}')
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    rlinf = args.frozen_src / "RLinf_train_r1"
    sys.path[:0] = [str(rlinf), str(args.frozen_src / "openpi/src")]
    spec = importlib.util.spec_from_file_location(
        "ca", rlinf / "examples/offline_rl/advantage_labeling/recap/process/compute_advantages.py"
    )
    ca = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ca)
    from openpi.transforms import compose
    from rlinf.models.embodiment.value_model.recap.checkpoint_utils import build_input_transforms
    from rlinf.models.embodiment.value_model.recap.processing import ValueProcessor
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.tokenizer, add_bos_token=True, local_files_only=True)
    processor = ValueProcessor(tokenizer=tok, max_token_len=200)  # from_checkpoint's value
    transform = compose(
        build_input_transforms(env_type="fr3_recap", model_type="pi05", action_dim=7,
                               default_prompt=None, norm_stats=None, use_quantile_norm=True)
    )
    dumps = {}
    for name, episodes in json.loads(args.samples).items():
        path = args.data / name
        ds = ca.LeRobotDataset(str(path), download_videos=False, video_backend="pyav")
        ds.hf_dataset.set_transform(ca.decode_image_struct_batch)
        tasks = {}
        for line in open(path / "meta/tasks.jsonl"):
            row = json.loads(line)
            tasks[row["task_index"]] = row["task"]
        starts, ends = ca.episode_boundaries(ds)
        for ep in episodes:
            n = ends[ep] - starts[ep]
            for f in sorted({0, 1, n // 3, n // 2, n - 2, n - 1}):
                sample = ds[starts[ep] + f]
                obs = ca.build_obs(sample, "fr3_recap", tasks)
                tr = transform({k: v.copy() if isinstance(v, np.ndarray) else v for k, v in obs.items()})
                prep = ca.ValueCriticModel._prepare_observation_cpu(tr, processor)
                key = f"{name}/{ep}/{f}"
                for cam, t in prep["images"].items():
                    dumps[f"{key}/img/{cam}"] = t.numpy()
                for cam, t in prep["image_masks"].items():
                    dumps[f"{key}/mask/{cam}"] = t.numpy()
                dumps[f"{key}/tokens"] = prep["tokenized_prompt"].numpy()
                dumps[f"{key}/tokmask"] = prep["tokenized_prompt_mask"].numpy()
                dumps[f"{key}/uint8/base"] = tr["image"]["base_0_rgb"]
                dumps[f"{key}/uint8/left"] = tr["image"]["left_wrist_0_rgb"]
                dumps[f"{key}/prompt"] = np.array(tr["prompt"])
    np.savez_compressed(args.out, **dumps)
    print(f"{len(dumps)} arrays -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
