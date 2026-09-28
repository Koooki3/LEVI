"""Reading a LEVI training manifest from a trainer (openpi, RLinf, …).

Standalone: numpy and pyarrow only, no LEVI import, so a training repository
can vendor this one file. It never changes the dataset; it tells a data
loader which frames to draw, how often, and which action targets count in
the loss. See LEVI docs/TRAINING_MANIFEST.md for the format.

Typical use in a LeRobot-based loader (openpi ``create_torch_dataset``)::

    m = ManifestFrames.open("/path/to/manifest_dir", dataset_root)
    episodes = m.episodes()                     # LeRobotDataset(episodes=…)
    weights = m.sampling_weights(dataset)       # WeightedRandomSampler
    mask = m.action_mask(ep, frame, horizon)    # [H] bool per action target
    prompt = m.prompt(task, ep, frame, rng)     # RECAP CFG conditioning

and in the loss: ``sum(mask * loss) / max(sum(mask), 1)`` instead of
``mean(loss)`` (openpi train.py takes ``jnp.mean`` over [B, H]).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

SCHEMA = "levi.training_manifest.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class ManifestFrames:
    """A manifest's frames, indexed by (episode_index, frame_index)."""

    def __init__(self, manifest: dict, table):
        self.manifest = manifest
        cols = table.to_pydict()
        self.episode = np.asarray(cols["episode_index"], dtype=np.int64)
        self.frame = np.asarray(cols["frame_index"], dtype=np.int64)
        self.include = np.asarray(cols["include"], dtype=bool)
        self.weight = np.asarray(cols["weight"], dtype=np.float64)
        self.positive = cols["recap_positive"]
        self._row = {
            (int(e), int(f)): i
            for i, (e, f) in enumerate(zip(self.episode, self.frame))
        }

    @classmethod
    def open(cls, directory, dataset_root=None, *, check_dataset=True):
        """Load and verify: the frames file matches its manifest, and (when
        ``dataset_root`` is given) the dataset's metadata matches the one the
        manifest was written for."""
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest.get("schema") != SCHEMA:
            raise ValueError(f"not a {SCHEMA} manifest")
        frames = directory / "frames.parquet"
        if _sha256(frames) != manifest["files"]["frames.parquet"]["sha256"]:
            raise ValueError("frames.parquet does not match manifest.json")
        if dataset_root is not None and check_dataset:
            expected = manifest["dataset"]["fingerprint"]["meta_sha256"]
            for name, digest in expected.items():
                path = Path(dataset_root) / "meta" / name
                if not path.is_file() or _sha256(path) != digest:
                    raise ValueError(
                        f"{path} differs from the dataset the manifest describes"
                    )
        return cls(manifest, pq.read_table(frames))

    # -- which episodes / frames --------------------------------------------

    def episodes(self) -> list[int]:
        """Episodes with at least one included frame (load only these)."""
        return sorted({int(e) for e in self.episode[self.include]})

    def included(self, episode: int, frame: int) -> bool:
        i = self._row.get((int(episode), int(frame)))
        return bool(i is not None and self.include[i])

    def sampling_weights(self, keys) -> np.ndarray:
        """One weight per dataset item, in the loader's order. ``keys`` is an
        iterable of (episode_index, frame_index); a frame the manifest does
        not list gets 0 (it is never drawn)."""
        out = np.zeros(len(keys), dtype=np.float64)
        for n, (e, f) in enumerate(keys):
            i = self._row.get((int(e), int(f)))
            if i is not None and self.include[i]:
                out[n] = self.weight[i]
        return out

    def action_mask(self, episode: int, frame: int, horizon: int) -> np.ndarray:
        """Which of the ``horizon`` action targets from ``frame`` count in the
        loss: target k is frame+k of the same episode, and counts when that
        frame exists and is included. Padding past the episode end is False
        (openpi's ``action_is_pad``); nothing is concatenated across a gap."""
        return np.array(
            [self.included(episode, frame + k) for k in range(horizon)], dtype=bool
        )

    # -- RECAP classifier-free-guidance conditioning -------------------------

    def prompt(
        self,
        task: str,
        episode: int,
        frame: int,
        rng: np.random.Generator,
        *,
        p_conditioned: float = 0.9,
    ) -> str:
        """RLinf's ``positive_only_conditional``: a positive frame carries
        ``"\\nAdvantage: positive"`` with probability ``p_conditioned``;
        negative and unlabelled frames stay unconditioned. At inference the
        policy is prompted with the positive suffix."""
        i = self._row.get((int(episode), int(frame)))
        if i is not None and self.positive[i] is True and rng.random() < p_conditioned:
            return task + "\nAdvantage: positive"
        return task


def audit(manifest: ManifestFrames, drawn) -> dict:
    """Did the loader honour the manifest? ``drawn`` is a list of
    (episode_index, frame_index) the sampler actually produced. Excluded
    frames must never appear; per-episode draw shares should follow weight
    mass (compare ``observed`` with ``expected``)."""
    drawn = list(drawn)
    excluded = [k for k in drawn if not manifest.included(*k)]
    mass: dict[int, float] = {}
    for e, w, inc in zip(manifest.episode, manifest.weight, manifest.include):
        if inc:
            mass[int(e)] = mass.get(int(e), 0.0) + float(w)
    total = sum(mass.values()) or 1.0
    counts: dict[int, int] = {}
    for e, _ in drawn:
        counts[int(e)] = counts.get(int(e), 0) + 1
    return {
        "drawn": len(drawn),
        "excluded_drawn": len(excluded),
        "expected": {e: m / total for e, m in sorted(mass.items())},
        "observed": {e: c / max(len(drawn), 1) for e, c in sorted(counts.items())},
    }
