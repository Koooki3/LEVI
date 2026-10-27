#!/usr/bin/env bash
# Give this checkout or worktree its own Python and Bun dependencies, quickly.
#
#   scripts/dev-env.sh          # uv sync --group dev, bun install (frozen)
#   scripts/dev-env.sh --agent  # also the `agent` extra (CI's full pytest run)
#
# Both installers link from their global caches (uv: hardlinks, bun:
# --backend=hardlink), so a warm install takes about a second and adds almost no
# disk. The environment stays private to this tree: no symlink to another
# checkout's node_modules or .venv. Files are hardlinked to the global caches, so
# do not edit a file inside node_modules or site-packages in place (that would
# write through to every tree sharing the cache); reinstall instead. Without
# --agent an installed `agent` extra is removed again (uv sync is exact). Nothing here touches the product checkout's environment, a
# running service or the network when the caches are warm (--offline is tried
# first). Prints the commands that run the checks from this tree.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# `uv sync` makes an environment match the lock exactly (it removes packages that
# are not in it), so never run it against the checkout a product LEVI runs from.
if [[ "$(git rev-parse --git-dir)" == "$(git rev-parse --git-common-dir)" && -z "${LEVI_DEV_ENV_MAIN:-}" ]]; then
  echo "refusing to run in the main checkout (its .venv belongs to the product); use a worktree, or set LEVI_DEV_ENV_MAIN=1" >&2
  exit 2
fi
# An exported UV_PROJECT_ENVIRONMENT would make `uv sync` rewrite another tree's venv.
unset UV_PROJECT_ENVIRONMENT

extras=()
[[ "${1:-}" == "--agent" ]] && extras=(--extra agent)

uv_bin="${LEVI_UV:-$(command -v uv || true)}"
[[ -x "$uv_bin" ]] || uv_bin="$HOME/.local/bin/uv"
[[ -x "$uv_bin" ]] || { echo "uv not found (set LEVI_UV)" >&2; exit 1; }

bun_bin="${LEVI_BUN:-$(command -v bun || true)}"
if [[ ! -x "$bun_bin" ]]; then
  # The maintainer's checkout keeps a pinned Bun under .runtime/.
  common="$(git rev-parse --path-format=absolute --git-common-dir)"
  bun_bin="$(dirname "$common")/.runtime/bun-linux-x64/bun"
fi
[[ -x "$bun_bin" ]] || { echo "bun not found (set LEVI_BUN)" >&2; exit 1; }

# A symlinked node_modules or .venv from an older setup is replaced, not followed.
for d in node_modules .venv; do
  [[ -L "$d" ]] && rm "$d"
done

"$uv_bin" sync --frozen --group dev "${extras[@]}" --offline 2>/dev/null \
  || "$uv_bin" sync --frozen --group dev "${extras[@]}"
"$bun_bin" install --frozen-lockfile --backend=hardlink
mkdir -p .state/tmp/build .state/tmp/pt

cat <<EOF

ready in $PWD
  python:   LEVI_WORKSPACE=\$PWD/.state TMPDIR=\$PWD/.state/tmp/build uv run --no-sync pytest <paths> --basetemp=\$PWD/.state/tmp/pt -p no:cacheprovider
  frontend: $bun_bin run test <paths>       # or: $bun_bin run validate
  which tests: python3 scripts/select_tests.py
(run the full Python suite one at a time, through the resource driver in the maintainer's workspace)
EOF
