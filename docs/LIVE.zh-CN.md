# 实时标注服务

`levi live` 是一个后台常驻的 LEVI：评测还在写 rollout 时，它就开始标注。它监视 rollout 目录，把**已完成**的片段拿进自己的工作区，用本地模型（vLLM 上的 Qwen3.8）标出时间片段和各时间片段的成败，并判断整个片段是否成功。人可以在实时页面看进度，在普通 LEVI 查看器里审核结果。[English](LIVE.md)

它是一个**独立的 LEVI 实例**（自己的工作区，界面端口 7880，核心端口 7881），长时间运行的工作不和你日常用的 LEVI（7860/7861）共用进程。设计目标是不妨碍机器人：自己降低优先级、限制线程、空闲时几乎零开销，只在 GPU 策略允许时才启动 GPU 模型。

它**不会**：写 rollout 目录、连接机器人或策略服务器端口、写人工成败标签、参与 gold 制作。它给出的判定都是自动的、未经审核的，并在所有地方这样标注。

## 快速开始

两条命令。策略服务器用实测推荐的较小显存池（`.22` = 7.6 GB，取代原来的 `.35`；推理延迟相同，约 59 ms）；实时服务自己启动和停止 vLLM：

```bash
# 终端 D2：策略服务器，和以前一样，只把 MEM_FRACTION 改成 .22
cd ~/work/wenkai/openpi && XLA_PYTHON_CLIENT_MEM_FRACTION=.22 uv run scripts/serve_policy.py --port 8000 policy:checkpoint --policy.config pi05_fr3_all_state --policy.dir checkpoints/pi05_fr3_all_step49999

# 另一个终端：实时服务（启动一次，一直运行）
cd ~/work/wenkai/LEVI && uv run levi live start --daemon --auto-approve
```

然后评测客户端“是否启用后台 LEVI 标注？”回答是。其他命令：

```bash
uv run levi live status                          # 它在做什么
uv run levi live doctor                          # CPU、内存、GPU、磁盘、警告
uv run levi live stop                            # 只停自己的进程
```

`levi live once [--fake-vlm]` 标完当前已完成的片段就退出（加 `--fake-vlm` 时使用假的模型服务，不需要 GPU）。`levi live init` 把所有默认值写成 `<工作区>/live.toml`。

不加 `--auto-approve` 时，服务仍会镜像、建视图并**生成计划**，然后等待：由人在 LEVI 页面批准计划（数据集显示 `awaiting_approval`）。加上它，由下文的“自动批准主体”（有审计）代为通过这些关口。

默认工作区 `/home/marvel/work/wenkai/levi-live-ws`；默认监视的根目录 `/home/marvel/work/wenkai/online_rollout_data/models`（评测客户端 `--rollout-root` 的默认值）。状态文件在 `~/.levi-live/`。

## 工作方式

```
评测客户端 ──写──▶ <根>/<group>/<task>/demo_NNNN/   （源目录，只读）
                      │ 是否完成？（criteria.py）
   监督进程（标准库，约 25 MiB）：扫描、读会话、决策、写 status.json
                      ▼
   GPU 策略 ──▶ vLLM（由监督进程启动和停止）
                      ▼
   worker（每个批次一个，一次一个数据集）
      镜像 → 视图 → 时间片段标注 → 提交 → 释放复核 → 清理
                      ▼
   <工作区>/captures/<group>__<task>/demo_NNNN/    硬链接
   LEVI 存储：已提交的时间片段（levi.review = auto）+ 锚定复核记录
```

- **监督进程**（`levi live start`）：空闲时只占几 MB，做少量 `stat`。不导入任何数值库。它询问 GPU 策略是否允许模型运行、启动 vLLM、为一个数据集启动一个 **worker**，工作结束或 GPU 要还回去时停掉 vLLM。
- **worker**（`python -m levi.live.worker`）：做重的导入，处理一个数据集的一个批次后退出。每一步可续跑：崩溃或被抢占后由下一个 worker 接着做，不会重复标注。
- **页面和核心**：为实时工作区运行 `levi serve`（界面 :7880，核心 :7881），由 `levi live start` 启动（没有生产构建时只启动核心并提示；`--no-ui` / `--no-core` 可跳过）。

一个数据集对应一个 `<group>/<task_folder>`，目录名是 `<group>__<task_folder>`。数据集**一次处理一个**，等得最久的先做，因为 LEVI 不允许同一数据集的标注基线变了之后还有第二个写入者。

## 什么算“已完成”（接口 C1）

客户端在 `close()` 的最后一步创建空文件 `.complete`。只有满足下面全部条件的 `demo_NNNN` 才会被取走：

1. `.complete` 存在（没有标记的历史片段：目录内 60 秒没有任何变化）；
2. `metadata.json` 的 `stopped_at` 非空；
3. `events.csv` 有 `episode_end` 行；
4. 没有残留的 `*_raw.avi`（还没合成的临时采集）。

下面两种是**拒收**（已结束但不可用，不再重试）：`media_storage.video_frames_match_csv` 不为真，或 `cameras.stall_detection.stalled` 非空。`incomplete_*` 和 `discarded_*` 目录从不取走，只计数；`eval.abort_reason == "fr3_fault"` 的 `incomplete_*` 会让页面把数据集标成 **FR3 故障**。源目录的 `.eval_sessions/` 只读，不镜像。

服务第一次启动之前（或该任务评测会话开始之前）完成的片段算**历史积压**：只计数、不动，除非 `watch.backlog = "process"` / `--process-backlog`。回答了“不启用后台标注”的会话（`levi.enabled` 为假）被跳过。`eval.outcome == "unlabeled"` 的片段就是等 LEVI 判定的；每个片段保留 `eval.run_id`。

## 镜像器

已完成的片段逐文件硬链接到 `<工作区>/captures/<名字>/.partial-demo_NNNN`，再改名为 `demo_NNNN`，所以 LEVI 不会看到半个镜像，硬链接也不占磁盘。无法链接时（跨文件系统）改为复制，`levi live doctor` 会指出。源目录永远不写。镜像只在**批次之间**进行，所以运行中数据集不会在脚下变化；如果某个片段晚完成、排在已标注片段之前，视图会重建，LEVI 按片段重新对应标注（测试覆盖）。

## 设置（`live.toml`）

`levi live init` 写出带全部默认值的文件；未知键或类型不对是错误，不会悄悄取默认值。`--workspace`、`--root`、`--gpu-mode`、`--auto-approve`、`--process-backlog`、`--since`、`--ui-port`、`--core-port`、`--home` 可覆盖文件。环境变量：`LEVI_LIVE_WORKSPACE`、`LEVI_LIVE_CONFIG`、`LEVI_LIVE_HOME`（`status.json` 所在目录，默认 `~/.levi-live`）。

各表、各键、默认值和含义与英文版表格一致（`service`、`watch`、`fr3`、`gpu`、`vllm`、`vllm.coexist`、`provider`、`pipeline`、`resources`），见 [LIVE.md](LIVE.md#settings-livetoml)。要点：

- `gpu.mode` 默认 `auto`：等于 `timeshare`（两者常驻 + 闸门）；`coexist` 和 `manual` 是手动选项。`gpu.busy_states` 默认 `["running"]`；`gpu.min_free_mib` 600、`gpu.policy_budget_mib` 8500 决定 vLLM 何时睡眠。
- `pipeline.auto_approve` 默认 **false**。
- `gpu.policy_ports` 默认 `[8000]`，只在内核的 socket 表里查，从不连接。
- `vllm` 默认是实测的共存配置（`serve-qwen38.sh`，端口 8100，`gpu_memory_utilization` 0.72，`max_model_len` 49152，`max_images` 128，`max_num_seqs` 2，`max_num_batched_tokens` 4096，`--enable-sleep-mode`）；`idle_timeout_s` 120 秒无活后睡眠或停止。
- 服务为实时工作区设置 `LEVI_DROID_SAMPLE=off`、`LEVI_GPU_SHARING=allow`（用它自己的策略取代 LEVI 的错峰守卫，见下）、`LEVI_SYNC_DISCOVER=off`、`LEVI_SYNC_INTERVAL=30`。

## GPU 管理

一块 32 GB 的 GPU 与机器人的策略服务器共用。实测（`levi-hub/reports/live-gpu.md`）给出规则：

- 策略服务器用 `XLA_PYTHON_CLIENT_MEM_FRACTION=.22`（7.6 GB，推理 p50 约 59 ms，与 `.35` 相同），vLLM 用 `--gpu-memory-utilization 0.72`，两者**可以同时常驻**（峰值约 31.3 / 32.6 GB，余量 1.3 GB）。原来的 `.35`（11.8 GB）和 vLLM 放不下。
- 两者**不能同时推理**：vLLM 工作时策略每次推理约 120 ms，而不是 59 ms，超过 100 ms 控制周期，记录的时间步会抖动。vLLM 空闲（已加载、没有请求）对策略没有影响。

所以默认的 **`timeshare`**（`auto` 的含义）让两者常驻，用**闸门**错开工作：

| 评测状态 | 闸门 | 模型 |
| --- | --- | --- |
| 有会话处于 `running`（策略在推理） | **关** | 不发请求；在途的请求立即取消，运行退避 |
| `homing`、`waiting_reset`、`standby`、`fault`、`stopped`、`finished`，或客户端已崩溃 | 开 | 工作 |
| 有策略服务器在监听，但没有任何会话文件说明情况 | **关**（可能有我们不知道的客户端在用） | 等待 |
| 没有策略服务器 | 开 | 工作 |

监督进程在 worker 运行时每秒重新决定闸门，写到 `live/gate.json`（过期的文件读作关闭，所以监督进程死了也不会把闸门留在开着的状态），worker 遵守：闸门关闭时，worker 暂停自己的运行（暂停会中止在途的 HTTP 请求，服务器随即停止生成），等运行线程释放租约，闸门打开后继续。不会重复任何工作：运行从最后一个完成的片段接着做。8 秒内没有让路的 worker 会被监督进程停掉。两集之间（`homing`、`waiting_reset`，约 20 秒）模型标注；会话结束后一口气标完剩下的。

| 模式 | 行为 |
| --- | --- |
| `timeshare`（`auto`） | 如上 |
| `coexist` | 同样两者常驻，但闸门永不关闭：模型有活就做，此时每次策略推理约多 60 ms。手动选项 |
| `manual` | 从不启动 vLLM：使用 `vllm.port` 上已有的那个 |

**启动、睡眠、唤醒。** 有批次在等、且 (a) 空闲显存够 `gpu_memory_utilization` × vLLM 总量 + `margin_mib`（第一批请求之后它还会涨约 1.3 GB，所以不能按空闲读数定容量），(b) 最近 `gpu.settle_s` 秒内没有策略服务器出现或消失（还在加载的策略服务器会预分配显存），(c) 工作区 GPU 锁空闲时，才启动 vLLM。:8000 从 `/proc/net/tcp` 读，显存用 `nvidia-smi --query-gpu`，从不连接策略端口。冷启动需要 **46–70 秒**（`levi live doctor` 会提示），期间状态是 `gpu_wait`。vLLM 带 `--enable-sleep-mode` 和 `VLLM_SERVER_DEV_MODE=1`（开发端点，只在 127.0.0.1）：

- **睡眠**（第 1 档：5.5 秒睡下，留 1.8 GB，权重放到主机内存），当它的显存被需要时：空闲显存低于 `gpu.min_free_mib`，或策略服务器占用超过 `gpu.policy_budget_mib`（8500 MiB：比 `.22` 更大的比例，清醒的 vLLM 放不下）。先停 worker。空闲 `vllm.idle_timeout_s` 后，评测仍在进行则也睡眠（`idle_action = auto`），没有任何评测时**停止**（评测不在进行时直接停止）。
- **唤醒**（0.75 秒），有活在等且空闲显存重新够用时。策略服务器较大时保持睡眠，等它退出后才标注（实测前的行为）。不使用第 2 档睡眠。

始终成立：只要本服务的 vLLM 常驻（清醒或睡眠）就持有工作区 GPU 锁（`levi-hub/.gpu.lock` 上的 `flock`，`LEVI_AGENT=live`）；只停自己启动的服务（按进程身份核对），不碰别人的；不是它启动的 vLLM 只有在 `adopt_external`（或 `manual`）下才使用且从不动它；启动失败后等 60 秒；:5000 和策略端口从不连接。残余风险（见实测报告）：余量只有 1.3–1.5 GB；第一集可能遇到一次 280 ms 的策略离群值；vLLM 清醒时用更大的 `MEM_FRACTION` 重启策略服务器会加载失败，请在 vLLM 唤醒前启动它，或先 `levi live stop`。

## 自动批准主体

其他 LEVI 工作区里只有人能批准计划和提交标注。实时服务无人值守，所以它的 worker 在**同时满足以下全部条件时**才以 `live-auto` 身份行动：

- 服务以 `--auto-approve`（`pipeline.auto_approve`）启动；此时只有 worker 得到 `LEVI_LIVE_AUTO_APPROVE=1`，界面和核心不会；
- 工作区有 `live/workspace.json`，只有 `levi live` 会写它；
- 由 HTTP 请求构造的主体永远不是 `auto`，所以页面和已连接的 agent 都变不成它。

它能做的：`runs.plan`、`plans.approve`、`runs.execute`/`resume`/`pause`/`cancel`、`runs.get`/`events`、`changes.diff`/`validate`/`approve`/`commit`、`anchored.get`，仅此而已（不能 reset、clean、试点验收、发布知识或改进）。它只能批准、运行、提交**自己规划的运行**；同一工作区里人的计划或草稿会被拒绝。它的计划在计划里放弃试点片段，批准即覆盖这一点。

它留下的痕迹：它批准的每个变更集带 `reviewer_type: auto`（审核者 `live-auto`），它提交的每个时间片段带 `levi.review: "auto"` 和 `levi.origin.review: "auto"`，每次调用在 `<工作区>/live/audit.jsonl` 各一行（计划、批准、提交，以及每次拒绝）。人在查看器里保存时若改了 auto 时间片段的文字或时间，标记变为 `edited`；原样保存则保持 `auto`。人写过、改过的内容永远不会被替换：重跑只替换仍与上次运行所写完全一致的时间片段，服务也会跳过已有非自己写入标注的片段（`skipped_human`）。

**不写成败标签。** 提交从不写人工成败标签（`annotations/outcomes/`），所以训练池和训练清单不会把自动判定当成真值。片段的自动判定是**锚定复核记录**（`anchored.get`），复制进数据集状态，带 `review: auto` 和 `evaluated: false`。释放复核运行停在 `waiting_for_review`，其中的 `outcome` 提议人可以在 LEVI 页面接受（那时的提交才是人的动作、人的标签）；服务自己不提交它。无人值守评测的 rollout 是 `eval.outcome = "unlabeled"`；LEVI 现在把它（和 `aborted`）读作“没有机器人标签”，而不是把占位的 `success_flag_final = 0` 当成失败。

## 通用配置

被评测的任务不是 LEVI 调优用过的任务，所以没有任何任务专用内容。`levi/live/specs/` 里四个文件，版本号在文件名里，用过之后不再改（改动=新文件+新设置）：

- `generic-guideline.v1.md`：标注指南。引用 rollout 的任务指令（`task_description.txt`，否则元数据的 `task_description`，否则目录名），逐步按它判断。六个子任务 id 与其他地方一致：`approach grasp transport place retreat other`。
- `generic-definitions.v1.json` / `generic-vocabulary.v1.json`：定义（每个数据集的词表只写一次）。
- `generic-release.v1.json`：锚定复核用的释放复核规格，**状态 `candidate`，未在任何任务上评估**。每次夹爪张开问一个问题：侧视 −2.5…+1.2 s、腕部 −1.5…+0.4 s（plates 复核用的那些帧），引用任务指令：是否夹着物体、物体是否落在指令指定的目的地、是否稳定不滑落。片段在至少 `pipeline.anchored_min_valid`（默认 1）次张开有效时判成功。它分不清需要两次放置的任务和一次放置的任务；这类任务设 `anchored_min_valid = 2`。它的准确率未知：信任判定前要先在开发数据上评估；每个判定都只能读作“自动、未审”。

时间片段运行使用评测过的配置（粗步 0.5 s、始终精修、lean 提示、`qwen38-27b-vllm-48k-lean` 配置同时两个请求），只用侧视相机；长片段会放宽步长，使帧数不超过模型的图像上限（LEVI 拒绝悄悄稀疏化）。

## 保持轻量

- **优先级**：服务启动的每个进程（监督进程、worker、页面、核心、vLLM 启动脚本）都设为 nice 19、ionice 空闲类（失败不致命，`doctor` 会报告）。
- **线程**：`OMP/MKL/OPENBLAS/NUMEXPR/ARROW/OPENCV` 的线程变量在这些库加载前设为 `resources.threads`（默认 2）；建视图用 `view_workers = 1`。
- **空闲**：监督进程用 `os.scandir` 列 rollout 目录，会话文件变化时才读，重写 `status.json`；不打开视频、不导入数值库（有测试）。空闲扫描每 `poll_idle_s` 一次，没变化的任务目录不会再次列出。
- **GPU**：vLLM 为批次启动，空闲 `idle_timeout_s` 后睡眠或停止，显存被需要时立即睡眠；闸门让它不挡策略的路。
- **磁盘**：镜像是硬链接（零额外）。批次结束后删除该批运行冻结的输入和证据图（仍开着的释放复核运行保留证据供审核）；残留运行目录超过 `cache_max_gib` 时从最旧的开始清理；日志按 `log_max_mb` × `log_backups` 轮转；`status.json` 和所有 API 响应体积有界。
- **实测**（假模型、不含 vLLM、本机、空闲 10 分钟）：见下文“实测”。

## 服务写的文件

| 位置 | 内容 |
| --- | --- |
| `~/.levi-live/status.json` | 状态文件（接口 C4） |
| `~/.levi-live/live.pid`、`live.lock` | 单实例锁和 pid 记录 |
| `<工作区>/live.toml` | 配置（只创建一次） |
| `<工作区>/live/effective.toml` | worker 读取的生效配置 |
| `<工作区>/live/datasets/<名字>.json` | 每个数据集的状态：各片段及状态、进行中的批次、上一批、判定 |
| `<工作区>/live/audit.jsonl` | 自动批准主体的审计日志（轮转） |
| `<工作区>/live/worker.json`、`gate.json`、`vllm.json`、`service.json` | worker 进度、闸门、本服务启动的 vLLM、首次启动时间 |
| `<工作区>/live/logs/` | `live.log`、`worker.log`、`ui.log`、`vllm-launch.log`（轮转） |
| `<工作区>/captures/<名字>/` | LEVI 登记的镜像采集 |
| `<工作区>/outputs/LEVI/…` | LEVI 自己的状态：视图、运行、已提交标注 |

## 状态文件（接口 C4）

`~/.levi-live/status.json`，每 `heartbeat_s`（4 秒，≤ 5 秒）原子重写。评测客户端只有在**全部**满足时才认为服务可用：`schema` 以 `levi.live.status.` 开头；`updated_at`（纪元秒）不到 15 秒；`pid` 存活；`accepts_sessions` 为真；`state` 是 `idle active annotating gpu_wait` 之一；`watch_roots`（绝对路径）中有一个等于或包含客户端的 `--rollout-root`（或被它包含）。否则客户端退回手动标注。`accepts_sessions` 在 `starting` 以及 `stopped`/`error` 时为假；`gpu_wait`（有活在等模型：闸门关闭、vLLM 正在启动或在睡眠）算可用。完整 JSON 形状见 [LIVE.md](LIVE.md#status-file-interface-c4)。

## 服务读取的机器人侧接口

- **C2 会话**：`<根>/.eval_sessions/<group>__<task_folder>.json`，schema `levi.eval.session.v1`（`state`：`standby homing running waiting_reset fault stopped finished`）。10 秒没有更新且进程已不在，或超过一小时未更新（回收的 pid 不能让它复活），会话算 `crashed`。`run_id`（被续跑的评测轮）、`episode.no`（本次会话的集序号）、`episode.counted`（该轮累计有效片段数）原样传给页面。`incomplete_*` 的中止原因：`fr3_fault`、`user_quit`、`interrupted`、`process_killed`（只有 `fr3_fault` 把数据集标成故障；所有原因都按类计数）。
- **C3 FR3 健康**：`fr3_health.json`，schema `levi.fr3.health.v1`（优先 `updated_at_epoch`，否则 `updated_at`）。缺失或超过 `fr3.stale_s` 是**离线**，不是红灯。`red_light` 还包括：5 秒没有实时机器人状态、5 秒连不上 controller manager、没有 Franka 硬件组件；服务只显示，不据此行动。

## HTTP API（只读，`/api/levi/live/*`）

任何有 `live/workspace.json` 的工作区的核心都提供这些路由；其他工作区全部回答 `{"enabled": false}`。没有任何路由会改东西或返回令牌。

| 路由 | 回答 |
| --- | --- |
| `GET /status` | `{"enabled", "alive", "age_s", "service": <status.json 或 null>, "faults": [{"dataset", "reasons": []}], "fr3_red"}`。`alive` = pid 存在、`updated_at` 不到 15 秒、状态不是 `stopped`。 |
| `GET /sessions` | `{"enabled", "sessions": [ {会话字段, "dataset", "fault"} ], "fr3": {…}, "active"}`，直接读机器人侧文件（≤ 64 个）。 |
| `GET /datasets` | `{"enabled", "datasets": {名字: 行}}`（`status.json` 里的行）。 |
| `GET /datasets/{name}` | 数据集详情：`repo_id`（登记后的 LEVI 数据集 id，否则 null）、任务文本、各状态计数、片段列表（最新在前，≤ 200；每项有状态、集序号、`run_id`、尝试次数、时间片段数、提交时间、自动判定 `verdict`：`outcome/events/valid_events/undecided/spec/review: "auto"/evaluated: false`）、`incomplete`（含按原因计数）、进行中的批次、上一批、`last_error`。未知名字返回 404。 |
| `GET /audit?limit=50` | 自动批准主体的审计记录，最新在前，≤ 100 条，每条含 `tool`、`decision: allowed/refused`、`run_id` 等。 |

用 `repo_id`（`local/<名字>`）链接到查看器；判定对应的运行可按 `run_id` 在 LEVI 页面打开。

## 出问题时

- **FR3 故障**：客户端中止该集（`incomplete_NNNN`，`abort_reason: fr3_fault`），会话进入 `fault`。页面把数据集标为故障。被中止的 rollout 不会被标注。操作员排除故障后客户端续跑。
- **服务或 worker 崩溃 / `levi live stop`**：进行中的批次记在数据集状态里。下次启动接着做；LEVI 自己的运行记录保存了已完成的片段，所以不会重复标注或提交（提交用幂等键）。收到 `SIGTERM` 的 worker 会暂停运行并等租约释放；被 `SIGKILL` 杀掉的会留下租约，3 分钟后过期。
- **期间有人提交了该数据集**：计划的基线过期；worker 取消该运行并重新规划。
- **模型服务失败**：运行阻塞，worker 退出，监督进程按退避重试（30 秒起翻倍到 10 分钟）；失败的片段消耗一次尝试（`max_attempts`）。
- **`levi live doctor`** 打印服务各进程的 RSS/线程/nice、GPU 占用、vLLM 状态、磁盘和运行缓存大小、队列、FR3 状态和警告（监督进程超预算、进程不在 nice 19、vLLM 开着却没事做、timeshare 下策略服务器在监听时 vLLM 仍在跑、磁盘不足、复制而非链接、状态文件过期）。

## 实测

MEASURED_PLACEHOLDER_ZH

## 局限

- 释放复核规格和通用标注指南**没有在任何任务上评估过**，准确率未知。
- 数据集的视图在加入片段时重建（LEVI 的原始采集路径）：是流拷贝，几十个片段几秒，随数据集增长。
- timeshare 的标注依赖客户端报告状态：两集之间模型约有 20 秒；长会话的批次在会话结束后标完。
- 不使用 `inotify`；低频轮询足够，并且在任何文件系统上都可用。
