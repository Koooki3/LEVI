#!/usr/bin/env bash
# Build the fast segmentation worker environment (RF-DETR-Seg student +
# ByteTrack). The SAM3 teacher keeps its own environment (integrations/sam3).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv sync --project "$here" --frozen
"$here/.venv/bin/python" -m levi_seg_worker.cli --check
echo "Fast segmentation worker ready: $here/.venv/bin/python"
echo "export LEVI_SEG_WORKER_PYTHON=$here/.venv/bin/python"
