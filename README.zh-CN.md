# LEVI · 机器人数据工坊

**LeRobot Exploration, Validation & Integration**

[![Checks](https://github.com/Koooki3/LEVI/actions/workflows/test.yml/badge.svg)](https://github.com/Koooki3/LEVI/actions/workflows/test.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/LEVI-0.3.0-9bd654.svg)](CHANGELOG.md)

[English](README.md)

**指南** — [转换教程](docs/CONVERSION.md) · [RECAP 导出](docs/RECAP.md) · [Agent 工作台](docs/AGENT_WORKBENCH.md) · [Codex / Claude Pilot](docs/PILOT.zh-CN.md) · [SAM3 对象标注](docs/SAM3.md) · [工作区结构](.state.md)
**参考** — [功能对照](docs/FEATURES.md) · [API](docs/API.md) · [Agent Harness](docs/HARNESS.md) · [审查与验证](docs/VALIDATION.md) · [许可](docs/UPSTREAM.md) · [第三方清单](THIRD_PARTY_NOTICES.md)

LEVI 是用于浏览、标注、转换和审核机器人数据的独立工作台。基于 [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer)，保留多相机与信号同步、视觉问答、动作分析和三维回放，默认英文，可在界面中切换中文；并提供**完全内置的采集数据转换流程**。无需另行下载转换项目或安装训练环境：先检查输入、列出各项要求的满足情况与支持的导出格式，再导出为 LeRobot v2.1 或 RECAP（π\*0.6）价值数据集。原始机器人采集在转换前即可浏览和标注，标注会随转换自动迁移。

界面同时适配独立浏览器和 Hugging Face Space 嵌入环境。标注工作台支持 Ctrl/Cmd+S 保存、Ctrl/Cmd+Z 撤销及播放快捷键；外部浏览器打开时不会触发浏览器原生的“保存网页”对话框。

![LEVI 中文首页](docs/assets/home-zh.png)

演示画面来自下文列出的公开 LeRobot 数据集；界面采用 LEVI 的石墨绿、米白与青柠配色。

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
uv run levi stop                        # 停止共享服务（清理前必须先停）
uv run levi clean                       # 预览可再生缓存与孤儿产物
uv run levi clean --apply               # 终端确认后删除
uv run levi migrate                     # 预演：把旧工作区升级到新命名/布局
uv run levi migrate --apply             # 执行迁移（需先停止服务）
```

从远程服务器访问时，在自己的电脑运行 `ssh -L 7860:127.0.0.1:7860 USER@SERVER`，然后访问本机上述地址。默认前端绑定本机 **7860（网页入口）**，后端绑定本机 **7861（内部 API）**；前端通过同源代理访问后端。不要把本机 7860 转发到服务器 7861。误开后端根路径会显示入口说明及网页链接；`uv run levi backend` 只启动 API。启动器会检查端口冲突，并等待前后端均就绪后才输出网页入口。

## Agent 辅助审阅与标注

标注机器人数据,大部分工作是「看」:来回拖动视频,判断一次尝试从哪一帧开始、有没有成功、哪个物体是哪个。LEVI 把「看」交给 agent,把「判断」交给你。

**agent 只能提议,批准和提交始终属于你。** 这条边界写在能力层里,不靠约定:任何 agent 通道都无法批准计划、接受试点、提交变更集、重置或清理工作区。原始数据集始终只读。每条建议都必须引用它实际读过的帧,而且证据账本比图像本身活得更久——几个月后的审阅者仍能看到当时看了什么。

```bash
uv sync --locked --extra agent
uv run --extra agent levi build
uv run --extra agent levi
```

### 三条驱动路径

区别只有一个要紧的:**谁在花 token**。

| 通道 | 模型运行在 | 谁付费 | 配置方式 |
| --- | --- | --- | --- |
| **在线** | LEVI 内部,调用你配置的端点 | 你在那个端点付费 —— LEVI 自己计量并记账 | **账号与连接** → 模型配置 |
| **外部 MCP** | 你自己的 agent(Claude Code、Codex 等) | agent 自己的上下文,由它自报用量 | `levi agent connect` |
| **托管 Pilot** | LEVI 监督下的 Codex / Claude Code 会话 | 该会话 | [Pilot 指南](docs/PILOT.zh-CN.md) |

外部 MCP 是最常用的一条,下面的流程按它来写。

```text
        你                           LEVI                      你的 agent
         │                             │                              │
  ① 建计划 ├──── 片段、相机 ───────────▶│                              │
         │      任务类型、子任务定义     │                              │
  ② 批准 ────── 冻结范围 ─────────────▶│    （此时不会自动开跑）        │
         │                             │◀── runs.list ────────────────┤ ③ 自己接手
         │                             │─── agent_prepare ───────────▶│
         │                             │◀── evidence.read（拼图）─────┤
         │                             │◀── 提交建议 ─────────────────┤
  ④ 观看 ◀────── 实时动态 ─────────────┤                              │
  ⑤ 审核 ────── 接受 / 拒绝 ──────────▶│                              │
     提交 ──────────────────────────────▶ 修订版 + 产物路径
```

### 一个任务的完整流程

**1 — 建计划。** 打开 **Agent 工作台 → 任务与审核**。表单直接列出数据集实际声明的内容:点击选片段、按住拖动可连选,相机从列表里挑,任务类型在「数据集审阅 / 视频子任务与事件 / 可见物体掩码」中选。做子任务标注时还要写清楚**什么算一个子任务**:何时开始、何时结束、怎样算成功。这些定义就是 agent 标注时对照的契约。

**2 — 批准。** 批准会**冻结范围**:片段、相机、指令、定义,以及源文件的内容摘要。之后不能再扩大。批准不发布任何东西;在 MCP 通道上它也不会启动任何东西——它只是解锁这个计划。

**3 — 交给 agent。** 让你的 agent 去接最新的 run。它调用 `runs.list`,就能看到作用域内每个 run 以及轮到谁:

```
human_approval → agent_prepare → agent_propose → human_review → human_commit
```

不需要你复制 run id。`workspace.get_context` 会把其余的一次性告诉首次接入的 agent:它能做什么、只有你能做什么、按什么顺序工作、以及决定成本的那个习惯。

**4 — 实时观看。** **实时动态**把每个动作即时流式显示:做了什么、针对哪个数据集和片段、耗时多久、被拒绝时的原因。每个 run 还会显示为一张任务卡,带进度和当前在等谁;点击某张卡可把动作流过滤到该任务。

**5 — 审核并提交。** 审核队列逐条显示建议和它引用的帧。接受、拒绝或修改;**unknown 永远不会被悄悄转成人工标签**。提交后,完成提示会给出修订版路径和「查看结果」链接。

### 为什么这些标注值得信任

- **每个结论都引用帧。** 引用了本片段之外证据的建议会被**点名拒绝**,而不是一句笼统报错。
- **不确定性是一等公民。** 「按 2 秒采样,无法分辨其中的各次尝试」是一个合法且会被记录的答案;覆盖缺口会被报告,而不是被抹平。
- **冻结快照会被校验。** 如果源数据在已批准的计划下发生变化,任务会停止,而不是去标注另一份数据。
- **agent 不发布任何东西。** 已提交的修订版带来源记录和逆向补丁,任何一次提交都能经同一条审核路径撤销。

### 成本是实测的,不是猜的

证据就是成本。`evidence.read` 可以每页返回**一张带标注的拼图**而不是每帧一张图,`evidence.refine` 只在 agent 无法判定的边界附近补帧。在一次真实运行中,这两个习惯让 10 个片段的成本相差约 4.3 万 vs 13 万 token。

```bash
uv run levi agent usage show                            # 各 agent 的历史
uv run levi agent usage estimate --workflow temporal --episodes 20
```

`plans.estimate` 在开工前给出区间,并说明依据、样本数、以及是否在向已记录规模之外外推。在线运行无需自报——LEVI 自己计量并记录样本;外部 agent 自报用量,下一次估算随之更准。

### 物体标注:用不用 SAM3 都行

`objects.strategy` 读取本机状态(worker、checkpoint、空闲显存)并给出建议。当 SAM3 跑不起来时,`objects.detect` 不用模型、不用 GPU 就能测量候选区域,返回每个区域的轮廓、位置、形状、中位颜色和一张带标号的叠加图;agent 判读命名后用 `candidate_id` 提交。两条路径最终都进入同一个暂存审核。

掩码只标注在采样帧上,因此**播放条会标出带掩码的帧并支持跳转**;两帧之间会沿用该轨迹最近一次实测的轮廓,以虚线显示并标明来源帧——既有视觉连续性,又不会把没观测过的位置说成观测结果。

### 产物存放在哪

相对 `LEVI_WORKSPACE` 存放在 `outputs/LEVI/workbench/agent/datasets/<数据集名称>/`:每个数据集一个目录,内部的 run 和修订版以**工作类型 + 分钟**命名 —— `temporal-20260920T0926`、`objects-20260920T0940`。没有哈希,没有语义不明的后缀。

```bash
uv run levi agent clean                    # 释放 runs.prepare 可重建的部分
uv run levi agent clean --abandon <run-id> # 关闭没人会继续的 run
uv run levi agent reset --dataset <name>   # 移除某数据集的 agent 历史
```

`clean` 释放已结束 run 的输入快照、证据图像和拼图,保留已提交修订版、来源记录、逆向补丁、证据账本和未完成草稿。`reset` 是它刻意的对应物:移除某一个数据集的工作记录本身,且绝不触碰其他数据集、你的连接,或非 agent 产生的标注。两者都先预演、再确认。

### 已知边界

界面驱动的外部 ACP 会话与 HTTP MCP 仍属后续阶段。自动化验证使用固定响应与替身模型:真实模型的标注质量和 SAM3 GPU 推理需单独测量,已列在[验证记录](docs/VALIDATION.md)中。**跨帧身份无法从相隔数秒的轮廓中恢复** —— LEVI 会如实说明,而不是编造轨迹。

## SAM3 对象标注（可选）

SAM3 在标注页提供模型辅助的对象遮罩。它是可选的：LEVI 不会自行下载 checkpoint，CPU 侧检查也不会导入 Torch 或探测 CUDA。真实作业需要 CUDA 主机上的独立 Python 3.12 worker、一个能读取 `1038lab/sam3` 的 Hugging Face 账号，以及约 7 GB 空闲显存。当显存被占满或 worker 缺失时，agent 可以自行勾画对象，并走同一条审核队列。

```bash
uv run levi sam3 check      # 仅检查配置，不加载模型、不探测 CUDA
```

完整的首次部署顺序、三道门槛、提示词与审核流程见 [SAM3 指南](docs/SAM3.md)。

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

### 默认演示数据集

公开的 LeRobot 数据集，按需联网读取，不打包进 Git：

- [lerobot/svla_so101_pickplace](https://huggingface.co/datasets/lerobot/svla_so101_pickplace) —— SO-101 抓放，50 片段、11,939 帧、30 fps、两路 640×480 相机。
- [lerobot/aloha_static_coffee](https://huggingface.co/datasets/lerobot/aloha_static_coffee) —— 双臂 ALOHA，50 片段、55,000 帧、50 fps、四路 640×480 相机。

两者均为 LeRobot v3.0。若某个演示数据集之后被设为私有或从 Hub 删除，匿名请求会收到 401，查看器会如实说明这一点，而不会报成 LEVI 的权限错误。

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

发布前先 `uv run levi stop` 停止服务，执行 `uv run levi clean` 预览，再 `uv run levi clean --apply`。只清理 LEVI 的可再生成缓存；已登记数据集、标注、审核、转换报告、`.env`、环境与生产构建保留。它同时会报告**已不在目录中的数据集留下的产物**：空目录会被删除，仍存有审核成果的目录只列出不删除——数据集退出目录不应连带删掉别人的标注。不要对共享工作目录使用 `git clean -xfd`。

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
