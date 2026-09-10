# 上游、版权和变更 / Upstream, copyright and changes

LEVI is an independent derivative of:

- Repository: https://github.com/huggingface/lerobot-dataset-visualizer
- Imported commit: `dc59887796fd41f37040c0df6b10e6f6a30a1854`
- License: Apache License 2.0; the full original [LICENSE](../LICENSE) is retained.
- Original documentation remains available at the pinned upstream revision and in Git history.
- Original acknowledgement: @Mishig25 and LeRobot PR #1055.

上游 Git 历史保留在本地仓库中。`NOTICE` 记录来源与 LEVI 改造范围。发布时应保留许可证、来源声明及适用的版权通知；不要把上游代码署名为完全原创。

Modified upstream source files carry a LEVI modification notice. The upstream history and notices are retained. The default remote was renamed to `upstream`; its push URL is disabled in this checkout. Add your own `origin` when publishing. There is no implication of Hugging Face endorsement.

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
git diff --name-status dc59887796fd41f37040c0df6b10e6f6a30a1854
```

## External material

The two default strawberry datasets are owned by `samanthalhy`. Their Hub cards declared `apache-2.0` when checked on 2026-09-10. Links:

- https://huggingface.co/datasets/samanthalhy/so100_strawberry_2
- https://huggingface.co/datasets/samanthalhy/eval_so100_smol_strawberry_2

Videos are streamed rather than bundled. Any LEVI screenshots showing those datasets should credit these sources. External robot URDFs/meshes are loaded from the upstream `lerobot/robot-urdfs` bucket; their own source licenses apply. The HF login badge and brand marks remain their owners' marks. The external `lerobot-doctor` app is linked, not vendored or relicensed.

The built-in `levi/conversion/` module reimplements the capture workflow requested by the project owner. It replaces the former external-script adapter. No private task mappings, collector deployments, datasets or credentials are distributed. Hardware-specific collection helpers are not runtime dependencies. See [conversion guide](CONVERSION.md) and [audit](AUDIT.md).
