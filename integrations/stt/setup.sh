#!/usr/bin/env bash
# Build the speech worker environment and install its model.
#
#   ./setup.sh [MODEL_DIR] [install-model options, e.g. --from-dir SNAPSHOT]
#
# MODEL_DIR defaults to ${LEVI_WORKSPACE:-<checkout>/.state}/checkpoints/stt.
# The model (Qwen/Qwen3-ASR-1.7B-hf at a pinned commit, 4.1 GB) is downloaded
# into MODEL_DIR/qwen3-asr-1.7b-hf and MANIFEST.sha256 is written next to it;
# the shared Hugging Face cache is never touched.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# Never let uv resolve the shared LEVI environment through an active venv.
unset VIRTUAL_ENV
model_dir="${LEVI_WORKSPACE:-$repo/.state}/checkpoints/stt"
if [ "$#" -gt 0 ] && [[ "$1" != --* ]]; then
  model_dir="$1"
  shift
fi
cd "$here"
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv sync --project "$here" --frozen
"$here/.venv/bin/python" -m levi_stt_worker.cli --check
"$here/.venv/bin/python" -m levi_stt_worker.cli install-model --model-dir "$model_dir" "$@"
echo "Speech worker ready: $here/.venv/bin/python"
echo "export LEVI_STT_WORKER_PYTHON=$here/.venv/bin/python"
echo "export LEVI_STT_MODEL_DIR=$model_dir"
