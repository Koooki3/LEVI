# RECAP value worker for LEVI

Computes V(o_t) for every frame of a dataset with a RECAP value model
(RLinf's `ValueCriticModel`) in its own uv environment, so LEVI's core never
imports Torch. LEVI writes a plan, starts this worker, and turns the values
into advantage labels itself (see [docs/RECAP.md](../../docs/RECAP.md)).

## Install

```bash
integrations/recap_value/setup.sh
export LEVI_RECAP_VALUE_WORKER_PYTHON="$PWD/integrations/recap_value/.venv/bin/python"
```

`setup.sh` runs `uv sync` (Python 3.12, Torch from the CUDA 12.8 index like the
SAM3 worker, `transformers==4.53.2`, `tokenizers<0.22`, PyAV) and then copies
`vendor/openpi_transformers_replace/models/*` over the environment's
`transformers/models/`, as RLinf's installer does. The result is
byte-identical to `rlinf-transformer-openpi==4.53.2`, the transformers fork
RLinf runs (checked file by file). `--check` reports whether the patch is in
place; a run refuses without it.

## Commands

```bash
.venv/bin/python -m levi_recap_worker.cli --check
.venv/bin/python -m levi_recap_worker.cli --inspect path/to/full_weights.pt
.venv/bin/python -m levi_recap_worker.cli --strict-check path/to/checkpoint_folder   # manifest.json → missing/unexpected keys
.venv/bin/python -m levi_recap_worker.cli --selftest [--size tiny|full] [--device cuda|cpu|both] [--frames N]
.venv/bin/python -m levi_recap_worker.cli --plan plan.json --output result.json --progress progress.json
```

`--selftest` needs no checkpoint and no download: it builds the model from
local configs (`tiny`, or `full` — the published SigLIP2-so400m / Gemma3-270M /
`gemma_100m` sizes, randomly initialised), saves random weights as a
`full_weights.pt` under a `model.` prefix, reloads them strictly into a
differently initialised model, writes a synthetic two-camera view and runs the
same inference path as a job. It checks shapes, V in [−1, 0], determinism and
CPU/GPU agreement, and reports time and peak memory. Its character-level
tokenizer is a stand-in for the selftest only.

The `fake` provider (`fake.py`) needs only numpy and pyarrow; LEVI runs it
with its own Python. `LEVI_RECAP_VALUE_FAKE_DELAY_SECONDS` slows it per
episode (tests use it to cancel a running job).

## What is RLinf's and what is LEVI's

`levi_recap_worker/rlinf/` is vendored from [RLinf](https://github.com/RLinf/RLinf)
commit `807e5fd` (Apache-2.0, `LICENSE` there): `configuration.py`,
`value_expert.py`, `modeling_critic.py`, `processing.py`,
`image_text_processing.py` (from `value_common/`) and
`rlinf/algorithms/offline/process/advantage.py`. Changes, each marked in the
file: a flat relative import in `processing.py`, and `value_expert.py` builds
SigLIP2 / Gemma3 from `config.json` when a base-model folder has no weights
(the checkpoint's strict load replaces every weight either way).
`vendor/rlinf/compute_advantages.py` is RLinf's advantage script, kept only so
LEVI's tests run its loop against LEVI's own implementation.
`vendor/openpi_transformers_replace/` is openpi's `transformers_replace`
(Apache-2.0, `LICENSE` there), identical in
Physical-Intelligence/openpi (`src/openpi/models_pytorch/transformers_replace`)
and in the `rlinf-openpi==0.1.1` package RLinf installs.

`rlinf_provider.py` replaces what RLinf takes from LeRobot and openpi: frames
are decoded with PyAV (RGB24, in order, frame i for row `frame_index` i),
converted to LeRobot's float [0, 1] and back through openpi's `_parse_image`
(`(255·x).astype(uint8)`, an exact round trip), and the manifest's `views`
fill the three image slots as LiberoInputs / FrankaEEInputs do (a padded
slot's mask is off unless the manifest's `model_type` is `pi0_fast`).
A plan episode with a `keep` list (the training static filter) is decoded in
full but only its kept rows are inferred. The rest —
resize-with-pad to 224, [−1, 1], the `Task: {prompt}.` prompt, tokenisation,
bf16 with RLinf's fp32 layers and the two-stage forward — is RLinf's own code
(`_prepare_observation_cpu`, `infer_batch`). Robot state is not a value-model
input. Weights load strictly: any missing or unexpected key fails the job
(an absent `lm_head.weight`, unused by the value, is reported only).

## Checking against the frozen FR3 RECAP sources

`tests/test_fr3_preprocessing.py` (`.venv/bin/python -m unittest discover
tests`) runs the frozen FR3 value r1 code — `build_input_transforms(env_type=
"fr3_recap")` and `_prepare_observation_cpu` from the package's
`recap_frozen_sources.tar.gz` (`LEVI_RECAP_FROZEN_SRC`) — against the
worker's preparation, and, with `LEVI_RECAP_FROZEN_PREP` from
`tools/frozen_prep.py` (run in an RLinf environment), compares real decoded
frames, masks and Gemma3 tokens bit for bit. Both skip without the sources.

