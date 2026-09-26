#!/usr/bin/env bash
# Build the RECAP value worker environment and apply openpi's
# transformers_replace patch, exactly as RLinf's installer does
# (requirements/install.sh: `cp -r .../transformers_replace/* .../transformers/`).
# transformers==4.53.2 plus this patch is byte-identical to the
# rlinf-transformer-openpi==4.53.2 fork RLinf installs.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv sync --project "$here" --frozen
py="$here/.venv/bin/python"
version="$("$py" -c 'import transformers; print(transformers.__version__)')"
if [ "$version" != "4.53.2" ]; then
  echo "transformers $version installed; the patch is for 4.53.2" >&2
  exit 1
fi
site="$("$py" -c 'import os, transformers; print(os.path.dirname(transformers.__file__))')"
cp -r "$here/vendor/openpi_transformers_replace/models/." "$site/models/"
"$py" -m levi_recap_worker.cli --check
echo "RECAP value worker ready: $py"
echo "export LEVI_RECAP_VALUE_WORKER_PYTHON=$py"
