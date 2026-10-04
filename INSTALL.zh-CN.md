# 安装 LEVI

从一台空机器到 LEVI 跑起来，再逐个装可选部分。[English](INSTALL.md) · 由 AI agent 来安装时看 [INSTALL.agent.md](INSTALL.agent.md)。

两个命令完成安装并告诉你还缺什么：

- `uv run levi install --profile <组件> [--plan] [--json] [--yes]` 安装一个组件。可以反复运行（已完成的步骤跳过），能自动做的自己做，需要人做的列出来。`--plan` 只列步骤（是否联网、下载多大、是否需要 root、令牌或人的同意），不做任何改动。
- `uv run levi doctor [--json]` 只读地检查本机：Python 和 uv、ffmpeg、Bun 与前端依赖、生产构建是否与源码一致、端口 7860/7861、工作区（可写、路径够短能放下核心的 socket、磁盘空间）、训练池设置（只报设没设，从不显示值）、可选 worker 及其权重、Hugging Face 令牌（只报有没有）、GPU 和驱动、本地模型服务（只在回环地址上 `GET`）、实时服务的配置。

两者的退出码：**0** 完成/一切正常，**1** 有警告（doctor），**2** 有一个用命令就能修的失败（报告里写了命令），**10** 剩下的需要人（root、令牌、许可、选择），或大下载需要 `--yes`。`levi install --plan` 在没有要做的事时退出码 0，只剩自动步骤时 1，还有需要人的步骤时 10。

## 1. 开始之前

| | |
| --- | --- |
| 平台 | 测试过的平台是 Linux x86_64。进程跟踪、GPU 守护和实时服务读 `/proc`；macOS 和 WSL2 没有验证 |
| 工具 | `git`、[uv](https://docs.astral.sh/uv/getting-started/installation/)、带 `ffprobe` 的 `ffmpeg`（`sudo apt install ffmpeg`，或不用 root、把静态二进制放进 `PATH`）。不需要 Node.js（LEVI 自带固定版本、校验过的 Bun），只有托管 Pilot 需要 Node.js 22+ |
| 磁盘 | LEVI 及其依赖约 2 GiB，加上你的数据；每个可选 worker 5–8 GiB，本地模型 3–30 GiB |
| GPU | 可选。本地模型、SAM3、快速分割和 RECAP 价值模型需要 NVIDIA 驱动。`LEVI_CPU_ONLY=1` 关闭所有 GPU 功能 |

## 2. 核心

```bash
git clone https://github.com/Koooki3/LEVI.git && cd LEVI
export LEVI_WORKSPACE="$PWD/.state"      # 数据集和 LEVI 状态的位置（任意目录；路径尽量短）
uv run --locked levi install --profile core --plan    # 先看要做什么
uv run --locked levi install --profile core           # Python 依赖、.env、工作区、Bun 和前端依赖、构建
uv run levi doctor
uv run levi                                           # 启动网页（7860）和 API（7861）
```

`uv run` 第一次使用时建好 Python 环境。core 组件接着运行 `uv sync --locked --inexact`（保留已经装的包，例如开发者的 dev 依赖组），没有 `.env` 时把 `.env.example` 复制成 `.env`（已有的 `.env` 绝不改动），建工作区，运行 `levi setup`（Bun 和 `bun install --frozen-lockfile`）和 `levi build`。然后打开 http://127.0.0.1:7860。`node_modules` 与 `bun.lock`/`package.json` 不一致时（不存在；两个文件的内容与上次安装时记录的不同——`node_modules/.levi-deps-installed` 记着它们的 sha256，没有这份记录时安装一次；或缺少 `package.json` 列出的某个依赖），`levi build` 会先用 LEVI 自带的 Bun 运行 `bun install --frozen-lockfile` 再构建；仍然构建失败时，它提示运行 `uv run levi setup`。本检出的 LEVI 在运行时（产品 LEVI，或从本检出启动的实时服务：两者的 `next start` 都读本检出的 `node_modules` 和 `.next`），`levi build` 拒绝构建（退出码 3），并说明要先停什么（`uv run --no-sync levi stop`、`uv run --no-sync levi live stop`）；`levi build --while-serving` 仍然构建，但会给出警告、不安装依赖，正在运行的页面在重启之前可能出错。

工作区路径有长度要求：核心在工作区里监听一个 Unix socket，socket 路径不能超过 103 字节，路径太长时 `levi doctor` 报失败。新工作区不会自己下载任何东西；需要 DROID 测试样本时运行 `uv run levi sample fetch droid`（500 个片段，约 11.6 GiB，见[工作区](docs/WORKSPACE.md#droid-test-sample)）。

设置写在 `.env` 里（`.env.example` 列出 LEVI 读取的所有变量及其作用）。通常要设的：

| 变量 | 什么时候设 |
| --- | --- |
| `LEVI_WORKSPACE` | 总是，除非检出目录下的 `.state` 正合适 |
| `LEVI_POOL_ROOTS`、`LEVI_POOL_HELDOUT`、`LEVI_EXPORT_ROOTS` | 训练池：要索引的只读目录、留出清单（不设时拒绝导出；`none` 表示没有留出集，由人决定）、导出可以写到哪里（[训练池](docs/TRAINING_POOL.md)） |
| `LEVI_GPU_LOCK_FILE` | GPU 与其他工具共用，且它们使用同一个 `flock` 文件 |
| `HF_TOKEN` | 私有或受限的 Hugging Face 仓库、SAM3 权重、上传（也可以在网页里登录） |
| `LEVI_CPU_ONLY=1` | 没有 GPU，或不允许 LEVI 使用 GPU |

## 3. 可选组件

每个组件运行 `uv run levi install --profile <名字>`（可以一次给多个 `--profile`；总会包含 `core`）。超过 1 GiB 的下载只有加 `--yes` 才会进行。

| 组件 | LEVI 自动做 | 需要人做 | 文档 |
| --- | --- | --- | --- |
| `agent` | 带 `agent` extra 的 `uv sync`（MCP、模型运行） | `levi agent connect --client claude\|codex --project <目录> --dataset local/<名字> --apply`：agent 能看哪些数据集由人决定 | [Agents](docs/AGENTS.md) |
| `local-model` | `agent` extra | 安装 Ollama（或搭 vLLM 环境），选并下载模型，在网页里创建并绑定模型档案 | [本地模型](docs/OLLAMA.zh-CN.md)、[vLLM](docs/VLLM.zh-CN.md) |
| `segmentation` | `integrations/segmentation/setup.sh`（Python 3.12、Torch CUDA，约 5 GiB） | 无（基础权重首次使用时下载，不需要令牌） | [快速分割](docs/SEGMENTATION.md) |
| `sam3` | SAM3 worker 环境（`integrations/sam3`，约 5 GiB） | 登录 Hugging Face，接受 SAM 许可，在网页里下载权重 | [SAM3](docs/SAM3.md) |
| `recap` | `integrations/recap_value/setup.sh`（约 7 GiB） | 导入一个 RECAP 价值检查点（LEVI 不附带） | [RECAP](docs/RECAP.md) |
| `live` | `agent` extra | 搭 vLLM 环境并下载权重，写一个写明本机 rollout 目录的 `live.toml` | [实时服务](docs/LIVE.zh-CN.md)、[vLLM](docs/VLLM.zh-CN.md) |
| `pilot` | 在 `integrations/pilot` 里 `bun install` | Node.js 22+ 和已登录的 `codex` 或 `claude` CLI | [Pilot](docs/PILOT.zh-CN.md) |

worker 环境使用 Torch 的 CUDA 12.8 wheel；驱动比这旧、或没有 NVIDIA GPU 的机器不能直接运行它们。

## 4. 作为服务运行

`levi serve` 在前台运行。要让它常驻，可以用 systemd **用户**单元（模板，维护者没有测试过；按实际改路径）：

```ini
# ~/.config/systemd/user/levi.service
[Unit]
Description=LEVI workbench
After=network-online.target

[Service]
WorkingDirectory=/path/to/LEVI
Environment=LEVI_WORKSPACE=/path/to/workspace
ExecStart=/path/to/uv run --no-sync levi serve
# 有导出、转换或模型作业在跑时 levi stop 会拒绝；--wait 等它们结束。
ExecStop=/path/to/uv run --no-sync levi stop --wait 30
TimeoutStopSec=1900
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now levi
loginctl enable-linger "$USER"     # 注销后继续运行（可能需要管理员）
```

**从别的机器访问。** LEVI 没有登录，是能访问文件的单用户工作台。让它留在 `127.0.0.1`，用端口转发访问（`ssh -L 7860:127.0.0.1:7860 user@server`；7861 是内部 API，不是工作台）。要共享，就放在有身份认证的反向代理后面，转发到 `127.0.0.1:7860`，HTTPS 下设 `LEVI_SECURE_COOKIES=1`；不要在开放网络上用 `--host 0.0.0.0`。

**更新：停止、安装、启动。** 这个检出的 LEVI 在运行时，安装的每一步都按这个顺序做，包括 Python 依赖（`uv sync`）：`git pull`；`uv run --no-sync levi stop`（不加 `--no-sync` 的 `uv run` 会先同步环境，等于在运行中的服务底下换包；有作业在跑时 `levi stop` 会拒绝：`--wait` 等它们结束；`--force` 会杀掉它们）；`uv run --locked levi install --profile core`（同步依赖；前端源码变了就重新构建；`levi doctor` 会报构建过期）；再启动服务。`levi install` 不改动本检出正在运行的 LEVI 所用的任何东西：端口 7860 在监听、有本检出的 `levi serve` 进程、或本检出的实时服务在运行时（`levi live start` 及其守护进程），Python 依赖、前端依赖（`node_modules`）和构建（`.next`）都列为人的步骤，排在“先停掉正在运行的 LEVI”（`stop-service`，其中也列出停实时服务的 `uv run --no-sync levi live stop`）之后。源码戳出现之前构建的 `.next` 状态为“未知”，只有加 `--yes` 才重建。如果还运行着实时服务（`levi live`），更新后要和产品 LEVI 一起重启：两者运行同一份代码，`live.toml` 的新键（例如 `vllm.model`、`gpu.lock_unavailable`）只在新启动的服务里生效。

## 5. 实时标注服务

`levi live` 在评测写 rollout 的同时在后台标注（[实时服务](docs/LIVE.zh-CN.md)）。在新机器上：

1. 搭 vLLM 环境并下载权重（[vLLM](docs/VLLM.zh-CN.md) 第 1 节）；默认启动脚本是仓库自带的 `scripts/vllm/serve.sh`。
2. `uv run levi live init --root /path/to/rollouts` 写出 `~/.levi-live/workspace/live.toml`（没有任何默认值指向某台机器的目录；有健康监控文件和 GPU 锁文件时设 `[fr3] health_file`、`[gpu] lock_file`）。
3. `uv run levi live doctor` 检查脚本、pid 目录、GPU 锁文件、根目录，以及从别的机器拷来的路径。
4. `uv run levi live once --fake-vlm --workspace /tmp/levi-live-smoke/ws --home /tmp/levi-live-smoke/home --root /path/to/rollouts` 用替身模型把流水线跑一遍（不用 GPU），然后 `uv run levi live start --daemon`。

## 6. 东西都在哪里

| | |
| --- | --- |
| `.venv/`、`.runtime/`（Bun、uv 下载的 Python）、`node_modules/`、`.next/` | 在检出目录里，由 `levi install` 重建。不能搬家：移动检出目录后要重新 `uv sync` |
| `$LEVI_WORKSPACE` | 数据集、LEVI 状态（`outputs/LEVI/`）、权重（`checkpoints/`）、缓存（[工作区](docs/WORKSPACE.md)） |
| `integrations/*/.venv`、`.venv-vllm/` | worker 和 vLLM 环境 |
| `~/.levi-live/` | 实时服务的状态、pid 文件和默认工作区 |

`levi clean` 预览可重建的缓存（服务停止后 `--apply` 删除）。Docker 镜像（`Dockerfile`）只包含核心，维护者没有构建过。
