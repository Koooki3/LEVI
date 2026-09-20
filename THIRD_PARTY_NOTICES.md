# Third-party notices / 第三方来源与许可证清单

## Scope and baseline / 范围与基线

This is the source-release inventory for LEVI v0.3.0, refreshed on 2026-09-13. The dependency baseline remains the v0.3.0 lock and source commit; this release adds canonical dataset-version handling, task indexing/filtering, and cache-isolation hardening.

当前清单覆盖源码来源、随附或使用的素材、直接依赖及构建/运行工具。它不是尚未发布的 Docker、PyPI 或二进制产物的完整传递依赖清单；当前正式发行提供源码，不包含安装后的环境、依赖目录或数据集视频。未来发布这些产物前，必须按下文流程生成并维护实际分发清单与许可证文本。

[LICENSE](LICENSE) applies to LEVI's code; [NOTICE](NOTICE) and [UPSTREAM](docs/UPSTREAM.md) record the inherited visualizer source. Third-party components retain their own licenses. The labels below are an index, not a substitute for the complete copyright, license and NOTICE texts that apply to a distributed component.

## Source and assets / 源码与素材

| Component / 组件 | Source and terms / 来源与条款 | Use / 使用方式 |
| --- | --- | --- |
| LeRobot Dataset Visualizer | [Hugging Face upstream](https://github.com/huggingface/lerobot-dataset-visualizer/tree/dc59887796fd41f37040c0df6b10e6f6a30a1854), Apache-2.0 | Modified source is included; retain upstream history, LICENSE and NOTICE. Original acknowledgement: @Mishig25 and LeRobot PR #1055. |
| Demo screenshots | `docs/assets/home-zh.png`, `docs/assets/insights-zh.png`; footage from [samanthalhy/so100_strawberry_2](https://huggingface.co/datasets/samanthalhy/so100_strawberry_2) and [samanthalhy/eval_so100_smol_strawberry_2](https://huggingface.co/datasets/samanthalhy/eval_so100_smol_strawberry_2), whose dataset cards declared Apache-2.0 at the baseline check | Screenshots of the LEVI interface are included; original dataset videos are fetched on demand. Those two datasets have since been removed from the Hub. |
| Default demonstrations | [lerobot/svla_so101_pickplace](https://huggingface.co/datasets/lerobot/svla_so101_pickplace), [lerobot/aloha_static_coffee](https://huggingface.co/datasets/lerobot/aloha_static_coffee) | Listed in the interface and streamed on demand; nothing is bundled. |
| Font Awesome Free 5 icons | [Font Awesome / Fonticons, Inc.](https://github.com/FortAwesome/Font-Awesome/tree/5.15.4); [CC BY 4.0 for SVG/JS icons](https://github.com/FortAwesome/Font-Awesome/blob/5.15.4/LICENSE.txt) | Used through `react-icons/fa` in playback and camera controls. React wrappers come from react-icons; LEVI styles/sizes the icons without editing their vector paths. Preserve this attribution in distributions containing these graphics. |
| Robot URDFs/meshes | [lerobot/robot-urdfs](https://huggingface.co/buckets/lerobot/robot-urdfs); model-specific source terms | Loaded remotely; not included in the source archive. Record each actual model, revision and license before bundling it. |
| Studio environment HDRI | [Studio Small 03 by Greg Zaal / Poly Haven](https://polyhaven.com/a/studio_small_03), [CC0](https://polyhaven.com/license); [Drei asset revision](https://github.com/pmndrs/drei-assets/blob/456060a26bbeb8fdf79326f224b6d99b8bcce736/hdri/studio_small_03_1k.hdr) | Loaded remotely by `Environment preset="studio"` in URDF playback; not included in the source archive. |
| Hugging Face sign-in badge | [Hugging Face badges](https://huggingface.co/datasets/huggingface/badges); respective source and trademark terms | Loaded remotely by the sign-in button. It is not relicensed by LEVI; record applicable terms before bundling a copy. |
| External lerobot-doctor service | External service linked from the workbench; see [UPSTREAM](docs/UPSTREAM.md) | Linked rather than vendored; LEVI's own diagnostics are separate source code. |
| SAM3 adapter source | [facebookresearch/sam3](https://github.com/facebookresearch/sam3/tree/660a5e9e1b8b4c02c0ad97229b88a09a6e4ff5b), pinned Git dependency; [SAM License](https://github.com/facebookresearch/sam3/blob/main/LICENSE) | Installed only in integrations/sam3; adapter code and checkpoints are not bundled. |
| SAM3 checkpoint mirror | [1038lab/sam3](https://huggingface.co/1038lab/sam3), sam3.pt; mirror/model-page terms apply | Downloaded at runtime after the user authenticates; never committed or included in source release. |

The react-icons package's MIT license does **not** replace the individual icon collections' licenses. Its installed `LICENSE` and `README.md` list those collections. If an artifact contains the entire react-icons package, inventory all included collections, not just the icons imported by LEVI.

## Direct Python dependencies / Python 直接依赖

Runtime dependencies (extras such as `uvicorn[standard]` introduce additional transitive dependencies):

| Package | Version | Declared license |
| --- | --- | --- |
| [python-dotenv](https://pypi.org/project/python-dotenv/1.2.3/) | 1.2.3 | BSD-3-Clause |
| [fastapi](https://pypi.org/project/fastapi/0.141.1/) | 0.141.1 | MIT |
| [uvicorn](https://pypi.org/project/uvicorn/0.52.4/) | 0.52.4 | BSD-3-Clause |
| [pydantic](https://pypi.org/project/pydantic/2.13.5/) | 2.13.5 | MIT |
| [pandas](https://pypi.org/project/pandas/2.3.3/) | 2.3.3 | BSD-3-Clause |
| [pyarrow](https://pypi.org/project/pyarrow/23.0.1/) | 23.0.1 | Apache-2.0 |
| [huggingface-hub](https://pypi.org/project/huggingface-hub/1.30.0/) | 1.30.0 | Apache-2.0 |
| [numpy](https://pypi.org/project/numpy/2.4.6/) | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| [opencv-python-headless](https://pypi.org/project/opencv-python-headless/4.14.0.94/) | 4.14.0.94 | Apache-2.0; bundled components: see LICENSE-3RD-PARTY.txt |

Optional Agent runtime (`uv sync --locked --extra agent`), locked in `uv.lock`:

| Package | Version | Declared license / source |
| --- | --- | --- |
| pydantic-ai-slim (openai extra) | 1.107.6 | MIT — https://github.com/pydantic/pydantic-ai |
| mcp | 1.30.0 | MIT — https://github.com/modelcontextprotocol/python-sdk |
| openai (adapter import; transitive SDK) | 3.16.2 | Apache-2.0 — https://github.com/openai/openai-python |

These packages are installed, not vendored. LEVI's adapters and skills are project code. Lockfile entries include the transitive dependency versions/artifact hashes; container or binary releases still require the full actual-platform inventory and license texts described below. No checkpoint is included by the Agent extra.

Development/test dependencies:

| Package | Version | Declared license |
| --- | --- | --- |
| [pytest](https://pypi.org/project/pytest/9.1.1/) | 9.1.1 | MIT |
| [httpx](https://pypi.org/project/httpx/0.28.1/) | 0.28.1 | BSD-3-Clause |
| [playwright](https://pypi.org/project/playwright/1.62.0/) | 1.62.0 | Apache-2.0 |
| [ruff](https://pypi.org/project/ruff/0.16.6/) | 0.16.6 | MIT |

Read the installed distribution's `METADATA`, `License-File` entries and accompanying license/NOTICE files. NumPy's complete expression is preserved above; NumPy, PyArrow and OpenCV wheels may also carry native libraries and additional notices. Record the exact platform wheel and its bundled components for a binary/container release. Do not classify all files in those wheels using only the package's top-level license.

## Direct JavaScript dependencies / JavaScript 直接依赖

Runtime dependencies:

| Package | Version | Declared package license |
| --- | --- | --- |
| [@huggingface/hub](https://www.npmjs.com/package/@huggingface/hub/v/2.11.0) | 2.11.0 | MIT |
| [@react-three/drei](https://www.npmjs.com/package/@react-three/drei/v/10.7.7) | 10.7.7 | MIT |
| [@react-three/fiber](https://www.npmjs.com/package/@react-three/fiber/v/9.5.0) | 9.5.0 | MIT |
| [hyparquet](https://www.npmjs.com/package/hyparquet/v/1.25.0) | 1.25.0 | MIT |
| [next](https://www.npmjs.com/package/next/v/15.5.25) | 15.5.25 | MIT |
| [react](https://www.npmjs.com/package/react/v/19.2.4) | 19.2.4 | MIT |
| [react-dom](https://www.npmjs.com/package/react-dom/v/19.2.4) | 19.2.4 | MIT |
| [react-icons](https://www.npmjs.com/package/react-icons/v/5.5.0) | 5.5.0 | MIT (package code; icon licenses are separate) |
| [recharts](https://www.npmjs.com/package/recharts/v/2.15.4) | 2.15.4 | MIT |
| [three](https://www.npmjs.com/package/three/v/0.182.0) | 0.182.0 | MIT |
| [urdf-loader](https://www.npmjs.com/package/urdf-loader/v/0.12.6) | 0.12.6 | Apache-2.0 |

Development/build dependencies:

| Package | Version | Declared package license |
| --- | --- | --- |
| [@eslint/eslintrc](https://www.npmjs.com/package/@eslint/eslintrc/v/3.3.3) | 3.3.3 | MIT |
| [@tailwindcss/postcss](https://www.npmjs.com/package/@tailwindcss/postcss/v/4.1.18) | 4.1.18 | MIT |
| [@types/bun](https://www.npmjs.com/package/@types/bun/v/1.3.10) | 1.3.10 | MIT |
| [@types/node](https://www.npmjs.com/package/@types/node/v/20.19.33) | 20.19.33 | MIT |
| [@types/react](https://www.npmjs.com/package/@types/react/v/19.2.14) | 19.2.14 | MIT |
| [@types/react-dom](https://www.npmjs.com/package/@types/react-dom/v/19.2.3) | 19.2.3 | MIT |
| [@types/three](https://www.npmjs.com/package/@types/three/v/0.182.0) | 0.182.0 | MIT |
| [eslint](https://www.npmjs.com/package/eslint/v/9.39.2) | 9.39.2 | MIT |
| [eslint-config-next](https://www.npmjs.com/package/eslint-config-next/v/15.5.25) | 15.5.25 | MIT |
| [prettier](https://www.npmjs.com/package/prettier/v/3.8.2) | 3.8.2 | MIT |
| [tailwindcss](https://www.npmjs.com/package/tailwindcss/v/4.1.18) | 4.1.18 | MIT |
| [typescript](https://www.npmjs.com/package/typescript/v/5.9.3) | 5.9.3 | Apache-2.0 |

Evidence is in each installed `node_modules/<package>/package.json` and its license files. The table identifies direct dependencies only: transitive, optional/platform packages and modules embedded under paths such as `next/dist/compiled` need their own artifact-level inventory. Build dependencies must also be included when actually shipped; the current Dockerfile copies the entire frontend `node_modules` tree into its final image.

## Build and runtime tools / 构建与运行工具

| Component | Version selection | Notice source / 分发时应核对的来源 |
| --- | --- | --- |
| Bun | 1.3.10 in bootstrap and Dockerfile | [Bun license and linked-library notices](https://github.com/oven-sh/bun/blob/bun-v1.3.10/LICENSE.md): Bun itself is MIT; the binary also embeds JavaScriptCore/WebKit and other components with their own terms. |
| uv | User-installed for source setup; Dockerfile selects 0.10.9 | [uv 0.10.9 license files](https://github.com/astral-sh/uv/tree/0.10.9): preserve the selected release's licenses and embedded dependency notices. |
| CPython | `>=3.11,<3.14`; Dockerfile uses floating `python:3.11-slim` | [Python license](https://docs.python.org/3/license.html); record the exact interpreter, image digest, OS packages and their notices per target. |
| FFmpeg / ffprobe and codecs | System packages; Dockerfile installs `ffmpeg` with apt | [FFmpeg licensing](https://ffmpeg.org/legal.html) depends on the build and enabled components. LEVI requests `libx264` encoding; inspect the actual FFmpeg, codec and OS package licenses rather than assuming a single permissive license. |
| Hatchling | Isolated build dependency in `pyproject.toml`, not pinned in `uv.lock` | [Hatch license](https://github.com/pypa/hatch/blob/master/LICENSE.txt), MIT. Capture/pin the actual build-environment version and its dependencies before publishing packages. |
| Playwright browser binaries | Selected/downloaded separately for browser tests | Not included in the source archive. If distributed later, inventory the exact browser build, third-party notices and accompanying native libraries. |

## Generate and maintain distribution notices / 生成与维护分发清单

Before a Docker, PyPI (sdist/wheel), executable or other bundled release:

1. Build the exact candidate for every supported target from its release commit. Record lockfile hashes, platform, build tool versions, base-image digests and artifact hashes. Refresh this source inventory when dependencies, assets or provenance change.
2. Generate a machine-readable dependency inventory/SBOM from the **actual artifact and build environment**. Include direct/transitive packages, extras, optional/native dependencies, compiled/vendored code, interpreters, OS packages, codecs, fonts, icons and other redistributed assets. Mark build-only components separately; do not omit a development dependency that was copied into the final artifact.
3. For each included component, record name, exact version/revision, origin, license expression, copyright holder/notices, license-text locations, local modifications and whether it is bundled or only downloaded at runtime. Cross-check scanner output against package license files and bundled-library notices. Missing or conflicting terms remain unresolved until reviewed; do not silently label them MIT or Apache-2.0.
4. Generate and review the matching `THIRD_PARTY_NOTICES.md` plus complete applicable license/NOTICE texts. Include the required materials inside the distributed wheel/sdist/image/binary bundle and alongside release downloads. Preserve any required source or relinking materials for the exact redistributed build. An SBOM or a table of license names alone does not replace these materials.
5. Inspect the final archive/image contents to verify that the notices and referenced files are present. Record artifact hashes and the review result in the release evidence. Repeat after dependency, base-image, bundling or build-option changes.

未来正式分发前，按实际产物逐一生成机器可读清单，核对传递依赖及原生库，并将完整许可证和适用 NOTICE 随产物交付。对扫描器无法识别的条目保留待核对状态，不能只凭直接依赖表宣称分发清单完整。

This procedure is a release requirement documented in [RELEASING](docs/RELEASING.md); it is not an automated license gate in the current CI. No Docker/PyPI/binary redistribution inventory is claimed complete by this source-only update.

### SAM3 integration dependencies

The SAM3 worker declares its own uv environment and is intentionally absent from the core uv.lock. It adds huggingface-hub for runtime download from the 1038lab/sam3 mirror. Its exact Torch/torchvision wheels, CUDA runtime, transitive dependencies, native libraries and model checkpoint must be inventoried from the actual environment before publishing a Docker/PyPI/binary artifact. The source release does not claim that inventory is complete.

## Optional Pilot integration

`integrations/pilot/package.json` and `bun.lock` pin
`@agentclientprotocol/codex-acp` 1.12.0 and
`@agentclientprotocol/claude-agent-acp` 0.79.0 (Apache-2.0 adapter packages).
Sources: https://github.com/agentclientprotocol/codex-acp and
https://github.com/agentclientprotocol/claude-agent-acp.
Their runtime/SDK dependencies retain their own licenses and service terms;
the adapter license does not relicense Codex/Claude binaries or model services.
Dependencies install locally and are not vendored in LEVI. Generate the complete
locked transitive license inventory before distributing a bundled binary/image.
Happy and OpenCode were reviewed for interaction/architecture only; no code,
assets or hosted services from those projects are included.
