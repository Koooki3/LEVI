"""The worker's per-frame preparation equals the frozen FR3 RECAP code's.

The frozen training/labelling sources (``recap_frozen_sources.tar.gz`` of the
FR3 value r1 package: ``RLinf_train_r1`` = RLinf 807e5fdd + FR3 patches, and
``openpi``) are run directly: ``build_input_transforms(env_type="fr3_recap")``
(InjectDefaultPrompt + RLinf's LiberoInputs + PadStatesAndActions) and
``ValueCriticModel._prepare_observation_cpu``. Their jax / flax imports are
stubbed (nothing used at call time needs them), and so are openpi's
``ModelType`` enum module and, when absent, einops' one ``c h w -> h w c``.

    LEVI_RECAP_FROZEN_SRC=<folder with RLinf_train_r1/ and openpi/> \\
      integrations/recap_value/.venv/bin/python -m unittest discover tests

``LEVI_RECAP_FROZEN_PREP`` may name an ``.npz`` written by
``tools/frozen_prep.py`` (the frozen pipeline on real LeRobot frames, run in
an RLinf environment) together with ``LEVI_RECAP_FROZEN_DATA`` (the datasets'
parent folder) and ``LEVI_RECAP_CHECKPOINT`` (a checkpoint folder whose
manifest names the tokenizer): the worker's decode + preparation must then
match it exactly on those frames.
"""

from __future__ import annotations

import enum
import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path

import numpy as np

FROZEN = os.environ.get("LEVI_RECAP_FROZEN_SRC")


class _Anything:
    """Permissive stand-in: attributes, calls, subscripts and decorators."""

    def __call__(self, *args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return _Anything()

    def __getattr__(self, name):
        return _Anything()

    def __getitem__(self, item):
        return _Anything()

    def __or__(self, other):
        return _Anything()

    __ror__ = __or__

    def __mro_entries__(self, bases):
        return (object,)


def _stub(name: str, **attrs):
    module = types.ModuleType(name)
    module.__getattr__ = lambda attr: _Anything()  # type: ignore[attr-defined]
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class ModelType(enum.Enum):  # openpi.models.model.ModelType (frozen values)
    PI0 = "pi0"
    PI0_FAST = "pi0_fast"
    PI05 = "pi05"


def _frozen_modules():
    """Import the frozen transform code with light stubs for jax & co."""
    root = Path(FROZEN)
    sys.path[:0] = [str(root / "RLinf_train_r1"), str(root / "openpi/src")]
    for name in (
        "jax",
        "jax.numpy",
        "flax",
        "flax.traverse_util",
        "openpi_client",
        "openpi_client.image_tools",
        "openpi.models.tokenizer",
        "openpi.shared.array_typing",
        "openpi.shared.normalize",
    ):
        _stub(name)
    _stub("openpi.models.model", ModelType=ModelType)

    def rearrange(x, pattern):  # the one einops call in LiberoInputs._parse_image
        assert pattern == "c h w -> h w c", pattern
        return np.transpose(x, (1, 2, 0))

    if importlib.util.find_spec("einops") is None:
        _stub("einops", rearrange=rearrange)
    transforms = importlib.import_module("openpi.transforms")
    # rlinf.models.embodiment.openpi.policies/__init__ may pull more; load the
    # two policy modules from their files.
    policies = root / "RLinf_train_r1/rlinf/models/embodiment/openpi/policies"
    loaded = {}
    for mod in ("libero_policy", "franka_policy"):
        spec = importlib.util.spec_from_file_location(
            f"rlinf.models.embodiment.openpi.policies.{mod}", policies / f"{mod}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 -- franka_policy is not needed
            if mod == "libero_policy":
                raise
        loaded[mod] = module
    pkg = _stub("rlinf.models.embodiment.openpi.policies", **loaded)
    pkg.__path__ = [str(policies)]
    recap = root / "RLinf_train_r1/rlinf/models/embodiment/value_model/recap"
    spec = importlib.util.spec_from_file_location("frozen_checkpoint_utils", recap / "checkpoint_utils.py")
    ckpt_utils = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ckpt_utils)
    return transforms, ckpt_utils


class _Tokenizer:
    """Deterministic stand-in shared by both sides (tokenisation itself is
    compared on real frames with the real Gemma3 tokenizer below)."""

    def encode(self, text, add_special_tokens=True):
        return ([2] if add_special_tokens else []) + [3 + (ord(c) % 250) for c in text]


@unittest.skipUnless(FROZEN and Path(FROZEN).is_dir(), "LEVI_RECAP_FROZEN_SRC is not set")
class FrozenTransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.transforms, cls.ckpt_utils = _frozen_modules()
        root = Path(FROZEN) / "RLinf_train_r1/rlinf/models/embodiment/value_model/recap"
        # The frozen static preparation, from the frozen file.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from levi_recap_worker import rlinf_provider
        from levi_recap_worker.rlinf.modeling_critic import ValueCriticModel
        from levi_recap_worker.rlinf.processing import ValueProcessor

        cls.provider = rlinf_provider
        cls.levi_prepare_cpu = staticmethod(ValueCriticModel._prepare_observation_cpu)
        text = (root / "modeling_critic.py").read_text()
        cls.frozen_is_vendored = "def _prepare_observation_cpu" in text
        cls.processor = ValueProcessor(tokenizer=_Tokenizer(), max_token_len=200)

    def _frozen(self, obs, model_type):
        chain = self.transforms.compose(
            self.ckpt_utils.build_input_transforms(
                env_type="fr3_recap",
                model_type=model_type,
                action_dim=7,
                default_prompt=None,
                norm_stats=None,
                use_quantile_norm=model_type != "pi0",
            )
        )
        return chain({k: v.copy() if isinstance(v, np.ndarray) else v for k, v in obs.items()})

    def _levi(self, view1_hwc, hand_hwc, prompt, model_type):
        critic = self.provider.Critic.__new__(self.provider.Critic)
        critic.manifest = {"model_type": model_type}
        critic.processor = self.processor
        critic.model = types.SimpleNamespace(_prepare_observation_cpu=self.levi_prepare_cpu)
        images = {
            "base_0_rgb": self.provider.lerobot_roundtrip(view1_hwc),
            "left_wrist_0_rgb": self.provider.lerobot_roundtrip(hand_hwc),
        }
        return critic.prepare(images, prompt), images

    def test_fr3_recap_transform_equals_the_worker(self):
        self.assertTrue(self.frozen_is_vendored)
        rng = np.random.default_rng(3)
        for model_type in ("pi05", "pi0_fast", "pi0"):
            for h, w in ((480, 640), (224, 224), (360, 640)):
                view1 = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
                hand = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
                prompt = "Stack the plates of same color together."
                # compute_advantages.build_obs: LeRobot float32 CHW in [0, 1].
                obs = {
                    "observation/image": (view1.transpose(2, 0, 1).astype(np.float32) / 255),
                    "observation/wrist_image": (hand.transpose(2, 0, 1).astype(np.float32) / 255),
                    "observation/state": np.zeros(7, np.float32),
                    "prompt": prompt,
                }
                # LeRobot divides a torch uint8 tensor; match that exactly.
                import torch

                obs["observation/image"] = (torch.from_numpy(view1).permute(2, 0, 1).float() / 255).numpy()
                obs["observation/wrist_image"] = (torch.from_numpy(hand).permute(2, 0, 1).float() / 255).numpy()
                frozen = self._frozen(obs, model_type)
                levi, images = self._levi(view1, hand, prompt, model_type)
                self.assertEqual(list(frozen["image"]), list(self.provider.SLOTS))
                np.testing.assert_array_equal(frozen["image"]["base_0_rgb"], images["base_0_rgb"])
                np.testing.assert_array_equal(frozen["image"]["left_wrist_0_rgb"], images["left_wrist_0_rgb"])
                expected = self.levi_prepare_cpu(frozen, self.processor)
                for key in ("images", "image_masks"):
                    self.assertEqual(list(expected[key]), list(levi[key]))
                    for cam in expected[key]:
                        np.testing.assert_array_equal(expected[key][cam].numpy(), levi[key][cam].numpy())
                for key in ("tokenized_prompt", "tokenized_prompt_mask"):
                    np.testing.assert_array_equal(expected[key].numpy(), levi[key].numpy())
                self.assertEqual(
                    bool(levi["image_masks"]["right_wrist_0_rgb"][0]), model_type == "pi0_fast"
                )


PREP = os.environ.get("LEVI_RECAP_FROZEN_PREP")


@unittest.skipUnless(PREP and Path(PREP).is_file(), "LEVI_RECAP_FROZEN_PREP is not set")
class FrozenRealFrameTests(unittest.TestCase):
    def test_real_frames_match_the_frozen_pipeline(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from levi_recap_worker import rlinf_provider as rp
        from levi_recap_worker.rlinf.modeling_critic import ValueCriticModel
        from levi_recap_worker.rlinf.processing import ValueProcessor

        ckpt = Path(os.environ["LEVI_RECAP_CHECKPOINT"])
        data = Path(os.environ["LEVI_RECAP_FROZEN_DATA"])
        manifest = json.loads((ckpt / "manifest.json").read_text())
        processor = ValueProcessor(
            tokenizer=rp.tokenizer_for(manifest, ckpt), max_token_len=int(manifest["max_token_len"])
        )
        critic = rp.Critic.__new__(rp.Critic)
        critic.manifest, critic.processor = manifest, processor
        critic.model = types.SimpleNamespace(_prepare_observation_cpu=ValueCriticModel._prepare_observation_cpu)
        ref = np.load(PREP)
        wanted: dict = {}
        for key in ref.files:
            if key.endswith("/tokens"):
                name, ep, frame, _ = key.split("/")
                wanted.setdefault((name, int(ep)), set()).add(int(frame))
        checked = 0
        for (name, ep), frames in sorted(wanted.items()):
            info = json.loads((data / name / "meta/info.json").read_text())
            tasks = {}
            for line in (data / name / "meta/tasks.jsonl").read_text().splitlines():
                row = json.loads(line)
                tasks[str(row["task_index"])] = row["task"]
            plan = {"dataset": {"root": str(data / name), "data_path": info["data_path"],
                                "video_path": info["video_path"], "chunks_size": info["chunks_size"],
                                "tasks": tasks}}
            for pos, (_fi, task, images) in enumerate(rp.episode_frames(plan, ep, manifest["views"])):
                if pos not in frames:
                    continue
                key = f"{name}/{ep}/{pos}"
                prep = critic.prepare(images, task)
                np.testing.assert_array_equal(images["base_0_rgb"], ref[f"{key}/uint8/base"])
                np.testing.assert_array_equal(images["left_wrist_0_rgb"], ref[f"{key}/uint8/left"])
                for cam, tensor in prep["images"].items():
                    np.testing.assert_array_equal(tensor.numpy(), ref[f"{key}/img/{cam}"])
                for cam, tensor in prep["image_masks"].items():
                    np.testing.assert_array_equal(tensor.numpy(), ref[f"{key}/mask/{cam}"])
                np.testing.assert_array_equal(prep["tokenized_prompt"].numpy(), ref[f"{key}/tokens"])
                np.testing.assert_array_equal(prep["tokenized_prompt_mask"].numpy(), ref[f"{key}/tokmask"])
                self.assertEqual(task, str(ref[f"{key}/prompt"]))
                checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
