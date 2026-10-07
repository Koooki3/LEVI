# References and how to cite / 参考文献与引用方式

LEVI is derived from, and uses, the work below. This page says what each item is used for, under which licence, and how to cite it. Machine-readable forms: [`CITATION.cff`](../CITATION.cff) (GitHub's "Cite this repository" button reads it; the works below are its `references:`) and [`references.bib`](references.bib). Licences of the software LEVI installs are inventoried in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md); source attribution of the inherited visualizer is in [UPSTREAM.md](UPSTREAM.md).

LEVI 基于下列工作开发或使用它们。本页说明各项的用途、许可证和引用方式。机器可读的形式见 [`CITATION.cff`](../CITATION.cff)（GitHub 的“Cite this repository”读它，下列工作是它的 `references:`）和 [`references.bib`](references.bib)。LEVI 安装的软件的许可证清单在 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)；继承来的可视化器的来源说明在 [UPSTREAM.md](UPSTREAM.md)。

## Citing LEVI / 引用 LEVI

```bibtex
@software{levi2026,
  author  = {{Koooki3}},
  title   = {{LEVI}: {LeRobot} Exploration, Validation \& Integration},
  year    = {2026},
  version = {0.3.0},
  url     = {https://github.com/Koooki3/LEVI},
  license = {Apache-2.0}
}
```

Cite the version you used (tags `v0.2.0`, `v0.3.0`; later changes are under "Unreleased" in the [changelog](../CHANGELOG.md)). If you used a component below, cite its work too. No DOI exists yet; when a release is archived (for example on Zenodo) add the DOI to `CITATION.cff`.

请引用你所用的版本（标签 `v0.2.0`、`v0.3.0`；之后的改动见[变更记录](../CHANGELOG.md)的 Unreleased）。用到下面某个组件时，请同时引用对应的工作。目前没有 DOI；某个版本归档（例如在 Zenodo）后，把 DOI 加进 `CITATION.cff`。

## Works LEVI builds on / LEVI 所基于的工作

All entries below were checked against the page named in "Source" on 2026-10-07; a field that could not be checked is marked in the notes. / 下列条目都在 2026-10-07 对照“Source”里的页面核对过；没能核对的字段在备注里标明。

| Work / 工作 | Used for / 用途 | Licence / 许可证 | Cite / Source |
|---|---|---|---|
| Hugging Face **LeRobot Dataset Visualizer**, revision `dc59887796fd41f37040c0df6b10e6f6a30a1854` | The code base LEVI is derived from (see [UPSTREAM.md](UPSTREAM.md)) / LEVI 的源码来源 | Apache-2.0 | No published citation; [repository](https://github.com/huggingface/lerobot-dataset-visualizer). Created by @Mishig25 from LeRobot PR #1055 (its README). |
| **LeRobot** | Dataset format and conventions LEVI reads, writes and converts to / LEVI 读写和转换的数据格式 | Apache-2.0 | The library's `@misc` entry in its README and the ICLR 2026 paper, [arXiv:2602.22818](https://arxiv.org/abs/2602.22818) (`references.bib`: `cadene2024lerobot`, `cadene2026lerobot`) |
| **DROID** | Optional raw-data reader and test sample (`levi sample fetch droid`) / 可选的原始数据读取与测试样例 | CC-BY 4.0 (stated in the paper; the project page and the Hugging Face LeRobot-format copy state different or no terms, so use the original licence) | [arXiv:2403.12945](https://arxiv.org/abs/2403.12945) |
| **SAM 3** (Meta) | Teacher model for instance segmentation (`integrations/sam3`, pinned commit `660a5e9e1b8b4c02c0ad97229b88a09a6e4ff5b7`) / 实例分割的教师模型 | SAM License (custom, not an SPDX identifier); its clause 1.b.ii asks publications that use SAM Materials to acknowledge them | [arXiv:2511.16719](https://arxiv.org/abs/2511.16719) |
| **RF-DETR** (Roboflow) | The `RF-DETR-Seg-S` student model distilled from SAM 3 / 从 SAM 3 蒸馏出的学生模型 | Apache-2.0 (package and the Seg weights); some `rfdetr_plus` components use PML 1.0 | [arXiv:2511.09554](https://arxiv.org/abs/2511.09554) (ICLR 2026) and the repository's `CITATION.cff`. The arXiv abstract does not cover the segmentation models: cite the repository for them. |
| **π\*0.6 and RECAP** (Physical Intelligence) | The RECAP value model and advantage labels (`docs/RECAP.md`) / RECAP 价值模型与优势标签 | n/a (paper) | [arXiv:2511.14759](https://arxiv.org/abs/2511.14759). RECAP ("RL with Experience and Corrections via Advantage-conditioned Policies") is the method named in that paper, not a separate paper. |
| **RLinf** | Value-model code vendored in `integrations/recap_value/levi_recap_worker/rlinf/` (snapshot at commit `807e5fd`) / 随源码附带的价值模型代码 | Apache-2.0 | OSDI 2026 paper [arXiv:2509.15965](https://arxiv.org/abs/2509.15965); RLinf-VLA [arXiv:2510.06710](https://arxiv.org/abs/2510.06710). RLinf's own RECAP page cites no RECAP paper. |
| **openpi**, **π0**, **π0.5** | Policies whose rollouts LEVI labels; `transformers_replace` files used by the RECAP worker / 被标注的策略；RECAP worker 用到的文件 | Apache-2.0 (openpi, plus a Gemma licence file) | [arXiv:2410.24164](https://arxiv.org/abs/2410.24164), [arXiv:2504.16054](https://arxiv.org/abs/2504.16054); openpi publishes no citation |
| **vLLM** | Local vision-language model server / 本地视觉语言模型服务 | Apache-2.0 | PagedAttention, [arXiv:2309.06180](https://arxiv.org/abs/2309.06180) (SOSP 2023), the paper the vLLM README asks to cite |
| **FlashInfer** | Attention kernels compiled just in time by vLLM / vLLM 即时编译的注意力内核 | Apache-2.0 | [arXiv:2501.01005](https://arxiv.org/abs/2501.01005) (MLSys 2025) |
| **CAST** (Glossop et al.) | The relabelling idea behind LEVI's counterfactual-data contract (`docs/COUNTERFACTUAL.md`, contract and validation only) / 反事实数据契约背后的重标注思路 | n/a (paper) | [arXiv:2508.13446](https://arxiv.org/abs/2508.13446) (abstract read; body not read when this page was written) |
| **Qwen3.8-27B** (served as `RedHatAI/Qwen3.8-27B-INT4`) and **Qwen3.5-4B** | The local vision-language models used by default for labelling / 默认用于标注的本地模型 | Apache-2.0 (model cards) | No technical report; the model cards ask to cite the blog posts in `references.bib` (`qwen38`, `qwen3.5`). The INT4 weights are Red Hat's quantisation (LLM Compressor, compressed-tensors) of the Qwen base. |

Software with no published citation (checked in its README, plus `CITATION.cff` where the path exists): Ollama (MIT), Next.js (MIT), Apache Arrow/PyArrow (Apache-2.0), huggingface_hub (Apache-2.0). FastAPI, Pydantic and pandas publish a `CITATION.cff`; OpenCV's wiki gives a BibTeX entry (Bradski, Dr. Dobb's Journal, 2000). Cite them where your work depends on them; their licences are in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

没有公开引用格式的软件（已查其 README，有 `CITATION.cff` 路径的也查了）：Ollama（MIT）、Next.js（MIT）、Apache Arrow/PyArrow（Apache-2.0）、huggingface_hub（Apache-2.0）。FastAPI、Pydantic、pandas 发布了 `CITATION.cff`；OpenCV 的 wiki 给出了 BibTeX（Bradski，Dr. Dobb's Journal，2000）。你的工作依赖它们时请引用；它们的许可证见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## Known uncertainties / 已知的不确定之处

- LeRobot's first-listed co-author is spelled "Aliberts" by arXiv and the paper PDF but "Alibert" in the LeRobot README. `references.bib` follows arXiv for the paper and the README for the repository entry; check the one you cite.
- RF-DETR: arXiv's title is "RF-DETR: Neural Architecture Search for Real-Time Detection Transformers"; the repository README gives a different title. The arXiv title is used.
- The visualizer, SAM 3, RLinf and openpi entries have no citation the projects publish: they are records with the organisation as author, and (except openpi, which has no pinned revision) the pinned commit and its author date.
- The RLinf snapshot commit `807e5fd` is the repository state LEVI copied from, not a commit that changed the value-model code.
- The DROID author list (101 names) was split into family and given names by rule; compound names may need a manual check for a journal style.
- LeRobot 作者拼写：arXiv 和论文 PDF 写“Aliberts”，LeRobot README 写“Alibert”；`references.bib` 论文条目按 arXiv，仓库条目按 README，引用时请自行核对。
- RF-DETR 的 arXiv 标题与仓库 README 的标题不同，这里用 arXiv 的标题。
- 可视化器、SAM 3、RLinf、openpi 没有项目自己发布的引用格式：这些条目是记录，作者写组织名；除 openpi（没有固定版本）外，写固定的提交和该提交的作者日期。
- RLinf 的 `807e5fd` 是 LEVI 复制代码时该仓库的状态，不是改动过价值模型代码的提交。
- DROID 的 101 个作者按规则拆成姓和名，复合姓名投稿时可能需要手工核对。

## Keeping this page right / 维护

When a dependency, model or method is added, replaced or dropped: update this table, `references.bib`, the `references:` block of `CITATION.cff`, and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) in the same change; when a release is cut also bump `version` and `date-released` in `CITATION.cff` ([RELEASING.md](RELEASING.md)). `levi docs check` fails on a broken link here; `tests/test_citation.py` checks the required Citation File Format fields, that `version`, `license` and the repository URL agree with `pyproject.toml`, and that every arXiv paper listed here is in both `CITATION.cff` and `references.bib`.

增加、替换或去掉依赖、模型或方法时，在同一次改动里更新本表、`references.bib`、`CITATION.cff` 的 `references:` 和 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)；发布版本时同时改 `CITATION.cff` 的 `version` 和 `date-released`（[RELEASING.md](RELEASING.md)）。`levi docs check` 会在这里出现失效链接时失败；`tests/test_citation.py` 检查 `CITATION.cff` 必需的字段，检查 `version`、`license` 和仓库地址与 `pyproject.toml` 一致，并检查本页列出的每篇 arXiv 论文都在 `CITATION.cff` 和 `references.bib` 里。
