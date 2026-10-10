# 上机向导：配方与状态探针

[English](SETUP_WIZARD.md) | 中文

LEVI 可以引导操作者准备一次真机会话：下一步运行哪条命令、它会动到什么、它要启动的组件是否已经健康运行。本页说明目前已有的两部分：**配方注册表**（把每一步和本机操作手册对应起来）和 `GET /api/levi/setup/status` 背后的**只读状态探针**。两者都不启动、不停止、不连接任何东西。

## 配方

一条配方对应本机操作手册（一个 Markdown 文件）里的一段摘录，并写明它会动到什么、界面可以怎样提供它。配方文件里有本机的路径、地址和序列号，所以放在仓库之外：用 `LEVI_SETUP_RECIPES` 指向配方文件，用 `LEVI_SETUP_DOC` 指向手册。

```toml
version = 1

[[recipe]]
id = "R-CHK-3"
title = "GPU、进程和端口快照"
risk = 1            # 1 只读诊断，2 LEVI/vLLM/记录器服务，3 机器人控制栈或策略服务，4 会让机器人动
ui = "native"       # native | execute | copy | link
requires = []      # 必须先完成的配方编号
preconditions = ["无"]

[recipe.source]
section = "1"       # 手册的节号
heading = "启动顺序与终端"   # 记录时的节标题（标题改了也算漂移）
block = 1           # 该节的第几个代码块，从 1 开始；不写 = 整节正文
pick = [4, 6]       # 可选：代码块内的行范围
lines = [58, 60]    # 记录时所在行（仅供参考）
sha256 = "<规范化摘录的 64 位十六进制哈希>"
# text = "..."      # 可选：记录时的摘录原文，漂移时用来显示差异

[recipe.touches]
robot = false
gpu = false
moves = false
commands_robot = false
listens = []        # 它会监听的端口
connects = []       # 它会连接的端口
```

**安全规则（违反任何一条，校验器拒绝该配方并把它排除）：**

- ③④类配方永远不是 `execute`，也不是 `native`，只能是 `copy` 或 `link`。
- 唯一例外是只加载模型的③类策略服务（`kind = "policy_server"`）：`touches.robot`、`moves`、`commands_robot` 都为 false，并带有 `confirm = { required = true, decision = "<谁、何时允许>" }` 时，可以标为 `execute`。目前 LEVI 里没有任何东西会执行它。
- `moves = true` 必须是④类；`commands_robot = true` 必须是③或④类。
- 会连接机器人侧端口（5000、5001、5100、7470、8000）的配方只能是 `copy` 或 `link`：LEVI 从不连接这些端口。

**漂移。** 哈希覆盖的是规范化后的摘录：Unicode NFC、去掉行尾空格、去掉首尾空行；其他任何改动，哪怕一个字，都算漂移。检查按节号和代码块序号定位摘录，不按行号：

| 状态 | 含义 |
| --- | --- |
| `ok` | 摘录未变（位置变了时标 `shifted`） |
| `drift` | 文字或节标题变了：有人复制这条命令之前要先复核（配方记录了 `text` 时给出差异） |
| `moved` | 同样的文字在别处（代码块换了顺序、节重新编号）：更新配方的 source |
| `missing` | 节、代码块或所选的行不存在了 |
| `ambiguous` | 同一个节号出现了不止一次 |
| `doc_error` | 手册无法可靠解析（有代码块没有闭合） |

```bash
levi setup recipes check --recipes site/setup-recipes.toml --doc setup.md    # 退出码 0 / 1（有问题或漂移）/ 2（读不了文件）
levi setup recipes check --json
levi setup recipes excerpt --doc setup.md --section 1 --block 1 --pick 4 6  # 打印哈希和行号，用来写配方
```

两条命令都只读这两个文件。漂移后是否更新配方文件由人决定（手册另有维护流程）。

## 状态探针

`GET /api/levi/setup/status`（LEVI API 的普通本机鉴权：网页界面令牌；LEVI Agent 凭据会被拒绝）返回本机的一次只读采样。每一部分都有 `state`；读不到的部分标 `unknown`（附 `detail`），不影响其他部分。

| 部分 | 来源 | 从不 |
| --- | --- | --- |
| `ports` | `/proc/net/tcp` 和 `tcp6` 中 5000、5001、5100、7470、7860、7861、7880–7882、8000、8100（以及实时服务配置里的策略、vLLM、判定端口）的 LISTEN 行；所属进程从 `/proc/<pid>/fd` 找（只限同一用户，找不到时 `owner_known: false`），只给短进程名 | 连接任何端口，包括 LEVI 自己的 |
| `gpu` | `nvidia-smi --query-gpu` 和 `--query-compute-apps`；进程按它或它的父进程监听的端口命名，否则为 `other` | 创建 CUDA 上下文 |
| `gpu_locks` | `/proc/locks` 按锁文件的设备号和 inode 匹配（`LEVI_GPU_LOCK_FILE`、实时服务的 `gpu.lock_file`）：`held`（附持有者）、`free`、`absent` | 加锁 |
| `live` | 实时服务的 `status.json`：是否存活、vLLM 睡/醒、GPU 门、准入决定、`labelling_paused` | 请求 vLLM 或实时核心 |
| `host` | `/proc/loadavg`、`/proc/stat`（两次采样之间的忙碌百分比）、`/proc/meminfo`（内存、swap）、`/proc/pressure/{cpu,memory,io}` | |
| `ros` | 最近 12 个 `ros2_control_node_*.log`（每个只读最后 256 KiB）近一小时内的计数：`comm_violation`、`cartesian_reflex`、`motion_generator`、`reflex_other`、`overrun`、`overrun_warn`、`error` | 运行 ROS |
| `fr3_health` | 实时服务的 `fr3.health_file`，读法与实时页相同（`ok`、`red`、`offline`、`missing`）；没有配置时为 `unknown` | |
| `disks` | 产品工作区、实时工作区、其 rollout 根目录和临时目录的剩余空间，只给标签 | 返回路径 |
| `recorder` | 诊断记录器在 `LEVI_FR3_RECORDER_DIR`（或其下 `data/`）里的 `recorder.pid`（与进程启动时间核对）和 `status.json`：`running`、`stopped`、`unknown` | 启动或停止它 |
| `guard` | 真机静默守卫：机器人侧进程（`ros2_control_node`、`franka_server`、`run_robotiq_client`、`policy_server`、`serve_policy`，按命令名识别，从不返回命令行本身）、压力 avg10 超过 CPU 25%、内存 5%、I/O 50% 或 swap 超过 50%、实时服务的门（`robot_quiet`）、`gpu.quiet_states`、`online.pressure_avg10_max`、产品正在运行的作业。`quiet` 为 `true`、`false` 或 `null`（无法判断）。FR3 健康文件显示手臂控制器已激活时，如果还有产品作业、swap 在用或主机压力过高，就给出 `banner` 黄色提示 | |

ROS 日志目录取 `LEVI_ROS_LOG_DIR`，其次 `ROS_LOG_DIR`，再次 `~/.ros/log`。记录器目录取 `LEVI_FR3_RECORDER_DIR`（未设置时记录器显示 `unknown`）。

**采样。** 后台从不采样。请求到来时才采样；2 秒内的下一个请求拿同一份结果（`cached: true`、`age_s`），并发请求等同一次采样，不各自再采。开销大的部分有更长的间隔和上限：查找套接字所属进程的全量扫描最多每 30 秒一次（已知所属进程只做低成本复核），机器人进程扫描每 5 秒，ROS 日志每 5 秒（文件没变就不重读），磁盘每 10 秒；最多扫描 4096 个进程、每个进程 4096 个文件描述符；`nvidia-smi` 超时 4 秒。返回内容不含命令行、环境变量、令牌或路径。
