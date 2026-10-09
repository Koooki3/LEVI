# 实时标注服务

`levi live` 是一个后台常驻的 LEVI：评测还在写 rollout 时，它就开始标注。它监视 rollout 目录，把**已完成**的片段拿进自己的工作区，用本地模型（vLLM 上的 Qwen3.8）标出时间片段和各时间片段的成败，并判断整个片段是否成功。人可以在实时页面看进度，在普通 LEVI 查看器里审核结果。[English](LIVE.md)

它是一个**独立的 LEVI 实例**（自己的工作区，核心端口 7881），长时间运行的工作不和你日常用的 LEVI（7860/7861）共用进程。**查看它就在日常用的 LEVI 里**：那里的 `/live` 页面和导航入口读取实时工作区的文件（见[在产品 LEVI 里查看实时评测](#在产品-levi-里查看实时评测)）；只有加 `--ui` 时，服务才另起自己的页面（端口 7880）。设计目标是不妨碍机器人：自己降低优先级、限制线程、空闲时几乎零开销，只在 GPU 策略允许时才启动 GPU 模型。

它**不会**：写 rollout 目录、连接机器人或策略服务器端口、写人工成败标签、参与 gold 制作。它给出的判定都是自动的、未经审核的，并在所有地方这样标注。

## 快速开始

**顺序要紧：先带 `--prewarm` 启动实时服务，再起策略服务器，最后开评测。** 这样 vLLM 在显卡还空着时加载（冷启动是 45–70 秒的重 GPU 负载，对策略推理延迟的影响没测过，所以会话处于 running、homing 或等待复位时服务从不冷启动），没活时睡眠；策略服务器（`.22` = 7.6 GB，推理延迟和以前相同，约 59 ms）随后在它旁边加载：

```bash
# 0. 每台机器做一次：vLLM 环境和权重（docs/VLLM.zh-CN.md），再写一个写明本机 rollout 目录的
#    live.toml（没有任何默认值指向它）。
uv run levi live init --root /path/to/rollouts   # 写出 ~/.levi-live/workspace/live.toml
uv run levi live doctor                          # 脚本、pid 目录、GPU 锁、根目录：缺什么

# 1. 实时服务（启动一次，一直运行）。--prewarm 让 vLLM 现在就起来。
cd /path/to/LEVI && uv run levi live start --daemon --auto-approve --prewarm
uv run levi live status        # 等到 vLLM 显示 ready（或 asleep）：约一分钟
# 在产品 LEVI 里查看：http://127.0.0.1:7860/live

# 2. 策略服务器，MEM_FRACTION 用 .22（vLLM 常驻时不要用 .25 或 .35）
cd /path/to/openpi && XLA_PYTHON_CLIENT_MEM_FRACTION=.22 uv run scripts/serve_policy.py --port 8000 policy:checkpoint --policy.config pi05_fr3_all_state --policy.dir checkpoints/pi05_fr3_all_step49999

# 3. 评测客户端；“是否启用后台 LEVI 标注？”回答是。
```

如果策略服务器先起，`--prewarm` 起不了 vLLM（没有会话为这个服务器作证，闸门是关的；状态里写 `prewarm_waiting_for_policy`），冷启动要等客户端写出第一个会话文件、且它进入 `standby` 至少 20 秒（`gpu.standby_min_s`）之后才开始。这个等待只降低与第一集重叠的可能，不能消除。不加 `--prewarm` 时，工作在 `standby` 到来也是同样情形。

其他命令：

```bash
uv run levi live status                          # 它在做什么
uv run levi live doctor                          # CPU、内存、GPU、磁盘、警告
uv run levi live stop                            # 只停自己的进程
```

`levi live once [--fake-vlm]` 标完当前已完成的片段就退出（加 `--fake-vlm` 时使用假的模型服务，不需要 GPU）。`levi live init` 把所有默认值写成 `<工作区>/live.toml`。`levi live report` 把某个数据集或会话的统计打印成报告，`levi live stats backfill` 为统计功能出现之前标注的片段补记录（见“统计与报告”）。`levi live start` 在启动核心之前，把它校验过的配置（文件、`--config`、命令行覆盖）写到 `<工作区>/live/effective.toml`，所以核心读到的是本次会话的设置，而不是上一次的。`levi live resume` 清除服务放弃了的 vLLM 启动失败。`once --fake-vlm` 必须指定临时的 `--workspace`：默认工作区、`LEVI_LIVE_WORKSPACE`、上一次 `start` 用的工作区以及任何 `start` 运行过的工作区都会被拒绝；没有 `--home` 或 `LEVI_LIVE_HOME` 时它用一个临时 home（临时目录下的 `levi-live-fake-home-*`，命令结束时删除），不碰真实服务的状态文件和锁。

不加 `--auto-approve` 时，服务仍会镜像、建视图并**生成计划**，然后等待：由人在 LEVI 页面批准计划（数据集显示 `awaiting_approval`）。加上它，由下文的“自动批准主体”（有审计）代为通过这些关口。

默认值不指向任何一台机器上的目录。工作区默认 `~/.levi-live/workspace`（状态文件、pid 文件和锁在 `~/.levi-live/`，可用 `--home` 或 `LEVI_LIVE_HOME` 改）；命令没有给 `--workspace`、`LEVI_LIVE_WORKSPACE` 或 `--config` 时，作用于用同一个 home 最近一次 `levi live start` 运行的工作区（记在 `<home>/started.json`；这条记录出现之前启动的服务，通过 `status.json` 找，且要求该工作区有只有 `start` 才写的服务日志），前提是它仍是实时工作区、不是产品工作区。`levi live once` 不会改变它：home 里没有 `started.json` 时，真实的 `once` 在改写状态文件之前，先把状态文件指向的旧服务工作区写进 `started.json`。监视的 rollout 根目录（评测客户端的 `--rollout-root`）**没有默认值**：没有它时 `levi live start` 和 `once` 拒绝运行，并说明怎样设置（`--root`、`[watch] roots`）。启动前服务按本机检查配置（`levi live doctor` 显示同样的检查）：vLLM 脚本存在且可执行、pid 目录可写、GPU 锁文件能打开、根目录存在、没有路径指向别的用户的家目录（从别的机器拷来的 `live.toml`）；会让服务无法工作的问题会中止启动并给出修法，其余是警告。

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
- **核心**（加 `--ui` 时还有页面）：为实时工作区运行 `levi backend`（核心 :7881），由 `levi live start` 启动，worker 需要它。服务自己的页面（`levi serve`，界面 :7880）只在加 `--ui` 时启动（没有生产构建时只启动核心并提示）；`--no-core` 两者都不启动。`--no-ui` 现在就是默认值，仍然接受。实时页面本身在产品 LEVI 里（见[在产品 LEVI 里查看实时评测](#在产品-levi-里查看实时评测)）。

一个数据集对应一个 `<group>/<task_folder>`，目录名是 `<group>__<task_folder>`。**同一个 `<group>/<task_folder>` 出现在两个被监视的根目录下，是两个数据集。** 朴素的名字留给状态里已经记着它的那个根目录，绝不改名、移动或覆盖（它的 `captures/<名字>/` 镜像可能是一次评测做完后的唯一副本）。另一个根目录用 `<group>__<task_folder>__at__<根目录标记>`，标记取该根目录的最后一段目录名（`online_rollout_data/models` 取 `models`），两个根目录的末段相同时再加 `-<根目录路径哈希的 6 位十六进制>`。数据集按根目录保有名字：每个数据集有自己的状态文件 `live/datasets/<名字>.json`、`source`、镜像目录 `captures/<名字>/` 和标注，所以两个数据集里相同的片段编号互不相干，重启后选出的名字也一样。名字最长 140 个字符，字符集 `A-Za-z0-9._-`。数据集**一次处理一个**，等得最久的先做，因为 LEVI 不允许同一数据集的标注基线变了之后还有第二个写入者。

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

`levi live init` 写出带全部默认值的文件；未知键或类型不对是错误，不会悄悄取默认值。`--workspace`、`--root`、`--gpu-mode`、`--auto-approve`、`--prewarm`、`--process-backlog`、`--since`、`--vllm-port`、`--ui-port`、`--core-port`、`--home` 可覆盖文件。`--adopt-workspace`：把已有的、不是实时工作区的 LEVI 工作区改成实时工作区才需要它；没有它时，`levi live` 拒绝任何有 LEVI 状态却没有实时标记的工作区（尤其是产品的 `.state`：自动批准的标记绝不能落进去），本仓库所有检出（产品 LEVI 运行的主检出，以及每个 git worktree）的 `.state`、位于其中的目录、包含它们的目录，以及 `LEVI_WORKSPACE` 指向的工作区（除非它已经是实时工作区），不管有没有 `--adopt-workspace` 都拒绝。受保护的始终是运行 `levi` 的那个检出所属的全部检出，没有任何设置能改变这一点。每个 home 和每个工作区各只运行一个服务。环境变量：`LEVI_LIVE_WORKSPACE`、`LEVI_LIVE_CONFIG`、`LEVI_LIVE_HOME`（`status.json` 所在目录，默认 `~/.levi-live`）。产品 LEVI 也读 `LEVI_LIVE_WORKSPACE` 和 `LEVI_LIVE_HOME`，用来找到它要显示的实时工作区（见[在产品 LEVI 里查看实时评测](#在产品-levi-里查看实时评测)）。`LEVI_LIVE_WORKER=1` 由服务在 worker 进程里设置（不要自己设）：它让 worker 不受“每次模型请求前查闸门”的约束，因为 worker 自己会让路。服务会把它从其他子进程的环境里去掉，所以 shell 里设了它也不会豁免任何东西。`LEVI_LIVE_VLLM_TIMINGS` 同样只由监督进程为它启动的 worker 设置：一个小 JSON，写着这个批次之前 vLLM 唤醒或冷启动花的秒数（`vllm_wake_s`、`vllm_cold_start_s`），worker 把它抄进 `stats.jsonl`；只传最近 10 分钟内的唤醒或冷启动，做完一个批次后就忘掉。服务也会把它从其他进程的环境里去掉。

各表、各键、默认值和含义与英文版表格一致（`service`、`watch`、`fr3`、`gpu`、`vllm`、`provider`、`pipeline`、`resources`），见 [LIVE.md](LIVE.md#settings-livetoml)。要点：

- `gpu.mode` 默认 `auto`：等于 `timeshare`（两者常驻 + 闸门）；`coexist` 和 `manual` 是手动选项。`gpu.busy_states` 默认 `["running"]`；`gpu.min_free_mib` 600、`gpu.policy_budget_mib` 8500 决定 vLLM 何时睡眠。
- `pipeline.temporal` 默认 true（时间片段运行：粗标 + 精修）。设为 false 时不做时间片段，只由释放复核给每个片段判定；这要求 `pipeline.anchored = true`，并且复核规格没有 `episode.require_place`（例如 `generic-release.v3.json` 或 `generic-final.v1.json`），否则配置校验拒绝。见[双标签](#双标签操作员和-agent)。
- `[judge.task_text]`（默认空表）：给判定的问题换一种指令说法，键是任务文字或任务文件夹名，值是判定用的说法；机器人策略收到的指令不动。见[判定用的任务文字](#判定用的任务文字)。
- `pipeline.coarse_step_seconds` 0.5、`pipeline.refine` `always`：评测过的时间片段设置，长片段会放宽步长使帧数不超过 `max_images`。`pipeline.cleanup` true：丢弃已完成运行的冻结输入和证据。
- `pipeline.auto_approve` 默认 **false**。`pipeline.keep_review_runs` 10：每个数据集保留冻结输入的开着的释放复核运行数（提交它需要输入），更旧的被取消并清理。`watch.stuck_s` 600：没有完成、也没有变化的片段超过这个时间算 `stuck`。`gpu.lead_s`/`lead_grace_s` 3/5：闸门在下一集开始前提前关闭。`service.gate_poll_s` 0.25。
- `resources.report_keep` 默认 20：`live/reports/` 里保留的会话报告份数，写入新报告时删除最旧的。
- `pipeline.background` 默认 true。设为 false 时关掉后台标注（不启动 worker，不做时间片段和复核），rollout 照常连同操作员标签和在线结果一起镜像，统计和页面照常可用。`[online]`（`enabled` false、`host` `127.0.0.1`、`port` 7882、`spec` `generic-final.v1.json`、`timeout_s` 15、`max_body_mb` 8）是在线判定的接口。两者见[在线判定](#在线判定接口-c5)。
- `gpu.policy_ports` 默认 `[8000]`，只在内核的 socket 表里查，从不连接。`gpu.policy_loaded_min_mib` 6000、`gpu.policy_load_wait_s` 120：监听着的策略端口只有进程占用到这么多显存才算“策略服务器已加载”（用于预算规划）；占得更少说明还在加载，vLLM 等待（`settling`），端口出现 `policy_load_wait_s` 秒后按“只有 vLLM”规划；读不到显存同样按“只有 vLLM”的保守预算。`gpu.standby_min_s` 20：冷启动要等会话在 `standby` 待满这么久（它的第一集几秒内就会开始）；这是缓解，不是保证，评测前用 `--prewarm`。`gpu.wake_margin_mib` 850：唤醒时在预算（减去睡眠中的 vLLM 仍占的部分）之外保留的空闲显存；启动用 `vllm.margin_mib`。**在真 GPU 上测过**（策略服务器 `.22`）：睡眠的 vLLM 旁空闲 22768 MiB，唤醒并做完第一批请求用了约 21758 MiB。原来的 300 会在空闲 21843 MiB 时放行唤醒，唤醒后只剩约 85 MiB，低于 `min_free_mib`（600），vLLM 会立刻又被放睡；800 在放行线上仍只剩约 585；850 时唤醒需要 22393，正好在线上也剩 635 MiB，实测的 22768 放行，富余约 375 MiB（按这些数字算出来的，没有观察到真正的来回抖动）。`gpu.blocked_pause_s` 300：GPU 锁被别的 agent 持有、:8100 上有别人的 vLLM、睡眠的 vLLM 因显存不够唤不醒，持续这么久后写进 `labelling_paused`。`gpu.resume_stable_s` 3（0.5–60 秒）、`gpu.resume_max_bounces` 3：人的运行被实时闸门拦住（`blocked`）后，闸门连续开着这么久就自动继续（避开 `episode_imminent` 窗口；用监督进程写在 `gate.json` 里的 `opened_at`，所以两次采样之间的关上又打开也算），每次打开一次；连续被拦这么多次、中间没有进展（完成片段或结算了 token），就留给人点“继续”；0 表示关闭自动恢复。`gpu.unknown_client_pause_s` 600：没有会话为之作证的策略服务器让闸门一直关着，超过这么久状态里 `labelling_paused` 写 `unknown_client`。
- `vllm.prewarm` 默认 false（`levi live start --prewarm`）：服务启动后、没有评测在跑时就把 vLLM 拉起来并保持常驻，空闲只睡眠，服务停止才停。这是**评测期间不冷启动**的办法。
- vLLM 的显存预算和上下文长度在每次启动时按**当时的空闲显存**选择（见下），`gpu_memory_utilization_max/min`、`min_utilization_with_policy/alone`（都是 0.725）、`kv_bytes_per_token`、`min_model_len` 是这个选择的界限（按**热**编译缓存下的实测校准，见下）；`margin_mib` 1100；`max_start_failures` 3、`start_backoff_s` 60、`start_backoff_max_s` 600。
- 路径类设置的默认值：`service.workspace` 为 `~/.levi-live/workspace`；`watch.roots` 无默认值，必须设置；`fr3.health_file` 为空（没有健康监控文件，FR3 状态为 `missing`，doctor 不报警）；`gpu.lock_file` 为空（不与其他 GPU 用户共用锁；需要时指向本机其他 GPU 用户也使用的 `flock` 文件，例如产品 LEVI 的 `LEVI_GPU_LOCK_FILE`）；`vllm.script` 和 `vllm.stop_script` 都是仓库自带的 `scripts/vllm/serve.sh`（相对路径相对 LEVI 检出目录），`vllm.pid_dir` 为 `~/.levi-live/vllm`，约定见 [vLLM](VLLM.zh-CN.md)；`vllm.model` 为 `RedHatAI/Qwen3.8-27B-INT4`（自带脚本服务的模型，`LEVI_VLLM_MODEL`），`vllm.served_model` 为 `qwen3.8-27b`。
- `gpu.lock_unavailable` 默认 `continue`：配置了锁文件却打不开或建不了时，`continue` 不带锁启动 vLLM（状态里 `gpu.lock.state = unavailable`，事件和 `levi live doctor` 都会指出）；`wait` 不启动 vLLM（决定码 `lock_unavailable`）。
- `vllm` 默认是实测的共存配置（端口 8100，`gpu_memory_utilization` 0.74，`max_model_len` 49152，`max_images` 128，`max_num_seqs` 2，`max_num_batched_tokens` 4096，`--enable-sleep-mode`）；`idle_timeout_s` 120 秒无活后睡眠或停止（从 vLLM **就绪**那一刻起算，40–60 秒的载入不算闲置；之后从它最后一次有活起算）。
- 另有四项此前没写进文档：`watch.settle_s` 2：完成标记至少这么旧（秒）才收这个片段，它是“一集结束到第一个模型请求”固定延迟的一部分（见下文“两集之间有多少时间”）。`pipeline.human_recheck_s` 30：关闭 `auto_approve` 时，等待中的计划或草稿每隔这么久查一次是否已有人决定。`pipeline.budget_seconds` 86400：worker 规划的每个运行的墙钟预算（最多 86400），用完则运行变为 `blocked`。`resources.worker_idle_exit_s` 5：配置文件接受这个键，但目前没有任何代码读取它，worker 在批次做完时退出，而不是空闲等待后退出。
- 服务为实时工作区设置 `LEVI_DROID_SAMPLE=off`、`LEVI_GPU_SHARING=allow`（用它自己的策略取代 LEVI 的错峰守卫，见下）、`LEVI_SYNC_DISCOVER=off`、`LEVI_SYNC_INTERVAL=30`。

## GPU 管理

一块 32 GB 的 GPU 与机器人的策略服务器共用。维护者在这块卡上的实测（RTX 5090，openpi π0.5 策略服务器）给出下面的规则；`[gpu]` 和 `[vllm]` 的数值就是这次标定，换卡或换模型要重新测：

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

始终成立：GPU 锁（配置了 `gpu.lock_file` 时，在它上面的 `flock`，`LEVI_AGENT=live`）跟着 vLLM 进程走：vLLM 启动时带着锁的文件描述符，所以监督进程被 `kill -9` 后，只要 vLLM 还活着锁就在；重启的监督进程只有在锁仍被它持有时才接管运行中的 vLLM（否则拒绝并说明）；只有确认 vLLM 真的退出才放锁，`levi live doctor` 会报告孤儿 vLLM 以及如何停止。只停自己启动的服务（按进程身份核对），不碰别人的；不是它启动的 vLLM 只有在 `adopt_external`（或 `manual`）下才使用且从不动它；:5000 和策略端口从不连接；空闲 tick 不碰 :8100。残余风险（见实测报告）：余量只有 1.3–1.5 GB；第一集可能遇到一次 280 ms 的策略离群值；vLLM 清醒时用更大的 `MEM_FRACTION` 重启策略服务器会加载失败，请在 vLLM 唤醒前启动它，或先 `levi live stop`。

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

被评测的任务不是 LEVI 调优用过的任务，所以没有任何任务专用内容。`levi/live/specs/` 里的文件，版本号在文件名里，用过之后不再改（改动=新文件+新设置）：

- `generic-guideline.v1.md`：标注指南。引用 rollout 的任务指令（`task_description.txt`，否则元数据的 `task_description`，否则目录名），逐步按它判断。六个子任务 id 与其他地方一致：`approach grasp transport place retreat other`。
- `generic-definitions.v1.json` / `generic-vocabulary.v1.json`：定义（每个数据集的词表只写一次）。
- `generic-release.v1.json`：锚定复核用的释放复核规格，**状态 `candidate`，未在任何任务上评估**。每次夹爪张开问一个问题：侧视 −2.5…+1.2 s、腕部 −1.5…+0.4 s（plates 复核用的那些帧），引用任务指令：是否夹着物体、物体是否落在指令指定的目的地、是否稳定不滑落。片段在至少 `pipeline.anchored_min_valid`（默认 1）次张开有效时判成功。它分不清需要两次放置的任务和一次放置的任务；这类任务设 `anchored_min_valid = 2`。它的准确率未知：信任判定前要先在开发数据上评估；每个判定都只能读作“自动、未审”。
- `generic-release.v2.json`：同一个释放复核，判定还会看片段怎么结束（最后一次有效释放之后没有再抓，最后一个放置不是失败）；候选，不是默认；不要求任务只放一次（见其局限）。见[终态感知的判定](#终态感知的判定候选)。
- `generic-release.v3.json`：去掉放置条件的第 2 版：至少 `min_valid` 次释放有效、并且最后一次有效释放之后夹爪没有再闭合，就判成功。它不读时间片段，所以能和 `pipeline.temporal = false`（只做释放复核）一起用。候选，不是默认；离线（一个任务，92 个片段）保住了 33 个成功中的 33 个，把 59 个失败中的 5 个判成成功（0.085，Wilson 95 % 0.037–0.184），第 2 版在同样的片段上是 59 个中 1 个。见[双标签](#双标签操作员和-agent)。
- `generic-final.v1.json`：另一种问法，不是释放复核的新版本：每个片段只问一次，看录像最后几秒（指令说的那个物体现在在哪里、是否静止）。它不读夹爪通道和时间片段，所以能和 `pipeline.temporal = false` 一起用。候选，不是默认，**没有在任何数据上评估过**。见[最终状态判定](#最终状态判定候选)。
- `generic-final.v2.json`：第 1 版的问题和规则，加上本实验室对“pick X in/on/into/onto Y”的读法，以及一个**开头检查**：对片段第一帧再问一个问题，机器人动手之前物体是不是已经在目的地；如果是，这个片段不是有效试验（记为失败并标“不确定”）。候选，不是默认，**没有在任何数据上评估过**。`generic-final.v1.json` 保持原样。见[第 2 版：说法和开头检查](#第-2-版说法和开头检查候选)。

时间片段运行使用评测过的配置（粗步 0.5 s、始终精修、lean 提示、`qwen38-27b-vllm-48k-lean` 配置同时两个请求），只用侧视相机；长片段会放宽步长，使帧数不超过模型的图像上限（LEVI 拒绝悄悄稀疏化）。

## 判定用的任务文字

机器人策略是按指令的原话训练的，所以这条文字从不改动。判定是另一回事：它的问题引用任务指令，让模型自己弄清机器人该拿起什么、最后应该放到哪里，而模型对同一句话的读法可能和实验室不同。在本实验室，“pick the eggplant on the bread”的意思是把茄子拿起来放到面包上；有一晚模型把它读成了拿起面包上的那个茄子。`live.toml` 的 `[judge.task_text]` 给这类指令另一种说法，只给判定用：

```toml
[judge.task_text]
"pick the eggplant on the bread" = "put the eggplant onto the bread"
```

- **键。** 任务文字或任务文件夹的名字，比较时转小写、下划线当空格、多个空格并成一个（和训练池同一条规则）：`Pick_the_eggplant_on_the_bread` 和 `pick the eggplant on the bread` 是同一个键。配置会拒绝：规范化之后相同的两个键、空的键、空的说法、超过 600 个字符或多行的说法。
- **优先级。** 先试任务文件夹的名字（文件夹更具体：同一条指令在别的部署里可能需要另一种说法），再试指令的文字；第一个匹配的生效。说法不会再被查一次，所以条目不会串联；一个条目写的和指令原文一样时，查找到此为止（不算改写）。没有匹配，或者没有这张表（默认）：照片段记录的指令原样引用。
- **作用范围。** 实时服务所有引用任务的问题：[在线判定](#在线判定接口-c5)（服务端用请求的 `task`，以及客户端带来的 `episode.task_folder` 去匹配；客户端照旧发策略自己的文字）、后台复核（释放复核或最终状态的问题，包括开头检查的问题）、时间片段标注（引用指令的标注指南）。它不作用于机器人策略、rollout 的文件、数据集的 `task_text`（页面和 API 显示的仍是记录的原指令）和训练池。
- **记录。** 用了某个说法做出的判定带 `task_rewritten`（`task_text` 或 `task_folder`，哪个键匹配就写哪个；没有则不写）；统计记录里的 `result.task_rewritten`（指令照原样引用时为 `null`）；在线结果及其日志行里也有 `task_rewritten`。具体说法写在 `live/effective.toml`（会话实际使用的配置）和运行冻结下来的问题里；在线日志不存任务文字。
- **先有通用约定，表只给例外。** `generic-final.v2.json` 在两个问题里都写明了本实验室怎么读“pick X in/on/into/onto Y”（见[下面](#第-2-版说法和开头检查候选)），所以条目只用于这句话解决不了的指令。其他规格没有改动（用过的规格文件从不原地修改）。
- **局限。** 一个说法是对模型怎么读这几个词的猜测。它是否改善判定，要拿操作员标签去对（`stats.agreement`），这里没有测过。表在服务启动时读取（`live.toml` 在启动时读），数据集状态里已有的判定不会重算。

## 终态感知的判定（候选）

默认判定只要有一次有效释放就判片段成功。策略放好物体后又把它抓走，或者在桌面上方松手后再次抓住，也会被判成功。`generic-release.v2.json` 是同一个释放复核，只是判定会看片段怎么结束。**它是候选：不是默认，不会自动切换，什么时候成为默认由人决定。**

**规则。** 同时满足三条才判成功：

1. 有效释放至少 `min_valid` 次（和以前一样，现在是必要条件）；
2. 最后一次有效释放之后夹爪没有再闭合（`episode.rule = "last_valid_not_regrasped"`：物体没有被重新抓起）；
3. 服务为该片段提交的时间片段里，起点最晚的 `place` 时间片段，结局既不是失败也不是未知（`episode.require_place = true`；没有 `place` 时间片段则不满足）。结局是未知时判定为**未定**：记为失败，界面显示“未定”。审核过的变更集里被人拒绝的 `place` 时间片段不算。

规则里没有任务用词，只用复核的回答、夹爪通道和时间片段的结局。用哪条规则是规格的字段（`episode.rule`：默认 `any_valid`，或 `last_valid_not_regrasped`；`episode.require_place`，默认 false），每个规格自己选；其他规格（`generic-release.v1.json`、plates 和 screws 的规格）的判定和以前完全一致，记录也逐字节不变。

**怎么启用。** 只能通过配置；默认仍是 `generic-release.v1.json`：

```toml
[pipeline]
anchored_spec = "generic-release.v2.json"
```

对服务重启之后规划的批次生效；数据集状态里已有的判定不重算。`anchored_min_valid` 仍然设置需要的有效释放次数。

**各部分在哪里运行。** 复核运行（`levi/agent/anchored.py`）知道夹爪：它把夹爪闭合的帧记进 `anchored` 记录的 `closes`（只有规则要读它的规格才写），它的结果应用第 1、2 条（`basis`：`rule`、`last_valid_frame`、`closes_after_last_valid`、`require_place`）。时间片段是同一批里的另一个步骤，先于复核提交；实时 worker（`levi/live/judge.py`）读取它们并应用第 3 条，在判定里加上 `place_outcome`（`success`、`failure`、`unknown`；`none` 表示没有 place 时间片段；`missing` 表示没有可读的时间片段），并把 `place` 从 `missing_inputs` 里去掉。判定还记录 `spec_version`（版本记录之前的判定为 null，所以同一个规格 id 的版本 1 和版本 2 的判定可以区分），规则下还有 `min_valid`；统计记录带 `spec.release_review_version`。所以复核记录和人在 LEVI 页面里可以接受的、来自复核运行的成败建议只含第 1、2 条；数据集状态和实时页面里的判定含全部三条。为了不让复核记录被当成确定的成功，带 `require_place` 的规格在实时 worker 应用该条件之前，让 `basis.missing_inputs` 写上 `place`：这条记录里的成功在单独读它的任何地方都是**未定**（`anchored.undecided`；训练清单的 `anchored_undecided`，除非要求包含未定的成功，否则 `verified_success` 会把它排除），建议的 `uncertainty` 写明以实时判定为准。

**这对训练清单和人工标签意味着什么。** 训练清单读的是复核记录，不是实时判定，所以最后一个放置的条件到不了它。另外，如果有人在 LEVI 页面接受复核运行的成败建议，那就是人工标签，训练清单把它排在任何自动判定之前：它会覆盖实时判定，放置条件也一起被覆盖。用版本 2 产生训练数据前必须知道这一点：训练用的标签取自人，或者只接受实时判定与之一致的建议。判定的 `rule`、`place_outcome`、`closes_after_last_valid`（以及 `basis`）出现在数据集状态和 `GET /datasets/{name}` 里，其中 `rule` 和 `place_outcome` 也写进统计记录；默认规则下的判定没有这几项。实时页面在“(有效/事件)”后写明原因：夹爪又闭合、最后一个放置是失败、未定或不存在，或者放置没法核对。

**缺少输入从不当作成功。** 该片段没有已提交的时间片段（时间片段步骤失败）时，判定与放置结局为未知时一样：记为失败、未定，`place_outcome` 为 `missing`，`basis.missing_inputs` 写明 `place`。统计把它算作失败（不算成功），同时单独算作未定。没有 `closes` 的复核记录（在记录闭合帧之前做的）退回第 1 条，成功只能是未定的成功（`missing_inputs` 写明 `closes`）。

**证据和局限。** 规则是在一个任务上离线选出的（把物体放进盘子，一台机器人，一个模型，两个策略共 91 个计分片段，由 agent 看视频核对，人没有确认）：默认规则在 61 个失败片段里有 12 个假成功；这条规则 0 个，30 个成功一个也没漏。这是小样本、单任务，不是测得的准确率；假成功率的 95% 区间上限约 0.06。它纠正的一部分来自第二个信号盖住了复核的错误回答（物体落在目标外却答“落在目标”）；改进复核的提问和取帧是另一项改动。之后又在两个多次放置的任务上检查了误判成失败（见下）。要试用，在 `live.toml` 的 `[pipeline]` 下设 `anchored_spec = "generic-release.v2.json"`（见上面的“怎么启用”）；默认仍是版本 1。已知局限：

- **不限于只放一次的任务。** 第 2 条只看**最后一次有效**释放之后发生了什么；放置之间还要再抓的任务（叠多个盘子、往盒子里放多颗螺丝），在最后一次放置之后没有闭合。离线在这些任务上检查，真成功一个也没被这条规则判成失败：叠盘子 0/3（14 个片段）、螺丝入盒 0/6（11 个片段）、茄子 0/30，合计 0/39（95% 区间上限约 0.09）。叠盘子这组是挑出来的困难和有分歧的片段，螺丝的金标准不是盲的，标签由 agent 给定、人没有逐条确认，所以这只是方向性证据，不是准确率。
- **误判成失败有两种途径**，这 39 个成功片段里一个也没出现。(a) 复核漏判了最后一次真放置（答无效或不确定）：最后一次有效释放退回到更早的一次，而多次放置的任务在更早的那次之后一定有闭合（去抓下一个物体）。只放一次的任务漏掉唯一的放置，不用这条规则本来也判失败。一个把录像最后 1.2 秒内所有释放都答“不确定”的复核变体，会让这 9 个多次放置的成功片段里的 4 个变成未定；实际使用的复核没有这样做。(b) 最后一次放置之后，策略又闭合夹爪（例如回到零位）。
- **多次放置任务更大的风险是假成功，而这条规则几乎拦不住。** 它既不数放了几次，也不分是哪个物体，所以拦不住做了一半就停手的片段、因错误回答把一次张开当成“有效”的片段，也拦不住同一个物体放两次的片段（叠盘子：11 个失败里仍有 3 个判成功；螺丝：5 个里有 1 个）。`episode.min_valid`（默认 1，`anchored_min_valid` 可设）是任务级的有效释放次数下界，不等于某个场景实际需要的次数：它在螺丝组去掉了 1 个假成功，在叠盘子组一个也没去掉；需要的次数还随场景变（螺丝颗数、每种颜色的盘子数）。只在任务里每个片段至少都需要这么多次放置时才用它。
- 放好之后策略又碰物体或闭合夹爪，会被判失败；评估过的成功片段里没有这种情况。
- 依赖时间片段的 `place` 结局，那是模型的第二次读图；换模型、相机或任务后，两个信号不一定还在不同的片段上出错。
- 复核回答为未知时按“无效”处理（离线评估把它们当不确定输入；评估过的片段里没有这种情况）。
- 夹爪通道按二值命令（开/合）读取，本机的 `robot_capture` 数据就是这样记录的。通道是测得宽度的数据集上，闭合帧来自宽度范围的穿越，会滞后于命令；夹着较宽的物体时，宽度可能一直高于闭合阈值，重新抓起可能检测不到。

## 最终状态判定（候选）

`generic-final.v1.json`（id `generic-final`，版本 1）按片段**怎么结束**来判定，不按夹爪张开的次数。释放规格（`generic-release.v1` 到 `v3`）在每次张开时问“夹着的物体有没有落到指令说的地方”，判定由这些张开推出（v3 再要求之后夹爪没有重新合上）。它们从不看最终画面，所以没人检查最后一次放置是否成功；片段在夹爪张开之前就结束时，也没有张开可判。最终状态规格反过来问，每个片段只问一次。

**问什么。** 锚点是片段的最后一帧（`anchor.event = end`）。模型看到侧视相机在结尾前 3.0、2.0、1.2、0.6、0.2 和 0 s，腕部相机在结尾前 2.0、1.0、0.4 和 0 s（共 10 张图），问题里引用任务指令，答两个字段：

- `object_state`：`resting_at_destination`（落在指令说的目的地上或里面，由目的地托着；手指可以还在它周围）、`held_over_destination`（在目的地上方，但仍挂在手指上）、`elsewhere`（桌面、别的物体、错的容器，或已经掉落）、`in_gripper`（挂在手指上，不在目的地）、`unclear`；
- `stable`：`yes`、`no`（滑动、滚动、倾倒、掉落）或 `unclear`。

措辞里没有任何任务专用的词：目的地就是指令说的地方。片段比 3 s 窗口短时，落在片段开头之前的偏移用第一帧代替（只有一帧的片段，每张图都是这一帧）；偏移会被夹到有效范围，不会报错。

**规则**（`episode.rule = "final_state"`）。成功：物体静止地放在目的地上，且稳定。失败：任何明确的其他答案（被夹着、在夹爪里、在别处、不稳定）。**不确定**：有 `unclear` 答案，且没有明确的失败答案压过它。“不确定”沿用框架已有的含义：判定记为失败并标“不确定”，统计里算作失败、另外再算一次“不确定”，不会记为成功。所以 `elsewhere` 加 `stable = unclear` 是确定的失败，`resting_at_destination` 加 `stable = unclear` 是不确定的失败。规则只读模型的这两个答案：不读夹爪通道、不读闭合帧、不读时间片段、不读 `eval.*` 元数据、不读操作员标签。判定只有一个事件：`events` 为 1，成功时 `valid_events` 为 1、否则为 0，另有 `rule` 为 `final_state`、`min_valid` 为 1 和 `basis.final_reading`。和所有实时判定一样，它是自动、未审的，不写 `annotations/outcomes`，也不计入任何成功率。配合它时 `pipeline.anchored_min_valid` 必须保持 1（每个片段只问一次，更大的值永远满足不了），配置校验拒绝更大的值。

**怎么启用**（设置在服务启动时读取，所以要重启实时服务；产品 LEVI 不受影响）：

```toml
[pipeline]
temporal = false
anchored_spec = "generic-final.v1.json"
```

`temporal = true` 时它也能跑，但时间片段白白占用模型时间，这个规格用不到它们。数据集状态里已有的判定不会重算。

**什么时候用。** 评测客户端在操作员判定后立刻结束片段、录像因此比步数预算短、可能在夹爪张开之前就结束时：这种片段没有张开可看，释放复核会把它判成失败。它也是目前唯一看最终画面的实时规格。信任它的判定之前，先拿它和操作员标签的一致情况对比（见[双标签](#双标签操作员和-agent)），并且把每个判定都读作“自动、未审”。

**已知局限。**

- **没有在任何数据上测过准确率。** 问法和规则都没有检验过；释放规格的离线数字不能搬过来。
- 它只看最后几秒。片段早先的错误放置、第二个出错的物体、或者半途停下但第一个物体已经到位，都看不到；指令需要几个物体、什么顺序，也不检查。对多物体的任务，它只能判结束时的场景。
- 操作员在夹爪张开之前就判定：物体还挂在手指上时读作 `held_over_destination`，片段判失败，即使操作员的意思是“这次会成功”。物体已经落在目的地上、手指还在周围，算静止。模型分不分得清这两种，没有测过。
- 结尾时两个相机都看不到目的地，读作 `unclear`，判定为不确定。
- 录像最后几帧可能是策略在复位或退出；问题问的是物体在哪里，不是手臂在做什么，但退出的手臂可能在侧视里挡住物体。

### 第 2 版：说法和开头检查（候选）

`generic-final.v2.json`（id `generic-final`，版本 2）是在第 1 版上做两处改动。`generic-final.v1.json` 保持原样、仍可使用；不会有任何东西自动切到第 2 版（默认的 `pipeline.anchored_spec` 和 `online.spec` 没变）。它是候选，**没有在任何数据上评估过**。

1. **说法。** 两个问题里都写明本实验室怎么读形如“pick X in/on/into/onto Y”的指令：把 X 拿起来放进/放到 Y 上，Y 是目的地；“pick X from Y”或“pick up X”表示物体最后被拿着或在别处。第 1 版把这件事留给模型。
2. **开头检查。** 对片段的**第一帧**（侧视和腕部两个相机，两张图）每个片段再问一个问题，只答一个字段 `start_state`：`already_at_destination`（指令说的那个物体在机器人动手之前已经在目的地上或里面）、`not_at_destination`、`unclear`。原因：2026-10-08 晚上五个失败片段里有四个，片段一开始物体就已经在目的地了（场景没复位，机器人几乎没动），而只看最后几秒的判定会把这样的片段判成功。

规则（规格的 `start.void_when`，读开头检查的答案；最终画面的问题、字段和规则就是第 1 版的）：

| `start_state` | 判定 |
| --- | --- |
| `not_at_destination` | 按第 1 版的规则读最终画面（`basis.start_check` 为 `passed`） |
| `already_at_destination` | **不是有效试验**：失败并且不确定，`basis.final_reading` 为 `already_satisfied_at_start`（`start_check` 为 `voided`），不管最终画面怎么说；最终画面自己的读数保存在 `basis.end_reading` |
| `unclear` | 最终状态判成功的，改记为失败并且不确定（`final_reading` 为 `start_unclear`，`start_check` 为 `unclear`）；明确的失败仍是确定的失败。理由是框架一贯的规则：读不出来的输入绝不当成成功（读不出最终状态时同样记为不确定） |
| 没问（调用方发不出第一帧） | 第 1 版的规则，`start_check` 为 `skipped` |

开头被判无效或读不出时，`valid_events` 记 0。页面和统计里是“不确定的失败”；审核队列里的提案写“第一帧里物体已经在目的地：不是有效试验”（原文为英文），并引用第一帧。`start.void_when` 是框架功能，不是任务规则：它是对开头检查答案的一组条件，只允许和 `final_state` 规则一起用（见 [Anchored review](ANCHORED_REVIEW.md)）。

后台复核里，worker 自己从镜像进来的视频里取第一帧（开头检查的视图是 `at: start`），不需要客户端：先问开头检查，再问最终问题，每个片段两次请求（计划的估算已经把它们算上）。在线判定则要由客户端发第一帧：见[接口 C5](#接口-c5)。

```toml
[pipeline]
temporal = false
anchored_spec = "generic-final.v2.json"

[online]
spec = "generic-final.v2.json"   # 可选：在线判定
```

**已知局限。**

- **没有在任何数据上测过准确率**，模型把第一帧答成 `already_at_destination` 或 `unclear` 的频率也不知道。开头检查做的一切都建立在对一帧（两张图）的这一次读数上；误答 `already_at_destination` 会把真正的成功变成不确定的失败，漏掉则保持第 1 版的行为。
- 开头只用一帧判断：物体原本在位、被移开又放回去，看不出来；手臂挡住的第一帧读作 `unclear`。
- 没有目的地的指令（“pick up X”）：开头检查的措辞让它答 `not_at_destination`；最终问题对这类指令的局限和第 1 版相同。
- 第 1 版对最终画面的局限都还在，每个片段还多一次请求。

## 双标签：操作员和 agent

双标签评测给每个片段两个标签，并且互不混用：**操作员标签（真值）**是操作员自己判定的成功或失败；**agent 标签（自动、未审）**是释放复核的判定。在新片段上比较两者，可以测出自动标签有多可信。

**流程。** 评测客户端（策略仓库 FR3 示例里的 `--levi-mode dual`）默认允许操作员在运行中判定：按 `s` 成功、`f` 失败或 `d` 作废，这一集立即结束（`--no-dual-early-key` 恢复旧行为：每个片段跑满步数预算，和无人值守运行一样，运行中 `s`/`f` 无效；两种情况下 `q` 都结束本轮）。没有被按键结束的片段跑满预算，之后客户端再问操作员打标签。客户端把过程写进 rollout 元数据：`eval.ended_by`（`operator_key` 或 `budget`）、`eval.operator_label_timing`（`during_run` 或 `after_budget`）、`eval.operator_labelled_step`。机械臂回零位，操作员复位场景，按 Enter 开始下一个片段。标签在 rollout 被标为完成之前写进它的 `metadata.json`，所以 LEVI 不会在操作员标签出现之前判定这个片段。之后 LEVI 在后台判定同一个片段。操作员判定之前，评测终端不显示 agent 的判定；启用[在线判定](#在线判定接口-c5)时，客户端在操作员判定之后显示一行结果。操作员给一个片段打标签之前，不要先去 `/live` 看它。

**服务设置**（`live.toml`；改完要重启服务，因为 `live/effective.toml` 在启动时写出）：

```toml
[pipeline]
auto_approve = true
temporal = false
anchored = true
anchored_spec = "generic-release.v3.json"
anchored_min_valid = 1

[vllm]
prewarm = true
```

`temporal = false` 跳过时间片段：一个片段的模型时间约 80 % 花在时间片段上，而一次释放复核请求约为每次张开 2 秒。配置校验拒绝没有释放复核（`anchored = false`）的 `temporal = false`，也拒绝要读放置时间片段的规格（`episode.require_place`，即第 2 版）。`generic-final.v1.json` 是另一个能这样运行的规格（见[最终状态判定](#最终状态判定候选)）。默认仍是 `temporal = true` 加 `generic-release.v1.json`。

**启动顺序。** 1. `levi live start --prewarm`，等 `levi live status` 显示 vLLM `ready`（评测期间不允许冷启动模型，冷着启动的服务要等本轮结束才开始标注）。2. 策略服务器。3. 带 `--levi-mode dual` 的客户端。客户端连不上服务时会用红字说明，并退回手动运行（`s`/`f`），没有 agent 标签。

**两个标签各在哪里。**

| | 操作员标签（真值） | agent 标签（自动、未审） |
| --- | --- | --- |
| 谁写 | 评测客户端，在按键时 | 实时 worker，在释放复核之后；或者[在线判定](#在线判定接口-c5)，由客户端转写（`eval.agent_label`） |
| 来源 | rollout 的 `metadata.json`：`eval.operator_outcome`（之后不再改）、`eval.outcome`、`eval.verdict_by = "operator"`、`eval.label_mode = "dual"` | 锚定复核记录（`anchored.get`） |
| 数据集状态里 | `demos[demo].operator_label` = `{outcome, by, source}`（被拒收的片段也有） | `demos[demo].verdict` |
| `live/stats.jsonl` 里 | `operator_label` = `{outcome, by}` | `result.verdict` |
| 页面上 | 判定前面一个实线框的“操作员”标记 | 虚线框的“auto”标记 |

`criteria.operator_label` 这样读操作员标签：双标签运行的 `eval.operator_outcome`；否则是操作员按键判定的成功或失败（`verdict_by` 为 `key` 或 `timeout-adjudicated`）；否则照原样记 `unlabeled` 或 `discarded`（没有成功或失败）。它从不读 `success_flag_final`。会话文件写 `levi.mode = "dual_label"`；页面给这种会话标“双标签”，不显示倒计时：操作员打标签时（客户端的原因写 `operator labelling episode N`）写“操作员正在给这个片段打标签”，等 Enter 时写“由操作员按 Enter 开始下一个片段”。最近一个片段只在成功或失败时带“操作员标签”字样；作废的片段写“已作废”。操作员复位和打标签时（`waiting_reset`，没有倒计时）GPU 闸门开着，片段运行时关上。

**两者为什么独立。** 操作员标签在 agent 能判定之前就已存在（rollout 标为完成之后 LEVI 才会取它）。模型只看到释放复核的帧和问题：`levi/agent` 和 `levi/live` 里没有任何代码把 `eval.*` 或 `success_flag` 交给模型。判定不读操作员标签。什么都不写回：没有 `annotations/outcomes` 文件，`eval.*` 和任何源文件都不改。自动批准主体仍然不能提交成败标签。

**一致性统计。** `stats.agreement` 用每个片段最新的一条记录比较两者。只统计操作员判为成功或失败的片段（`pairs`）。agent 标签是 `success`、`failure`、`undecided`（判定自己说未决）或 `none`（还没有判定）。`agree` 是 agent 判了成功或失败（`judged`）的片段里与操作员一致的比例；`rate_undecided_as_failure` 把未决按失败计。`false_success`（假成功）是操作员判失败、agent 判成功，分母是 agent 判了或未决的操作员失败片段；`missed_success`（漏判成功）是操作员判成功、agent 判失败或未决。两者都带 Wilson 95 % 区间。`none` 是覆盖缺口，不算一致。这些数字出现在统计汇总（`summary.agreement`）、每个会话的行（`pairs`、`agree`、`judged`、`false_success`、`missed_success`、`operator_success`）、逐片段的行和 CSV（`operator`、`agreement`：`yes`、`no`、`undecided`、`no_agent`）、数据集视图（覆盖全部片段，不只是列出的 200 个）、页面，以及报告里的“agent 与操作员对照”一节；只有至少一个片段有操作员标签时才出现。标签里写明片段怎么结束（`ended_by`）时，`agreement.by_ended_by` 把这些数字（`pairs`、`matrix`、`judged`、`agree`、`false_success`、`missed_success` 等）按 `budget` 和 `operator_key` 分开再给一遍；没有 `ended_by` 的标签（旧片段、其他来源）计入总体和第三组 `unknown`，不会报错。没有任何标签带 `ended_by` 时这个键不出现；逐片段的行和 CSV 有 `ended_by` 列；报告只在有两组或更多时才加“片段怎么结束的”一张表。数据集状态、`stats.jsonl` 和数据集视图里的操作员标签同样带 `ended_by`（读自 `eval.ended_by`，不读 `success_flag`，也不给模型看）。

**局限。** 第 3 版是候选，只在一个任务上检查过（把物体放进盘子，一台机器人，同一模型的两个策略，92 个片段，真值由 agent 看视频核对，没有人确认）。它防假成功不如第 2 版：没有检查最后一次放置是否成功。默认的运行中按键会在操作员判定后立刻结束片段，所以片段比无人值守的短，全部片段的一致率不能直接外推到无人值守。只有 `ended_by = "budget"` 的片段（跑满了预算）给 agent 的输入与无人值守相同，它们的一致率才能外推；按键提前结束（`operator_key`）的片段单独看（`summary.agreement.by_ended_by`）。操作员在夹爪还没松开时就判定，释放复核（第 1、2、3 版）没有张开事件可看，会把这个片段判成失败；这种运行请用[最终状态判定](#最终状态判定候选)。操作员不看 `/live` 是对操作员的要求，页面不强制。传 `--allow-candidate-anchored` 时，训练清单仍把候选的锚定标签排在机器人自己的标记之前（见“自动批准主体”）。

## 在线判定（接口 C5）

后台复核在一个片段结束几秒后才给出标签，评测期间常常要等整个会话结束。**在线判定**在评测进行时就回答：片段结束时，评测客户端把侧视和腕部相机最后几秒的图像发给实时服务，服务用最终状态判定规格（`generic-final.v1.json`，见[最终状态判定](#最终状态判定候选)）问本地模型一次，返回结构化结果，客户端把结果写进 rollout 的 `metadata.json`，字段是 `eval.agent_label`。默认关闭。

**在哪个进程里。** 一个小的 HTTP 接口，开在监督进程（`levi live start`）里，也就是管 GPU 闸门和 vLLM 启动、睡眠、唤醒的那个进程；`levi live once` 不启动它。它只监听回环地址（默认 `127.0.0.1:7882`），只做一件事：用配置的规格判定一个片段。它不接受提示词，也不回答别的问题。代码在 `levi/live/online.py`。

**和后台复核同一套逻辑。** 问题就是规格里的问题，任务指令的引用方式和后台复核相同（`generic.anchored_spec`：压成一行，最多 600 个字符），`[judge.task_text]` 有对应条目时用实验室的说法（[判定用的任务文字](#判定用的任务文字)）。模型请求走 worker 的同一条 provider 路径（`LocalProvider.ask`、worker 建的那个 provider 配置、按规格答案 schema 的约束解码、规格的 `max_output_tokens`、服务器端的贪心解码）。答案用 `anchored.validate_answer` 校验，用 `anchored.judge`（规格的 `valid_when`）读取，再用 `anchored.outcome` 和 `anchored.undecided` 按 `final_state` 规则得出结局。没有另写一份逻辑。

### 设置

```toml
[online]
enabled = true                  # 默认 false
host = "127.0.0.1"              # 只能是回环地址，其他一律拒绝
port = 7882
spec = "generic-final.v1.json"  # levi/live/specs 里 episode.rule 为 final_state 的规格
timeout_s = 15.0                # 从请求到达算起，这么多秒内必须有答案
max_body_mb = 8.0               # 请求体超过这个大小就拒绝（413）

[pipeline]
background = false              # 可选：关掉后台标注（见下）

[vllm]
prewarm = true                  # 必需：在线判定从不冷启动 vLLM
```

| 表 | 键 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `online` | `enabled` | false | 随服务启动这个接口 |
| | `host`、`port` | `127.0.0.1`、7882 | 回环地址（`127.0.0.1`、`::1`；`localhost` 这样的主机名被拒绝）；端口不能是 5000、8000、7860、7861、策略端口、`service.ui_port`、`service.core_port` 或 `vllm.port`（启用时检查） |
| | `spec` | `generic-final.v1.json` | `levi/live/specs` 里的文件，`episode.rule` 为 `final_state`，没有否决项，每个视图的偏移用秒给出，问题里有 `{task}` 占位符；可以有一个让片段无效的开头检查（`start.void_when`，即 `generic-final.v2.json`），其视图的 role 要和最终画面视图的不同（见[接口 C5](#接口-c5)） |
| | `timeout_s` | 15 | 1–120 秒，从请求到达时算起（包括等闸门和唤醒）；客户端可以按自己的截止时间提前放弃 |
| | `max_body_mb` | 8 | 0.5–64 MiB |
| `pipeline` | `background` | true | false：不启动 worker，不做后台模型请求（见下） |

设置在启动时读取：改完 `live.toml` 后 `levi live stop` 再 `levi live start`（重启的是实时服务，不是产品 LEVI）。`enabled = true` 时，监督进程在启动时载入答案校验相关的代码（pydantic、numpy、模型客户端），常驻内存多约 26 MiB（本机实测）。

**关掉后台标注（`pipeline.background = false`）。** 监督进程不启动 worker，自己也不发模型请求：没有时间片段，没有释放复核或最终状态复核。它照常把每个已完成的 rollout 连同操作员标签和客户端转来的在线结果一起镜像进来，写状态文件，响应页面和 API，并给每个片段写一条 `live/stats.jsonl` 记录，所以页面、`stats.agreement`、会话报告和 `levi live report` 都照常可用，在线结果也能和操作员标签对照。镜像进来的片段立刻记为 `done`：`eval.agent_label.status` 为 `ok` 时带在线判定，否则没有判定（`no_agent`，`reason` 写明原因）。`accepts_sessions` 仍为 true。此时 `temporal`、`anchored`、`anchored_spec`、`anchored_min_valid`、`guideline`、`vocabulary` 都不使用，也不校验（后台开着时会被拒绝的组合，例如 `temporal = false` 加 `anchored = false`，这时也接受）；它们保持原样，切回去不用改别的。切换之前已经镜像、还在等标注的片段继续等，直到后台重新打开；后台关着时收进来的片段，之后不会补标。`vllm.prewarm` 照样会启动 vLLM（在线判定需要它）。后台开着（默认）时一切和以前一样，同一片段的后台复核会在 `verdict` 里替换在线判定（在线判定仍保存在数据集状态的 `online.verdict` 里）。

### 接口 C5

**第 2 版接口（只增不改）。** 接口的第一版只有最终画面。第 2 版为带开头检查的规格（如 `generic-final.v2.json`）加上可选的**开头帧**（片段的第一帧），响应里多四个键。第一版发出和读取的东西都没变：三个 `schema` 字符串仍是 `levi.online.judge.spec.v1`、`.request.v1`、`.result.v1`（第一版的客户端会检查它们），请求的键相同，`views` 列的恰好是最终画面的视图，不发开头帧的客户端只按最终画面判定（`start_check: "skipped"`）。第 2 版的客户端从 `GET /v1/judge/spec` 里的 `revision: 2` 和 `start_views` 列表得知服务认开头帧。

状态文件（接口 C4）多一个字段 `online_judge`：接口关闭时为 `null`，开启时为

```json
{"url": "http://127.0.0.1:7882", "spec": "generic-final", "spec_version": 1, "ready": true}
```

`ready` 为 true 的条件：接口在监听，并且服务自己的 vLLM 已就绪或在睡眠（不需要冷启动）；`manual` 模式或 `adopt_external` 时，是 `vllm.port` 上有 vLLM 回答 `/health`（最多每 10 秒查一次，只在接口启用时查）。它不反映闸门：`ready` 的接口在策略推理时照样回答 `unavailable`。客户端对状态文件的可用性检查不变。

**`GET /v1/judge/spec`** 告诉客户端该发什么（从规格文件读出，不写死）：

```json
{"schema": "levi.online.judge.spec.v1", "revision": 2, "spec_id": "generic-final", "spec_version": 1,
 "views": [{"role": "side", "offsets_seconds": [-3.0, -2.0, -1.2, -0.6, -0.2, 0.0]},
           {"role": "wrist", "offsets_seconds": [-2.0, -1.0, -0.4, 0.0]}],
 "image": {"format": "jpeg", "max_side": 1280}, "timeout_s": 15.0}
```

`online.spec = "generic-final.v2.json"` 时，响应多一个键 `start_views`（没有开头检查的规格没有它），`spec_version` 为 2：

```json
 "start_views": [{"role": "start_side", "anchor": "start", "offsets_seconds": [0.0]},
                 {"role": "start_wrist", "anchor": "start", "offsets_seconds": [0.0]}]
```

`views` 的偏移是相对片段最后一帧的秒数；`start_views`（`anchor: "start"`）的偏移是相对片段**第一**帧的秒数，所以 `0.0` 就是客户端录到的第一帧。开头帧故意不放进 `views`：第一版的客户端会读 `views` 的每一项，遇到不认识的 role 就拒绝整个规格。`max_side` 是建议：发相机原始帧的 JPEG（后台复核看的是原始分辨率），只有长边超过它时才缩小。片段比某个偏移短时，那个位置用第一帧，和后台复核一样。

**`POST /v1/judge`**，`Content-Type: application/json`，带 `Content-Length`，不带 `Origin` 头（浏览器页面发来的请求被拒绝）：

```json
{"schema": "levi.online.judge.request.v1",
 "task": "<任务指令>",
 "episode": {"group": "...", "task_folder": "...", "demo": "demo_0003", "run_id": "...", "steps": 412, "fps": 15.0},
 "images": [{"role": "side", "offset_s": -3.0, "step": 367, "jpeg_b64": "<base64 编码的 JPEG>"}, ...]}
```

第 2 版客户端对 `generic-final.v2.json` 发的同一个请求，多两张图（其余十张同上）：

```json
 "images": [..., {"role": "start_side", "offset_s": 0.0, "step": 1, "jpeg_b64": "<base64 编码的 JPEG>"},
                 {"role": "start_wrist", "offset_s": 0.0, "step": 1, "jpeg_b64": "<base64 编码的 JPEG>"}]
```

- 必填 `schema`、`task`（1–2000 个字符）和 `images`；`episode` 及其中每个键都可省略，只用于日志（`fps` 是客户端实测频率的中位数，不是名义值）。图像的 `step` 可省略（整数或 null），只写进日志：客户端发的是 `frames.csv` 的 `frame_index`，从 1 开始，最后一帧等于 `steps`；它从不当作从 0 开始的视频帧号使用。客户端发质量 90 的 JPEG，role 只有 `side` 和 `wrist`。
- **严格模式。** 任何一层出现约定里没有的键都以 422 拒绝；键名像操作员或评测数据（`operator...`、`outcome`、`success`、`label`、`eval`、`verdict` 等）时，原因里会专门说明。重复的键、NaN、Infinity 也拒绝。
- **开头帧（第 2 版，可选）。** role 为 `start_views` 里那些（`start_side`、`start_wrist`，偏移 `0.0`）的图像，就是 `images` 里的普通条目：没有新增任何键。要么全发要么不发：不发时不问开头检查（`start_check: "skipped"`，对最终画面按第 1 版的规则）；只发一部分时，422 会写明缺哪些。规格里没有开头检查的服务会拒绝它们（422：role 不是规格的视图），所以客户端只在规格响应里有 `start_views` 时才发。
- 规格的每个 `(role, offset)` 恰好一张图，偏移在 1 毫秒内算匹配；不管请求里怎么排，模型都按规格的顺序看到它们（先按视图，再按每个视图的偏移）。缺图、同一位置两张图、未知的 role、规格里没有的偏移、不是 base64 的文本、不是 JPEG 的字节，都返回 422，并写明哪里不对。
- 请求体超过 `max_body_mb` 返回 413（不读取请求体）；没有 `Content-Length` 返回 411；`Content-Type` 不是 JSON 返回 415；`Host` 不是本接口的回环地址或带 `Origin` 头返回 403；请求体不是 JSON 返回 400。

**响应**（`ok`、`unavailable`、`error` 都是 HTTP 200；被拒绝的请求用同样的结构，`status: "error"`，HTTP 状态码为对应的拒绝码）：

```json
{"schema": "levi.online.judge.result.v1",
 "status": "ok", "reason": null,
 "outcome": "success", "undecided": false, "reading": "supported",
 "answer": {"object_state": "resting_at_destination", "stable": "yes"},
 "checks": [{"field": "object_state", "value": "resting_at_destination", "result": "supported"},
            {"field": "stable", "value": "yes", "result": "supported"}],
 "spec": {"id": "generic-final", "version": 1}, "model": "qwen3.8-27b",
 "tokens": 4312, "prompt_tokens": 4290, "elapsed_s": 2.41, "request_id": "9f0c3b6a1d2e4f50",
 "final_reading": "supported", "start_check": null, "start_answer": {}, "task_rewritten": null}
```

- `outcome` 和 `undecided` 按 `final_state` 规则：物体静止地放在目的地且稳定为成功；任何明确的其他答案为失败；有 `unclear` 答案、且没有明确的失败答案压过它时，为失败并且 `undecided: true`。`reading` 是规格的 `valid_when` 对答案的读取结果（`supported`、`contradicted`、`unknown`），`checks` 是逐条条件。`status` 不是 `ok` 时 `outcome` 和 `reading` 为 null。
- 最后四个键是第 2 版接口的，总是存在。`final_reading` 是规则对片段的读数：`reading`（`supported`、`contradicted`、`unknown`），或者开头检查做了决定时的 `already_satisfied_at_start`、`start_unclear`（两者都是失败并且 `undecided: true`）。`start_check`：规格没有开头检查时为 `null`，否则是 `skipped`（没发开头帧）、`passed`、`voided`（物体一开始就在目的地）或 `unclear`；`start_answer` 是开头问题的答案（没问时为 `{}`）。`task_rewritten` 为 `null`，或在引用了实验室的说法时为 `task_text` / `task_folder`（[判定用的任务文字](#判定用的任务文字)）。有开头检查时模型被问两次，先问开头检查；`tokens`、`prompt_tokens`、`elapsed_s` 包含两次，`timeout_s` 也是两次合计。开头答案不是规格允许的值时是 `error`（`invalid_answer: start check: ...`），和最终答案不合规时一样。
- `tokens` 和 `prompt_tokens` 是服务器报告的数（没报告时为 null）；`elapsed_s` 从请求到达算起。
- `reason` 的格式是 `<代码>: <说明>`，第一个冒号前的代码是固定的，供程序判断。**瞬时**代码（评测客户端在自己的截止时间内重试）：`gate_closed`、`busy`、`service_busy`。其余代码在两集之间的空当里重试也不会消失。`ok` 时为 null，只有答案等过闸门时写 `gate_waited: ...`（见下）。**`unavailable`**（什么都没发给模型；除下面说的等待闸门外立即返回，不到 1 秒）：`busy`（已有一个在线判定在进行，一次只做一个）、`gate_closed`（策略正在推理，或等待的闸门没有及时打开：`policy_inferring`、`episode_imminent`、`unknown_client`）、`cold_start`（vLLM 没在运行：在线判定从不冷启动它）、`vllm_starting`、`no_room`（vLLM 在睡眠，按 GPU 规则此刻不能唤醒）、`wake_failed`、`vllm_failed`（服务已放弃启动 vLLM）、`service_busy`（监督进程正在启动、停止 vLLM 或让它睡眠）、`shutting_down`；以及模型工作时被打断的 `gate_closed ... (the request was cut)`、`vllm_sleeping`、`vllm_stopping`。**`error`**：`timeout`（`timeout_s` 内没有答案，请求被切断）、`model_error`（服务器出错或拒绝）、`invalid_answer`（答案不是规格允许的值）、`internal_error`，以及被拒绝的请求的 `invalid_request`。

**模型什么时候可以回答。** 请求到达时，监督进程当场读会话文件（tick 最多每秒决定一次闸门），只有闸门开着才放行：没有会话处于 `running`，`gpu.lead_s` 内没有片段要开始（`episode_imminent`），也没有无会话作证的策略服务器。客户端在写完 `waiting_reset`（或 `homing`）后立即发请求：闸门没有防抖，这时立刻就是开的。如果闸门关着、但没有会话处于 `running`（`episode_imminent` 窗口、无会话作证的策略服务器、会话文件还没改写），请求会等它打开，每 0.1 秒再问一次，最多等到请求到达后 `timeout_s`；等待期间有会话开始 `running` 就立即结束等待（`gate_closed`），等满仍未打开则返回 `gate_closed: ... (waited N s for it to open)`。策略正在推理时，以及上面列的其他原因，都立即回答。客户端用自己的截止时间（从片段结束算起），在截止前可以对 `gate_closed` 和 `busy` 重试。醒着的 vLLM 直接回答；睡眠中的 vLLM 只在批次唤醒同样的规则下才会被唤醒（约 0.75 秒，最多等 5 秒）：闸门开着，空闲显存够唤醒（`gpu.wake_margin_mib`），策略服务器没有超过 `gpu.policy_budget_mib`。在线判定从不冷启动 vLLM，所以要在策略服务器之前用 `--prewarm` 启动服务。模型工作期间每 0.25 秒读一次闸门：闸门一关（下一集开始了），请求立即被切断，和 worker 的请求一样。放行时最多等监督进程自己的 GPU 操作 0.5 秒。答案进行中，vLLM 不会因为闲置而被放睡；但显存被需要时仍会被放睡或停止，这会打断答案（`vllm_sleeping`）。后台开着时，worker 可能同时在发请求；vLLM 两边都服务（`max_num_seqs` 2），答案可能因此变慢。

**客户端写什么**（这一侧由策略仓库的客户端实现）。在 rollout 的 `metadata.json` 里写 `eval.agent_label`，要在 `.complete` 之前写（和操作员标签一样：LEVI 只在镜像时读一次元数据）：

- 上面的响应，再加 `"source": "online"`、`requested_at` 和 `received_at`（纪元秒；ISO 时间也能读）、`frames: [{role, offset_s, step}]`，以及 `timing`：`during_run` 或 `after_budget`；
- 或者没有答案时写 `{"source": "online", "status": "unavailable" | "error" | "timeout" | "skipped", "reason": "..."}`。

镜像器（`criteria.agent_label`）把格式正确的 `ok` 变成数据集状态里这个片段的自动判定：`verdict = {outcome, events: 1, valid_events: 1 或 0, undecided, rule: "final_state", min_valid: 1, basis: {final_reading, start_check}, spec, spec_version, review: "auto", evaluated: false, at: received_at, source: "online", task_rewritten}`（`basis.start_check` 和 `task_rewritten` 只在结果里有时才写；`final_reading` 取结果的 `final_reading`，没有就取它的 `reading`；以开头检查为依据的 `final_reading` 若不是“失败且 `undecided: true`”，算格式错误），形状和 worker 的判定一样，所以页面、`stats.agreement` 和报告不用改就能读。其他状态，或者字段对不上的 `ok`（结局不是成功或失败、成功却标了不确定），都不产生判定：这个片段算 `no_agent`。客户端转来的整个标签保存在 `online` 下（`status`、`reason`、`timing`、`request_id`、`usage`、`verdict`）。这种片段的统计记录带 `result.verdict.source: "online"`，把这次判定的成本记为一次 `review` 请求，另有 `result.online`（`status`、`reason`、`timing`）；`timeline.to_verdict_s` 可能是负数，因为答案通常在客户端把 rollout 标为完成之前就到了。API 的片段行带 `verdict.source`。

**日志。** 每个 `POST /v1/judge` 在 `<工作区>/live/online.jsonl` 里加一行（schema `levi.live.online.v1`，和其他日志一样轮转）：`at`、`time`、`request_id`、`http`、`episode`（请求里给的标识）、`status`、`reason`、`outcome`、`undecided`、`reading`、`final_reading`、`start_check`、`task_rewritten`、`tokens`、`prompt_tokens`、`elapsed_s`、`images`（最终帧和开头帧合计）、`spec`、`model`。不存图像，也不存任务文字。图像只在模型读取期间放在 `<工作区>/live/online-tmp/<request_id>/`。

### 独立性

模型只看到图像和带任务指令的规格问题，别的都看不到：请求里不能带操作员标签或其他评测数据（以 422 拒绝），`episode` 只进日志，其中的 `task_folder` 还在服务端用来查 `[judge.task_text]`（模型看到的是那个条目给的说法，从不是文件夹名），测试检查了发给模型服务器的请求（包括开头检查的）里没有任何片段标识。客户端在操作员给出标签之后才显示模型结果，所以操作员标签仍是真值；这个顺序由客户端保证，服务看不到。`live/online.jsonl` 在模型一回答就记下结局，通常早于操作员按键：操作员判定之前不要查看它（LEVI 没有页面或命令读它）。服务不往 rollout 里写任何东西。

### 客户端要为第 2 版做什么

策略仓库的客户端（本次没有改它）从规格响应读 `start_views`；有的话，从片段**第一个**录到的帧里取每个相机的图（`start_side` 取侧视相机，`start_wrist` 取腕部相机，偏移 `0.0`；JPEG 编码和 `max_side` 同其他图），在整个片段期间保留它们（只存最后几秒的缓冲里没有它们），并把两张都加进 `images`。`task` 仍发策略自己的指令。没有 `start_views` 时，发的和以前一样。它可以把 `start_check: voided` 在终端那一行显示为“不是有效试验”；结果里的 `undecided: true` 已经让它现有的那一行写成“不确定”。旧客户端不用改：只是不做开头检查。

### 风险和局限

- **显存和闸门。** 策略服务器以 `.22` 常驻时，只剩约 1.3–1.5 GB 余量（见“GPU 管理”）。两集之间的唤醒和请求遵循后台标注的同一套规则，但策略服务器常驻时唤醒的显存峰值，以及片段刚结束就发出的请求对策略推理延迟的影响，**都没有**用真实模型和 GPU 测过。闸门关闭时进行中的请求会被切断。
- **准确率没有评估。** 最终状态规格在任何数据上都没有测过准确率；每个在线判定都要读作“自动、未审”，信任之前先和操作员标签对照（`stats.agreement`，按 `ended_by` 分开看）。
- **开头检查的成本和准确率。** 带开头帧时模型被问两次（先是对两张图的一个短问题，再是最终问题），所以回答比一次请求慢；开头检查的准确率没有测过（见[第 2 版](#第-2-版说法和开头检查候选)）。
- **时间。** 唤醒（约 0.75 秒）加一次请求（27B 模型看 10 张图：几秒，这里没测）必须在下一集开始前完成；`reset_wait_s` 太短时会得到 `gate_closed ... (the request was cut)`。
- 接口除了回环地址、`Host` 检查和拒绝浏览器请求之外没有身份验证：本机任何进程都可以让它判定（它也只能做这件事）。
- 端口被占用时接口起不来：事件日志和 `last_error` 会写明，`online_judge.ready` 保持 false，客户端会连接被拒。

## 保持轻量

- **优先级**：服务启动的每个进程（监督进程、worker、页面、核心、vLLM 启动脚本）都设为 nice 19、ionice 空闲类（失败不致命，`doctor` 会报告）。
- **线程**：`OMP/MKL/OPENBLAS/NUMEXPR/ARROW/OPENCV` 的线程变量在这些库加载前设为 `resources.threads`（默认 2）；建视图用 `view_workers = 1`。
- **空闲**：监督进程用 `os.scandir` 列 rollout 目录，会话文件变化时才读，重写 `status.json`；不打开视频、不导入数值库（有测试）。空闲扫描每 `poll_idle_s` 一次，没变化的任务目录不会再次列出。
- **GPU**：vLLM 为批次启动，空闲 `idle_timeout_s` 后睡眠或停止，显存被需要时立即睡眠；闸门让它不挡策略的路。
- **磁盘**：镜像是硬链接（零额外）。批次结束后删除该批运行冻结的输入和证据图（仍开着的释放复核运行保留证据供审核）；残留运行目录超过 `cache_max_gib` 时从最旧的开始清理；日志按 `log_max_mb` × `log_backups` 轮转；`status.json` 和所有 API 响应体积有界。
- **实测**（假模型、不含 vLLM、本机、空闲 10 分钟）：见下文“实测”。

## 实时页面

`/live` 显示评测进行时发生的事。它不会启动、停止或批准任何东西；在这里唯一能改的是把片段从数据集中排除、再恢复（见[排除片段（可恢复）](#排除片段可恢复)）。它由产品 LEVI 提供（见[在产品 LEVI 里查看实时评测](#在产品-levi-里查看实时评测)）；服务带 `--ui` 运行时，实时工作区自己的页面也有它。找不到实时服务时，页面说明原因（尚未配置、找不到、或设置有误），不显示其他内容。

**只要显示着一个实时服务，入口就会出现，并且是导航里最显眼的一个。** 找不到实时服务的 LEVI，顶部导航没有“实时评测”，首页也不变。否则它是导航的第一项，带轮廓，有一个状态小圆点（绿：运行正常且空闲；蓝：正在标注；琥珀：标注已暂停，或有事在等人处理，包括被闸门拦住、需要你点“继续”的运行（会自己继续的被拦运行不改变圆点和数字，实时页只显示数量，不列出运行编号）；红：FR3 红灯，或评测会话出了故障；灰：读不到状态），有事等人时显示数字，悬停有一句中文或英文的说明。此时首页（`/`）顶部是一条大横幅，圆点和说明相同，点击进入 `/live`；这是横幅而不是跳转，所以首页的 LEVI 标识和 `?path=`/`?dataset=` 链接照常工作，其他页面也只差一次点击。页面问 `/api/levi/live/status` 判断有没有实时服务可显示：返回 `{"enabled": false}` 时，标签页可见期间每分钟再问一次（之后才启动的服务不用刷新页面就会出现，每次只读一个小文件）；有实时服务时，标签页可见期间每 8 秒问一次来更新圆点（失败后最长 30 秒）。

- **红色横幅**“检测到 FR3 出错，评测已中断”：健康监视器报告红灯或有会话处于 `fault` 时出现，相关数据集卡片带红色标记。监视器缺失或陈旧**不算**红灯（琥珀色“监视器离线”）。
- **评测会话**：状态、评测编号、片段 `第几个 / 目标`（有效片段数）、步数进度条、策略 config 和 checkpoint 文件夹名、最近一个片段、是否启用 LEVI 标注、复位等待；客户端心跳超过 10 s 显示“已失联”。
- **FR3 机械臂**：模式、红灯、错误、硬件和控制器状态、原因。
- **标注管线**：每个数据集一张卡片，显示已镜像、等待、标注中、完成、失败的片段数，已提交的时间片段，以及**自动**成败结果（虚线框、标“自动”，写明“未经审核，准确率未评估”，不会画得像金标准）。留待人工复核的复核运行会列出（默认最新 3 个；按浏览器保存的只读筛选可隐藏较早的，不改变任何数据），并链接到查看器。“显示片段”列出数据集的片段（先 10 个最新的，可展开全部）；每个片段可在列表或详情里排除，也可多选后一起排除；“已排除（n）”列出被排除的片段，带“恢复”按钮。
- **统计**：当前范围（全部、某个数据集、某个会话）的关键数字：片段数、中位和 p90 延迟、实时倍率、每个片段的 token、等 GPU 门控的时间、会话内标注比例；评测会话表；逐片段表（延迟或模型开销两种视图）；下载（Markdown、JSON、CSV）。随页面的轮询更新，评测进行时最多每 5 s 一次，否则每 30 s 一次；还没有记录时会明确说明。口径见“统计与报告”。
- **服务与资源**：状态、GPU 模式、vLLM 状态、用人话解释的标注闸门（例如策略推理时为什么暂不标注）、队列、工作进程、最近错误、监督进程的内存、线程和 CPU。服务未运行时页面会说明，并提供可复制的 `levi live start`。
- **需要人处理**：服务放弃启动模型服务器（`attention`，写明原因并给出可复制的 `levi live resume`）、数据集在等你于 LEVI 页面批准计划或提交草稿（`awaiting`）、或服务启动的页面/核心没起来（`frontend`）时，出现琥珀色横幅。数据集卡片还会显示卡住的片段和被替换的源。复位等待在两次轮询之间由浏览器倒计时（`reset_wait_s`、`waiting_reset_since`），过期后提示“下一个片段应已开始”。GPU 闸门和决定的每个代码都有中英文的人话解释。当服务因不会自行消失的原因暂停了标注（`labelling_paused`：`vllm_failed`、`vllm_error`、`insufficient_vram`、`policy_large`、`unknown_client`、`vram`、`lock`、`external_busy`），横幅会说明原因、评测不受影响且片段不会丢，以及该怎么做（需要恢复时附可复制的 `levi live resume`）；主循环超过 5 分钟没动作（`loop_at`）也会提示。在 Agent 工作台里，策略推理期间被拒绝的 Run 或 Resume 会显示一句人话（几秒后再试；已经在跑的运行会自己继续），而不是核心的日志原文。

评测运行时每 2 s、空闲时每 10 s 轮询 `/api/levi/live/status` 和 `/sessions`；标签页不可见时暂停；失败后退避（最长 30 s）；答复是 `{"enabled": false}` 时改为显示说明（每分钟再问一次）；只为少数重要或已展开的卡片加载数据集详情，且仅在该行变化后再次加载。页面唯一的写操作是排除和恢复片段（由人用页面自己的令牌发起），也不显示令牌。

## 在产品 LEVI 里查看实时评测

实时服务保留自己的工作区（默认 `levi-live-ws`）：自动批准主体、它的运行、镜像、状态文件和审计都不进入产品 LEVI 的 `.state`，`levi live` 也拒绝把产品的 `.state` 当作自己的工作区。共享的是**页面**：产品 LEVI（:7860，核心 :7861）读取实时工作区的文件，显示 `/live` 和“实时评测”入口，所以只需要打开一个页面，也只有一个训练池。

**产品 LEVI 怎样找到实时工作区**（`levi/live/locate.py`，每次请求都重新判断，不缓存）：

1. 自己的工作区，如果它就是实时工作区（实时服务自己的核心，或加 `--ui` 时它的页面）；
2. 否则用 `LEVI_LIVE_WORKSPACE`（如果产品 LEVI 的环境里设置了；**推荐的部署方式**：在产品检出的 `.env` 里写 `LEVI_LIVE_WORKSPACE=<实时工作区的绝对路径>`，这样不管实时 home 里记的是什么，页面都显示这个工作区）；
3. 否则作为兜底，用同一个实时 home 最近一次 `levi live start` 运行的工作区，记在 `<LEVI_LIVE_HOME 或 ~/.levi-live>/started.json`（与 `levi live` 命令在没给工作区时跟随的是同一条记录；服务停止后仍保留）。没有这条记录的 home（记录出现之前启动的服务）退回用状态文件 `status.json` 里的 `workspace` 字段，但要求该工作区有只有 `start` 才写的服务日志：`levi live once` 也写状态文件，它的目标不会被当成实时工作区显示。

候选目录必须是绝对路径（相对路径一律算 `not_live`），实时标记（`live/workspace.json`）所在的 `live/` 目录必须真的在它里面（不能是指向别处的链接），并且它既不能是、也不能位于或包含产品 LEVI 自己的工作区或本仓库任何检出（每个 git worktree 和主检出）的 `.state`。否则 `/api/levi/live/*` 回答 `{"enabled": false, "reason": "not_configured" | "not_live" | "product_workspace"}`，导航不显示入口，`/live` 说明缺什么。回答里没有路径、令牌或密钥。服务不必在运行：已停止的服务留下的 `started.json` 仍然指向工作区，页面显示它未运行。页面只用目录名指称实时工作区（`workspace_name`）；它转发的状态里去掉了状态文件中的 `workspace` 和 `config` 路径。

**产品 LEVI 读什么。** 所有只读路由（状态、会话、数据集、某个数据集的片段、审计、统计和下载、状态里的 GPU 与闸门）都由产品核心从实时工作区的文件回答：`<home>/status.json`、`live/effective.toml`、`live/datasets/*.json`、`live/audit.jsonl`、`live/stats.jsonl`、机器人侧的会话和 FR3 文件，以及实时工作区自己的目录（用于数据集的 `repo_id`；不用产品的目录，产品里可能有同名的另一个数据集）。这些都不写进产品工作区。

**在产品页面上排除片段**，做法和实时工作区自己的页面一样，仍然只有人能做：请求必须带产品 LEVI 的界面令牌（与该页面其他写操作相同），智能体凭据一律 403。随后由产品核心自己在实时工作区的状态文件上修改，用的是实时 worker 也会取的同一把锁（`exclusion.py`），所以实时核心不必在运行，也不读取、不发送它的任何密钥。审计行写在实时工作区的 `live/audit.jsonl`，带 `"via": "product"`。`levi live exclude` 仍然经实时核心执行（见下）。

**在产品 LEVI 里打开实时数据集（只读关联）。** 实时数据集（镜像的 rollout、它们的时间片段、操作员标签和自动结局标签、释放复核的证据）以 `live.<名字>` 列在产品 LEVI 的“本地数据集”里，带“实时评测 · 只读”标记，用普通查看器打开，不论实时服务是否在运行（`levi/links.py`）。卡片上的“在查看器中打开”打开的就是它。这是关联，不是复制：产品 LEVI 直接读实时工作区里的文件（镜像的浏览视图、标注目录或版本包、只读方式打开的 agent 存储），条目是被问到时才算出来的，从不写进自己的目录，所以实时工作区删掉的数据集会马上从列表里消失，也就没有需要保持同步的副本。什么也不写：对这类数据集的任何写入（标签、时间片段、复核旗标、SAM3 和 RECAP 运行、导出、取消登记）都返回 403，读取也不会在实时工作区里新建任何文件。只有实时工作区的 `captures/` 及其浏览视图作为文件提供。关联哪些实时工作区：本 LEVI 的实时页当前显示的、训练池记得它显示过的，以及 `LEVI_LINKED_WORKSPACES`（逗号分隔的绝对路径；必须是实时工作区，且不是本 LEVI 自己的工作区，也不是任何检出的 `.state`）。两个实时工作区有同名数据集时，分别是 `live.<名字>` 和 `live.<工作区路径哈希前 6 位>.<名字>`。浏览视图没有就绪（实时工作区里 `view_status` 不是 `ready`）的数据集也会列出，打开时会说明。审核、批准计划和提交草稿仍在实时工作区自己的 LEVI 里做：服务不带 `--auto-approve`（`awaiting_approval`）运行时，用 `--ui`（或 `--auto-approve`）启动它，卡片上的“转换与审核”会打开那个页面（链接是服务看到的 `http://<service.host>:<service.ui_port>`：`host = "0.0.0.0"`，或通过隧道、从另一台机器访问产品页面时，浏览器可能打不开）。

**只有一个训练池：产品 LEVI 的。** 两个 LEVI 的训练池内容不同，是因为训练池按工作区存放：索引、扫描摘要、配方、导出作业和本体规则都在 `<工作区>/pool/` 下（`levi/pool/settings.py`），每个工作区各自扫描、扫描时间也不同。原因不在设置：实时核心从同一个检出启动，读同一个 `.env`（`levi/paths.py` 在每个进程里读 `<检出>/.env`），所以 `LEVI_POOL_ROOTS`、`LEVI_EXPORT_ROOTS`、`LEVI_POOL_HELDOUT` 相同。人工标签也不同：扫描读取池根下各工作区以及自己工作区的人工成败标签（`scanner.scan`），所以实时工作区的训练池看不到产品工作区的标签（除非产品工作区在池根下），产品的训练池看得到。因此实时工作区自己的页面（现在只在 `--ui` 时才有）不再提供训练池：导航里没有“训练池”，那里的 `/pool` 指向产品 LEVI。排除只要发生在产品训练池会读取的实时工作区里，就会作用到产品训练池。训练池读取：上次扫描找到的工作区、产品实时页面当前显示的实时工作区，以及该页面以前显示过的所有实时工作区（记在 `<产品工作区>/pool/live_workspaces.json`，只增不减；已不存在的目录跳过；`levi/pool/exclusions.py`）。所以不在池根下的实时工作区也算数，页面改为显示另一个实时工作区之后，之前的排除仍然有效。产品从没显示过、扫描也没找到的实时工作区不会被读。这个列表由产品 LEVI 自己的 `GET /api/levi/live/*` 路由在第一次找到某个实时工作区时写入（这是 GET 唯一的写操作，只写产品自己的 `.state/pool/live_workspaces.json`，绝不写实时工作区）。列表最多 20 条：超出后新的实时工作区不再加入（页面显示它期间仍会读取），实时页面和日志会提示。`levi pool live-workspaces list` 列出列表（以及当前显示的工作区）；`levi pool live-workspaces forget <路径>` 停止读取某一个，不删除任何数据，页面再次显示该工作区时它会重新加入。状态文件在训练池被查询时读取，文件没变就用缓存。

## 排除片段（可恢复）

人可以在实时页面把一个片段从实时数据集里拿掉（机器人出了意外、复位没做好、某一条没人想标）：片段详情里的**删除片段（可恢复）**，每个片段前的复选框加**删除所选（n）**用于批量，确认框会写清后果，原因可选。卡片上的**已排除（n）**列出被排除的片段（带原因和时间），**恢复**（或**恢复所选**）把它们放回来。

**它做什么、不做什么。** 这是软删除：rollout 源目录不会被动（LEVI 本来就从不写它），`captures/` 下的镜像保留，已经写进 LEVI 的标注和证据也保留。片段只是不再*被计数*、不再*被处理*：

- **存放位置**：数据集状态文件（`live/datasets/<名字>.json`）里该片段自己的那一行：`"excluded": {"at": <epoch>, "by": "person", "reason": "…"}`。没有这个键就是没排除，所以旧文件不用迁移；恢复就是删掉这个键。文件仍按原来的方式在锁内原子写入。
- **计数**：被排除的片段不进任何计数：状态行里的 `episodes`、`pending`、`done`、`failed`、`skipped` 不含它，数据集详情的 `counts`、`total_demos` 不含它，卡片上的自动成败统计也不含它。状态行和详情另带 `excluded` / `excluded_count`，详情把被排除的片段单独列出（`excluded_demos`）。
- **工作进程**：队列、批次选择和工作进程最后的过滤都会跳过被排除且尚未标注的片段；它保持 `mirrored`，恢复后再标注。被排除片段的源发生变化时不会重新镜像（刷新在重建镜像前会在状态文件的锁内再看一次是否已被排除）。
- **已标注的片段**：标注留在 LEVI 里，但不再计入计数、数据集总数和自动成败统计。
- **正在标注的片段会被拒绝**：属于进行中批次（数据集状态的 `current`）的片段不能排除（HTTP 409，“being labelled (part of the batch in progress); try again after the batch”）：它的运行已经带着它做了计划，点一下列表不该把做到一半的工作打断。检查和写入在状态文件的锁内一起完成，工作进程的过滤（`Worker.filter_demos`）也在同一把锁内跳过被排除的行，所以恰好在选批次时点下去，要么被拒绝、要么生效，不会各做一半。数据集的批次在跑时，仍在等待的片段可以排除。（服务停止时，以及批次在等人批准计划或提交草稿时（`awaiting`，自动批准关闭），批次都保存在状态文件里，所以它的片段在批次结束前一直被拒绝。）
- **复核运行不会被取消**：一个复核运行里的片段*全部*被排除时，该运行保持打开，冻结输入和提案都还在：取消会让清理把它们删掉，恢复后的片段就再也没法被接受。它只是不再计入“待复核”数量（`review_runs_open`、卡片上的“留待人工复核的复核运行”和数据集详情的 `review_runs`），这样这个数字和其他数字一致；恢复其中任何一个片段，它就重新计入。它的存续仍由 `pipeline.keep_review_runs` 决定，和其他运行一样。复核运行里还有没排除的片段时照常计入，它对被排除片段的提案仍在里面。响应里 `review_hidden` 列出不计入的运行。状态文件加锁期间只写 `excluded` 标记：那里不调用运行存储。
- **不能排除**：被拒收或卡住的片段（从未纳入数据集），以及服务还没镜像的 rollout（它还没有行，等它出现在列表里再排除）。
- **训练池**：见下。
- **查看器**：镜像和 LEVI 据此建的数据集视图里仍然有这个片段，所以普通查看器，以及从实时工作区自己的数据集导出的训练清单仍会列出它。只有实时页面、计数、工作进程和训练池遵守排除。

**命令行。** `levi live exclude <数据集> <demo>... [--reason "…"] [--restore] [--workspace <实时工作区>]` 通过该实时工作区正在运行的核心做同样的事（一次可排除同一数据集的多个片段；要么全部成功要么都不改；回答和审计行都相同）。数据集名就是 `levi live status` 显示的那个。它和页面一样是人的操作：命令从核心自己持有的文件读取人的密钥（`<工作区>/outputs/LEVI/workbench/agent/core/human.key`，仅属主可读），放进请求头发送，从不打印；智能体凭据照旧被路由拒绝。需要核心在运行（`levi live start`）；核心没运行或没有密钥文件时会说明并以 2 退出；核心拒绝时（例如片段在进行中的批次里）以 1 退出。

**数据集名。** 路由接受服务生成的数据集名，包括两个根目录里有同一 group 和任务时加的后缀 `__at__<根目录标记>`：唯一的规则是它是 `live/datasets/` 下的一个文件名（没有 `/`、`\`、控制字符，不以点开头）并且对应的状态文件存在。排除功能不依赖名字怎么构造，别的字符（例如 `@`）同样可用。

**这是人的操作，并有审计。** 两个调用都要界面令牌（和页面其他写操作同样的检查）；智能体凭据（`Authorization: Bearer`）即使同时带着有效令牌也会被拒绝（403）；拒绝*任何* Bearer 是有意的，因为 LEVI 认识的 Bearer 凭据只有智能体的。Agent API 的任何能力、自动批准主体的任何调用（`auto.ALLOWED`）都做不了这件事。路由自己也做这个检查，不只靠服务的中间件，两道检查各自独立有效。实际上“人”指的是带界面令牌的请求，或经页面自己代理发来的请求；代理会给没有 `Origin` 头的本机请求自动加上令牌，所以本机任何进程不带 Bearer 直接 POST 到页面端口，也能以“人”的身份操作。页面其他写操作也是这样，不是本分支引入的。每次改动，每个片段在 `live/audit.jsonl` 写一行：`{"time", "principal": "local-human", "actor": "person", "tool": "episode.exclude" | "episode.restore", "dataset", "demo", "reason"?, "via"?, "decision": "completed"}`（在产品 LEVI 页面上操作时带 `"via": "product"`），不记录令牌。排除已排除的片段、恢复未排除的片段什么都不改，也不写审计。

**训练池**：实时工作区在池根目录之下时，它的镜像会像任何原始采集一样被登记，而同一次录制还会从镜像所链接的 rollout 目录再登记一次（同一指纹、同一组；rollout 目录是规范副本）。所以训练池不只看镜像：它在**被查询时**读实时工作区的数据集状态（不需要重新扫描），把整组当作已排除，和一个副本上的标签或留出标记对所有副本生效一样。训练池除了认镜像的目录，也按状态文件自己的 `source` 路径认 rollout 原件，所以扫描先于镜像、或源在镜像之后被替换（指纹变了，不再和镜像同组）时，原件也照样被排除；这些路径只和索引里的键比较，从不打开。被排除的录制不会被列出或计数（`facets.removed_in_live` 给出录制数，一个录制算一个，不管有几个副本；训练池页面把这个数显示为“在实时页面被删除的片段数”，数字不会悄悄变少），任何配方都会跳过它（预览的排除原因里是 `excluded_in_live`，不管配方本来会选哪个副本），导出在规划、运行和续跑时都会拒绝它（“removed on the live page”），所以计划冻结之后才做的排除也能拦住导出。只读取上次扫描在池根下找到的实时工作区、训练池自己的工作区，以及本 LEVI 实时页面正在显示或显示过的实时工作区（见[在产品 LEVI 里查看实时评测](#在产品-levi-里查看实时评测)）：上次扫描没见过的其他实时工作区不会被读，新起一个之后要重新扫描。

## 服务写的文件

| 位置 | 内容 |
| --- | --- |
| `~/.levi-live/status.json` | 状态文件（接口 C4） |
| `~/.levi-live/live.pid`、`live.lock` | 单实例锁和 pid 记录 |
| `<工作区>/live.toml` | 配置（只创建一次） |
| `<工作区>/live/effective.toml` | worker 读取的生效配置 |
| `<工作区>/live/datasets/<名字>.json` | 每个数据集的状态：各片段及状态（被人排除的带 `excluded`）、进行中的批次、上一批、判定 |
| `<工作区>/live/audit.jsonl` | 自动批准主体的审计日志，以及人排除或恢复每个片段的一行（轮转） |
| `<工作区>/live/worker.json`、`gate.json`、`vllm.json`、`service.json` | worker 进度、闸门、本服务启动的 vLLM、首次启动时间 |
| `<工作区>/live/gate.jsonl` | 闸门的每一次变化（轮转；见“历史与统计”） |
| `<工作区>/live/stats.jsonl` | 每个已标片段一条记录（轮转；见“历史与统计”） |
| `<工作区>/live/online.jsonl` | 每个在线判定请求一行，不存图像（轮转；见[在线判定](#在线判定接口-c5)）；`live/online-tmp/` 只在模型读取期间存放请求的图像 |
| `<工作区>/live/reports/` | 每个结束的评测会话一份报告：`<数据集>__<会话>.md`、`.zh-CN.md`、`.json`（见“统计与报告”） |
| `<工作区>/live/logs/` | `live.log`、`worker.log`、`ui.log`、`vllm-launch.log`（轮转） |
| `<工作区>/captures/<名字>/` | LEVI 登记的镜像采集 |
| `<工作区>/outputs/LEVI/…` | LEVI 自己的状态：视图、运行、已提交标注 |

## 历史与统计

**闸门历史（`live/gate.jsonl`）。** 闸门的状态或原因代码每变化一次就加一行：`at`（纪元秒）和 `time`（本地时间）、`from` 与 `to`（`{open, code}`；第一行的 `from` 是 null，服务写的最后一行 `to.code = service_stopped`）、`reason`、`idle`、`policy_up`、`policy_ports`（配置的端口）、`sessions`（各会话的 group、任务和状态，最多 8 个）。不含令牌和密钥。它按 `resources.log_max_mb` 轮转，保留 `resources.log_backups` 个旧文件；读取时跳过损坏的行。最近五次转换在状态文件里（`gpu.gate.history`），`levi live status` 以 `gate` 行打印。事后要回答“策略推理时闸门有没有关着”，看它。

**worker 日志。** 一个批次做完后，`logs/worker.log` 对批次里的每个片段写一行，方便 grep：`episode demo=demo_0003 ep=3 batch=2 temporal_wall=28.3s temporal_model=10.6s temporal_requests=2 temporal_tokens=21034 review_wall=6.0s ... segments=3 verdict=failure valid_events=0 gated=no state=done`。`*_wall` 是该阶段对整个批次的墙钟；模型的数字是这个片段自己的。`gated=1x/5.2s` 表示闸门让批次停了一次、共 5.2 秒。一个批次由多个 worker 进程做完时（重启、为策略服务器停过），墙钟和闸门数字跨进程累加：每个阶段结束时存进批次，所以只有被无预警杀掉的 worker 会丢它当前阶段的那一份。某个片段的行或记录写不出来，不影响其他片段。

**每个片段的统计（`live/stats.jsonl`）。** 批次做完后，worker 对批次里的每个片段（做完的或失败的）追加一行 JSON，schema 是 `levi.live.episode_stats.v1`，轮转方式同闸门历史。`levi.live.stats.read(live_dir, limit=None, since=None)` 按从旧到新返回记录，跳过损坏的行，缺的字段读作 `null`；请用它，不要自己写解析。字段名和单位是稳定的：可能新增字段，不会改名。所有时长的单位是秒，没能测到的值是 `null`。

| 字段 | 含义 |
| --- | --- |
| `schema`、`at` | `levi.live.episode_stats.v1`；这一行写入的纪元秒 |
| `dataset`、`demo`、`episode_index` | 实时数据集（`<group>__<task>`）、采集文件夹（`demo_0003`）、片段在数据集视图里的编号 |
| `session` | 该片段所属的评测运行 id（其 metadata 里的 `eval.run_id`） |
| `attempts`、`excluded` | 此前对这个片段失败了几次（第一次就成功为 0）；写记录时若人已排除该片段（可恢复）则为 `true`，通常是 `false`：运行中批次里的片段不能排除，被排除的片段也不会被标注 |
| `backfilled` | 由 `levi live stats backfill` 事后重建的记录为 `true`，否则 `null` |
| `batch.id`、`batch.size` | 该片段所在的标注批次：批次开始时间（纪元秒）和批次里的片段数；这两个字段出现之前写的记录里是 `null` |
| `episode.frames`、`episode.episode_seconds` | 帧数和片段自身的时长 |
| `timeline.to_mirror_s` | 从 `.complete`（片段结束）到镜像完成 |
| `timeline.to_plan_s` | 到该批次的时间片段计划生成 |
| `timeline.to_first_request_s` | 到这个片段的第一个模型请求开始 |
| `timeline.to_commit_s`、`timeline.to_verdict_s` | 到时间片段提交；到自动判定出来 |
| `timeline.completed_at`、`timeline.first_request_at` | 片段的结束时刻（`.complete`）和它第一个模型请求的开始时刻，都是纪元秒（没发过请求的片段和旧记录里是 `null`）：“会话内标注比例”由它们算出 |
| `model.requests.{coarse,refine,review,probe}` | 按种类的模型请求数：粗标、边界精修（一次或多次）、释放复核（每个问题一次）、请求开销校准（运行级的开销，记在批次的第一个片段上）。缓存命中不算请求 |
| `model.model_seconds.{coarse,refine,review,probe}` | 模型在这些请求上花的秒数（`probe` 不计时：`null`） |
| `model.tokens.{coarse,refine,review,probe}` | 按同样种类分开的、服务器报告的 token（`probe` 是校准开销，即 `probe_tokens`）；某种类没有报告用量的请求时为 `null`，旧记录里也是 `null` |
| `model.prompt_tokens`、`completion_tokens`、`total_tokens` | 服务器报告了用量的那些请求的 token 之和（`total_tokens` = prompt + completion；没有请求报告时三项都是 `null`）。校准探测和预留不在其中 |
| `model.probe_tokens` | 该批次请求开销校准的 token（运行级的开销，只记在批次的第一个片段上）；其余为 `null` |
| `model.reserved_tokens`、`model.unreported_steps` | 服务器没给用量的请求，LEVI 按预留额度记账：这些预留之和（不是实际花掉的 token；没有则 `null`）和这类请求的个数 |
| `model.images` | 发送的图片数 |
| `model.external_tokens` | 恒为 0：没有任何东西离开这台机器 |
| `gate.closed_wait_s`、`gate.interruptions` | worker 因闸门关闭而让路的秒数和次数，按这个片段所在的整个批次算（一个批次有多个片段，每个都带批次的数字） |
| `gate.vllm_wake_s`、`gate.vllm_cold_start_s` | 监督进程在启动 worker 之前 10 分钟内做的唤醒（约 0.75 秒）或冷启动的耗时。它记在做完的那个批次的第一个片段上；worker 没活可做、在等模型或等人、或失败时，会把它留给下一个 worker，超过 10 分钟的丢弃，所以不会记到几小时后的批次上。其余片段为 `null` |
| `result.state`、`result.reason` | `done`、`failed` 或 `mirrored`（之后重试）；没做完时的原因 |
| `result.segments`、`result.segment_labels` | 提交的时间片段数，以及按子任务 id 的计数 |
| `result.verdict` | 自动释放复核的 `{outcome, events, valid_events, undecided}`，或 `null`；终态感知规则下的判定另有 `rule` 和 `place_outcome`；最终状态规则（`final_state`）下另有 `rule`（`min_valid` 和带 `final_reading` 的 `basis` 在数据集状态的 `verdict` 里，不在这里） |
| `result.review` | `auto` 或 `human`（谁提交的时间片段） |
| `result.spec` | `{guideline, release_review, release_review_version, sha256}`：用到的文件和它们的哈希；判定没有记录复核规格版本时为 null |
| `result.provider`、`result.model` | 模型配置名和服务的模型 |
| `result.task_rewritten` | 判定引用的是 rollout 记录的原指令时为 `null`，否则是 `task_text` 或 `task_folder`，即匹配到的 `[judge.task_text]` 的键（[判定用的任务文字](#判定用的任务文字)）；后台标注关闭、只收在线结果的片段，用在线结果自带的 `task_rewritten` |
| `operator_label` | `{outcome, by}`（元数据写明片段怎么结束时另有 `ended_by`，取值 `budget` 或 `operator_key`）：来自 rollout 元数据的操作员标签（真值）（`success`、`failure`、`discarded` 或 `unlabeled`；`by` 为 `operator`、`key`、`timeout-adjudicated` 等），或 `null`（没有标签，或旧记录）。它从不属于 `result.verdict`；见[双标签](#双标签操作员和-agent) |

这个文件是本服务工作的记录，LEVI 自己从不读取，也不是训练数据。

**账本里的运行时间。** 运行账本的 `wall_seconds`（`cost-rise-*` 改进提议拿它比较）不含运行在等人、或为策略服务器让路的时间：一个释放复核运行停在 `waiting_for_review`、四十分钟后被取消，不再读作 2400 秒的运行。扣掉的总数是 `idle_seconds`，拆成 `waiting_for_person_seconds` 和 `stood_down_seconds`；准确规则见 `docs/AGENTS.md`。旧账本保持旧值，成本基线（取历史中位数）的历史里还有一部分是旧口径，所以一段时间内新旧运行会混着比较。

## 统计与报告

标注工作的定量记录：片段结束后每个阶段花多久、模型花了多少、GPU 门控挡了多少、产出是什么。所有数字都由 `levi/live/stats.py` 里的纯函数从 `live/stats.jsonl`（以及 `live/gate.jsonl`，和用来确定会话结束时刻的数据集状态文件）算出；页面、API、`levi live report` 和会话报告显示的是同一套数字。没测到的数字是 `null`，显示为“—”，从不显示成 0。

**哪条记录算数。** 一个片段可能有多条记录（重试过）。逐片段的数字（个数、延迟、每个片段的 token、结果）取每个片段**最新**的一条。按次数花掉的合计（token、请求数、门控和 vLLM 的数字）取**全部**记录，所以失败的第一次尝试按它实际的开销计入。

| 数字 | 定义 |
| --- | --- |
| 片段数 | 有记录的片段；`done`（已标注）、`failed`、`retrying`（状态 `mirrored`，之后重试）、`retried`（`attempts` 是此前失败的次数：已标注的片段 ≥ 1，失败的片段 ≥ 2，或有不止一条记录的片段）、`excluded`。**被人排除的片段**（数据集状态文件里有 `excluded`，或记录里 `excluded: true`）默认不计入任何数字和报告，除非要求 `include_excluded`（页面上的“包含已排除的片段”开关、`--include-excluded`）；回答里的 `excluded_demos` 说明这个范围内有多少已排除的片段。以状态文件为准，两个方向都是：记录是在没人能排除它之前写下的，被恢复的片段重新计入（状态文件读不到的数据集沿用记录自带的标志）。下面说的会话结束时刻同样不含被排除的片段 |
| 延迟 | `timeline.to_mirror_s`、`to_plan_s`、`to_first_request_s`、`to_commit_s`、`to_verdict_s`：个数、中位、p90、最大、均值。都是片段 `.complete` 之后的秒数。p90 是第 90 百分位（相邻名次之间线性插值） |
| 实时倍率 | 片段总秒数 ÷ 模型总秒数，只统计两者都有的片段。两边都取每个片段最新的一次尝试，这组数字都是这样（`throughput.model_seconds` 也是）。模型秒数是各模型请求耗时之和（粗标、精修、释放复核），不是墙钟时间：0.2 表示每 1 秒片段模型要用 5 秒，大于 1 表示标注得比片段本身的时长快 |
| 每墙钟秒标注的片段秒数 | 片段总秒数 ÷（最后一次判定（没有则提交）到达的时刻 − 第一个片段结束的时刻）。这段时间包含机器人还在运行的部分 |
| 模型开销 | 每个片段的请求数（`probe` 的校准请求属于批次而不是片段，不计入；它们的总数在按种类的表里）；每个片段的 token（均值、中位）和总数；提示词占比 = prompt ÷（prompt + completion），只统计服务器报告了拆分的记录；每个片段的图片数；外部 token（恒为 0）。**token 取服务器报告的数字**（只含它报告了用量的请求，`total_tokens` = prompt + completion）；校准开销（`probe_tokens`）、服务器没给用量的请求 LEVI 按预留额度记的账（`reserved_tokens`）以及这类请求的个数（`unreported_steps`）单独列出，从不加进去；这些字段出现之前的记录读作 `null`，completion 也绝不再用 total − prompt 推出来。没有任何步骤报告用量（或没有请求）的片段，`total_tokens` 是 `null` 而不是 0。数字是 NaN 或 Infinity 的记录不是合法 JSON，像其他损坏的行一样被跳过。按种类（`coarse`、`refine`、`review`、`probe`）：请求数、模型秒数、token 及各自的占比；token 占比是占 `tokens_total` 的比例，所以不在其中的 `probe` 没有占比。`probe` 是批次的请求开销校准，记在批次第一个片段上，不计时 |
| GPU 与门控 | `closed_wait_s` 和 `interruptions`：worker 因闸门关闭而让路的秒数和次数，**每个批次只算一次**（批次由 `batch.id` 确定；没有它的记录，写入时间相差不超过 5 秒且数字相同的算同一批）；vLLM 唤醒和冷启动（次数、合计、最长），记在它们所服务批次的第一个片段上；`gate_window`：第一个片段结束到最后一条记录之间闸门关闭的秒数和占比、关闭次数和 `unknown_s`，来自 `live/gate.jsonl`。闸门从开（或未知）变成关时才算一次关闭，所以关闭期间只是原因代码变化不算又一次；没有状态的转换（服务停止）会截断它之前的区间：到下一次转换之前什么都不知道，这段时间记入 `unknown_s`，不算关闭时间（策略推理时闸门关闭，所以这是不允许标注的时间，不是 worker 等待的时间）。`closed_wait_s` 近似对应运行账本里的 `stood_down_seconds`（两者在不同地方测量，略有差别；账本的 `wall_seconds` 已经扣掉它，以及运行等人的时间，见“账本里的运行时间”）；这些统计不读账本，这里所有墙钟数字（`span_s`、各延迟）都是包含这些等待的实际经过时间。vLLM 睡眠没有任何地方记录，显示“未记录” |
| 会话内标注比例 | **首个模型请求发生在所属评测会话最后一个片段结束之前**的片段占比：衡量标注有多“实时”。会话的结束时刻取它各片段 `timeline.completed_at` 的最大值，数据集状态里已知但还没有记录的片段也算进去（还在进行的会话不会显得比实际更实时）。会话的最后一个片段在分母里但永远不可能在分子里（它的请求不可能早于它自己的结束），所以 n 个片段时这个比例最大是 (n−1)/n：只有一个片段就是 0 %。没发过请求的片段算“不在会话内”。被人排除的片段不会改变会话的结束时刻，除非要求 `include_excluded`。没有会话 id（`eval.run_id`）或没有 `completed_at`（旧记录）的片段不进分子也不进分母；一个都不剩时显示“—” |
| 结果 | 时间片段总数和每个片段的数量（均值、范围、直方图）、各标签的计数、自动成败判定（`success`、`failure`、`none`）及未决数、谁提交的时间片段（`auto`、`human`） |
| agent 与操作员对照 | `agreement`：在操作员判为成功或失败的片段上，agent 标签对照操作员标签（真值）：矩阵、`judged`、`agree` 和 `rate`、`rate_undecided_as_failure`、`false_success` 和 `missed_success`（各为 `{n, of, rate, wilson95}`）、`undecided`、`no_agent`、两边的成功率；没有操作员标签时 `pairs` 为 0。每个会话：`pairs`、`agree`、`judged`、`false_success`、`missed_success`、`operator_success`；逐片段和 CSV：`operator`、`agreement`。页面的表只在有数据时显示这些列，报告也只在那时加这一节。定义见[双标签](#双标签操作员和-agent) |

**会话报告。** 评测会话结束（会话文件是 `stopped`、`finished` 或已崩溃，或被更新的会话取代），且它的每个片段都已标完或放弃（没有 `mirrored`、`annotating` 或在队列里等待的）后，服务写出 `<工作区>/live/reports/<数据集>__<会话>.md`（英文）、`.zh-CN.md` 和 `.json`。每 30 s 检查一次，批次运行期间不检查，在独立的线程里做（历史再长也不会拖慢 GPU 让出和 gate 文件的刷新），每次检查最多生成两份报告。只考虑最新的 `resources.report_keep` 个已结束会话，`reports/index.json` 记录每份报告的 signature（记录数、最后一条记录的时间、被排除的片段数）和为了不超过上限而被删除的报告名：报告是否最新由索引和对记录的一次遍历决定，没有变化就不生成，被删掉的旧报告不会再写回（`levi live report` 仍可随时生成任何会话的报告）。报告内容：设置（当时生效的模型配置、管线、vLLM 和 GPU 设置，取自生效配置，不含路径、端口或密钥；标注指南和释放复核规格及各文件哈希的前 12 位；会话文件里的策略 config 和 checkpoint 文件夹名）、事实、延迟和开销表、GPU 与门控数字、每个片段一行的明细、与同一数据集上一份报告的对比、口径说明。写入是幂等的：记录没变的报告不会重写，迟到的记录会就地更新报告，每个文件先渲染好、再写 `.partial` 并改名，所以失败时上一份报告保持完整；只保留最新的 `resources.report_keep` 份（默认 20，按会话最后一条记录的时间）。报告里没有令牌和路径。逐会话表不显示 gate 窗口。跨两个会话的批次，它的 `closed_wait_s` 会同时出现在两个会话的行里，所以各行之和可能大于总数（总数里每个批次只算一次）。

`levi live report [--dataset X] [--session Y] [--format md|json|csv] [--lang en|zh] [--out PATH]` 按工作区里的文件为任意范围生成同样的报告（服务不必在运行）；`csv` 是逐片段表。

**回填。** `levi live stats backfill [--dataset X] [--dry-run] [--json]` 为数据集状态里是 `done` 或 `failed`、但没有记录的片段（在 `stats.jsonl` 出现之前标注的）补记录。它重建还留得下来的内容：片段的各个时刻、尝试次数、时间片段数和判定来自数据集状态；模型请求、token 和耗时来自工作区存储里的运行日志（只读方式打开）；时间片段标签来自已提交的变更集。记录带 `backfilled: true`，`at` 是它所描述的那个时刻。被人排除的片段也会回填，带 `excluded: true`（开销是真的，记录保留，统计默认不计入）。数据集名可能带根标记（`<group>__<task>__at__<root>`）：`--dataset` 要写成 `levi live status` 显示的名字。没有任何文件保存的内容保持 `null`：当时生效的设置（标注指南、模型配置、模型）、门控等待、vLLM 唤醒耗时、批次、片段时长，以及批次的请求开销校准（`probe`）。不估算，也不拿今天的配置充数。它从不改动已有的行，已经有记录的片段会跳过，所以运行两次不会多出东西。存在但读不了的存储（被锁、损坏）算失败，不算空日志：该片段会跳过并报告（退出码 1，dry-run 里写 “READ FAILED”），之后再运行就能补上；完全没有日志的片段照常回填，模型字段为 null。`--dry-run` 列出每个片段，以及 schema 里每个字段的取值来源（或“没有记录”），什么也不写。

## 状态文件（接口 C4）

`~/.levi-live/status.json`，每 `heartbeat_s`（4 秒，≤ 5 秒）原子重写。评测客户端只有在**全部**满足时才认为服务可用：`schema` 以 `levi.live.status.` 开头；`updated_at`（纪元秒）不到 15 秒；`pid` 存活；`accepts_sessions` 为真；`state` 是 `idle active annotating gpu_wait` 之一；`watch_roots`（绝对路径）中有一个等于或包含客户端的 `--rollout-root`（或被它包含）。否则客户端退回手动标注。`accepts_sessions` 在 `starting` 以及 `stopped`/`error` 时为假；`gpu_wait`（有活在等模型：闸门关闭、vLLM 正在启动或在睡眠）算可用。 `gpu.free_mib` 是最近一次 `nvidia-smi` 的读数，**只在不到 30 秒内才给出**，否则为 `null`（睡眠中的 vLLM 有意不去探测，睡下之前的读数，例如 2254 MiB，不是现在的空闲显存）；`gpu.free_mib_at` 是读数的时间。`gpu.idle_since` 是 vLLM 开始闲置的时间（有活、正在载入或本就该常驻时为 `null`），`gpu.prewarm` 说明它是否以预热方式启动；`levi live doctor` 会读这两个字段。`attention` 在服务放弃启动 vLLM、需要人（`levi live resume`）时设置，此时标注暂停，但不改变 `accepts_sessions` 和 `state`。**`labelling_paused`** 为 `null`，或在“没有标注、且原因不会自己消失”时为 `{code, reason, since}`：`vllm_failed`（放弃启动 vLLM，`levi live resume`）、`vllm_error`（启动失败、正在退避）、`insufficient_vram`（空闲显存连最短上下文也装不下）、`policy_large`（策略服务器占用超过 `gpu.policy_budget_mib`：改用 `.22`；立即报告，按服务器的显存判断，不管闸门或评测在做什么，是一次稳定的暂停）、`unknown_client`（没有会话为之作证的策略服务器让闸门关了 `gpu.unknown_client_pause_s` 以上），以及持续 `gpu.blocked_pause_s` 之后的 `vram`（睡眠的 vLLM 唤不醒，或启动没有足够显存）、`lock`（别的 agent 持有 GPU 锁）、`external_busy`（`vllm.port` 上有别人的 vLLM）和 `gpu_not_free`（vLLM 已停但它的进程仍占着显存，所以锁保留）。策略推理时闸门关闭、策略服务器还在加载，都是正常等待，不设置它；它也不改变 `accepts_sessions`。`loop_at` 是主循环上次 tick 的时间（`updated_at` 来自单独的心跳线程，循环卡住时它仍然新鲜；`levi live doctor` 在 `loop_at` 超过 5 分钟时告警）。`frontend` 说明服务启动的页面/核心 API 是否真的起来（`levi live start --daemon` 失败时打印原因并以退出码 2 返回，标注继续运行）。新增字段：数据集行的 `stuck`、`source_changed`、`review_runs_open`、`awaiting`，会话的 `root`、`reset_wait_s`、`waiting_reset_since`、`label_mode`（`dual_label`、`unattended` 或 null），顶层的 `pipeline`（`temporal`、`anchored`、`anchored_spec`），闸门代码 `episode_imminent`，决策代码 `evaluation_active`、`standby_settling`、`prewarm_waiting_for_policy`、`gpu_not_free`，以及顶层的 `online_judge`（`null`，或启用在线判定时的 `{url, spec, spec_version, ready}`，见[在线判定](#在线判定接口-c5)；只是新增，其他字段不变，所以上面客户端的可用性检查不受影响）。完整 JSON 形状见 [LIVE.md](LIVE.md#status-file-interface-c4)。

## 服务读取的机器人侧接口

- **C2 会话**：`<根>/.eval_sessions/<group>__<task_folder>.json`，schema `levi.eval.session.v1`（`state`：`standby homing running waiting_reset fault stopped finished`）。10 秒没有更新且进程已不在，或超过一小时未更新（回收的 pid 不能让它复活），会话算 `crashed`。`waiting_reset_since`（本次等待第一次进入 `waiting_reset` 的纪元秒；闸门的提前量从它算起，旧客户端不写时从本服务第一次看到该会话算起；会话在机器人上或在两集之间时，监督进程至少每秒看一次）、`run_id`（被续跑的评测轮）、`episode.no`（本次会话的集序号）、`episode.counted`（该轮累计有效片段数）原样传给页面。`incomplete_*` 的中止原因：`fr3_fault`、`user_quit`、`interrupted`、`process_killed`（只有 `fr3_fault` 把数据集标成故障；所有原因都按类计数）。
- **C3 FR3 健康**：`fr3_health.json`，schema `levi.fr3.health.v1`（优先 `updated_at_epoch`，否则 `updated_at`）。缺失或超过 `fr3.stale_s` 是**离线**，不是红灯。`red_light` 还包括：5 秒没有实时机器人状态、5 秒连不上 controller manager、没有 Franka 硬件组件；服务只显示，不据此行动。

## HTTP API（`/api/levi/live/*`）

实时工作区的核心和产品 LEVI 的核心都提供这些路由，后者读取它找到的实时工作区（见[在产品 LEVI 里查看实时评测](#在产品-levi-里查看实时评测)）。找不到时所有 GET 回答 `{"enabled": false, "reason"}`（`not_configured`、`not_live` 或 `product_workspace`），POST 回答 404。GET 路由不改任何东西，任何路由都不返回令牌。唯一会改东西的是排除和恢复片段的路由（见[上文](#排除片段可恢复)）。

| 路由 | 回答 |
| --- | --- |
| `GET /status` | `{"enabled", "embedded"（由不是实时工作区的 LEVI，即产品 LEVI 提供时为 true）, "live_ui"（服务运行着自己的页面且页面正常时为其地址，否则 null）, "workspace_name"（实时工作区的目录名）, "alive", "age_s", "service": <status.json 或 null；在产品 LEVI 上去掉其中的 `workspace` 和 `config` 路径>, "faults": [{"dataset", "reasons": []}], "fr3_red", "blocked_runs": {"count", "waiting", "needs_person"}}`。`alive` = pid 存在、`updated_at` 不到 15 秒、状态不是 `stopped`。 |
| `GET /sessions` | `{"enabled", "sessions": [ {会话字段, "dataset", "fault"} ], "fr3": {…}, "active"}`，直接读机器人侧文件（≤ 64 个）。 |
| `GET /datasets` | `{"enabled", "datasets": {名字: 行}}`（`status.json` 里的行）。 |
| `GET /datasets/{name}` | 数据集详情：`repo_id`（实时工作区目录里登记后的数据集 id，否则 null）、`linked_repo_id`（本 LEVI 不是实时工作区且关联了它时，本 LEVI 里打开它用的 id `local/live.<名字>`，否则 null）、`embedded` 和 `live_ui`（同 `/status`）、任务文本、各状态计数（被排除的片段不计入）、`total_demos`（数据集里的片段数）、片段列表（最新在前，≤ 200；每项有 `excluded`（null）、状态、集序号、`run_id`、尝试次数、时间片段数、提交时间、自动判定 `verdict`：`outcome/events/valid_events/undecided/spec/review: "auto"/evaluated: false`，另有 `rule/place_outcome/closes_after_last_valid/min_valid`，默认规则下为空；以及 `spec_version`）、`excluded_count` 和 `excluded_demos`（被排除的片段，行的格式相同，带 `excluded: {at, by: "person", reason}`，最新在前，全部列出，不在 200 处截断）、`incomplete`（含按原因计数）、进行中的批次、上一批、`last_error`；每个片段另有 `operator_label`（`{outcome, by, source}` 或 null）和 `agreement`（`yes`、`no`、`undecided`、`no_agent` 或 null），整个数据集有 `agreement`（覆盖全部保留的片段，不只是列出的 200 个）和 `pipeline`（`{"temporal"}`）。未知名字返回 404。 |
| `GET /stats?dataset=&session=&since=&limit=100&offset=0&include_excluded=false` | `{"enabled", "schema": "levi.live.stats.v1", "generated_at", "scope", "datasets": [有记录的数据集名], "summary": {…各项聚合，见“统计与报告”}, "sessions": [每个（数据集，会话）一行，最新在前，≤ 200], "sessions_total", "episodes": {"total", "offset", "limit", "rows": [每个片段一行，最新在前，limit ≤ 500]}}`。`dataset` 和 `session` 只能是普通名字（否则 400）；`since` 是纪元秒。不含路径、令牌或密钥。 |
| `GET /stats/export?format=csv\|json\|md&dataset=&session=&since=&lang=en\|zh&include_excluded=false` | 同样的统计，作为下载（`Content-Disposition: attachment`）：`csv` 每个片段一行（以 `=`、`+`、`-`、`@`、制表符或回车开头的文本单元格前面会加一个撇号，电子表格不会把它当公式执行），`json` 含全部片段的完整数据，`md` 一份可读报告。 |
| `GET /audit?limit=50` | 自动批准主体的审计记录，最新在前，≤ 100 条，每条含 `tool`、`decision: allowed/refused`、`run_id` 等。人排除或恢复片段的行是 `principal: "local-human"`、`actor: "person"`、`tool: "episode.exclude"` 或 `"episode.restore"`、`dataset`、`demo`、可选的 `reason`、可选的 `via: "product"`（在产品 LEVI 页面上操作）、`decision: "completed"`。 |
| `POST /datasets/{name}/exclude` | 请求体 `{"demos": ["demo_0003", …], "reason": "…"?}`（1 到 500 个名字，原因最长 300 字符）。排除这些片段（要么全部成功，要么都不改）。回答 `{"enabled", "dataset", "changed": [...], "unchanged": [...]（本来就已排除）, "counts", "excluded_count", "review_runs_open", "review_hidden": [...]（片段已全部排除、不计入的待复核运行）}`。404：数据集或片段不存在（会点名），或不是实时工作区；409：片段在进行中的批次里（“being labelled …”），或从未纳入数据集（被拒收、卡住）；401/403：不是人在操作（见上）。 |
| `POST /datasets/{name}/restore` | 请求体 `{"demos": [...]}`。把已排除的片段放回来；回答同上（`review_hidden` 是恢复之后仍不计入的运行）；没排除的片段在 `unchanged` 里。片段不存在返回 404。 |
| `POST /datasets/{name}/demos/{demo}/exclude`、`…/restore` | 对单个片段做同样的事（前者可带可选请求体 `{"reason"}`）。 |

在实时工作区自己的页面（`live_ui`）上用 `repo_id`（`local/<名字>`）链接到查看器；判定对应的运行也在那里按 `run_id` 打开。产品 LEVI 自己的查看器里没有实时数据集。

## 出问题时

- **FR3 故障**：客户端中止该集（`incomplete_NNNN`，`abort_reason: fr3_fault`），会话进入 `fault`。页面把数据集标为故障。被中止的 rollout 不会被标注。操作员排除故障后客户端续跑。
- **服务或 worker 崩溃 / `levi live stop`**：进行中的批次记在数据集状态里。下次启动接着做；LEVI 自己的运行记录保存了已完成的片段，所以不会重复标注或提交（提交用幂等键）。收到 `SIGTERM` 的 worker 会暂停运行并等租约释放；被 `SIGKILL` 杀掉的会留下租约，3 分钟后过期。
- **策略推理时有人在页面点“运行”“继续”或任务控制台的推进**：实时工作区的核心读 `live/gate.json`，闸门关闭时拒绝（`409`，“评测正在推理，请稍后再试”）。闸门打开时启动的运行也会停：核心在**每一次**模型请求之前都读闸门（只 stat 工作区标记，小文件最多每 0.2 秒读一次；所有发请求的入口走同一个检查，`gpu.require_free`），闸门关着就让请求以 `GpuBusy` 失败，运行停在 `blocked`（带 `blocked_by: gpu` 和 `blocked_gate: live`，这是闸门自己的标记）。worker 自己的运行不受它约束（它自己会让路）。闸门文件超过 20 秒没人刷新（监督进程没了）时，worker 一律当作关闭（没人再管它，而且之后可能出现了策略服务器）。人只有在文件最后写的是 `idle`（没有策略服务器、没有评测：没有要保护的东西）**并且**文件里列的策略端口此刻都没有在监听（读 `/proc/net/tcp`，不连接）时才放行；否则页面提示闸门文件已过期、请确认监督进程是否在运行。监督进程活着时，文件每次变化都重写、至少每 4 秒一次（vLLM 的长时间停止等待期间也刷新），所以空闲时的标注不会因此停摆；正常停止的服务会删除这个文件。

  **这样的运行会自己继续。** 核心里有一个小的守护线程（`levi/live/resumer.py`；只在实时工作区存在，空闲时每秒一次 stat 加一次小文件读取）每秒读一次 `live/gate.json`，调用 `Workbench.launch`，也就是点“继续”最终走的那条路，所以启动时的所有检查依然有效（计划已批准、租约空闲、没有已批准未提交的草稿）。它放在核心里，因为运行、租约和执行线程都在那里，闸门也本来就在那里按请求读取；它不是自动批准者，也不是经过分发器的调用，所以自动批准者“只能处理自己规划的运行”这条规则丝毫没放宽。它只恢复：带闸门标记的 `blocked` 运行（模型错误、校验失败、老师的待决定、GPU 守卫的拦截、人主动暂停或取消的运行都没有这个标记，暂停和取消绝不会被它撤销）；**人**规划的运行（主体 `local-human`，也就是 LEVI 页面；worker 自己的运行归 worker，连接的 agent 的运行归它自己）；没有待处理的暂停/取消、也没有试点片段在等人审核的运行。并且只有当闸门文件**新鲜且开着**（过期的文件对它永远不算“开”，不管它最后写的是什么；文件不存在也不算），并且**已连续开着 `gpu.resume_stable_s`**（3 秒：`episode_imminent` 的窗口在这之前就被提前量关上了）。同一个运行在闸门的每一次打开期间最多恢复一次，每秒最多恢复一个运行。运行被闸门连续打回 `gpu.resume_max_bounces`（3）次、中间没有进展（完成片段或结算了 token；`requests` 不算，它在预留调用时就加了），就留给人（在页面点“继续”），状态里会写明。每次恢复、启动失败和放弃都会在 `live/audit.jsonl` 里留一行（主体 `live-resume`，工具 `runs.resume`，决定 `auto_resumed`/`failed`/`given_up`，带运行 id、运行的原因和闸门的 `open`/`code`/`updated_at`/`open_for_s`）。同一个线程写的 `live/blocked_runs.json` 列出被闸门拦住的运行；`/api/levi/live/status` 里是 `blocked_runs: {count, waiting, needs_person}`：`waiting` 会自己继续，`needs_person` 不会（不是人的运行、有暂停待处理、试点待审核、已放弃、或用 `resume_max_bounces = 0` 关闭了自动恢复）。人在线程读取和 launch 之间点“暂停”以人为准：`Workbench.launch(expect=…)` 在自己的事务里重新检查（审计为 `skipped`），每次恢复也会在该运行的事件里留一条 `auto_resumed`。核心重启后在启动时扫一遍运行记录，找回被闸门留在 blocked 的运行；在核心以外的进程里被拦的运行只有那时才会找到，人自己点“继续”也要等运行有进展才重置放弃计数（已知局限）。GPU 守卫（`inference.gpu.Watch`）跳过这些运行。
- **等人**（自动批准关闭）：等待（`awaiting`：计划或草稿、哪个运行）存进数据集状态；重启的监督进程会读回它，不为它启动 vLLM 或 worker；万一 worker 还是被启动了，它只读存储就能发现计划未批准或草稿未提交，不需要模型。人批准、提交或拒绝、取消了运行，或草稿不在了，等待就结束。只有在运行真要执行之前才要求模型在线。
- **期间有人提交了该数据集**：计划的基线过期；worker 取消该运行并重新规划。
- **模型服务失败**：运行阻塞，worker 退出，监督进程按退避重试（30 秒起翻倍到 10 分钟）；失败的片段消耗一次尝试（`max_attempts`）。vLLM 自己起不来的情况见上文（退避，然后 `levi live resume`）。
- **worker 找不到事可做**，而监督进程以为有一批要做（它为镜像不了的片段启动了 worker）：不再每半秒重启一次，监督进程等 2、4、8 …… 秒（最多 `poll_idle_s`）再起下一个，只在每次加倍时写一行 `nothing to do (n in a row); next try in … s`，而不是每次启动都写；原因是数据集的源目录（不存在或为空）时，在它的状态里标 `unavailable`（页面上 `available: false`，带 `unavailable_reason`），目录里有东西之前不再排它。已经镜像的片段不会丢。
- **永远完不成的片段**（合成失败留下的 raw 采集、客户端写到一半死了）在 `watch.stuck_s` 之后变为 `stuck`：计数、由 `levi live doctor` 列出，只有它再变化才会重新检查。
- **镜像之后源被替换**（片段删掉后以同一编号重写，或标记之后 `metadata.json` 被替换）：标 `source_changed`；还没标注的会重新镜像；已标注的保留标记（标注的是旧内容）。
- **`levi live doctor`** 打印服务各进程的 RSS/线程/nice、GPU 占用、vLLM 状态、磁盘和运行缓存大小、队列、FR3 状态、警告（监督进程超预算、主循环 5 分钟没有 tick、进程不在 nice 19、vLLM 开着却没事做（闲置超过 `idle_timeout_s` + 120 秒；预热的、还在载入的、刚被唤醒的都不报）、vLLM 起不来、孤儿 vLLM、GPU 锁不可用、上面的配置检查（vLLM 脚本缺失或不可执行、pid 目录不可写、锁文件建不了、监视根目录不存在、路径指向别的用户的家目录、工作区路径太长放不下核心的 socket）、磁盘不足、复制而非链接、stuck 片段、被替换的源、状态文件过期、某个服务进程占用的打开文件数超过上限的一半：文件描述符泄漏）和提示（vLLM 冷启动 45–70 秒）。

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
