# 上游、版权和变更 / Upstream, copyright and changes

LEVI is an independent derivative of:

- Repository: https://github.com/huggingface/lerobot-dataset-visualizer
- Imported commit: `dc59887796fd41f37040c0df6b10e6f6a30a1854`
- License: Apache License 2.0; the full original [LICENSE](../LICENSE) is retained.
- Original documentation remains available at the pinned upstream revision and in Git history.
- Original acknowledgement: @Mishig25 and LeRobot PR #1055.

上游 Git 历史保留在本地仓库中。`NOTICE` 记录来源与 LEVI 改造范围。发布时应保留许可证、来源声明及适用的版权通知；不要把上游代码署名为完全原创。

Modified upstream source files that support stable comment headers carry a LEVI modification notice; structured/generated-file records are described below. The upstream history and notices are retained. The default remote was renamed to `upstream`; its push URL is disabled in this checkout. Add your own `origin` when publishing. There is no implication of Hugging Face endorsement.

## LEVI changes

- Original landing page, navigation, graphite/parchment/lime design, project branding and local guide.
- Chinese/English catalogs and React presentation boundaries in `src/`; original source dataset text is retained.
- New `levi/` Python package: portable workspace paths, uv CLI, local runtime bootstrap, catalog/review persistence, diagnostics, bundled conversion jobs and file service.
- `backend/app.py`: v2 episode metadata support, new-file exports, managed paths, separate annotation sidecars, request-scoped Hub credentials and complete export downloads.
- `src/utils/` and API routes: local data sources, backend bridge, local/HTTPS cookie selection and fixed v3.1 detection.
- Dataset-scoped flags, local HF Token login, safe current-annotation save before export and stale-request protection.
- Locked Python/JavaScript dependencies, independent Docker/CI, tests and bilingual documentation.
- Removed the upstream workflow that pushed automatically to `lerobot/visualize_dataset`.

For the exact modified-file list, run:

```bash
git diff --name-status dc59887796fd41f37040c0df6b10e6f6a30a1854 HEAD
```

## Structured and generated files / 结构化与生成文件

Strict JSON cannot contain comment headers, and some machine-generated files lose manually inserted headers when regenerated. For these files, LEVI keeps a modification record through the **pinned upstream commit + Git diff/history + [NOTICE](../NOTICE)**, together with the file-specific entries below. Do not add invalid syntax or unstable fields merely to insert a header. Preserve existing copyright, license and attribution notices wherever the format supports them.

严格 JSON 或无法稳定保留注释的机器生成文件，采用 **固定上游提交 + Git 差异及历史 + NOTICE** 记录修改。下表明确文件来源及修改范围；支持稳定注释的源码仍保留文件内变更说明。

| File / 文件 | Record relative to the imported commit / 相对上游的记录 |
| --- | --- |
| `package.json` | Modified: LEVI identity, scripts, dependency updates, repository and license metadata. / 修改项目身份、脚本、依赖与发布元数据。 |
| `bun.lock` | Modified/generated: LEVI workspace identity and the dependency resolution resulting from the frontend manifest. / 更新工作区身份及前端依赖解析。 |
| `uv.lock`, `src/i18n/en.json`, `src/i18n/zh.json` | Added by LEVI; absent from the imported baseline. Git records their introduction and subsequent changes. / LEVI 新增，并非原上游文件的改名署名。 |

Audit the committed tree, rather than unrelated uncommitted edits:

```bash
git diff --name-status dc59887796fd41f37040c0df6b10e6f6a30a1854 HEAD
git diff dc59887796fd41f37040c0df6b10e6f6a30a1854 HEAD -- package.json bun.lock
git log --oneline -- package.json bun.lock uv.lock src/i18n
```

Replace `HEAD` with the exact release tag or commit when auditing a distribution. The initial public release can also be inspected through the [v0.2.0 comparison](https://github.com/Koooki3/LEVI/compare/dc59887796fd41f37040c0df6b10e6f6a30a1854...v0.2.0); the [original source snapshot](https://github.com/huggingface/lerobot-dataset-visualizer/tree/dc59887796fd41f37040c0df6b10e6f6a30a1854) remains available independently. Source archives without `.git` retain this document, `LICENSE` and `NOTICE`. Future packaged distributions must also carry their applicable notices; this record does not replace those license requirements. See [Apache-2.0 redistribution terms](https://www.apache.org/licenses/LICENSE-2.0).

第三方依赖、随附素材与未来构建产物的清单维护见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## External material

The two default strawberry datasets are owned by `samanthalhy`. Their Hub cards declared `apache-2.0` when checked on 2026-09-10. Links:

- https://huggingface.co/datasets/samanthalhy/so100_strawberry_2
- https://huggingface.co/datasets/samanthalhy/eval_so100_smol_strawberry_2

Videos are streamed rather than bundled. Any LEVI screenshots showing those datasets should credit these sources. External robot URDFs/meshes are loaded from the upstream `lerobot/robot-urdfs` bucket; their own source licenses apply. The HF login badge and brand marks remain their owners' marks. The external `lerobot-doctor` app is linked, not vendored or relicensed.

The built-in `levi/conversion/` module reimplements the capture workflow requested by the project owner. It replaces the former external-script adapter. No private task mappings, collector deployments, datasets or credentials are distributed. Hardware-specific collection helpers are not runtime dependencies. See [conversion guide](CONVERSION.md) and [audit](AUDIT.md).

## SAM3 integration / SAM3 集成

integrations/sam3/ is a LEVI-authored adapter boundary, not a vendored copy of
the SAM3 source. It pins the official Meta repository commit
660a5e9e1b8b4c02c0ad97229b88a09a6e4ff5b as a user-installed Git dependency. The
core package contains only protocol/schema/RLE code and never bundles
checkpoints.

The default runtime model is the 1038lab/sam3 Hugging Face mirror, file sam3.pt,
revision main. The worker downloads it into the active LEVI_WORKSPACE after the
current account has access. LEVI displays account and progress state but never
stores a token in a plan, job record, log or sidecar. The model mirror, official
SAM3 code and weights retain their own access, license and redistribution terms;
the official SAM License applies to the SAM3 source/model materials.

The worker runs in a separate uv project. Its model and checkpoint metadata are
recorded in result JSON and sidecar revisions without copying private credentials.
For pyproject.toml, lockfiles, JSON and other machine-generated or structured
files, the modification record is the pinned upstream commit plus Git
diff/history and NOTICE; no invalid header is inserted. Stable source files keep
their LEVI modification notice. See SAM3.md for the ordered deployment sequence
and CPU-safe verification policy.
