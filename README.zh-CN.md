# LEVI · 机器人数据工坊

**LeRobot Exploration, Validation & Integration**

[![Checks](https://github.com/Koooki3/LEVI/actions/workflows/test.yml/badge.svg)](https://github.com/Koooki3/LEVI/actions/workflows/test.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/LEVI-0.3.0-9bd654.svg)](CHANGELOG.md)

[English](README.md) · 简体中文

LEVI 是面向机器人学习数据的本地工作台：浏览 LeRobot 数据集和原始采集数据，转换为训练格式，检查数据质量，并由 AI agent 辅助标注——每一条建议都要经人审核后才会发布。它运行在你自己的机器上：源数据不会被修改；除非你接入远程模型，数据不会离开本机。

![LEVI 界面](docs/assets/home-zh.png)

## 能做什么

| | |
| --- | --- |
| **浏览与分析** | 多相机同步播放、状态/动作曲线、统计、筛选、首尾帧画廊、Action Insights 和 3D 机器人回放；支持 LeRobot v2.0–v3.1，本地或 Hugging Face Hub。源自 [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer)。 |
| **转换** | 把原始采集（CSV + 视频或图片目录）和 LeRobot v2.x 转成 **LeRobot v2.1** 或 **RECAP（π\*0.6）价值数据集**；转换前先检查输入，逐项列出满足了哪些要求。单遍、并行，并提供无损重定时模式。原始采集转换前即可浏览和标注，标注会随转换带过去。 |
| **检查与治理** | 逐集的结构化质量检查（时间戳、动作、视频、元数据）、集级成败标签、审查标记，以及由 agent 完成的内容审查：这条 demo 实际是什么任务、有没有成功。 |
| **agent 辅助标注** | 子任务片段、事件和物体掩码可由外部 MCP agent（Claude Code、Codex 等）、API 模型或 **Ollama 本地模型**提出，再由人审核、提交，并可撤销。一句自然语言即可发起完整任务。 |
| **每个任务都沉淀经验** | harness 在每个任务结束时写下任务账本、实测的 token 与耗时、该数据集上已被人验证的本地记忆，以及可由人发布给下一个任务使用的改进项。 |

## 快速上手

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 FFmpeg（`sudo apt install ffmpeg` 或 `brew install ffmpeg`）。已在 Linux x86_64 上测试；macOS 可用；Windows 请用 WSL2。

```bash
git clone https://github.com/Koooki3/LEVI.git
cd LEVI
export LEVI_WORKSPACE="$PWD/.state"          # 数据集与 LEVI 状态所在目录
export UV_CACHE_DIR="$LEVI_WORKSPACE/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
cp .env.example .env
uv sync --locked --extra agent
uv run levi setup                             # 安装经校验的 Bun 与前端依赖
uv run levi build
uv run levi                                   # 同时启动网页与 API
```

看到 **Ready** 后打开 **http://127.0.0.1:7860**。首页以两个公开的 LeRobot 数据集（[svla_so101_pickplace](https://huggingface.co/datasets/lerobot/svla_so101_pickplace)、[aloha_static_coffee](https://huggingface.co/datasets/lerobot/aloha_static_coffee)）作演示，按需流式读取。把你自己的数据集或原始采集放进 `$LEVI_WORKSPACE`，会被自动登记。Ctrl+C 同时停止两个服务。

远程服务器上请转发网页端口：`ssh -L 7860:127.0.0.1:7860 user@server`。7861 是内部 API，不是工作台页面。

严格仅使用 CPU 时，请在启动服务前设置 `export LEVI_CPU_ONLY=1`。这会禁用后台 GPU 轮询，并阻止本地加速器推理及 SAM3。

DROID 原始目录（`demo_0000/trajectory.h5`、元数据及三路 MP4）是可选的**浏览与标注输入**，目前不支持直接转换成训练数据。使用前运行 `uv sync --locked --extra agent --extra droid`，将数据集目录直接放入 `$LEVI_WORKSPACE`，然后点击 **Sync now** 或重启 LEVI。只读派生视图位于 `outputs/LEVI/workbench/views/<数据集名>/`，不会改动 HDF5 源文件。源 MP4 时间与控制时间不一致，视图采用名义 14.3 FPS；原始时间戳和逐集偏差记录在 `meta/levi_provenance.jsonl`。精确时间边界应复核该记录。详见[转换指南](docs/CONVERSION.md#droid-raw-browsing-view)。新建的工作区还会自动下载一份 DROID 测试数据集：从公开发布版中可复现地抽取 500 集（`uv run levi sample draw` 可再抽 500 集，与之前的抽取不重复），详见 [DROID 测试样本](docs/WORKSPACE.md#droid-test-sample)。

```bash
uv run levi stop                # 停止共享服务及其子进程（--all：连同 LEVI 启动的 Ollama）
uv run levi clean               # 预览可再生缓存（需先停服务）；加 --apply 执行
uv run levi migrate             # 预览旧工作区的升级；加 --apply 执行
uv run levi convert --help
uv run levi agent --help        # 在终端里管理任务、连接、审核、记忆与改进项
```

## agent 辅助，人来把关

agent 读取采样证据并提出建议；只有人能批准计划、验收试点、提交结果。这条边界由网页、REST、MCP 和 CLI 共用的同一层能力强制执行。每条建议都注明依据的帧，"不确定"是合法答案，提交后也能通过同样的审核流程撤销。

| 通道 | 模型 | 配置方式 |
| --- | --- | --- |
| 外部 MCP | 你自己的 agent（Claude Code、Codex 或任意 MCP 客户端） | `uv run levi agent connect --client claude --project <目录> --dataset local/<名称> --apply` |
| 在线 | OpenAI 兼容端点，由 LEVI 计量 | Agent Workbench → Accounts & connections |
| 本地 | 本机 Ollama 模型（默认 `qwen3.5:4b`），由 LEVI 计量，无需 API key | [本地模型](docs/OLLAMA.zh-CN.md) |
| 托管 Pilot | 由 LEVI 监督的 Codex 或 Claude Code 会话 | [Pilot](docs/PILOT.zh-CN.md) |

一个任务的流程是 **规划 → 批准 → 试点 → 试点验收 → 其余集 → 审核 → 提交**。使用本地模型时，一句话就能发起：

```bash
uv run levi agent task new "对 <数据集> 进行数据质量检查，然后对前10条demo进行子任务标注，最后给出全流程token和耗时统计" --provider qwen-local
```

LEVI 会按数据目录校验解析出的任务规格，等你批准后才会执行。每个任务前后，**harness** 都会记录成本（token 与耗时，按 agent 和数据集统计），保存经人验证的事实作为下一个任务的起点，并提出改进项，由你评估和发布。GPU 与机器人训练共用时，本地模型自动错峰让路。详见 [Agents](docs/AGENTS.md)。

## 文档

| 指南 | 内容 |
| --- | --- |
| [Agents](docs/AGENTS.md) | 通道、任务生命周期、自然语言任务、证据、物体掩码、harness（成本、记忆、自改进、老师监督）、能力参考 |
| [本地模型](docs/OLLAMA.zh-CN.md) · [English](docs/OLLAMA.md) | Ollama 安装、模型绑定、GPU 错峰、师生学习循环 |
| [Pilot](docs/PILOT.zh-CN.md) · [English](docs/PILOT.md) | 托管的 Codex / Claude Code 会话 |
| [数据质量](docs/QUALITY.md) | 结构化检查、内容审查、成败标签、审查标记与导出清单 |
| [转换](docs/CONVERSION.md) | 输入格式、检查、帧时间模式、选项、性能、溯源 |
| [RECAP](docs/RECAP.md) | RECAP 价值数据集格式与使用方式 |
| [SAM3](docs/SAM3.md) | 可选的模型辅助物体掩码 |
| [评测记录](docs/EVALUATION.md) | 记录人工标注过程；人工与 agent 子任务标注的质量与成本记录（逐条可比） |
| [内置知识](docs/KNOWLEDGE.md) | LEVI 中所有模型遵循的通用规则，以及本地记忆如何提升为内置知识 |
| [工作区](docs/WORKSPACE.md) | `LEVI_WORKSPACE` 下各类文件的位置、命名、同步与清理 |
| [API](docs/API.md) | REST 路由与 agent 能力 |
| [验证记录](docs/VALIDATION.md) | 测了什么、用什么测、哪些还没测 |
| [架构进展](docs/architecture/IMPLEMENTATION_STATUS.md) | 相对架构规划的实现进度 |
| [上游](docs/UPSTREAM.md) · [发布](docs/RELEASING.md) · [更新日志](CHANGELOG.md) | 来源与功能对照、发布流程、变更记录 |

## 工作区

`LEVI_WORKSPACE` 存放数据和状态，可以放在仓库之外（默认 `.state/`）。数据集直接放在它下面；LEVI 自己的状态在 `outputs/LEVI/`；模型权重在 `checkpoints/`。名称与数据集和集编号对应（`episode_000007`），每次运行用可读的时间戳（`temporal-20260922T0941`），从不使用哈希。LEVI 运行时持续与工作区同步：拷入的数据集自动登记，变化的会刷新，删除的会移除。详见 [工作区](docs/WORKSPACE.md)。

## 部署

LEVI 是有文件访问权限的单用户本地工作台。Hugging Face 登录（令牌、OAuth 或后端的 `HF_TOKEN`）只控制 Hub 访问，不是 LEVI 的用户权限；浏览器中的令牌保存在本地存储和一个 HttpOnly 视频代理 cookie 里，退出登录时清除。切勿提交凭据或 `.env`；多人共享部署请放在带认证的反向代理之后，HTTPS 或 Space 嵌入请设置 `LEVI_SECURE_COOKIES=1`。上传到 Hub 需要显式的 API 操作；转换和保存从不上传。

```bash
docker build -t levi:local .
docker run --rm -p 127.0.0.1:7860:7860 -v "$HOME/levi-data:/workspace" levi:local
```

## 现状与限制

当前发布版本为 **0.3.0**；`main` 分支包含尚未发布的 agent harness、本地模型和数据治理功能，见[更新日志](CHANGELOG.md)。证据是采样的：密集细化只覆盖被指向的区间，无法证明其他地方没有发生事件。模型标注质量按数据集实测，不做笼统承诺；自动化测试使用固定样例和模拟模型，本地模型的标注质量仍在验证中。MCP 目前只支持 stdio。尚无多用户权限控制、操作系统级离线沙箱或 GPU 调度器。详见[验证记录](docs/VALIDATION.md)。

## 常见问题

- **看到 `{"detail":"Not Found"}` 或 API 说明页**：请求进入了 7861；请打开 7860，并确认 SSH 或编辑器的端口转发目标是服务器的 7860。
- **本地路径被拒绝**：必须是 LeRobot 数据集（含 `meta/info.json`）或可识别的原始采集（`task/demo_NNNN`，或 DROID 的 `demo_NNNN/trajectory.h5`），且真实路径位于 `LEVI_WORKSPACE` 内。
- **转换失败**：查看输入检查清单和任务日志。重复或缺失的帧 ID、未知夹爪命令、视频数量不符会在发布任何结果前阻止转换；源数据不会被修改。
- **本地模型任务被阻塞**：原因里会写明正在使用 GPU 的进程；GPU 空闲满 `LEVI_GPU_QUIET_SECONDS` 后即可继续。
- **服务重启后任务中断**：任务会标记为 interrupted，不会自动续写；请重新规划。

## 开发

```bash
uv sync --locked --group dev --extra agent
uv run levi check
mkdir -p "$LEVI_WORKSPACE/tmp/build"
uv run pytest --basetemp="$LEVI_WORKSPACE/tmp/build/pytest"
export PATH="$(ls -d "$PWD"/.runtime/bun-*):$PATH"   # levi setup 安装的 Bun
bun run format && bun run validate                # 类型检查、lint、格式、前端测试
uv run levi build
```

CI 运行同样的检查和生产构建。提交改动前请阅读 [CONTRIBUTING](CONTRIBUTING.md)；问题请提交到 [Issues](https://github.com/Koooki3/LEVI/issues)。LEVI 采用 Apache-2.0 许可，保留上游的 `LICENSE`、`NOTICE` 与[来源说明](docs/UPSTREAM.md)；另见[第三方声明](THIRD_PARTY_NOTICES.md)。
