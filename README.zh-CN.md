# LEVI · 机器人数据工坊

Codex / Claude Code 无界面工具链、托管会话、终端人工审批和实时追踪见 [Pilot 顺序指南](docs/PILOT.zh-CN.md)。

**LeRobot Exploration, Validation & Integration**

[![Checks](https://github.com/Koooki3/LEVI/actions/workflows/test.yml/badge.svg)](https://github.com/Koooki3/LEVI/actions/workflows/test.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/LEVI-0.3.0-9bd654.svg)](CHANGELOG.md)

[English](README.md) · [转换教程](docs/CONVERSION.md) · [RECAP 导出](docs/RECAP.md) · [工作区结构](.state.md) · [功能对照](docs/FEATURES.md) · [API](docs/API.md) · [审查与验证](docs/VALIDATION.md) · [许可](docs/UPSTREAM.md) · [第三方清单](THIRD_PARTY_NOTICES.md) · [SAM3 对象标注](docs/SAM3.md) · [Agent 工作台](docs/AGENT_WORKBENCH.md)

LEVI 是用于浏览、标注、转换和审核机器人数据的独立工作台。基于 [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer)，保留多相机与信号同步、视觉问答、动作分析和三维回放，默认英文，可在界面中切换中文；并提供**完全内置的采集数据转换流程**。无需另行下载转换项目或安装训练环境：先检查输入、列出各项要求的满足情况与支持的导出格式，再导出为 LeRobot v2.1 或 RECAP（π\*0.6）价值数据集。原始机器人采集在转换前即可浏览和标注，标注会随转换自动迁移。

界面同时适配独立浏览器和 Hugging Face Space 嵌入环境。标注工作台支持 Ctrl/Cmd+S 保存、Ctrl/Cmd+Z 撤销及播放快捷键；外部浏览器打开时不会触发浏览器原生的“保存网页”对话框。

![LEVI 中文首页](docs/assets/home-zh.png)

演示画面来自下文列出的 `samanthalhy` 数据集（数据卡标注 Apache-2.0）；界面采用 LEVI 的石墨绿、米白与青柠配色。

## 安装与启动

准备 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 `ffmpeg` / `ffprobe`。Python、前端依赖与本地 Bun 由项目管理。克隆仓库后，在其根目录运行：

```bash
git clone https://github.com/Koooki3/LEVI.git
cd LEVI
# 仓库所在目录及其父目录可以任意命名
export LEVI_WORKSPACE="$PWD/.state"
cp .env.example .env
export UV_CACHE_DIR="$LEVI_WORKSPACE/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
uv sync --locked
uv run levi setup
uv run levi build
uv run levi serve
```

`uv run levi` 等同于 `uv run levi serve`，会同时启动网页和 API。看到 **已就绪 / Ready** 后，访问 **http://127.0.0.1:7860**；`Ctrl+C` 停止两个服务。转换所需的 Python 依赖已包含在 `uv.lock` 中；不需要其他项目的 Python 环境。`setup` 将 Bun 1.3.10 下载到 `.runtime/`，校验官方 SHA-256 清单并安装 `bun.lock` 中的依赖。

已测试 Linux x86_64。安装器支持 Linux/macOS x86_64 与 ARM64；Windows 推荐 WSL2。Ubuntu/Debian 可用 `sudo apt install ffmpeg`，macOS 可用 `brew install ffmpeg`。浏览器支持取决于视频编码，内置转换器输出 H.264/yuv420p。

常用命令：

```bash
uv run levi dev                         # 前端热更新；Python 改动后重启
uv run levi serve --port 7870 --backend-port 7871
uv run levi convert --help              # 不启动网页也能使用转换器
uv run levi clean                      # 预览可清理缓存
uv run levi clean --apply               # 删除预览列表内的可再生成缓存
uv run levi migrate                     # 预演：把旧工作区升级到新命名/布局
uv run levi migrate --apply             # 执行迁移（需先停止服务）
```

从远程服务器访问时，在自己的电脑运行 `ssh -L 7860:127.0.0.1:7860 USER@SERVER`，然后访问本机上述地址。默认前端绑定本机 **7860（网页入口）**，后端绑定本机 **7861（内部 API）**；前端通过同源代理访问后端。不要把本机 7860 转发到服务器 7861。误开后端根路径会显示入口说明及网页链接；`uv run levi backend` 只启动 API。启动器会检查端口冲突，并等待前后端均就绪后才输出网页入口。

## Agent 辅助审阅与标注（实验性）

**Agent 工作台**提供基于证据的草稿和集中人工审核队列。SAM3 是其中可选的对象标注工具；原始数据集始终只读。[完整指南、MCP 配置、架构和限制](docs/AGENT_WORKBENCH.md)。

完成普通安装后，依次执行：

```bash
export LEVI_WORKSPACE="$PWD/.state"
uv sync --locked --extra agent
uv run --extra agent levi build
uv run --extra agent levi
```

1. 打开 **Accounts & connections（账号与连接）**，配置兼容接口地址、模型 ID，并明确声明图像能力。密钥通过服务端环境变量或仅保留于服务端内存的会话输入提供；卡片支持选择、编辑、断开、重连和移除。HF 账户使用独立的切换／退出菜单，不等同于 Agent 提交权限。
2. 选择数据集、明确的 episode／相机范围、指令与预算；按需允许证据发送到选定端点，点击 **Inspect & create plan（检查并创建计划）**。远端固定 commit，本地创建有内容摘要的独立快照。
3. 先 **Run pilot（小范围试运行）**，检查证据后 **Execute remaining（执行剩余）**。恢复时保留已完成分片和人工草稿修改。
4. 需要对象遮罩时打开 **SAM3 · optional object tool**，规划有限帧范围，再明确启动已配置的 worker。逐帧看叠加遮罩，按轨迹或相机批量接受／拒绝；修订保留在暂存区。此工具只使用已有 checkpoint，不自动下载。
5. 按待审核、问题或标注类型筛选；使用 J/K 导航，逐项或批量接受／拒绝，修改文本及时间边界。先保存草稿修改，再记录审核决定；跟随证据不会自动离开未保存的编辑页面。
6. 依次 **Validate & approve（校验并批准）**、**Commit approved changes（提交已批准变更）**。未知结果不会成为人工成功／失败标签，拒绝项不提交；版本冲突须重新审核。**Create undo draft（创建撤销草稿）**也需要人工批准。
7. 重启后恢复凭据再继续中断任务，或直接审核已完成的部分。使用现有数据集导出入口生成含原生语言列、对象 sidecar 和来源记录的数据；原始采集先转换，按源帧映射迁移标注。

产物相对于 `LEVI_WORKSPACE` 存放在 `outputs/LEVI/workbench/agent/datasets/<数据集名称>/`，按可读时间戳组织运行和版本，LEVI 产物名称不使用哈希后缀。MCP 外部 Agent 可读取允许范围并生成草稿，不能自行批准或提交。由界面驱动外部 ACP Agent、HTTP MCP 仍属后续阶段。自动化验证使用固定响应与替身；真实模型质量及 SAM3 GPU 推理需另行人工验收，不在本次自动测试内。

## SAM3 首次部署顺序

在克隆后的 LEVI 根目录按以下顺序执行；目录名称和工作区绝对路径可任意：

~~~bash
# 1) 设置工作区并安装核心依赖
export LEVI_WORKSPACE="$PWD/.state"
cp .env.example .env
uv sync --locked
uv run levi setup

# 2) 登录可读取 1038lab/sam3 的 Hugging Face 账号
HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" hf auth login
HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" hf auth whoami
# 没有全局 hf 时使用：
# HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" uvx hf auth login
# HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" uvx hf auth whoami

# 3) 在 CUDA 主机安装独立 worker
uv venv --python 3.12 integrations/sam3/.venv
uv sync --project integrations/sam3
export LEVI_SAM3_WORKER_PYTHON="$PWD/integrations/sam3/.venv/bin/python"
export LEVI_SAM3_ENABLED=1

# 4) 只做无模型配置检查
uv run --project integrations/sam3 levi-sam3-worker --check
uv run levi sam3 check

# 5) 构建并启动网页
uv run levi build
uv run levi serve
~~~

看到 Ready 后访问 http://127.0.0.1:7860，在任意数据集的标注页面按状态卡完成 Hub access、CUDA worker、Checkpoint 三项检查。登录后若 checkpoint 尚未缓存，页面会显示明确的下载提示、保存路径、进度条和“下载 checkpoint”按钮；点击后等待状态变为“Checkpoint 已就绪”，再设置 prompt、范围和相机并运行 SAM3。下载使用当前 Hugging Face 会话并保存到 $LEVI_WORKSPACE/checkpoints/sam3，后续数据集复用该文件；失败时可直接重试。切换 Hugging Face 账号或工作区时，LEVI 会按账号 digest 和工作区重新隔离 Hub 快照与 sidecar。可设置 LEVI_SAM3_ENABLED=0 暂时隐藏真实 worker。不要提交 token、checkpoint 或工作区数据。

## 工作目录与数据位置

`LEVI_WORKSPACE` 是**数据工作目录**，可以与 Git 仓库分开放在任意磁盘位置。未设置时使用仓库内 `.state/`；相对路径按启动时的当前目录解析。配置可写入不提交 Git 的 `.env`，参见 `.env.example`。项目不识别任何特定机器名称、父目录标记或其他项目环境变量。

| 内容 | 位置 |
| --- | --- |
| 源码与锁文件 | 克隆的仓库 |
| Python / Bun /前端依赖 | 仓库内 `.venv/`、`.runtime/`、`node_modules/` |
| 本地登记、标注、结局标签、审核、任务与报告 | 工作目录内 `outputs/LEVI/workbench/` |
| 原始采集的浏览视图 | 工作目录内 `outputs/LEVI/workbench/views/<名称>/` |
| 转换产物（默认） | 工作目录内 `<源名>_<lerobot\|recap>_<时间戳>/`，该目录本身就是数据集 |
| 标注导出 | 工作目录内 `outputs/LEVI/exports/<名称>_annotated/` |
| 下载与运行缓存 | 工作目录内 `.cache/`、`tmp/` |

把采集数据放在工作目录内，例如 `$LEVI_WORKSPACE/captures/session-a/`，或将工作目录设置为已有数据的共同父目录。本地登记和转换都会检查真实路径边界。转换输入不接受符号链接，防止快照随外部文件变化。已有目录不会被转换器覆盖，输入数据不会被修改。

LEVI 运行时会持续与工作区同步：拷入的数据集会自动登记；片段/demo 的增减会被识别（原始采集自动重建浏览视图、标注按 demo 重新对应）；被删除的数据集会从列表消失；正在查看的数据集在磁盘上变化时会提示重新加载。可用 `LEVI_SYNC_INTERVAL`、`LEVI_SYNC_SETTLE`、`LEVI_SYNC_DISCOVER` 调整或关闭（见 `.env.example` 与 [工作区结构](.state.md#runtime-sync)）。

所有名称都不带哈希后缀：每个数据集一份的产物（标注、审核、诊断、导出）以数据集的登记名命名，每次运行一份的产物（任务、转换输出、SAM3 修订）以时间戳命名。从旧版 LEVI 升级时，在 `.env` 中设置原工作目录、停止服务后运行 `uv run levi migrate` 预演，再加 `--apply` 执行；旧的 `/local/<哈希>` 链接会自动跳转。详见 [工作区结构](.state.md)。

## 浏览、标注与审核

| 页面 | 功能 |
| --- | --- |
| 首页 / 探索 | Hub 搜索、分页浏览、默认演示、本地数据集入口 |
| 片段 | 多相机同步、播放/拖动/快捷键、隐藏/恢复/全屏、状态/动作曲线和数值；多任务数据集可按任务筛选片段列表 |
| 标注 | 持续任务扩写、子任务、计划、记忆；插话、语音和 VQA；SAM3 对象/轨迹 sidecar；视频拖框/点选、时间轴和检查器 |
| 统计 | 元数据、分辨率、总时长、片段长度分布及最短/最长片段 |
| 筛选 / 帧概览 | 运动量、突变、长度排序；首帧/末帧相机网格；单个/批量标记与审核导出 |
| 动作洞察 | 自相关、动作块长度、状态动作时序对齐、示范速度与跨片段方差；分析范围可选全数据集、片段区间或单个任务，采样上限可设为「全部」做全量审查 |
| 三维回放 | 上游支持的机器人模型、关节映射、末端轨迹 |
| 数据诊断 | 版本感知的检查、视频抽样解码、报告/JSON、外部原始 Doctor 入口 |
| 转换与审核 | 输入检查与要求清单、各导出格式的兼容性（原因与一键解决）、LeRobot v2.1 / RECAP 导出、单遍并行转换与无损重定时、实时进度、自动登记；数据集列表显示格式、版本与来源 |
| 原始采集 | 登记后生成无损浏览视图，可查看、统计、标注、SAM3、结局标签；导出需先转换，标注随转换迁移 |

浏览器支持视频型 LeRobot v2.0/v2.1/v3.0/v3.1。沿用上游对图片直接嵌入 Parquet 的限制；可先转换为视频数据集。原始任务内容、特征标识和关节名称保留原文。

### SAM3 对象标注（全局）

标注页对演示集、Hub 数据集和登记的本地数据集统一提供 SAM3 对象/轨迹 sidecar。界面先检查 Hub 访问、CUDA worker 和 checkpoint 三道门槛，再按片段范围／任务／全量及相机组合生成并校验标注计划；每个 episode/camera 组合独立处理，模型建议写入独立的无损 RLE sidecar，原生 LeRobot 文件保持只读。页面会显示当前 Hugging Face 账号、worker 状态、checkpoint 下载进度和工作区保存位置，并按轨迹提供帧区间与接受／拒绝审核。默认模型为 1038lab/sam3 的 sam3.pt；首次真实作业需要独立 Python 3.12 uv worker 和 CUDA 主机，核心 CPU 检查不会导入 Torch、探测 CUDA、下载模型或执行推理。完整顺序见 SAM3 指南。

默认演示：

- [samanthalhy/so100_strawberry_2](https://huggingface.co/datasets/samanthalhy/so100_strawberry_2)
- [samanthalhy/eval_so100_smol_strawberry_2](https://huggingface.co/datasets/samanthalhy/eval_so100_smol_strawberry_2)

视频按需联网读取，不打包进 Git。评估集包含 10 片段、32,033 帧、30 FPS、front/top/hand 三路相机。

## 内置转换：检查 → 选择导出 → 运行

在「转换与审核」中输入原始采集目录（`task/demo_NNNN`，含位姿/夹爪 CSV 及每路相机的视频或图像文件夹）或 LeRobot v2.x 数据集，点击「检查输入」。LEVI 自动识别格式（遥操作或策略 rollout 采集、图像序列、LeRobot），逐项列出要求及其状态——需要完整解码才能判断的项目标为「转换时检查」，不会显示为已通过——并评估每种导出：

| 导出 | 用途 | 默认帧时间处理 |
| --- | --- | --- |
| LeRobot v2.1 | 模仿学习、openpi、LEVI 查看器 | `resample`：重采样到目标 FPS（自动降到实测帧率），过滤静止帧 |
| RECAP 价值数据集（π\*0.6） | 训练 RECAP 价值函数；兼容 RLinf 的 `meta/returns.parquet`、`is_success`、逐步奖励 | `retime`：保留每个采集帧，视频无损流拷贝 |

某种导出不可用时（例如遥操作数据缺少成功/失败标签而无法导出 RECAP），卡片会说明原因并给出解决办法：在 LEVI 中标注结局、排除相关片段，或作为示教数据导出。选择导出、调整参数、审阅计划后运行；任务会显示阶段、进度、当前片段和剩余时间。结果先写入隐藏的暂存目录，只有全部片段通过预检且数据集校验通过才会发布。

```bash
uv run levi convert inspect  --source captures/session-a --output unused
uv run levi convert pipeline --source captures/session-a --output session-a-lerobot
uv run levi convert pipeline --source captures/session-a --output session-a-recap \
  --options configs/recap.json   # {"target": "recap_value"}
```

这里输入/输出相对 `LEVI_WORKSPACE`；输出必须不存在。每路相机只处理一遍：源视频解码一次，同时完成预检扫描和唯一一次 H.264 编码；`retime` 模式下视频只做重封装，时间戳精确为 `i / fps`，像素逐位一致。转换在多个工作进程中并行执行。CSV、视频与图像必须有明确一一对应关系，不能通过设置 FPS 修复已经错位的数据。RECAP 的格式依据（论文、RLinf、LeRobot 提案）及使用方法见 [RECAP.md](docs/RECAP.md)。

### 原始采集：浏览、标注、转换

在「本地数据集」中像登记普通数据集一样登记原始采集目录。LEVI 在后台构建浏览视图（只做视频重封装，数秒完成），之后即可在查看器中逐帧浏览。查看、统计、筛选、帧概览、动作洞察、语言/事件标注、SAM3、结局标签和审核标记均可使用；数据诊断针对浏览视图；导出会提示先转换。转换时，标注、结局标签和 SAM3 掩码会迁移到新数据集的对应帧（报告见 `meta/levi_annotation_carryover.json`）。

详细输入格式、所有阶段、参数 JSON、数学语义、质量门槛与退出码见 [转换教程](docs/CONVERSION.md)。数值状态采用绝对位置（米）和旋转（默认连续 Euler 弧度，可选四元数）；默认 `action[t] = state[t+1]`，最后一帧复用末态。夹爪为命令开合值，**不是测得的夹爪宽度**。这些语义必须与训练策略一致。

网页「数据诊断」使用抽样视频检查；内置转换会核对每个视频的帧数、帧率、分辨率与 parquet 及元数据是否一致。两者均是明确范围的数据检查，不等同于某个训练框架的加载或训练成功保证。

## 保存与导出

- 「保存当前片段」写入工作目录的独立标注文件，不改动源数据集。离线时浏览器会话暂存需要恢复服务后再保存。
- 「导出标注数据集」先保存当前编辑，再创建新目录，将语言列写入 Parquet 并携带元数据/视频；不隐式升级数据格式。人工结局标签以 `levi_outcome` 写入 `meta/episodes.jsonl`。原始采集的浏览视图不能导出，请先转换。
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

- **看到 `{"detail":"Not Found"}` 或 API 说明页**：说明请求可能进入了 7861；打开 7860，并核对 SSH / 编辑器端口转发目标是否为服务器 7860。不要将终端中 Uvicorn 的监听地址当作网页入口。
- **无法访问网页**：核对终端日志、端口和 SSH 转发；浏览器的 localhost 指自己的电脑。
- **Hub 视频加载失败**：检查网络、仓库 ID、访问权限与编码支持；首次获取大视频和模型需要时间。
- **本地路径被拒绝**：登记目录必须是 LEVI 数据集（含 `meta/info.json`）或可识别的原始采集（`task/demo_NNNN`），真实路径必须处于 `LEVI_WORKSPACE` 内。
- **转换失败**：先看「检查输入」的要求清单和任务日志；重复/缺失帧 ID、未知夹爪命令、视频计数不符会在发布任何结果之前阻止转换，源数据不会被修改。可使用任务映射和明确的排除路径修正输入范围。
- **服务重启后任务中断**：任务标记为 interrupted，不自动续写；重新规划会使用新目录。


Harness 执行计划批准、试标验收、视频证据、模块接口及当前限制，见 [Agent Harness](docs/HARNESS.md)。
