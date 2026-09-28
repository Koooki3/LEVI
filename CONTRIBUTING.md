# Contributing / 贡献

请针对可复现的问题提交尽量小的改动；影响数据读写、路径保护或格式解析的改动，请附上有意义的测试。

Please include a reproducible problem statement, the format/version of a minimal dataset and the relevant checks. Do not attach private data, credentials or full robot captures to public issues.

```bash
export LEVI_WORKSPACE="$PWD/.state"
export UV_CACHE_DIR="$LEVI_WORKSPACE/.cache/uv"
uv sync --locked --group dev --extra agent
uv run levi setup
uv run levi check
mkdir -p "$LEVI_WORKSPACE/tmp/build"
uv run pytest --basetemp="$LEVI_WORKSPACE/tmp/build/levi-pytest"
uv run levi build
export PATH="$(ls -d "$PWD"/.runtime/bun-*):$PATH"
bun run format && bun run validate
```

Set `LEVI_WORKSPACE` to a local test data directory. Run `bun run format` with the local Bun before the frontend checks. You can add the project-local runtime to the current shell's PATH without changing shell startup files.

- Preserve Apache-2.0 attribution and upstream history. New third-party assets need a documented source and compatible license.
- Keep both locale catalogs, both READMEs and the paired Chinese guides (`docs/*.zh-CN.md`) current. Record unreleased changes in `CHANGELOG.md` and open problems in `docs/architecture/issue-register.md`; remove a register entry once its fix is tested. Dataset contents and schema identifiers must remain unchanged by translation.
- Do not place caches, datasets, checkpoints, tokens or logs in source control.
- Jobs must use an allowlist, argv arrays and a new output directory. Do not add arbitrary shell execution from HTTP requests.
- Reproduce video behavior in a real browser. A passing build alone does not validate media playback.
- Compare upstream changes against the pinned baseline in `docs/UPSTREAM.md` before merging.
