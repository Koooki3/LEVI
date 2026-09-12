# SAM3 对象标注 / SAM3 object annotation

LEVI 将 SAM3 作为全局对象级标注能力。核心工作台保持 CPU 安全：原生 LeRobot
数据只读，模型结果写入独立的 annotation sidecar。演示数据集、Hub 数据集和登记的
本地数据集共用同一套流程；sidecar、模型缓存和作业状态都由当前 LEVI_WORKSPACE
隔离。默认中文，可通过界面右上角切换英文。

## 支持范围

- LeRobot v2.0/v2.1/v3.0/v3.1 的 episode、相机特征和帧索引由元数据解析。
- 文本 prompt 在选定相机和起始帧初始化对象，SAM3 video predictor 负责传播并保留
  episode 内的 track ID。
- 每帧输出 bbox、COCO uncompressed RLE mask、score、可见性、概念和来源信息。
- 模型结果初始为 suggested；人工接受、拒绝、重标、遮挡和精修都创建新的 sidecar
  revision，不会修改原始 meta/、data/ 或 videos/。
- UI 在每个演示集和本地数据集的标注页显示当前 Hugging Face 账号、worker 状态、
  checkpoint 下载进度和实际保存位置。旧版 lerobot-viz-oauth 浏览器存储会在迁移
  到 LEVI 时清除，必须重新登录。

SAM3 worker 是独立的 uv 项目。LEVI 后端默认将 SAM3 设为全局启用；未安装 worker
时按钮会显示“需要配置”，不会导入 Torch 或探测 CUDA。只有真实标注作业在已配置的
CUDA 主机上才会加载模型。

## 模型来源与账号

默认模型配置固定为：

| 项目 | 默认值 |
| --- | --- |
| Hugging Face 仓库 | 1038lab/sam3 |
| checkpoint 文件 | sam3.pt |
| revision | main |
| 本地保存 | $LEVI_WORKSPACE/checkpoints/sam3/sam3.pt |

1038lab/sam3 是公开的模型镜像仓库，sam3.pt 与当前适配器使用的 PyTorch
checkpoint 布局兼容。LEVI 不把权重提交到 Git，也不会把 token 写入计划、作业 JSON、
日志或 sidecar。模型页可能要求先申请访问权限；申请通过后，使用 Hugging Face CLI
或 LEVI 界面登录同一个账号。

源码 adapter 固定在 integrations/sam3/pyproject.toml 的官方 SAM3 commit。SAM3
代码、模型和权重仍受 Meta [SAM License](https://github.com/facebookresearch/sam3/blob/main/LICENSE)
及模型页条款约束；请同时阅读 UPSTREAM.md 和 THIRD_PARTY_NOTICES.md。

## 首次部署：严格按顺序执行

以下命令从克隆后的 LEVI 根目录执行。目录名称和绝对路径可以任意，示例不会依赖
特定工作区名称。

### 1. 准备系统工具

安装 uv、ffmpeg 和 ffprobe。真实 SAM3 主机还需要 Python 3.12、匹配驱动的
CUDA-enabled PyTorch 环境；官方 SAM3 当前要求 PyTorch 2.7 或更高版本及兼容的
CUDA 运行时。LEVI 的 CPU 检查不会验证这些 GPU 条件。

### 2. 克隆并建立独立工作区

~~~bash
git clone https://github.com/Koooki3/LEVI.git
cd LEVI
export LEVI_WORKSPACE="$PWD/.state"
# 也可以把工作区放在仓库外：
# export LEVI_WORKSPACE="/absolute/path/to/levi-workspace"
cp .env.example .env
uv sync --locked
uv run levi setup
~~~

LEVI_WORKSPACE 只保存数据集登记、sidecar、报告、checkpoint 和运行缓存。换一个
工作区即可得到完全隔离的账号缓存、数据缓存和标注版本；不需要修改源码。

### 3. 登录 Hugging Face

先在 [1038lab/sam3 模型页](https://huggingface.co/1038lab/sam3) 登录准备使用的账号。
如果页面显示 `Request access`、受限模型或许可确认，按页面提示提交访问申请并接受
适用条款；只有页面允许该账号读取模型后才继续。不要用另一个账号申请、登录或下载，
否则数据快照和 checkpoint 会被隔离到不同账号作用域。

选择一种登录方式即可：

~~~bash
# 推荐：浏览器/设备流程，token 不出现在 shell 历史中
hf auth login
hf auth whoami
~~~

如果系统没有全局 hf 命令，也可以使用：

~~~bash
uvx hf auth login
uvx hf auth whoami
~~~

登录账号需要能读取 1038lab/sam3。也可以直接打开 LEVI，点击页面中的 Connect
Hugging Face，输入只读 token；浏览器会把当前 token 放入 HttpOnly 会话 cookie，
真实 worker 只在当前作业进程中使用它。若不希望浏览器保存 token，可只使用本机
hf auth login 或运行环境中的 HF_TOKEN。

### 4. 安装独立 SAM3 worker

在 CUDA 主机上，从 LEVI 根目录执行：

~~~bash
uv venv --python 3.12 integrations/sam3/.venv
uv sync --project integrations/sam3
export LEVI_SAM3_WORKER_PYTHON="$PWD/integrations/sam3/.venv/bin/python"
~~~

项目的 integration 配置会安装固定 commit 的官方 SAM3 源码和 CUDA PyTorch 索引。
第一次同步可能下载较大的 Python/CUDA 包；这些内容留在 uv 缓存和运行时目录，不会
写入 Git。

### 5. 配置并检查（不运行模型）

~~~bash
export LEVI_SAM3_ENABLED=1
export LEVI_SAM3_MODEL_REPO=1038lab/sam3
export LEVI_SAM3_MODEL_FILENAME=sam3.pt
export LEVI_SAM3_MODEL_REVISION=main
uv run --project integrations/sam3 levi-sam3-worker --check
uv run levi sam3 check
~~~

两个 check 命令只检查文件、环境变量和路径，输出
model_imported=0、cuda_probe_performed=0。它们不会下载 checkpoint、导入 Torch、
探测 CUDA 或执行推理。

### 6. 构建并启动 LEVI

~~~bash
uv run levi build
uv run levi serve
~~~

看到 Ready 后，在浏览器打开 http://127.0.0.1:7860。7860 是网页入口，7861
是内部 API；不要把浏览器指向 API 端口。停止服务使用 Ctrl+C。

### 7. 在 UI 中完成首次模型准备

1. 打开任意演示数据集或登记的本地数据集，进入一个 episode 的“标注”页面。
2. 在 SAM3 运行状态卡确认账号。若显示“未登录”，点击 Connect Hugging Face；
   已通过 CLI 登录的账号会显示为 environment/cache。
3. 确认模型为 1038lab/sam3 / sam3.pt，保存位置为当前工作区下的
   checkpoints/sam3/sam3.pt。
4. 选择相机，输入逗号分隔的文本 prompt，例如 cup, plate, robot gripper。
5. 点击“运行 SAM3 标注”。第一次真实作业会下载 checkpoint，状态卡显示已下载字节、
   总字节（若 Hub 提供元数据）和进度条；完成后显示“Checkpoint 已就绪”。
6. 作业完成后，列表中的对象建议保持 suggested。逐条接受或拒绝；每次操作都会
   创建新的 revision，随后可在同一页面继续复核。

页面不要求数据集属于某个固定账号。Hub 数据集使用当前账号作用域缓存；本地数据集
使用真实本地路径和当前 LEVI_WORKSPACE 作用域。切换账号、revision 或工作区不会
复用另一个账号的 Hub 快照或 sidecar。

## 环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| LEVI_WORKSPACE | 仓库内 .state | 数据、sidecar、checkpoint 和缓存的根目录 |
| LEVI_SAM3_ENABLED | 1 | 全局显示并允许 SAM3；设为 0 可隐藏真实 worker |
| LEVI_SAM3_MODEL_REPO | 1038lab/sam3 | 兼容的 Hub 模型仓库 |
| LEVI_SAM3_MODEL_FILENAME | sam3.pt | worker 下载的 checkpoint 文件 |
| LEVI_SAM3_MODEL_REVISION | main | Hub 模型 revision |
| LEVI_SAM3_CHECKPOINT_DIR | $LEVI_WORKSPACE/checkpoints/sam3 | checkpoint 目录，必须位于工作区内 |
| LEVI_SAM3_CHECKPOINT | 空 | 已存在的本地 checkpoint；设置后跳过 Hub 下载 |
| LEVI_SAM3_WORKER_PYTHON | integrations/sam3/.venv/bin/python | 独立 worker Python |
| LEVI_SAM3_DOWNLOAD_VIDEOS | 1 | Hub 作业启动前准备视频资产；设为 0 时由部署者自行准备 |
| HF_TOKEN | 空 | 后端/worker 的非浏览器读取凭据，不要提交 |

正常情况下只需设置 LEVI_WORKSPACE 和 worker Python；模型仓库、文件和目录会由
默认配置自动适配当前工作区。若设置 LEVI_SAM3_CHECKPOINT，路径可以是外部只读
文件，但建议把 checkpoint 复制到工作区以便备份和迁移。

## Sidecar layout / Sidecar 目录

~~~text
<LEVI_WORKSPACE>/outputs/LEVI/workbench/object_annotations/<dataset-hash>/
├── meta.json
├── current.json
├── revisions/<revision-id>/
│   ├── revision.json
│   ├── objects.parquet
│   ├── tracks.parquet
│   ├── masks/episode-000000/<camera>.parquet
│   ├── qa.parquet
│   └── events.parquet
└── staging/{plans,jobs,results}/
~~~

每行 mask 记录包含 episode_index、frame_index、episode-local timestamp、camera_key、
object_id、track_id、concept、bbox_xyxy、image_size、rle_size、rle_counts、score、
visible、occluded、status、source 和 prompt。v3 共享视频 shard 会按
meta/episodes 中的 camera chunk/file/from_timestamp/length 定位，并把 predictor 的
文件级帧号转换为 episode-local 帧号，同时限制传播范围不越过当前 episode。RLE 使用
COCO 列优先（Fortran）运行长度编码，不使用有损视频保存类别像素。源数据和 sidecar
revision 可分别备份和比较。

## API

接口通过同源 /api/annotation 代理访问：

| Method | Route | Purpose |
| --- | --- | --- |
| GET | /api/sam3/status | 返回全局开关、worker 文件、模型配置、账号用户名、checkpoint 路径和下载进度；不导入 Torch、不探测 CUDA |
| GET | /api/sam3/capabilities | /status 的兼容别名 |
| POST | /api/sam3/plan | 校验 episode、相机、prompt 并写入 staged plan |
| POST | /api/sam3/run | provider=sam3 启动真实 worker；provider=fake 仅供 CPU 合约测试 |
| GET | /api/sam3/jobs/{id} | 轮询作业并在成功时发布 sidecar revision |
| POST | /api/sam3/jobs/{id}/cancel | 取消作业 |
| GET | /api/sam3/revisions | 列出对象标注 revision |
| GET | /api/sam3/episodes/{id}/objects | 按相机、帧或 revision 读取对象记录 |
| POST | /api/sam3/edits | 带 base_revision 的接受/拒绝/重标/遮挡/删除/精修 |

provider=sam3 的响应为 202 和 job_id。worker 先解析已有 checkpoint 或下载
1038lab/sam3/sam3.pt，随后逐个读取 episode/camera，最后写入结果 JSON。结果必须
通过 schema、计划范围和 RLE coverage 校验，失败时不会产生半成品 revision。

## 兼容原生 LeRobot

对象 mask 保留在 sidecar，不塞进 H.264/yuv420p 视频 feature，也不改变原生
language_persistent、language_events、任务、动作、状态和相机字段。导出会把
sidecar 复制到新数据树的 annotations/sam3/；原始目录永远只读。若后续确实需要将
bbox、center 或 grasp state 投影为轻量 LeRobot feature，应使用官方
add_features/modify_features 复制工具，并在导出报告中记录来源 revision。

当前标注层：

| Layer | 内容 | 状态 |
| --- | --- | --- |
| L1 Geometry | mask、bbox、score、可见性 | 已实现 |
| L2 Object | category、track ID、人工语义重标 | 已实现基础字段 |
| L3 Manipulation | grasp、move、place、contact、target relation | events.parquet 预留，需后续审核后推断 |

## 常见迁移问题

- **页面仍显示旧账号**：退出 LEVI，在浏览器开发者工具中确认
  levi-hf-auth-v1；旧的 lerobot-viz-oauth 会被自动删除；重新使用目标账号登录。
- **状态卡显示未登录，但 CLI 已登录**：确认启动 LEVI 的同一用户可以读取
  HF_HOME/HF_HUB_CACHE；状态卡会把 CLI 凭据显示为 environment/cache。
- **模型下载到了旧目录**：停止 LEVI，设置新的 LEVI_WORKSPACE 后重新执行
  uv run levi serve。不要手动把另一个工作区的 sidecar 复制到新数据集。
- **提示 checkpoint 访问失败**：在模型页申请访问，执行 hf auth whoami，然后刷新
  页面。浏览器 token、CLI token 和 HF_TOKEN 都只读取当前账号。
- **worker environment not found**：重新执行第 4 步，或设置绝对路径
  LEVI_SAM3_WORKER_PYTHON；路径不应写入 README。

## CPU-safe verification

源码检查、前端类型/格式、API fake provider、RLE、sidecar、转换和文档校验可以在
CPU 主机完成。仓库验证不会导入 Torch、探测 CUDA、下载模型或执行 SAM3 推理。真实
模型验证应由部署者在自己的 CUDA 主机按第 7 步运行，并将结果作为本地运行记录，不要
提交 checkpoint 或 token。

## 参考资料

- [1038lab/sam3 model page](https://huggingface.co/1038lab/sam3) — 默认 checkpoint 镜像和访问说明。
- [Meta SAM3 repository](https://github.com/facebookresearch/sam3) — 官方 predictor API、源码和许可。
- [Hugging Face CLI authentication](https://huggingface.co/docs/huggingface_hub/guides/cli) — hf auth login 与设备流程。
- [LeRobot Dataset v3](https://huggingface.co/docs/lerobot/lerobot-dataset-v3) — 元数据定义 episode 边界。
- [LeRobot annotation pipeline](https://huggingface.co/docs/lerobot/annotation_pipeline) — reader/staging/validator/writer 分层。
- [COCO mask API](https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/mask.py) — RLE 语义。

LEVI 只借鉴公开 API 和数据格式，不复制参考项目的受版权保护界面或外部资产。
