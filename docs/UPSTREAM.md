# Upstream, copyright and changes / 上游、版权和变更

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
- Agent workbench and harness (`levi/agent/`, `levi/harness/`): one capability layer for UI, REST, MCP and CLI; plan, pilot and commit gates; evidence ledgers; task closure, measured cost, local memory, improvement candidates, teacher supervision and grading. Local model integration (`levi/inference/`): Ollama provider, owned service and an off-peak GPU guard.
- Built-in conversion (`levi/conversion/`), raw-capture browsing views, outcome labels and RECAP export.
- Locked Python/JavaScript dependencies, independent Docker/CI, tests and bilingual documentation.
- Removed the upstream workflow that pushed automatically to `lerobot/visualize_dataset`.

For the exact modified-file list, run:

```bash
git diff --name-status dc59887796fd41f37040c0df6b10e6f6a30a1854 HEAD
```

## Feature parity with the upstream visualizer

Baseline: `huggingface/lerobot-dataset-visualizer@dc59887796fd41f37040c0df6b10e6f6a30a1854`.

| Reference feature | LEVI implementation | Verification |
| --- | --- | --- |
| Hub browsing, search, pagination | Retained explore grid; original LEVI landing/search | Real default Hub cards and episode loading |
| Private datasets / OAuth | Retained OAuth + local Token sign-in + same-origin video proxy | Public proxy and cookie route checks; live private-account login needs the user's credentials |
| Multi-camera synchronized playback | Retained player, shared time context, keyboard, fullscreen/hide | Three real 640×480 camera feeds loaded in Chromium |
| Action / state signal charts | Retained chart grouping, toggles, live cursor | Reference episode signal rendering |
| Language annotations and timeline | Retained upstream schema/editor; v2 backend compatibility added | v2/v3 Parquet round-trip, exact event snapping, original file unchanged |
| Grounded VQA | Retained drag bbox / click point and overlays; text answer modes | Browser interaction and persisted atoms |
| Dataset statistics | Retained metadata and episode length panels | v2 length histogram + JSONL tests; v3.1 multi-chunk browser fixture |
| Filtering and CLI export | Retained movement, smoothness and length filtering | Browser tabs, dataset-scoped flags and export tests |
| First / last frames | Retained gallery and camera selector | Real 10-episode gallery |
| Action autocorrelation | Retained upstream normalized analysis | Reference evaluation collection: suggested 31-step chunk |
| State/action alignment | Retained upstream differential cross-correlation | Reference collection: mean peak at lag 2 |
| Speed and cross-episode variance | Retained histogram and heatmap | Reference collection charts |
| 3D URDF replay | Retained upstream models, mapping, controls, end-effector trails; enabled for compatible v2 robots too | Real SO-100 model loaded in Chromium |
| Doctor | Native version-aware local checks ([Data quality](QUALITY.md)) + external original doctor entry | Synthetic anomalies, full 10-episode real reference report |
| Annotation export / Hub push | Retained backend API; source-safe new exports | Export round-trip tested; no Hub upload performed |
| Built-in conversion | Format registry (robot capture teleop/rollout, image sequence, LeRobot v2.x → LeRobot v2.1, RECAP value), input inspection with requirement checklist and per-target compatibility, single-pass parallel pipeline with lossless retime, live progress, automatic registration; legacy single stages retained | Contract tests over every registered input/output pair, equivalence test against the stage chain, real 171-demo inspection (2 s) and remux checks |
| RECAP value dataset (LEVI addition) | `is_success`, `next.reward`, `next.done`, RLinf `meta/returns.parquet`, LeRobot-proposal `episode_labels.csv`, manifest; from raw captures or existing LeRobot datasets | Conformance test reproducing RLinf's `compute_returns.py` (commit `db66ac56`) |
| Raw captures and outcome labels (LEVI addition) | Browsing view of raw captures, annotation carry-over by source demo, human success/failure labels, format/version/origin in the dataset list | View/carry-over/rebuild tests; real screws capture registered and browsed in Chromium |
| SAM3 object annotation | Global mask/bbox/track sidecar for demos, Hub and local datasets, pinned worker with 1038lab/sam3 checkpoint, account/progress UI, human accept/reject/refine and source-safe export | RLE round-trip, API fake flow, revision/edit/export tests; real model intentionally not run in CPU CI |
| Chinese / English | Added React locale boundaries, catalogs, live switch | Both languages and viewport checks |
| Independent environment / distribution | uv.lock, .venv, local Bun, launcher, Dockerfile, CI, bilingual docs | Python suite, frontend suite and production build |

## Formats and retained upstream constraints

- v2.0/v2.1: episode-per-file Parquet and video, JSON/JSONL metadata.
- v3.0/v3.1: shared Parquet/video files and episode metadata Parquet. Multi-chunk metadata iteration is retained; a two-chunk v3.1 shared-video fixture was verified in Chromium.
- Browser support depends on the video's codec. H.264 and the demonstrated AV1 videos were tested in Chromium. Unsupported camera codecs may require conversion.
- Embedded-image-only datasets are rejected by the retained upstream loader.
- Hub video, OAuth, external doctor, URDF meshes and research-paper links require network access. Their text is not translated by LEVI.
- Auto chunk suggestions and alignment estimates preserve upstream mathematical behavior, not universal training prescriptions.
- Runtime is a source checkout, not a standalone frontend bundled inside a PyPI wheel. Use the documented source install or Docker image.
- Stopping the service terminates conversion workers; interrupted jobs are marked for inspection and outputs are only published on success. Restart never automatically resumes writes.
- A raw capture's browsing view is for viewing and annotation, not training: it carries no pixel statistics and cannot be exported directly.

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

The default demonstration datasets are public LeRobot datasets, checked reachable on 2026-09-20:

- https://huggingface.co/datasets/lerobot/svla_so101_pickplace
- https://huggingface.co/datasets/lerobot/aloha_static_coffee

They replace the two `samanthalhy` strawberry datasets, which were the defaults until 2026-09-20 and have since been removed from the Hub. Existing screenshots still show that footage and continue to credit `samanthalhy`, whose cards declared `apache-2.0` when checked on 2026-09-10.

Videos are streamed rather than bundled. Any LEVI screenshots showing a dataset should credit its source. External robot URDFs/meshes are loaded from the upstream `lerobot/robot-urdfs` bucket; their own source licenses apply. The HF login badge and brand marks remain their owners' marks. The external `lerobot-doctor` app is linked, not vendored or relicensed.

The built-in `levi/conversion/` module reimplements the capture workflow requested by the project owner. It replaces the former external-script adapter. No private task mappings, collector deployments, datasets or credentials are distributed. Hardware-specific collection helpers are not runtime dependencies. See the [conversion guide](CONVERSION.md).

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
