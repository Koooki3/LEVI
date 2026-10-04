#!/usr/bin/env bash
# Build a CUDA_HOME made only of symbolic links into the pip CUDA toolchain wheels of a vLLM
# virtual environment, for a machine without a system CUDA toolkit (no sudo needed).
# FlashInfer and torch compile kernels at run time and look for nvcc under CUDA_HOME.
#
# Usage: make-cuda-home.sh [VENV] [OUT]
#   VENV  the vLLM environment (default: $LEVI_VLLM_VENV, else <checkout>/.venv-vllm)
#   OUT   where to build it   (default: VENV/cuda-home)
# Then: export LEVI_VLLM_CUDA_HOME=OUT (scripts/vllm/serve.sh exports CUDA_HOME from it).
#
# First pin the toolchain wheels to the CUDA version of the installed torch (docs/VLLM.md), e.g.
#   uv pip install --python VENV/bin/python "cuda-toolkit[nvcc,crt,nvvm,cccl,cudart]==<torch's CUDA>.*"
# Idempotent: re-run it after any install that changes the nvidia-* wheels.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VENV="${1:-${LEVI_VLLM_VENV:-$ROOT/.venv-vllm}}"
OUT="${2:-$VENV/cuda-home}"
PY="$VENV/bin/python"
[[ -x "$PY" ]] || { echo "no python in $VENV" >&2; exit 1; }
# The newest nvidia/cu<N> folder of the environment's site-packages.
CU="$("$PY" - <<'PY'
import pathlib, site
dirs = sorted(
    (p for s in site.getsitepackages() for p in pathlib.Path(s, "nvidia").glob("cu[0-9]*")),
    key=lambda p: int(p.name[2:]),
)
print(dirs[-1] if dirs else "")
PY
)"
[[ -n "$CU" && -x "$CU/bin/nvcc" ]] || { echo "no nvcc under $VENV site-packages/nvidia/cu*/bin (install the CUDA toolchain wheels first)" >&2; exit 1; }
LIBCUDA="$(ldconfig -p 2>/dev/null | awk '/libcuda\.so\.1 /{print $NF; exit}')"
[[ -n "$LIBCUDA" ]] || { echo "the NVIDIA driver's libcuda.so.1 was not found (is the driver installed?)" >&2; exit 1; }

mkdir -p "$OUT/lib64/stubs"
for d in bin include nvvm; do ln -sfn "$CU/$d" "$OUT/$d"; done
ln -sfn lib64 "$OUT/lib"
find "$OUT/lib64" -maxdepth 1 -type l -delete
for f in "$CU"/lib/*; do ln -sfn "$f" "$OUT/lib64/$(basename "$f")"; done
CUDART="$(cd "$OUT/lib64" && ls libcudart.so.* 2>/dev/null | head -1 || true)"
[[ -n "$CUDART" ]] && ln -sfn "$CUDART" "$OUT/lib64/libcudart.so"
ln -sfn "$LIBCUDA" "$OUT/lib64/stubs/libcuda.so"
"$OUT/bin/nvcc" --version | tail -2
echo "CUDA_HOME built at $OUT; export LEVI_VLLM_CUDA_HOME=$OUT"
