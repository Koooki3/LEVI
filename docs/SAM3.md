# SAM3 对象标注 / SAM3 object annotation

LEVI 将 SAM3 作为**可选的对象级标注 worker**。核心工作台不依赖 Torch、CUDA 或
SAM3，LeRobot 原始目录始终只读；模型结果写入独立、可回滚的 annotation sidecar。
默认中文，可通过右上角切换英文。

## 当前实现 / Current implementation

第一阶段已经包含：

- 读取 LeRobot v2.0/v2.1/v3.0/v3.1 的 episode、相机特征和帧索引；边界由
  `meta/info.json` 与 episode metadata 决定，不扫描 MP4 猜 episode。
- `provider=fake` 的确定性 CPU 演示：文本提示、相机选择、轨迹 ID、bbox、无损
  COCO uncompressed RLE、Parquet revision、接受/拒绝审核。
- 标注页的对象/轨迹审核面板，以及按当前 episode-local 帧显示的无损 mask/bbox overlay。模型建议的初始状态始终是 `suggested`；`review_threshold` 作为真实 worker 的输出下限，
  `accept_threshold` 记录在计划中供后续审核策略使用，任何模型结果都不会自动确认。
- 可选异步 worker 协议：计划、作业状态、取消、结果校验和 revision 发布。worker
  仅在用户设置 `LEVI_SAM3_ENABLED=1` 并使用独立 uv 环境时才导入 Torch/SAM3。

真实 SAM3 推理没有纳入 LEVI 的 CPU 测试，也不会在默认启动中执行。没有安装可选
环境时，CPU 演示与手工 LeRobot 语言/VQA 标注仍可用。

## 数据流 / Data flow

```text
LeRobot source (read-only)
        │  LeRobot metadata + frame timestamps
        ▼
  annotation plan (JSON, staged)
        │  text prompt (box/point extension planned)
        ▼
  optional SAM3 worker ──────── CPU fake provider for tests/demo
        │  masks + boxes + confidence + persistent track IDs
        ▼
  protocol validation
        │
        ▼
  annotation sidecar revision (Parquet + RLE)
        │
        ├── visual review: accept / reject / relabel / occlusion / refine
        ├── export: copy sidecar into a new dataset tree
        └── future: object relations and manipulation events
```

SAM3 的官方视频 API 使用 `start_session`、`add_prompt` 和
`propagate_in_video`。LEVI 的 adapter 保留这层边界，并把输出规整成 model-neutral
记录，以便更换模型或导入人工标注。每个 episode、相机和 prompt 共用一个 session；
SAM3 返回的 object ID 在一个相机 episode 内保持为 `track_id`。

## Sidecar layout / Sidecar 目录

例如数据集身份为 `repo_id@revision` 时，工作目录下会有一个哈希隔离的目录：

```text
<LEVI_WORKSPACE>/outputs/LEVI/workbench/object_annotations/<dataset-hash>/
├── meta.json                         # schema, identity, review policy
├── current.json                      # current revision pointer
├── revisions/<revision-id>/
│   ├── revision.json                 # parent, model, counts, timestamp
│   ├── objects.parquet               # object-level summary
│   ├── tracks.parquet                # episode/camera/track summary
│   ├── masks/episode-000000/
│   │   └── observation.images.front.parquet
│   ├── qa.parquet                    # temporal QA findings
│   └── events.parquet                # reserved for manipulation events
└── staging/{plans,jobs,results}/     # atomic worker hand-off files
```

`masks/*.parquet` 每一行包含 `episode_index`、`frame_index`、`timestamp`、
`camera_key`、`object_id`、`track_id`、`concept`、`bbox_xyxy`、`image_size`、
`rle_size`、`rle_counts`、`score`、`visible`、`occluded`、`status`、`source` 和
`prompt`。RLE 采用 COCO 的列优先（Fortran）运行长度编码，避免把几十万张 PNG
塞进仓库或使用有损 H.264 存储类别像素。`timestamp` 是 episode-local seconds，
与 LEVI 播放器和审核跳转一致；v3 共享视频中的 `from_timestamp` 只用于定位源视频
片段，不会写入这个播放时间轴。

每一次人工编辑都会以当前 revision 为 parent 创建新 revision。旧版本保留，源数据
的 `meta/`、`data/`、`videos/` 不写回。导出操作把当前 sidecar 复制到新目录的
`annotations/sam3/`，因此可以比较 `raw + v1 + v2` 并重新审核。

## API / 接口

接口位于 `/api/annotation`（从前端同源代理访问）：

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/sam3/capabilities` | 报告 worker 文件和显式开关；不导入 Torch，不探测 CUDA |
| POST | `/api/sam3/plan` | 校验 episode/camera/prompt，写入 staged plan |
| POST | `/api/sam3/run` | `provider=fake` 同步生成 CPU 演示；`provider=sam3` 启动可选异步 worker |
| GET | `/api/sam3/jobs/{id}` | 收集 worker 结果并在成功时发布 sidecar revision |
| POST | `/api/sam3/jobs/{id}/cancel` | 终止可选 worker 作业 |
| GET | `/api/sam3/revisions` | 列出版本及当前指针 |
| GET | `/api/sam3/episodes/{id}/objects` | 按相机、帧或 revision 读取对象记录 |
| POST | `/api/sam3/edits` | 带 `base_revision` 的接受/拒绝/重标/遮挡/删除/精修 |

最小 CPU 请求：

```json
{
  "repo_id": "local/registered-id",
  "episode_indices": [0],
  "camera_keys": ["observation.images.front"],
  "prompts": ["cup", "plate", "robot gripper"],
  "start_frame": 0,
  "max_frames": 24,
  "review_threshold": 0.60,
  "accept_threshold": 0.90,
  "provider": "fake"
}
```

`sam3` provider 的响应是 `202` 和 `job_id`，前端应轮询作业状态；模型异常、缺少
视频或无效 RLE 会使作业失败，不会生成半成品 revision。作业结果必须通过 Pydantic
schema 与 RLE coverage 校验后才可写入 Parquet。

## 可选 SAM3 环境 / Optional worker environment

核心安装不带 Torch。只有需要真实模型的用户才执行：

```bash
uv venv --python 3.12 integrations/sam3/.venv
uv sync --project integrations/sam3
uv run --project integrations/sam3 levi-sam3-worker --check
hf auth login
export LEVI_SAM3_ENABLED=1
export LEVI_SAM3_WORKER_PYTHON="$PWD/integrations/sam3/.venv/bin/python"
uv run levi
```

SAM3 checkpoint 需要在 Hugging Face 申请访问权限，不能放入 Git。适配器源码固定
在 `integrations/sam3/pyproject.toml` 的官方 commit；官方代码、权重及其访问条款
仍由 Meta/Facebook Research 的 [SAM License](https://github.com/facebookresearch/sam3/blob/main/LICENSE)
约束。完整来源记录见 [UPSTREAM.md](UPSTREAM.md) 与
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

`uv run levi sam3 check` 和 `levi-sam3-worker --check` 只检查文件和环境变量，明确
输出 `model_imported=0`、`cuda_probe_performed=0`；仓库 CI 仅运行这些 CPU-safe 检查，
不会下载模型或执行推理。

## LeRobot 兼容性 / Compatibility

- 通过 `LeRobotDataset` 所采用的 metadata 语义读取 episode；v3 共享 Parquet/video
  shard 不被假设为“一文件一个 episode”。
- 原生 `language_persistent`、`language_events`、VQA bbox/keypoint 仍按原 schema
  保存；对象 mask 不塞进普通视频 feature，也不改变原始 feature 名称。
- 需要把 bbox/center 等轻量结果写回 LeRobot 时，使用官方
  `add_features`/`modify_features` 复制工具；mask 保留在 RLE sidecar。
- 导出后的数据是一个新目录，保留版本、任务、相机、动作、状态和视频；source
  fingerprint 与 sidecar parent revision 可用于审核追溯。

## 三层标注路线 / Three annotation layers

| Layer | 记录 | 当前状态 |
| --- | --- | --- |
| L1 Geometry | mask、bbox、score、可见性 | 已实现，CPU fake + 可选 SAM3 adapter |
| L2 Object | category、track ID、属性、语义重标 | track/category 已有；VLM planner 留在后续 PR |
| L3 Manipulation | grasp、move、place、contact、target relation | `events.parquet` schema 已预留；尚未自动推断 |

从 L1 到 L3 的升级必须保持 source + sidecar 版本化。推荐的事件推断证据是
`gripper`/object/target 的时序 mask、IoU、中心位移和 LeRobot action/state；任何
自动事件先进入 `suggested` 状态，再由人工确认。

## 分阶段提交计划 / Staged PR plan

1. **PR1 — Geometry and review (本次)**：RLE/schema、revision sidecar、CPU fake
   provider、计划/作业 API、对象审核 UI、LeRobot source-safe export、CPU tests。
2. **PR2 — Model worker and semantics**：在独立环境验证 pinned SAM3 commit；补充 box/point
   prompt、关键帧重检测、遮挡/漂移 QA、可选 rule/VLM concept planner。VLM 默认关闭，
   只产生 prompt 建议。
3. **PR3 — Relations and events**：从经过审核的 mask/track 与 action/state 推断 grasp、
   place、contact 和 object trajectory；增加事件时间轴、人工 split/merge/精修以及
   `dataset_tools` 的可选轻量 feature 投影。

每个 PR 都应包含：变更记录、schema version、源数据不变性测试、CPU-only CI、真实
模型不进入仓库、以及对应的第三方许可核对。SAM3、LeRobot、COCO RLE、CVAT 和
Label Studio 仅作为公开 API/交互设计参考；LEVI 不复制其受版权保护的界面或资产。

## 参考实现 / References

- [Meta SAM3 official repository](https://github.com/facebookresearch/sam3) — text/box/point prompt and video propagation API.
- [SAM3.1 release notes](https://github.com/facebookresearch/sam3/blob/main/RELEASE_SAM3p1.md) — multiplex tracking direction; not vendored by LEVI.
- [LeRobot Dataset v3](https://huggingface.co/docs/lerobot/lerobot-dataset-v3) — metadata-defined episode boundaries.
- [LeRobot annotation pipeline](https://huggingface.co/docs/lerobot/annotation_pipeline) — reader/staging/validator/writer separation.
- [LeRobot dataset tools](https://github.com/huggingface/lerobot/blob/main/src/lerobot/datasets/dataset_tools.py) — optional feature-copy path.
- [COCO mask API](https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/mask.py) — uncompressed RLE semantics.
- [CVAT overview](https://github.com/cvat-ai/cvat/blob/develop/site/content/en/docs/getting_started/overview.md) and [Label Studio SAM backend](https://github.com/HumanSignal/label-studio-ml-backend/tree/master/label_studio_ml/examples/segment_anything_model) — human-in-the-loop interaction references.
