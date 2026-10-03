# 实时标注服务

`levi live` 是一个后台常驻的 LEVI：评测还在写 rollout 时，它就开始标注。它监视 rollout 目录，把**已完成**的片段拿进自己的工作区，用本地模型（vLLM 上的 Qwen3.8）标出时间片段和各时间片段的成败，并判断整个片段是否成功。人可以在实时页面看进度，在普通 LEVI 查看器里审核结果。[English](LIVE.md)

它是一个**独立的 LEVI 实例**（自己的工作区，界面端口 7880，核心端口 7881），长时间运行的工作不和你日常用的 LEVI（7860/7861）共用进程。设计目标是不妨碍机器人：自己降低优先级、限制线程、空闲时几乎零开销，只在 GPU 策略允许时才启动 GPU 模型。

它**不会**：写 rollout 目录、连接机器人或策略服务器端口、写人工成败标签、参与 gold 制作。它给出的判定都是自动的、未经审核的，并在所有地方这样标注。

## 快速开始

**顺序要紧：先带 `--prewarm` 启动实时服务，再起策略服务器，最后开评测。** 这样 vLLM 在显卡还空着时加载（冷启动是 45–70 秒的重 GPU 负载，对策略推理延迟的影响没测过，所以会话处于 running、homing 或等待复位时服务从不冷启动），没活时睡眠；策略服务器（`.22` = 7.6 GB，推理延迟和以前相同，约 59 ms）随后在它旁边加载：

```bash
# 1. 实时服务（启动一次，一直运行）。--prewarm 让 vLLM 现在就起来。
cd ~/work/wenkai/LEVI && uv run levi live start --daemon --auto-approve --prewarm
uv run levi live status        # 等到 vLLM 显示 ready（或 asleep）：约一分钟

# 2. 策略服务器，MEM_FRACTION 用 .22（vLLM 常驻时不要用 .25 或 .35）
cd ~/work/wenkai/openpi && XLA_PYTHON_CLIENT_MEM_FRACTION=.22 uv run scripts/serve_policy.py --port 8000 policy:checkpoint --policy.config pi05_fr3_all_state --policy.dir checkpoints/pi05_fr3_all_step49999

# 3. 评测客户端；“是否启用后台 LEVI 标注？”回答是。
```

如果策略服务器先起，`--prewarm` 起不了 vLLM（没有会话为这个服务器作证，闸门是关的；状态里写 `prewarm_waiting_for_policy`），冷启动要等客户端写出第一个会话文件、且它进入 `standby` 至少 20 秒（`gpu.standby_min_s`）之后才开始。这个等待只降低与第一集重叠的可能，不能消除。不加 `--prewarm` 时，工作在 `standby` 到来也是同样情形。

其他命令：

```bash
uv run levi live status                          # 它在做什么
uv run levi live doctor                          # CPU、内存、GPU、磁盘、警告
uv run levi live stop                            # 只停自己的进程
```

`levi live once [--fake-vlm]` 标完当前已完成的片段就退出（加 `--fake-vlm` 时使用假的模型服务，不需要 GPU）。`levi live init` 把所有默认值写成 `<工作区>/live.toml`。`levi live start` 在启动核心之前，把它校验过的配置（文件、`--config`、命令行覆盖）写到 `<工作区>/live/effective.toml`，所以核心读到的是本次会话的设置，而不是上一次的。`levi live resume` 清除服务放弃了的 vLLM 启动失败。`once --fake-vlm` 必须指定临时的 `--workspace`，不能是实时工作区。

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

`levi live init` 写出带全部默认值的文件；未知键或类型不对是错误，不会悄悄取默认值。`--workspace`、`--root`、`--gpu-mode`、`--auto-approve`、`--prewarm`、`--process-backlog`、`--since`、`--vllm-port`、`--ui-port`、`--core-port`、`--home` 可覆盖文件。`--adopt-workspace`：把已有的、不是实时工作区的 LEVI 工作区改成实时工作区才需要它；没有它时，`levi live` 拒绝任何有 LEVI 状态却没有实时标记的工作区（尤其是产品的 `.state`：自动批准的标记绝不能落进去），本检出的 `.state`、（从 git worktree 运行时）主检出（产品 LEVI 运行的地方）的 `.state`，以及 `LEVI_WORKSPACE` 指向的工作区（除非它已经是实时工作区），不管有没有 `--adopt-workspace` 都拒绝。每个 home 和每个工作区各只运行一个服务。环境变量：`LEVI_LIVE_WORKSPACE`、`LEVI_LIVE_CONFIG`、`LEVI_LIVE_HOME`（`status.json` 所在目录，默认 `~/.levi-live`）。`LEVI_LIVE_WORKER=1` 由服务在 worker 进程里设置（不要自己设）：它让 worker 不受“每次模型请求前查闸门”的约束，因为 worker 自己会让路。服务会把它从其他子进程的环境里去掉，所以 shell 里设了它也不会豁免任何东西。`LEVI_LIVE_VLLM_TIMINGS` 同样只由监督进程为它启动的 worker 设置：一个小 JSON，写着这个批次之前 vLLM 唤醒或冷启动花的秒数（`vllm_wake_s`、`vllm_cold_start_s`），worker 把它抄进 `stats.jsonl`；只传最近 10 分钟内的唤醒或冷启动，做完一个批次后就忘掉。服务也会把它从其他进程的环境里去掉。

各表、各键、默认值和含义与英文版表格一致（`service`、`watch`、`fr3`、`gpu`、`vllm`、`provider`、`pipeline`、`resources`），见 [LIVE.md](LIVE.md#settings-livetoml)。要点：

- `gpu.mode` 默认 `auto`：等于 `timeshare`（两者常驻 + 闸门）；`coexist` 和 `manual` 是手动选项。`gpu.busy_states` 默认 `["running"]`；`gpu.min_free_mib` 600、`gpu.policy_budget_mib` 8500 决定 vLLM 何时睡眠。
- `pipeline.auto_approve` 默认 **false**。`pipeline.keep_review_runs` 10：每个数据集保留冻结输入的开着的释放复核运行数（提交它需要输入），更旧的被取消并清理。`watch.stuck_s` 600：没有完成、也没有变化的片段超过这个时间算 `stuck`。`gpu.lead_s`/`lead_grace_s` 3/5：闸门在下一集开始前提前关闭。`service.gate_poll_s` 0.25。
- `gpu.policy_ports` 默认 `[8000]`，只在内核的 socket 表里查，从不连接。`gpu.policy_loaded_min_mib` 6000、`gpu.policy_load_wait_s` 120：监听着的策略端口只有进程占用到这么多显存才算“策略服务器已加载”（用于预算规划）；占得更少说明还在加载，vLLM 等待（`settling`），端口出现 `policy_load_wait_s` 秒后按“只有 vLLM”规划；读不到显存同样按“只有 vLLM”的保守预算。`gpu.standby_min_s` 20：冷启动要等会话在 `standby` 待满这么久（它的第一集几秒内就会开始）；这是缓解，不是保证，评测前用 `--prewarm`。`gpu.wake_margin_mib` 850：唤醒时在预算（减去睡眠中的 vLLM 仍占的部分）之外保留的空闲显存；启动用 `vllm.margin_mib`。**在真 GPU 上测过**（策略服务器 `.22`）：睡眠的 vLLM 旁空闲 22768 MiB，唤醒并做完第一批请求用了约 21758 MiB。原来的 300 会在空闲 21843 MiB 时放行唤醒，唤醒后只剩约 85 MiB，低于 `min_free_mib`（600），vLLM 会立刻又被放睡；800 在放行线上仍只剩约 585；850 时唤醒需要 22393，正好在线上也剩 635 MiB，实测的 22768 放行，富余约 375 MiB（按这些数字算出来的，没有观察到真正的来回抖动）。`gpu.blocked_pause_s` 300：GPU 锁被别的 agent 持有、:8100 上有别人的 vLLM、睡眠的 vLLM 因显存不够唤不醒，持续这么久后写进 `labelling_paused`。`gpu.resume_stable_s` 3（0.5–60 秒）、`gpu.resume_max_bounces` 3：人的运行被实时闸门拦住（`blocked`）后，闸门连续开着这么久就自动继续（避开 `episode_imminent` 窗口；用监督进程写在 `gate.json` 里的 `opened_at`，所以两次采样之间的关上又打开也算），每次打开一次；连续被拦这么多次、中间没有进展（完成片段或结算了 token），就留给人点“继续”；0 表示关闭自动恢复。`gpu.unknown_client_pause_s` 600：没有会话为之作证的策略服务器让闸门一直关着，超过这么久状态里 `labelling_paused` 写 `unknown_client`。
- `vllm.prewarm` 默认 false（`levi live start --prewarm`）：服务启动后、没有评测在跑时就把 vLLM 拉起来并保持常驻，空闲只睡眠，服务停止才停。这是**评测期间不冷启动**的办法。
- vLLM 的显存预算和上下文长度在每次启动时按**当时的空闲显存**选择（见下），`gpu_memory_utilization_max/min`、`min_utilization_with_policy/alone`（都是 0.725）、`kv_bytes_per_token`、`min_model_len` 是这个选择的界限（按**热**编译缓存下的实测校准，见下）；`margin_mib` 1100；`max_start_failures` 3、`start_backoff_s` 60、`start_backoff_max_s` 600。
- `vllm` 默认是实测的共存配置（`serve-qwen38.sh`，端口 8100，`gpu_memory_utilization` 0.74，`max_model_len` 49152，`max_images` 128，`max_num_seqs` 2，`max_num_batched_tokens` 4096，`--enable-sleep-mode`）；`idle_timeout_s` 120 秒无活后睡眠或停止（从 vLLM **就绪**那一刻起算，40–60 秒的载入不算闲置；之后从它最后一次有活起算）。
- 另有四项此前没写进文档：`watch.settle_s` 2：完成标记至少这么旧（秒）才收这个片段，它是“一集结束到第一个模型请求”固定延迟的一部分（见下文“两集之间有多少时间”）。`pipeline.human_recheck_s` 30：关闭 `auto_approve` 时，等待中的计划或草稿每隔这么久查一次是否已有人决定。`pipeline.budget_seconds` 86400：worker 规划的每个运行的墙钟预算（最多 86400），用完则运行变为 `blocked`。`resources.worker_idle_exit_s` 5：配置文件接受这个键，但目前没有任何代码读取它，worker 在批次做完时退出，而不是空闲等待后退出。
- 服务为实时工作区设置 `LEVI_DROID_SAMPLE=off`、`LEVI_GPU_SHARING=allow`（用它自己的策略取代 LEVI 的错峰守卫，见下）、`LEVI_SYNC_DISCOVER=off`、`LEVI_SYNC_INTERVAL=30`。

## GPU 管理

一块 32 GB 的 GPU 与机器人的策略服务器共用。实测（`levi-hub/reports/live-gpu.md`）给出规则：

- 策略服务器用 `XLA_PYTHON_CLIENT_MEM_FRACTION=.22`（7.6 GB，推理 p50 约 59 ms，与 `.35` 相同），vLLM 用 `--gpu-memory-utilization 0.72`，两者**可以同时常驻**（峰值约 31.3 / 32.6 GB，余量 1.3 GB）。这个实测是编译缓存还是冷的第一次启动；之后每次启动要 0.74 才装得下 49152 token 的上下文（见下“为什么不是 0.72”），和策略服务器并存时余量约 0.6 GB。原来的 `.35`（11.8 GB）和 vLLM 放不下。
- 两者**不能同时推理**：vLLM 工作时策略每次推理约 120 ms，而不是 59 ms，超过 100 ms 控制周期，记录的时间步会抖动。vLLM 空闲（已加载、没有请求）对策略没有影响。

所以默认的 **`timeshare`**（`auto` 的含义）让两者常驻，用**闸门**错开工作：

| 评测状态 | 闸门 | 模型 |
| --- | --- | --- |
| 有会话处于 `running`（策略在推理） | **关** | 不发请求；在途的请求立即取消，运行退避 |
| 会话处于 `waiting_reset`，且下一集将在 `gpu.lead_s`（3 秒）内开始（开始 = 它开始等待后再过 `levi.reset_wait_s`） | **关**（`episode_imminent`） | 新一集的第一次推理不会撞上模型请求 |
| `homing`、更早的 `waiting_reset`、`standby`、`fault` | 开 | 已醒着的 vLLM 工作（此时不**新启动**，见下） |
| 有策略服务器在监听，且没有仍然有效的会话为它作证（策略服务器出现之前就结束的会话不算：会话文件从不删除） | **关**（可能有我们不知道的客户端在用） | 等待 |
| 没有策略服务器 | 开 | 工作 |

监督进程在 worker 运行时每 0.25 秒重新决定闸门（worker 每 0.2 秒读一次；测试断言 `running` 出现后 1 秒内在途请求被切断），写到 `live/gate.json`（状态变化时重写，否则在距上次写入至少 4 秒后的第一个循环 tick 重写；tick 在空闲时每 `heartbeat_s`（4 秒）一次，评测未结束时每秒一次，worker 运行时每 `gate_poll_s` 一次，所以空闲时文件每 4–8 秒重写一次，实测约 6 秒，远在下面 20 秒的限度之内；worker 把超过 20 秒没人刷新的文件一律读作关闭，不管它写了什么，所以监督进程死了也不会把闸门留在开着的状态；人的处理略有不同，见“出问题时”；监督进程停止时会删除该文件），worker 遵守：闸门关闭时，worker 暂停自己的运行（暂停会中止在途的 HTTP 请求，服务器随即停止生成），等运行线程释放租约，闸门打开后继续。不会重复任何工作：运行从最后一个完成的片段接着做。8 秒内没有让路的 worker 会被监督进程停掉。两集之间（`homing`、`waiting_reset`）模型标注，可用的窗口见下一段；会话结束后一口气标完剩下的。

**两集之间有多少时间。** 窗口不是固定的 20 秒，而是客户端的 `levi.reset_wait_s`（等操作员复位的时间；评测客户端的计划默认值是 10 秒，一次试跑用了 5 秒）减去闸门的提前量 `gpu.lead_s`（3 秒），再加上它之前的 `homing` 时间。而且窗口的开头用不上：从一集结束（`.complete`）到第一个模型请求有约 6–9 秒的固定延迟，这是试跑中测到的：片段在 3.4–5.6 秒后镜像完成（`watch.settle_s` 2 秒、下一次扫描最多 `poll_active_s` 3 秒、复制），然后 worker 启动并做规划。`reset_wait_s` = 5 时窗口约 2 秒，比这个延迟还短：一个批次的第一个请求要等下一集已经开始之后才发出，被闸门拦住，所以实际上是会话结束后才标。取 10 秒时窗口约 7 秒，也只够发第一个请求。加长 `reset_wait_s`，或缩短延迟（`settle_s`、`poll_active_s`）才能让标注落进两集之间；十集以上的长会话还没有测过。

| 模式 | 行为 |
| --- | --- |
| `timeshare`（`auto`） | 如上 |
| `coexist` | 同样两者常驻，但闸门永不关闭：模型有活就做，此时每次策略推理约多 60 ms。手动选项 |
| `manual` | 从不启动 vLLM：使用 `vllm.port` 上已有的那个 |

**启动、睡眠、唤醒。** 有批次在等、且 (a) **闸门是开的并且没有评测在进行**：冷启动是 45–70 秒的重 GPU 负载，**它对策略推理延迟的影响没有测过**，所以只要有会话处于 `running`、`homing` 或 `waiting_reset` 就不启动（决策 `evaluation_active`），只在 `standby`（并且会话已在那里待满 `gpu.standby_min_s`，否则决策 `standby_settling`）、会话结束之后或没有会话时启动。要在评测前就让模型就绪，用 `--prewarm`（或 `vllm.prewarm = true`）在**策略服务器之前**启动服务，并等 `levi live status` 显示 vLLM ready：它立即起来并常驻（空闲只睡眠，`levi live stop` 之前不会停）。如果在策略服务器之后才加 `--prewarm`，它会被关着的闸门挡住（决策 `prewarm_waiting_for_policy`，状态里有大白话说明）。预热只配 `.22` 的策略服务器：更大的（`.25` 占 8575 MiB）让 vLLM 无法在旁边保持清醒。预热期间，即使睡眠它也一直持有工作区 GPU 锁和约 2.2 GB（见局限）。从睡眠**唤醒**（0.75 秒）同样要闸门开着、且下一集不是马上开始（`episode_imminent`），并且空闲显存够“预算减去睡眠中的 vLLM 仍占的部分再加 `gpu.wake_margin_mib`（850；启动的 `margin_mib` 是 1100）”。(b) 空闲显存允许一个预算：预算在每次启动时按空闲显存选，不是固定的。策略服务器已加载（其进程占用至少 `gpu.policy_loaded_min_mib`；`.22`：空闲约 24.9 GB）时，预算 = 空闲显存减 `margin_mib`，上限 0.747；策略服务器占用超过 `gpu.policy_budget_mib`（8500 MiB；`.25` 占 8575）时，在任何冷启动之前直接拒绝（决策和 `labelling_paused` 的代码都是 `policy_large`，并说明改用 `.22`），因为 vLLM 起来后会立刻睡下再也醒不了；策略端口在监听、却几乎没占显存说明还在加载：vLLM 等待（`settling`），`gpu.policy_load_wait_s` 之后（或读不到显存时）按只有 vLLM 的情况规划。显卡上只有 vLLM 时预算是配置的 0.74。**为什么不是 0.72**：这套参数第一次启动在 0.72 成功，只是因为编译缓存还是冷的；之后每次启动都加载已编译的图，KV 缓存之外多占约 0.3 GiB，0.72 只剩 1.73 GiB KV 缓存，不够 49152 token 所需的 1.82 GiB（GPU 完全空闲时实测）。0.74 有 2.36 GiB，并为之后才启动的策略服务器留约 0.6 GB（没有在真 GPU 上验证；清空编译缓存后的第一次启动可能不同）。预算装不下 `max_model_len` 时，上下文每次降 4096，直到 `min_model_len`（32768）；再小也装不下就不启动，状态写 `insufficient_vram` 和具体数值（原来的 `.35` 策略服务器就是这种情况）。预检和启动用同一个数，事件日志写明用了什么值。vLLM 仍因 KV 缓存不足失败时，错误信息会直说（“显存预算太小：…至少用 X，或把上下文降到 N token 以内”）。(c) 最近 `gpu.settle_s` 秒内没有策略服务器出现或消失（还在加载的会预分配显存），(d) 工作区 GPU 锁空闲时，才启动 vLLM。:8000 从 `/proc/net/tcp` 读，显存用 `nvidia-smi --query-gpu`，从不连接策略端口。期间状态是 `gpu_wait`。vLLM 带 `--enable-sleep-mode` 和 `VLLM_SERVER_DEV_MODE=1`（开发端点，只在 127.0.0.1）：

- **睡眠**（第 1 档：5.5 秒睡下，留 1.8 GB，权重放到主机内存），当它的显存被需要时：空闲显存低于 `gpu.min_free_mib`，或策略服务器占用超过 `gpu.policy_budget_mib`（8500 MiB：比 `.22` 更大的比例，清醒的 vLLM 放不下）。先停 worker。空闲 `vllm.idle_timeout_s` 后，评测仍在进行则也睡眠（`idle_action = auto`），没有任何评测时**停止**（评测不在进行时直接停止）。
- **唤醒**（0.75 秒），有活在等且空闲显存重新够用时。策略服务器较大时保持睡眠，等它退出后才标注（实测前的行为）。不使用第 2 档睡眠。

**启动失败。** 启动失败（从 vLLM 自己的日志读最后一行错误，例如 `ValueError ... KV cache ...`）后 60 秒重试，然后 120 秒，最长 600 秒；连续 `vllm.max_start_failures`（3）次失败后服务停止尝试：标注暂停（`status.attention`，`gpu.decision.code = needs_attention`），`levi live doctor` 带原因告警，`levi live resume` 清除。同样的信息在状态的 `labelling_paused` 里（见下）。期间服务保持 `accepts_sessions` 为真：评测客户端只需要有地方接收它的 rollout，rollout 仍在被镜像，恢复后再标注。

**停止 vLLM。** 只有服务器的整个进程组都退出、`nvidia-smi` 也不再列出它的任何进程之后才放锁（最多等 60 秒）；如果它们还在释放显存，锁保留，决策是 `gpu_not_free`，每个 tick 再看一次。

始终成立：工作区 GPU 锁（`levi-hub/.gpu.lock` 上的 `flock`，`LEVI_AGENT=live`）跟着 vLLM 进程走：vLLM 启动时带着锁的文件描述符，所以监督进程被 `kill -9` 后，只要 vLLM 还活着锁就在；重启的监督进程只有在锁仍被它持有时才接管运行中的 vLLM（否则拒绝并说明）；只有确认 vLLM 真的退出才放锁，`levi live doctor` 会报告孤儿 vLLM 以及如何停止。只停自己启动的服务（按进程身份核对），不碰别人的；不是它启动的 vLLM 只有在 `adopt_external`（或 `manual`）下才使用且从不动它；:5000 和策略端口从不连接；空闲 tick 不碰 :8100。残余风险（见实测报告）：余量只有 1.3–1.5 GB；第一集可能遇到一次 280 ms 的策略离群值；vLLM 清醒时用更大的 `MEM_FRACTION` 重启策略服务器会加载失败，请在 vLLM 唤醒前启动它，或先 `levi live stop`。

## 自动批准主体

其他 LEVI 工作区里只有人能批准计划和提交标注。实时服务无人值守，所以它的 worker 在**同时满足以下全部条件时**才以 `live-auto` 身份行动：

- 服务以 `--auto-approve`（`pipeline.auto_approve`）启动；此时只有 worker 得到 `LEVI_LIVE_AUTO_APPROVE=1`，界面和核心不会；
- 工作区有 `live/workspace.json`，只有 `levi live` 会写它；
- 由 HTTP 请求构造的主体永远不是 `auto`，所以页面和已连接的 agent 都变不成它。

它能做的：`runs.plan`、`plans.approve`、`runs.execute`/`resume`/`pause`/`cancel`、`runs.get`/`events`、`changes.diff`/`validate`/`approve`/`commit`、`anchored.get`，仅此而已（不能 reset、clean、试点验收、发布知识或改进）。它只能批准、运行、提交**自己规划的运行**；同一工作区里人的计划或草稿会被拒绝。它的计划在计划里放弃试点片段，批准即覆盖这一点。**它只能批准和提交纯 temporal 运行的时间片段**：对复核（anchored）运行和任何非时间片段的提议（例如成败结局），`changes.approve` 与 `changes.commit` 一律拒绝，不管谁调用；写结局标签的代码本身也拒绝任何经它批准的东西。实时数据集永远没有人工成败标签，由授权层保证，而不是靠 worker 的调用顺序。

它留下的痕迹：它批准的每个变更集带 `reviewer_type: auto`（审核者 `live-auto`），它提交的每个时间片段带 `levi.review: "auto"` 和 `levi.origin.review: "auto"`，`<工作区>/live/audit.jsonl` 对每个改变状态的调用，在放行时记一行、结果出来后再记一行（`completed`，或带错误的 `failed`），每次拒绝也记；纯读取不记。人在查看器里保存时若改了 auto 时间片段的文字或时间，标记变为 `edited`；原样保存则保持 `auto`。人写过、改过的内容永远不会被替换：重跑只替换仍与上次运行所写完全一致的时间片段，服务也会跳过已有非自己写入标注的片段（`skipped_human`）。

**不写成败标签。** 提交从不写人工成败标签（`annotations/outcomes/`），所以训练池不会把自动判定当成人工标签。片段的自动判定是**锚定复核记录**（`anchored.get`），复制进数据集状态，带 `review: auto` 和 `evaluated: false`。释放复核运行停在 `waiting_for_review`，其中的 `outcome` 提议人可以在 LEVI 页面接受（那时的提交才是人的动作、人的标签）；服务自己从不提交它。每个数据集最新的 `pipeline.keep_review_runs` 个这样的运行保留冻结输入（提交它需要）；更旧的被取消并清理，判定仍留在数据集状态里（`review_runs_open` 统计开着的）。无人值守评测的 rollout 是 `eval.outcome = "unlabeled"`；LEVI 现在把它（和 `aborted`）读作“没有机器人标签”，而不是把占位的 `success_flag_final = 0` 当成失败。训练池的扫描签名带版本号，所以修复之前写下的索引在产品 LEVI 重启后会被重新读取（先重启，再扫描无人值守评测的数据）。

**训练清单怎么处理它。** 涉及的只有*结局*的来源，而自动判定不是验证过的判定：`levi export manifest` 默认跳过规格为 `candidate` 的锚定复核（通用释放复核就是），除非用 `--anchored-run` 指明运行，或加 `--allow-candidate-anchored`；清单会记录 `anchored.spec_status`。自动的时间片段带 `levi.review: auto`，进入 `frames.parquet` 的 `subtask_review` 列和 `manifest.json` 的 `annotation.subtask_auto_share`，训练方可以丢掉或降权。这些判定和时间片段都没有评估过。

**没有 `--auto-approve` 时。** 服务只规划并等待。每个批次需要人通过两到三道关：时间片段计划、它的草稿（批准并提交）、释放复核自己的计划。监督进程知道 worker 在哪道关等，关没过之前不启动 worker，也不为这个数据集保留 vLLM（计划被批准；草稿被提交或拒绝），人提交的内容记为人完成（`review: human`），而不是失败的尝试。

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

## 实时页面

`/live` 以只读方式显示评测进行时发生的事。它需要实时工作区的核心（`levi live start`）；在其他 LEVI 里手动打开这个地址，会提示“当前 LEVI 不是实时标注工作区”。

**入口只在实时工作区出现，并且在那里是最显眼的一个。** 在其他 LEVI（你平时用的那个）里，顶部导航没有“实时评测”，首页也不变。在实时工作区里，它是导航的第一项，带轮廓，有一个状态小圆点（绿：运行正常且空闲；蓝：正在标注；琥珀：标注已暂停，或有事在等人处理，包括被闸门拦住、需要你点“继续”的运行（会自己继续的被拦运行不改变圆点和数字，实时页只显示数量，不列出运行编号）；红：FR3 红灯，或评测会话出了故障；灰：读不到状态），有事等人时显示数字，悬停有一句中文或英文的说明。实时工作区的首页（`/`）顶部是一条大横幅，圆点和说明相同，点击进入 `/live`；这是横幅而不是跳转，所以首页的 LEVI 标识和 `?path=`/`?dataset=` 链接照常工作，其他页面也只差一次点击。页面靠每次加载时问一次 `/api/levi/live/status` 判断是不是实时工作区：返回 `{"enabled": false}` 就到此为止（没有定时器，不再请求，产品 LEVI 不增加负载）；在实时工作区里，标签页可见时每 8 秒再问一次来更新圆点（失败后最长 30 秒）。

- **红色横幅**“检测到 FR3 出错，评测已中断”：健康监视器报告红灯或有会话处于 `fault` 时出现，相关数据集卡片带红色标记。监视器缺失或陈旧**不算**红灯（琥珀色“监视器离线”）。
- **评测会话**：状态、评测编号、片段 `第几个 / 目标`（有效片段数）、步数进度条、策略 config 和 checkpoint 文件夹名、最近一个片段、是否启用 LEVI 标注、复位等待；客户端心跳超过 10 s 显示“已失联”。
- **FR3 机械臂**：模式、红灯、错误、硬件和控制器状态、原因。
- **标注管线**：每个数据集一张卡片，显示已镜像、等待、标注中、完成、失败的片段数，已提交的时间片段，以及**自动**成败结果（虚线框、标“自动”，写明“未经审核，准确率未评估”，不会画得像金标准）。留待人工复核的复核运行会列出（默认最新 3 个；按浏览器保存的只读筛选可隐藏较早的，不改变任何数据），并链接到查看器。
- **服务与资源**：状态、GPU 模式、vLLM 状态、用人话解释的标注闸门（例如策略推理时为什么暂不标注）、队列、工作进程、最近错误、监督进程的内存、线程和 CPU。服务未运行时页面会说明，并提供可复制的 `levi live start`。
- **需要人处理**：服务放弃启动模型服务器（`attention`，写明原因并给出可复制的 `levi live resume`）、数据集在等你于 LEVI 页面批准计划或提交草稿（`awaiting`）、或服务启动的页面/核心没起来（`frontend`）时，出现琥珀色横幅。数据集卡片还会显示卡住的片段和被替换的源。复位等待在两次轮询之间由浏览器倒计时（`reset_wait_s`、`waiting_reset_since`），过期后提示“下一个片段应已开始”。GPU 闸门和决定的每个代码都有中英文的人话解释。当服务因不会自行消失的原因暂停了标注（`labelling_paused`：`vllm_failed`、`vllm_error`、`insufficient_vram`、`policy_large`、`unknown_client`、`vram`、`lock`、`external_busy`），横幅会说明原因、评测不受影响且片段不会丢，以及该怎么做（需要恢复时附可复制的 `levi live resume`）；主循环超过 5 分钟没动作（`loop_at`）也会提示。在 Agent 工作台里，策略推理期间被拒绝的 Run 或 Resume 会显示一句人话（几秒后再试；已经在跑的运行会自己继续），而不是核心的日志原文。

评测运行时每 2 s、空闲时每 10 s 轮询 `/api/levi/live/status` 和 `/sessions`；标签页不可见时暂停；失败后退避（最长 30 s）；答复是 `{"enabled": false}`（不是实时工作区的说明页）时永久停止；只为少数重要或已展开的卡片加载数据集详情，且仅在该行变化后再次加载。页面没有任何写操作，也不显示令牌。

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
| `<工作区>/live/gate.jsonl` | 闸门的每一次变化（轮转；见“历史与统计”） |
| `<工作区>/live/stats.jsonl` | 每个已标片段一条记录（轮转；见“历史与统计”） |
| `<工作区>/live/logs/` | `live.log`、`worker.log`、`ui.log`、`vllm-launch.log`（轮转） |
| `<工作区>/captures/<名字>/` | LEVI 登记的镜像采集 |
| `<工作区>/outputs/LEVI/…` | LEVI 自己的状态：视图、运行、已提交标注 |

## 历史与统计

**闸门历史（`live/gate.jsonl`）。** 闸门的状态或原因代码每变化一次就加一行：`at`（纪元秒）和 `time`（本地时间）、`from` 与 `to`（`{open, code}`；第一行的 `from` 是 null，服务写的最后一行 `to.code = service_stopped`）、`reason`、`idle`、`policy_up`、`policy_ports`（配置的端口）、`sessions`（各会话的 group、任务和状态，最多 8 个）。不含令牌和密钥。它按 `resources.log_max_mb` 轮转，保留 `resources.log_backups` 个旧文件；读取时跳过损坏的行。最近五次转换在状态文件里（`gpu.gate.history`），`levi live status` 以 `gate` 行打印。事后要回答“策略推理时闸门有没有关着”，看它。

**worker 日志。** 一个批次做完后，`logs/worker.log` 对批次里的每个片段写一行，方便 grep：`episode demo=demo_0003 ep=3 batch=2 temporal_wall=28.3s temporal_model=10.6s temporal_requests=2 temporal_tokens=21034 review_wall=6.0s ... segments=3 verdict=failure valid_events=0 gated=no state=done`。`*_wall` 是该阶段对整个批次的墙钟；模型的数字是这个片段自己的。`gated=1x/5.2s` 表示闸门让批次停了一次、共 5.2 秒。

**每个片段的统计（`live/stats.jsonl`）。** 批次做完后，worker 对批次里的每个片段（做完的或失败的）追加一行 JSON，schema 是 `levi.live.episode_stats.v1`，轮转方式同闸门历史。`levi.live.stats.read(live_dir, limit=None, since=None)` 按从旧到新返回记录，跳过损坏的行，缺的字段读作 `null`；请用它，不要自己写解析。字段名和单位是稳定的：可能新增字段，不会改名。所有时长的单位是秒，没能测到的值是 `null`。

| 字段 | 含义 |
| --- | --- |
| `schema`、`at` | `levi.live.episode_stats.v1`；这一行写入的纪元秒 |
| `dataset`、`demo`、`episode_index` | 实时数据集（`<group>__<task>`）、采集文件夹（`demo_0003`）、片段在数据集视图里的编号 |
| `session` | 该片段所属的评测运行 id（其 metadata 里的 `eval.run_id`） |
| `attempts`、`excluded` | 此前对这个片段失败了几次（第一次就成功为 0）；`false`（留给被人排除的片段） |
| `episode.frames`、`episode.episode_seconds` | 帧数和片段自身的时长 |
| `timeline.to_mirror_s` | 从 `.complete`（片段结束）到镜像完成 |
| `timeline.to_plan_s` | 到该批次的时间片段计划生成 |
| `timeline.to_first_request_s` | 到这个片段的第一个模型请求开始 |
| `timeline.to_commit_s`、`timeline.to_verdict_s` | 到时间片段提交；到自动判定出来 |
| `model.requests.{coarse,refine,review,probe}` | 按种类的模型请求数：粗标、边界精修（一次或多次）、释放复核（每个问题一次）、请求开销校准（运行级的开销，记在批次的第一个片段上）。缓存命中不算请求 |
| `model.model_seconds.{coarse,refine,review,probe}` | 模型在这些请求上花的秒数（`probe` 不计时：`null`） |
| `model.prompt_tokens`、`completion_tokens`、`total_tokens` | 服务器报告了用量的那些请求的 token 之和（`total_tokens` = prompt + completion；没有请求报告时前两项是 `null`）。校准探测和预留不在其中 |
| `model.probe_tokens` | 该批次请求开销校准的 token（运行级的开销，只记在批次的第一个片段上）；其余为 `null` |
| `model.reserved_tokens`、`model.unreported_steps` | 服务器没给用量的请求，LEVI 按预留额度记账：这些预留之和（不是实际花掉的 token；没有则 `null`）和这类请求的个数 |
| `model.images` | 发送的图片数 |
| `model.external_tokens` | 恒为 0：没有任何东西离开这台机器 |
| `gate.closed_wait_s`、`gate.interruptions` | worker 因闸门关闭而让路的秒数和次数，按这个片段所在的整个批次算（一个批次有多个片段，每个都带批次的数字） |
| `gate.vllm_wake_s`、`gate.vllm_cold_start_s` | 监督进程在启动 worker 之前 10 分钟内做的唤醒（约 0.75 秒）或冷启动的耗时。它记在做完的那个批次的第一个片段上；worker 没活可做、在等模型或等人、或失败时，会把它留给下一个 worker，超过 10 分钟的丢弃，所以不会记到几小时后的批次上。其余片段为 `null` |
| `result.state`、`result.reason` | `done`、`failed` 或 `mirrored`（之后重试）；没做完时的原因 |
| `result.segments`、`result.segment_labels` | 提交的时间片段数，以及按子任务 id 的计数 |
| `result.verdict` | 自动释放复核的 `{outcome, events, valid_events, undecided}`，或 `null` |
| `result.review` | `auto` 或 `human`（谁提交的时间片段） |
| `result.spec` | `{guideline, release_review, sha256}`：用到的文件和它们的哈希 |
| `result.provider`、`result.model` | 模型配置名和服务的模型 |

这个文件是本服务工作的记录，LEVI 自己从不读取，也不是训练数据。

**账本里的运行时间。** 运行账本的 `wall_seconds`（`cost-rise-*` 改进提议拿它比较）不含运行在等人、或为策略服务器让路的时间：一个释放复核运行停在 `waiting_for_review`、四十分钟后被取消，不再读作 2400 秒的运行。扣掉的总数是 `idle_seconds`，拆成 `waiting_for_person_seconds` 和 `stood_down_seconds`；准确规则见 `docs/AGENTS.md`。旧账本保持旧值，成本基线（取历史中位数）的历史里还有一部分是旧口径，所以一段时间内新旧运行会混着比较。

## 状态文件（接口 C4）

`~/.levi-live/status.json`，每 `heartbeat_s`（4 秒，≤ 5 秒）原子重写。评测客户端只有在**全部**满足时才认为服务可用：`schema` 以 `levi.live.status.` 开头；`updated_at`（纪元秒）不到 15 秒；`pid` 存活；`accepts_sessions` 为真；`state` 是 `idle active annotating gpu_wait` 之一；`watch_roots`（绝对路径）中有一个等于或包含客户端的 `--rollout-root`（或被它包含）。否则客户端退回手动标注。`accepts_sessions` 在 `starting` 以及 `stopped`/`error` 时为假；`gpu_wait`（有活在等模型：闸门关闭、vLLM 正在启动或在睡眠）算可用。 `gpu.free_mib` 是最近一次 `nvidia-smi` 的读数，**只在不到 30 秒内才给出**，否则为 `null`（睡眠中的 vLLM 有意不去探测，睡下之前的读数，例如 2254 MiB，不是现在的空闲显存）；`gpu.free_mib_at` 是读数的时间。`gpu.idle_since` 是 vLLM 开始闲置的时间（有活、正在载入或本就该常驻时为 `null`），`gpu.prewarm` 说明它是否以预热方式启动；`levi live doctor` 会读这两个字段。`attention` 在服务放弃启动 vLLM、需要人（`levi live resume`）时设置，此时标注暂停，但不改变 `accepts_sessions` 和 `state`。**`labelling_paused`** 为 `null`，或在“没有标注、且原因不会自己消失”时为 `{code, reason, since}`：`vllm_failed`（放弃启动 vLLM，`levi live resume`）、`vllm_error`（启动失败、正在退避）、`insufficient_vram`（空闲显存连最短上下文也装不下）、`policy_large`（策略服务器占用超过 `gpu.policy_budget_mib`：改用 `.22`；立即报告，按服务器的显存判断，不管闸门或评测在做什么，是一次稳定的暂停）、`unknown_client`（没有会话为之作证的策略服务器让闸门关了 `gpu.unknown_client_pause_s` 以上），以及持续 `gpu.blocked_pause_s` 之后的 `vram`（睡眠的 vLLM 唤不醒，或启动没有足够显存）、`lock`（别的 agent 持有 GPU 锁）、`external_busy`（`vllm.port` 上有别人的 vLLM）和 `gpu_not_free`（vLLM 已停但它的进程仍占着显存，所以锁保留）。策略推理时闸门关闭、策略服务器还在加载，都是正常等待，不设置它；它也不改变 `accepts_sessions`。`loop_at` 是主循环上次 tick 的时间（`updated_at` 来自单独的心跳线程，循环卡住时它仍然新鲜；`levi live doctor` 在 `loop_at` 超过 5 分钟时告警）。`frontend` 说明服务启动的页面/核心 API 是否真的起来（`levi live start --daemon` 失败时打印原因并以退出码 2 返回，标注继续运行）。新增字段：数据集行的 `stuck`、`source_changed`、`review_runs_open`、`awaiting`，会话的 `root`、`reset_wait_s`、`waiting_reset_since`，闸门代码 `episode_imminent`，决策代码 `evaluation_active`、`standby_settling`、`prewarm_waiting_for_policy`、`gpu_not_free`。完整 JSON 形状见 [LIVE.md](LIVE.md#status-file-interface-c4)。

## 服务读取的机器人侧接口

- **C2 会话**：`<根>/.eval_sessions/<group>__<task_folder>.json`，schema `levi.eval.session.v1`（`state`：`standby homing running waiting_reset fault stopped finished`）。10 秒没有更新且进程已不在，或超过一小时未更新（回收的 pid 不能让它复活），会话算 `crashed`。`waiting_reset_since`（本次等待第一次进入 `waiting_reset` 的纪元秒；闸门的提前量从它算起，旧客户端不写时从本服务第一次看到该会话算起；会话在机器人上或在两集之间时，监督进程至少每秒看一次）、`run_id`（被续跑的评测轮）、`episode.no`（本次会话的集序号）、`episode.counted`（该轮累计有效片段数）原样传给页面。`incomplete_*` 的中止原因：`fr3_fault`、`user_quit`、`interrupted`、`process_killed`（只有 `fr3_fault` 把数据集标成故障；所有原因都按类计数）。
- **C3 FR3 健康**：`fr3_health.json`，schema `levi.fr3.health.v1`（优先 `updated_at_epoch`，否则 `updated_at`）。缺失或超过 `fr3.stale_s` 是**离线**，不是红灯。`red_light` 还包括：5 秒没有实时机器人状态、5 秒连不上 controller manager、没有 Franka 硬件组件；服务只显示，不据此行动。

## HTTP API（只读，`/api/levi/live/*`）

任何有 `live/workspace.json` 的工作区的核心都提供这些路由；其他工作区全部回答 `{"enabled": false}`。没有任何路由会改东西或返回令牌。

| 路由 | 回答 |
| --- | --- |
| `GET /status` | `{"enabled", "alive", "age_s", "service": <status.json 或 null>, "faults": [{"dataset", "reasons": []}], "fr3_red", "blocked_runs": {"count", "waiting", "needs_person"}}`。`alive` = pid 存在、`updated_at` 不到 15 秒、状态不是 `stopped`。 |
| `GET /sessions` | `{"enabled", "sessions": [ {会话字段, "dataset", "fault"} ], "fr3": {…}, "active"}`，直接读机器人侧文件（≤ 64 个）。 |
| `GET /datasets` | `{"enabled", "datasets": {名字: 行}}`（`status.json` 里的行）。 |
| `GET /datasets/{name}` | 数据集详情：`repo_id`（登记后的 LEVI 数据集 id，否则 null）、任务文本、各状态计数、片段列表（最新在前，≤ 200；每项有状态、集序号、`run_id`、尝试次数、时间片段数、提交时间、自动判定 `verdict`：`outcome/events/valid_events/undecided/spec/review: "auto"/evaluated: false`）、`incomplete`（含按原因计数）、进行中的批次、上一批、`last_error`。未知名字返回 404。 |
| `GET /audit?limit=50` | 自动批准主体的审计记录，最新在前，≤ 100 条，每条含 `tool`、`decision: allowed/refused`、`run_id` 等。 |

用 `repo_id`（`local/<名字>`）链接到查看器；判定对应的运行可按 `run_id` 在 LEVI 页面打开。

## 出问题时

- **FR3 故障**：客户端中止该集（`incomplete_NNNN`，`abort_reason: fr3_fault`），会话进入 `fault`。页面把数据集标为故障。被中止的 rollout 不会被标注。操作员排除故障后客户端续跑。
- **服务或 worker 崩溃 / `levi live stop`**：进行中的批次记在数据集状态里。下次启动接着做；LEVI 自己的运行记录保存了已完成的片段，所以不会重复标注或提交（提交用幂等键）。收到 `SIGTERM` 的 worker 会暂停运行并等租约释放；被 `SIGKILL` 杀掉的会留下租约，3 分钟后过期。
- **策略推理时有人在页面点“运行”“继续”或任务控制台的推进**：实时工作区的核心读 `live/gate.json`，闸门关闭时拒绝（`409`，“评测正在推理，请稍后再试”）。闸门打开时启动的运行也会停：核心在**每一次**模型请求之前都读闸门（只 stat 工作区标记，小文件最多每 0.2 秒读一次；所有发请求的入口走同一个检查，`gpu.require_free`），闸门关着就让请求以 `GpuBusy` 失败，运行停在 `blocked`（带 `blocked_by: gpu` 和 `blocked_gate: live`，这是闸门自己的标记）。worker 自己的运行不受它约束（它自己会让路）。闸门文件超过 20 秒没人刷新（监督进程没了）时，worker 一律当作关闭（没人再管它，而且之后可能出现了策略服务器）。人只有在文件最后写的是 `idle`（没有策略服务器、没有评测：没有要保护的东西）**并且**文件里列的策略端口此刻都没有在监听（读 `/proc/net/tcp`，不连接）时才放行；否则页面提示闸门文件已过期、请确认监督进程是否在运行。监督进程活着时，文件每次变化都重写、至少每 4 秒一次（vLLM 的长时间停止等待期间也刷新），所以空闲时的标注不会因此停摆；正常停止的服务会删除这个文件。

  **这样的运行会自己继续。** 核心里有一个小的守护线程（`levi/live/resumer.py`；只在实时工作区存在，空闲时每秒一次 stat 加一次小文件读取）每秒读一次 `live/gate.json`，调用 `Workbench.launch`，也就是点“继续”最终走的那条路，所以启动时的所有检查依然有效（计划已批准、租约空闲、没有已批准未提交的草稿）。它放在核心里，因为运行、租约和执行线程都在那里，闸门也本来就在那里按请求读取；它不是自动批准者，也不是经过分发器的调用，所以自动批准者“只能处理自己规划的运行”这条规则丝毫没放宽。它只恢复：带闸门标记的 `blocked` 运行（模型错误、校验失败、老师的待决定、GPU 守卫的拦截、人主动暂停或取消的运行都没有这个标记，暂停和取消绝不会被它撤销）；**人**规划的运行（主体 `local-human`，也就是 LEVI 页面；worker 自己的运行归 worker，连接的 agent 的运行归它自己）；没有待处理的暂停/取消、也没有试点片段在等人审核的运行。并且只有当闸门文件**新鲜且开着**（过期的文件对它永远不算“开”，不管它最后写的是什么；文件不存在也不算），并且**已连续开着 `gpu.resume_stable_s`**（3 秒：`episode_imminent` 的窗口在这之前就被提前量关上了）。同一个运行在闸门的每一次打开期间最多恢复一次，每秒最多恢复一个运行。运行被闸门连续打回 `gpu.resume_max_bounces`（3）次、中间没有进展（完成片段或结算了 token；`requests` 不算，它在预留调用时就加了），就留给人（在页面点“继续”），状态里会写明。每次恢复、启动失败和放弃都会在 `live/audit.jsonl` 里留一行（主体 `live-resume`，工具 `runs.resume`，决定 `auto_resumed`/`failed`/`given_up`，带运行 id、运行的原因和闸门的 `open`/`code`/`updated_at`/`open_for_s`）。同一个线程写的 `live/blocked_runs.json` 列出被闸门拦住的运行；`/api/levi/live/status` 里是 `blocked_runs: {count, waiting, needs_person}`：`waiting` 会自己继续，`needs_person` 不会（不是人的运行、有暂停待处理、试点待审核、已放弃、或用 `resume_max_bounces = 0` 关闭了自动恢复）。人在线程读取和 launch 之间点“暂停”以人为准：`Workbench.launch(expect=…)` 在自己的事务里重新检查（审计为 `skipped`），每次恢复也会在该运行的事件里留一条 `auto_resumed`。核心重启后在启动时扫一遍运行记录，找回被闸门留在 blocked 的运行；在核心以外的进程里被拦的运行只有那时才会找到，人自己点“继续”也要等运行有进展才重置放弃计数（已知局限）。GPU 守卫（`inference.gpu.Watch`）跳过这些运行。
- **等人**（自动批准关闭）：等待（`awaiting`：计划或草稿、哪个运行）存进数据集状态；重启的监督进程会读回它，不为它启动 vLLM 或 worker；万一 worker 还是被启动了，它只读存储就能发现计划未批准或草稿未提交，不需要模型。人批准、提交或拒绝、取消了运行，或草稿不在了，等待就结束。只有在运行真要执行之前才要求模型在线。
- **期间有人提交了该数据集**：计划的基线过期；worker 取消该运行并重新规划。
- **模型服务失败**：运行阻塞，worker 退出，监督进程按退避重试（30 秒起翻倍到 10 分钟）；失败的片段消耗一次尝试（`max_attempts`）。vLLM 自己起不来的情况见上文（退避，然后 `levi live resume`）。
- **永远完不成的片段**（合成失败留下的 raw 采集、客户端写到一半死了）在 `watch.stuck_s` 之后变为 `stuck`：计数、由 `levi live doctor` 列出，只有它再变化才会重新检查。
- **镜像之后源被替换**（片段删掉后以同一编号重写，或标记之后 `metadata.json` 被替换）：标 `source_changed`；还没标注的会重新镜像；已标注的保留标记（标注的是旧内容）。
- **`levi live doctor`** 打印服务各进程的 RSS/线程/nice、GPU 占用、vLLM 状态、磁盘和运行缓存大小、队列、FR3 状态、警告（监督进程超预算、主循环 5 分钟没有 tick、进程不在 nice 19、vLLM 开着却没事做（闲置超过 `idle_timeout_s` + 120 秒；预热的、还在载入的、刚被唤醒的都不报）、vLLM 起不来、孤儿 vLLM、磁盘不足、复制而非链接、stuck 片段、被替换的源、状态文件过期、某个服务进程占用的打开文件数超过上限的一半：文件描述符泄漏）和提示（vLLM 冷启动 45–70 秒）。

## 实测

假模型服务、不含 vLLM、本机（32 线程，未使用 RTX 5090），服务以 `--daemon --no-ui` 启动（监督进程 + 核心 API；页面本身需要生产构建，未运行），没有任务，稳定 20 秒后测 10 分钟：

| 进程 | CPU（10 分钟） | 常驻内存 | 线程 | 优先级 |
| --- | --- | --- | --- | --- |
| 监督进程 | 0.33 秒 = 单核的 **0.055 %** | **26 MiB** | 1 | nice 19 |
| 核心（`levi live` 启动的 API 进程） | 0.14 秒 = 0.023 % | 177 MiB | 8 | nice 19 |

批次运行时（4 个片段，假模型）：监督进程 29 MiB / 2 线程；worker 进程峰值约 215 MiB、44 线程，建视图 148 MiB（8 线程）外加一个 134 MiB 的池进程，ffmpeg 流拷贝每个一个线程；全部 nice 19。worker 只在有批次时存在。2 个片段的批次用假模型端到端约 8 秒，真实耗时由模型决定（见 GPU 实测）。`levi live doctor` 对运行中的服务报告同样的数字，监督进程超过 150 MiB 或 12 线程时告警。


## 局限

- 释放复核规格和通用标注指南**没有在任何任务上评估过**，准确率未知。
- 数据集的视图在加入片段时重建（LEVI 的原始采集路径）：是流拷贝，几十个片段几秒，随数据集增长。
- timeshare 的标注依赖客户端报告状态：两集之间模型有 `reset_wait_s` 减 `lead_s` 秒，且要先过 6–9 秒的固定延迟（见“两集之间有多少时间”），5 秒的复位等待留不出这个窗口；长会话的批次在会话结束后标完。
- 不使用 `inotify`；低频轮询足够，并且在任何文件系统上都可用。
- **已结束的会话会为之后的客户端“作证”。** 在策略服务器出现之后才结束的 finished/stopped 会话让闸门保持开着（此时服务器空闲）。之后若有不写会话文件的客户端（旧版或 `--no-record`）用同一个服务器，服务注意不到：它推理时闸门仍是开的。写会话文件的客户端不受影响。
- **候选的锚定规格在所有数据集上都被训练清单默认跳过，不只实时数据集。** `levi/training_manifest.py` 的 `_anchored` 默认忽略规格为候选的锚定复核；随 LEVI 提供的 `plates-release-3`（以及评测里用的 screws 锚定规格）也是候选，所以非实时数据集的训练清单默认也不再使用它们的判定；指定运行（`--anchored-run`）或加 `--allow-candidate-anchored`（见 TRAINING_MANIFEST.md）。
- **预热从服务启动那天起就持有 GPU 锁和约 2.2 GB（睡眠时），不管有没有评测**（`levi live stop` 释放）；按工作区规则用 `flock -w 14400` 等锁的其他作业会一直等到服务停止。用预热的话请写进 `COORDINATION.md`。
- **评测期间睡眠的 vLLM 仍占着 GPU 锁和约 2.2 GB。** 按工作区规则用 `flock -w 14400` 等这把锁的其他 agent 最多会等 4 小时。`levi live stop` 可以释放（停掉本服务启动的 vLLM 并放锁）；评测进行时不会自动释放，这是有意的。
- **工作区守卫只拒绝它认得出的。** `levi live` 总是拒绝它所在检出的 `.state`、（git worktree 时）主检出的 `.state`，以及 `LEVI_WORKSPACE` 指向的工作区（除非那已经是实时工作区），不管有没有 `--adopt-workspace`。其他有 LEVI 状态却没有实时标记的工作区需要 `--adopt-workspace`，之后就被接受：用别的 `LEVI_WORKSPACE` 在别处启动的产品 LEVI 认不出来。不要把实时服务指向有人在用的工作区。
- **产品 LEVI 重启后的第一次池扫描更慢**（合并本分支之后）：池扫描器的缓存版本（`FACTS_VERSION`）变了，缓存失效，每个视频的 `video_sha256` 要重算一次。
- **锚定记录按路径排序后的片段序号存储。** 编号小的片段很晚才完成，或 `demo_NNNN` 编号被复用，会让之后的片段在下一个视图里序号移位。评测客户端按顺序写片段，所以只会发生在续跑或孤儿恢复之后；文件变了的源会标 `source_changed`。

## 状态与已知问题（2026-10-01）

没有合并进 `main` 的分支：`feat/live-eval`（服务）、`fix/live-review`（审查修复和下面这些项）、`feat/live-ui`（`/live` 页面）。按这个顺序合并；合并会改产品运行代码，之后产品 LEVI 要重启（且只在没有作业运行时）。

假件（无 GPU）验证过：镜像、控制器、自动批准、闸门（含提前量和 1 秒内取消）、两种启动顺序下的 vLLM 预算规划、退避与 `resume`、监督进程崩溃后的 GPU 锁、等人、stuck 与被替换的片段、资源占用。用真实 Qwen3.8 在 3 个开发集片段上验证过一次，**没有策略服务器在场**：整条管线含 63 秒 vLLM 冷启动共 209 秒；会话变成 `running` 后闸门 0.6–1.6 秒内关闭；结束后 vLLM 被释放。

未解决：

- **vLLM 预算没有由本服务在 GPU 上跑过。** 默认 0.74 和下限 0.725 来自 GPU 空闲、编译缓存热时的两次实测（0.72：KV 缓存 1.73 GiB；0.74：2.36 GiB；`live-validation.md` 第 5 节）和 KV 公式（49152 token × 39.8 KB = 1.82 GiB）；`plan_budget` 只用假的 `nvidia-smi` 数值测过。“冷编译缓存时非 KV 内存更少（曾经成功的 0.72）”是从两次运行推测的，不是单独的实验；两个 vLLM 启动脚本多出的选项（`--structured-outputs-config`、`--override-generation-config`）也没有分开验证是不是原因。未验证：vLLM 先以 0.74 起、之后再起 .22 的策略服务器（余量约 0.6 GB），以及真实显卡上的上下文降级。要在两种启动顺序下各跑一次；`vllm.gpu_memory_utilization` 和 `min_utilization_*` 是要调的数。
- **唤醒余量（850 MiB）是按一次真实测量算出来的，standby 等待（20 秒）是取舍。** 在测过的策略 `.22` 情形里（睡眠时空闲 22768 MiB，唤醒用了约 21758 MiB），比放行线还多约 375 MiB；standby 等待降低冷启动与第一集重叠的可能，不能消除。
- **自动批准关闭（`auto_approve = false`）时，每道人工关仍可能让 vLLM 唤醒或冷启动一次**：监督进程只在 vLLM ready 时才派 worker，人提交草稿之后 worker 要规划释放复核，需要模型。有 `--prewarm` 时只是唤醒（便宜）；没有、且在评测期间，`evaluation_active` 把它推迟到评测之后。
- **评测客户端不读 `loop_at`**：主循环卡住而心跳线程让 `updated_at` 保持新鲜时，只有 `levi live doctor` 能发现。
- **闸门关闭时，任务控制台的“推进”整个被拒**，尽管它也做不发请求的步骤（刷新等待状态）；逐请求的检查本来就够了。影响很小。
- **主体名字不是凭证。** worker 的主体 `live-planner`/`live-auto` 在“运行/继续”的检查里按名字豁免；逐请求的检查改用 worker 的环境变量。叫这个名字的人能启动运行（它的请求仍会在下一次请求时停下）。
- **冷启动对策略的影响没测过。** 服务在会话 `running`、`homing`、`waiting_reset` 时从不冷启动 vLLM；想在评测前就绪用 `--prewarm`。
- **守护模式下的共存、有真策略服务器时的睡眠与唤醒、空闲释放**没有用真实模型验证。
- **通用释放复核规格和通用标注指南没有评估过**（只有 3 个开发集片段，其中 1 个假成功）。所有判定都当作自动、未审。与任务专用规格在开发集上的对比没有跑。
- 训练清单默认跳过候选的锚定复核，但自动时间片段没有任何验证；用 `subtask_review` / `annotation.subtask_auto_share` 自行决定。
- 释放复核运行一旦被归档（比 `keep_review_runs` 更旧），任何人都不能再提交它；它的判定仍在数据集状态和锚定记录里。
- 没有 `--auto-approve` 时，每个批次需要人通过三道关（时间片段计划、草稿、复核计划）。
- 其他局限见上文“局限”。
