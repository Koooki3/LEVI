# LEVI · 机器人数据工坊

**LeRobot Exploration, Validation & Integration**

[![Checks](https://github.com/Koooki3/LEVI/actions/workflows/test.yml/badge.svg)](https://github.com/Koooki3/LEVI/actions/workflows/test.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/LEVI-0.2.0-9bd654.svg)](CHANGELOG.md)

[English](README.en.md) · [转换教程](docs/CONVERSION.md) · [功能对照](docs/FEATURES.md) · [API](docs/API.md) · [审查与验证](docs/VALIDATION.md) · [许可](docs/UPSTREAM.md) · [第三方清单](THIRD_PARTY_NOTICES.md)

LEVI 是用于浏览、标注、转换和审核机器人数据的独立工作台。基于 [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer)，保留多相机与信号同步、视觉问答、动作分析和三维回放，提供默认中文与英文切换，以及**完全内置的采集数据转换流程**。无需另行下载转换项目或安装训练环境。

![LEVI 中文首页](docs/assets/home-zh.png)

演示画面来自下文列出的 `samanthalhy` 数据集（数据卡标注 Apache-2.0）；界面采用 LEVI 的石墨绿、米白与青柠配色。

## 安装与启动

准备 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 `ffmpeg` / `ffprobe`。Python、前端依赖与本地 Bun 由项目管理。克隆仓库后，在其根目录运行：

```bash
git clone https://github.com/Koooki3/LEVI.git
cd LEVI
# 仓库所在目录及其父目录可以任意命名
export LEVI_WORKSPACE="$PWD/.state"
export UV_CACHE_DIR="$LEVI_WORKSPACE/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
uv sync --locked
uv run levi setup
uv run levi build
uv run levi serve
```

访问 **http://127.0.0.1:7860**，`Ctrl+C` 停止服务。转换所需的 Python 依赖已包含在 `uv.lock` 中；不需要其他项目的 Python 环境。`setup` 将 Bun 1.3.10 下载到 `.runtime/`，校验官方 SHA-256 清单并安装 `bun.lock` 中的依赖。

已测试 Linux x86_64。安装器支持 Linux/macOS x86_64 与 ARM64；Windows 推荐 WSL2。Ubuntu/Debian 可用 `sudo apt install ffmpeg`，macOS 可用 `brew install ffmpeg`。浏览器支持取决于视频编码，内置转换器输出 H.264/yuv420p。

常用命令：

```bash
uv run levi dev                         # 前端热更新；Python 改动后重启
uv run levi serve --port 7870 --backend-port 7871
uv run levi convert --help              # 不启动网页也能使用转换器
uv run levi clean                      # 预览可清理缓存
uv run levi clean --apply               # 删除预览列表内的可再生成缓存
```

从远程服务器访问时，在自己的电脑运行 `ssh -L 7860:127.0.0.1:7860 USER@SERVER`，然后访问本机上述地址。默认前端绑定本机 7860，后端绑定本机 7861；前端通过同源代理访问后端。

## 工作目录与数据位置

`LEVI_WORKSPACE` 是**数据工作目录**，可以与 Git 仓库分开放在任意磁盘位置。未设置时使用仓库内 `.state/`；相对路径按启动时的当前目录解析。配置可写入不提交 Git 的 `.env`，参见 `.env.example`。项目不识别任何特定机器名称、父目录标记或其他项目环境变量。

| 内容 | 位置 |
| --- | --- |
| 源码与锁文件 | 克隆的仓库 |
| Python / Bun /前端依赖 | 仓库内 `.venv/`、`.runtime/`、`node_modules/` |
| 本地登记、标注、审核、任务与报告 | 工作目录内 `outputs/LEVI/workbench/` |
| 转换产物 | 工作目录内 `datasets/levi_<时间>_<ID>/` |
| 标注导出 | 工作目录内 `outputs/LEVI/exports/` |
| 下载与运行缓存 | 工作目录内 `.cache/`、`tmp/` |

把采集数据放在工作目录内，例如 `$LEVI_WORKSPACE/captures/session-a/`，或将工作目录设置为已有数据的共同父目录。本地登记和转换都会检查真实路径边界。转换输入不接受符号链接，防止快照随外部文件变化。已有目录不会被转换器覆盖，输入数据不会被修改。

已有 LEVI 安装迁移时，在 `.env` 中明确设置原有数据工作目录，即可继续访问原登记、标注与产物。旧版外部转换计划不能执行，需重新创建内置计划；旧数据不会自动移动。

## 浏览、标注与审核

| 页面 | 功能 |
| --- | --- |
| 首页 / 探索 | Hub 搜索、分页浏览、默认演示、本地数据集入口 |
| 片段 | 多相机同步、播放/拖动/快捷键、隐藏/恢复/全屏、状态/动作曲线和数值；多任务数据集可按任务筛选片段列表 |
| 标注 | 持续任务扩写、子任务、计划、记忆；插话、语音和 VQA；视频拖框/点选、时间轴和检查器 |
| 统计 | 元数据、分辨率、总时长、片段长度分布及最短/最长片段 |
| 筛选 / 帧概览 | 运动量、突变、长度排序；首帧/末帧相机网格；单个/批量标记与审核导出 |
| 动作洞察 | 自相关、动作块长度、状态动作时序对齐、示范速度与跨片段方差；分析范围可选全数据集、片段区间或单个任务，采样上限可设为「全部」做全量审查 |
| 三维回放 | 上游支持的机器人模型、关节映射、末端轨迹 |
| 数据诊断 | 版本感知的检查、视频抽样解码、报告/JSON、外部原始 Doctor 入口 |
| 转换与审核 | 内置全流程、独立阶段、参数、计划预览、日志/退出码、自动登记产物 |

浏览器支持视频型 LeRobot v2.0/v2.1/v3.0/v3.1。沿用上游对图片直接嵌入 Parquet 的限制；可先转换为视频数据集。原始任务内容、特征标识和关节名称保留原文。

默认演示：

- [samanthalhy/so100_strawberry_2](https://huggingface.co/datasets/samanthalhy/so100_strawberry_2)
- [samanthalhy/eval_so100_smol_strawberry_2](https://huggingface.co/datasets/samanthalhy/eval_so100_smol_strawberry_2)

视频按需联网读取，不打包进 Git。评估集包含 10 片段、32,033 帧、30 FPS、front/top/hand 三路相机。

## 内置转换：采集 → 过滤 → LeRobot → 验证

「转换与审核」选择输入采集目录和「完整流程」，先预览计划，再执行。流程会准备独立副本、按需处理图像/FPS、质量预检、过滤静止帧、转换为 **LeRobot v2.1**、完整解码验证并登记。高级参数支持相机映射、任务文本映射、排除采集路径、旋转表示、动作语义及质量阈值。

```bash
uv run levi convert pipeline \
  --source captures/session-a \
  --output datasets/session-a-reviewed \
  --fps 10 --source-fps 30
```

这里输入/输出相对 `LEVI_WORKSPACE`；输出必须不存在。CSV、视频与图像必须有明确一一对应关系，不能通过设置 FPS 修复已经错位的数据。源帧 ID 和采集时间保存在转换来源清单中。

详细输入格式、所有阶段、参数 JSON、数学语义、质量门槛与退出码见 [转换教程](docs/CONVERSION.md)。数值状态采用绝对位置（米）和旋转（默认连续 Euler 弧度，可选四元数）；默认 `action[t] = state[t+1]`，最后一帧复用末态。夹爪为命令开合值，**不是测得的夹爪宽度**。这些语义必须与训练策略一致。

网页「数据诊断」使用抽样视频检查；内置转换的 `validate` 阶段逐视频完整解码并验证结构与对齐。两者均是明确范围的数据检查，不等同于某个训练框架的加载或训练成功保证。

## 保存与导出

- 「保存当前片段」写入工作目录的独立标注文件，不改动源数据集。离线时浏览器会话暂存需要恢复服务后再保存。
- 「导出标注数据集」先保存当前编辑，再创建新目录，将语言列写入 Parquet 并携带元数据/视频；不隐式升级数据格式。
- 标注导出的视频默认硬链接，不可用时复制；API 的 `copy_videos=true` 可强制复制。内置转换的快照和输出均使用独立文件。
- 片段标记按数据集隔离；审核 JSON 包含数据集 ID、排除片段 ID 和备注。`meta/levi_provenance.jsonl` 提供内置转换产生的片段到原采集路径的映射，供人工核对后填入 `exclude_demos`。标记不会删除输入数据。
- Hub 上传保留在 API，需要显式目标仓库与令牌。普通保存/转换不会上传，详见 [API](docs/API.md)。

## Hugging Face 与部署

本地使用右上角入口填写 HF 读取令牌；也可配置 OAuth。浏览器令牌保存在本地存储，并设置供视频代理使用的 HttpOnly Cookie，退出时清除。服务器批量读取可设置 `HF_TOKEN`。不要提交令牌或 `.env`。

LEVI 是具备本地文件处理能力的单用户工作台。HF 登录用于 Hub 权限，不是 LEVI 用户管理。共享部署需放在有访问控制的反向代理后；HTTPS/HF iframe 部署设置 `LEVI_SECURE_COOKIES=1`。

```bash
docker build -t levi:local .
docker run --rm -p 127.0.0.1:7860:7860 \
  -v "$HOME/levi-data:/workspace" levi:local
```

容器包括内置转换器、独立 Python 环境及 ffmpeg，无需挂载额外代码项目。仅挂载要交给 LEVI 管理的数据目录。

## 开发、精简与发布

```bash
# 延用安装部分设置的 LEVI_WORKSPACE 和缓存变量
uv sync --locked --group dev
uv run levi check
mkdir -p "$LEVI_WORKSPACE/tmp/build"
uv run pytest --basetemp="$LEVI_WORKSPACE/tmp/build/pytest"
uv run levi build
# 服务启动后，可选真实浏览器检查
PLAYWRIGHT_BROWSERS_PATH="$LEVI_WORKSPACE/.cache/playwright" uv run playwright install chromium
uv run python scripts/verify_browser.py
uv run python scripts/verify_conversion.py
```

发布前执行 `uv run levi clean` 预览，再执行 `uv run levi clean --apply`。只清理 LEVI 的可再生成缓存；已登记数据集、标注、审核、转换报告、`.env`、环境与生产构建保留。不要对共享工作目录使用 `git clean -xfd`。

项目仓库：[Koooki3/LEVI](https://github.com/Koooki3/LEVI)。通过 [Issues](https://github.com/Koooki3/LEVI/issues) 报告问题或建议；参与开发前请阅读 [贡献指南](CONTRIBUTING.md)。维护者发布流程见 [发布指南](docs/RELEASING.md)。

保留 Apache-2.0 `LICENSE`、`NOTICE` 和 [来源说明](docs/UPSTREAM.md)。CI 包含格式、类型、单元/内置转换测试和生产构建。已验证范围与限制见 [验证记录](docs/VALIDATION.md)。

## 常见问题

- **无法访问网页**：核对终端日志、端口和 SSH 转发；浏览器的 localhost 指自己的电脑。
- **Hub 视频加载失败**：检查网络、仓库 ID、访问权限与编码支持；首次获取大视频和模型需要时间。
- **本地路径被拒绝**：登记根目录必须包含 `meta/info.json`，真实路径必须处于 `LEVI_WORKSPACE` 内。
- **转换失败**：阅读结构化报告和日志；重复/缺失帧 ID、未知夹爪命令、视频计数不符会阻止转换。使用任务映射和明确的排除路径修正输入范围。
- **服务重启后任务中断**：任务标记为 interrupted，部分产物保留供检查。新计划使用新目录，不自动续写失败产物。
