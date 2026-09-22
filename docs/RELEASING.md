# Releasing / 发布指南

Repository: https://github.com/Koooki3/LEVI

## Repository presentation

About description:

> A bilingual workbench for robot-learning data: browse and convert LeRobot datasets and raw captures, check their quality, and annotate them with human-reviewed agents — external MCP, API or local Ollama models — with measured cost and local memory.

Topics: `lerobot`, `robotics`, `robot-learning`, `dataset-visualization`, `data-annotation`, `dataset-conversion`, `data-quality`, `imitation-learning`, `data-curation`, `mcp`, `ollama`, `huggingface`, `nextjs`, `fastapi`, `uv`.

README.md is the English project landing page and `README.zh-CN.md` is the Chinese companion; the two are updated together. Guides live in `docs/`; the [README](../README.md#documentation) lists them. No hosted demo or deployment URL is claimed.
README.md 是英文项目入口；README.zh-CN.md 提供中文版本；没有将本地服务地址用作公开演示网址。

## Release checklist

1. Update `pyproject.toml`, `package.json`, `uv.lock`, README version badges and `CHANGELOG.md` together: move the **Unreleased** entries under a new version heading. That section is the release notes.
2. Follow the development checks in [CONTRIBUTING](../CONTRIBUTING.md), including a production build. Validate affected browser/conversion workflows; document tested scope in [VALIDATION](VALIDATION.md).
3. Review `git status --short`, `git diff --check` and `git diff --cached`. Exclude `.env`, runtimes, caches, datasets, reports and credentials. Run `uv run levi clean`, inspect its preview and then use `--apply` if appropriate.
4. Retain `LICENSE`, `NOTICE`, upstream history and [source attribution](UPSTREAM.md). Refresh [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md); before publishing Docker/PyPI/binary artifacts, generate and review their complete dependency/asset inventory and ship the applicable license/NOTICE texts as described there. Keep SAM3 checkpoints, Hugging Face tokens, worker environments and workspace data out of the source release; verify the global SAM3 status and model-free worker checks without CUDA/model execution. Publish only to the intended `origin`; never push LEVI releases to upstream.
5. Push `main` and wait for **LEVI checks** to pass on the exact release commit. Create a version tag only after validation, and publish the matching notes.

发布前同步版本与锁文件，验证构建及受影响流程，检查暂存内容和清理预览。CI 通过后再创建版本标签及 Release。不得覆盖已有标签或强制改写公共历史。

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

Earlier release notes (v0.2.0, v0.3.0) are kept on the GitHub releases page and in `CHANGELOG.md`.

GitHub provides source archives for the tag. This release is installed from source using uv and Bun; it does not claim a published PyPI package, container image or native installer. Docker, private Hub login and Hub upload require separate deployment validation as documented.
