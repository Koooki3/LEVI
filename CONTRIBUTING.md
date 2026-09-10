# Contributing / 贡献

感谢改进 LEVI。请围绕可复现的问题提交最小更改，并为影响数据读写、路径保护或格式解析的改动提供有意义的测试。

Please include a reproducible problem statement, the format/version of a minimal dataset and the relevant checks. Do not attach private data, credentials or full robot captures to public issues.

```bash
export LEVI_WORKSPACE="$PWD/.state"
export UV_CACHE_DIR="$LEVI_WORKSPACE/.cache/uv"
uv sync --locked --group dev
uv run levi setup
uv run levi check
mkdir -p "$LEVI_WORKSPACE/tmp/build"
uv run pytest --basetemp="$LEVI_WORKSPACE/tmp/build/levi-pytest"
uv run levi build
```

Set `LEVI_WORKSPACE` to a local test data directory. Run `bun run format` using the local Bun before frontend checks. The project-local runtime can be added to the current shell PATH without changing shell startup files.

- Preserve Apache-2.0 attribution and upstream history. New third-party assets need a documented source and compatible license.
- Keep both locale catalogs and README versions current. Dataset contents and schema identifiers must remain unchanged by translation.
- Do not place caches, datasets, checkpoints, tokens or logs in source control.
- Jobs must use an allowlist, argv arrays and a new output directory. Do not add arbitrary shell execution from HTTP requests.
- Reproduce video behavior in an actual browser. Passing a build alone does not validate media playback.
- Compare upstream changes against the pinned baseline in `docs/UPSTREAM.md` before merging.
