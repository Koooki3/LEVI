# Releasing / 发布指南

Repository: https://github.com/Koooki3/LEVI

## Repository presentation

About description:

> A bilingual workbench for robot-learning data: browse and convert LeRobot datasets and raw captures, check their quality, and annotate them with human-reviewed agents — external MCP, API or local (Ollama, vLLM) models — with measured cost and local memory.

Topics: `lerobot`, `robotics`, `robot-learning`, `dataset-visualization`, `data-annotation`, `dataset-conversion`, `data-quality`, `imitation-learning`, `data-curation`, `mcp`, `ollama`, `huggingface`, `nextjs`, `fastapi`, `uv`.

README.md is the English project landing page and `README.zh-CN.md` is the Chinese companion; the two are updated together. Guides live in `docs/`; the [README](../README.md#documentation) lists them. No hosted demo or deployment URL is claimed.
README.md 是英文项目首页，README.zh-CN.md 是中文版本，两者同步更新；不声称有托管演示或部署网址，也不把本地服务地址当作公开演示网址。

## Release checklist

1. Update `pyproject.toml`, `package.json`, `uv.lock`, README version badges and `CHANGELOG.md` together: move the **Unreleased** entries under a new version heading. That section is the release notes.
2. Follow the development checks in [CONTRIBUTING](../CONTRIBUTING.md), including a production build. Validate affected browser/conversion workflows; document tested scope in [VALIDATION](VALIDATION.md).
3. Review `git status --short`, `git diff --check` and `git diff --cached`. Exclude `.env`, runtimes, caches, datasets, reports and credentials. Run `uv run levi clean`, inspect its preview and then use `--apply` if appropriate.
4. Retain `LICENSE`, `NOTICE`, upstream history and [source attribution](UPSTREAM.md). Refresh [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md); before publishing Docker/PyPI/binary artifacts, generate and review their complete dependency/asset inventory and ship the applicable license/NOTICE texts as described there. Keep SAM3 checkpoints, Hugging Face tokens, worker environments and workspace data out of the source release; verify the global SAM3 status and model-free worker checks without CUDA/model execution. Publish only to the intended `origin`; never push LEVI releases to upstream.
5. Push `main` and wait for **LEVI checks** to pass on the exact release commit. Create a version tag only after validation, and publish the matching notes.
   Both legs (with and without the `agent` extra) always finish. When pytest fails, the run's summary page lists the failed tests with the first line of each message, so no log download is needed; the JUnit file is attached as an artifact (`pytest-junit-agent-<true|false>`, kept 14 days).

发布前同步更新版本号和锁文件，验证构建和受影响的流程，检查暂存内容和清理预览。CI 通过后再创建版本标签和 Release。不得覆盖已有标签，也不得强制改写公开历史。
CI 的两组（带与不带 `agent` extra）总会各自跑完。pytest 失败时，运行的摘要页列出失败的测试和每条信息的第一行，不必下载日志；JUnit 文件作为 artifact 附在运行上（`pytest-junit-agent-<true|false>`，保留 14 天）。

Version strings to change together on a release: `pyproject.toml`, `package.json`, `uv.lock` (`uv lock`), `CITATION.cff` (`version`, `date-released`), `docs/references.bib` (`levi2026` `version`) and the "Cite the version you used" sentences in `docs/REFERENCES.md` and both READMEs, the badge and "current release" line in both READMEs, the section name in `CHANGELOG.md`, the baseline line of `THIRD_PARTY_NOTICES.md`, and the two hard-coded `"0.3.0"` strings in `levi/service.py` (FastAPI) and `levi/agent/pilot.py` (client info). The integrations (`integrations/*`) carry their own version (0.1.0) on purpose. Add or change a dependency, model or method: update `docs/REFERENCES.md`, `docs/references.bib`, `CITATION.cff` and `THIRD_PARTY_NOTICES.md` in the same change.

发布时要一起改的版本号：`pyproject.toml`、`package.json`、`uv.lock`（`uv lock`）、`CITATION.cff`（`version`、`date-released`）、`docs/references.bib` 里 `levi2026` 的 `version`、`docs/REFERENCES.md` 和两份 README 里“请引用你所用的版本”那句、两份 README 的徽章和“当前版本”、`CHANGELOG.md` 的节名、`THIRD_PARTY_NOTICES.md` 的基线、以及写死 `"0.3.0"` 的 `levi/service.py`（FastAPI）和 `levi/agent/pilot.py`（客户端信息）。`integrations/*` 有自己的版本号（0.1.0），这是有意的。增加或更换依赖、模型、方法时，在同一次改动里更新 `docs/REFERENCES.md`、`docs/references.bib`、`CITATION.cff` 和 `THIRD_PARTY_NOTICES.md`。标签 `v0.2.0`、`v0.3.0` 于 2026-10-07 补在当年的发布提交上，用 `git push origin v0.2.0 v0.3.0` 推送；GitHub 的 Release 页面要 `gh release create` 之后才有。

Example, from a clean checkout with an authorized GitHub CLI account (replace `X.Y.Z`):

```bash
git remote get-url origin
git push -u origin main
gh run list --repo Koooki3/LEVI --workflow test.yml --branch main
# Wait for the matching commit to pass before continuing.
awk '/^## X.Y.Z/{f=1;next} /^## /{f=0} f' CHANGELOG.md > "$LEVI_WORKSPACE/tmp/release-notes.md"
git tag -a vX.Y.Z -m "LEVI vX.Y.Z"
git push origin vX.Y.Z
gh release create vX.Y.Z --repo Koooki3/LEVI --verify-tag \
  --title "LEVI vX.Y.Z" --notes-file "$LEVI_WORKSPACE/tmp/release-notes.md"
```

Release notes are in `CHANGELOG.md` (the section named like the version). The tags `v0.2.0` and `v0.3.0` were created on 2026-10-07 at their release commits (`72489de`, `fa3c9f6`) and are pushed with `git push origin v0.2.0 v0.3.0`; a GitHub release page exists only after `gh release create`.

GitHub provides source archives for the tag. This release is installed from source using uv and Bun; it does not claim a published PyPI package, container image or native installer. Docker, private Hub login and Hub upload require separate deployment validation as documented.
