# 上机向导：配方与状态探针

[English](SETUP_WIZARD.md) | 中文

LEVI 可以引导操作者准备一次真机会话：下一步运行哪条命令、它会动到什么、它要启动的组件是否已经健康运行。本页说明目前已有的两部分：**配方注册表**（把每一步和本机操作手册对应起来）和 `GET /api/levi/setup/status` 背后的**只读状态探针**。两者都不启动、不停止、不连接任何东西。**marvel0** 页面建立在探针之上，见“状态探针”一节末尾：状态接口是只读的，页面的策略服务器面板只启动和停止一个策略服务器单元，不做别的。

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
# context_sha256 = "..."  # 可选：整节正文的哈希，代码块周围的说明文字变了（例如新加了警告）也算漂移

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
- 唯一例外是只加载模型的③类策略服务（`kind = "policy_server"`）：带有 `confirm = { required = true, decision = "<谁、何时允许>" }` 时，可以标为 `execute`。目前 LEVI 里没有任何东西会执行它。
- 所有策略服务配方（不论 `ui`）都必须自己写明 `touches.robot`、`moves`、`commands_robot` 为 `false`（不写算未声明，会被拒绝），不连接任何端口，也不监听 5000、5001、5100、7470。
- 任何 `execute` 配方都必须 `touches.robot = false`，且不监听机器人侧端口（只有策略服务可以监听 8000）。
- `moves = true` 必须是④类；`commands_robot = true` 必须是③或④类。
- 会连接机器人侧端口（5000、5001、5100、7470、8000）的配方只能是 `copy` 或 `link`：LEVI 从不连接这些端口。
- `requires` 指向不存在或被拒绝的配方、或指向因此被排除的配方时，该配方也被排除；处在 `requires` 环上的配方全部排除。检查会逐条写明原因。

**漂移。** 哈希覆盖的是规范化后的摘录：Unicode NFC、去掉行尾空格、去掉首尾空行；其他任何改动，哪怕一个字，都算漂移。检查按节号和代码块序号定位摘录，不按行号：

| 状态 | 含义 |
| --- | --- |
| `ok` | 摘录未变（位置变了时标 `shifted`） |
| `drift` | 文字或节标题变了：有人复制这条命令之前要先复核（配方记录了 `text` 时给出差异） |
| `moved` | 同样的文字在别处（代码块换了顺序、节重新编号）：更新配方的 source |
| `missing` | 节、代码块或所选的行不存在了 |
| `ambiguous` | 同一个节号出现了不止一次 |
| `doc_error` | 手册无法可靠解析（有代码块没有闭合） |

手册按 UTF-8 读取（忽略 BOM），只按换行符分行，所以行号与编辑器一致。节号是标题开头的数字，每段最多三位（`2.2`、`10`；`## 2026 notes` 是标题，不是节号）。只有缩进不超过三个空格的代码围栏算代码块：缩进更多的（例如在列表里）当作正文，不能用 `block` 引用。

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
| `fr3_health` | 实时服务的 `fr3.health_file`，读法与实时页相同（`ok`、`red`、`offline`、`missing`）；没有配置或不是普通文件时为 `unknown` | |
| `disks` | 产品工作区、实时工作区、其 rollout 根目录和临时目录的剩余空间，只给标签 | 返回路径 |
| `recorder` | 诊断记录器在 `LEVI_FR3_RECORDER_DIR`（或其下 `data/`）里的 `recorder.pid`（与进程启动时间核对）和 `status.json`：`running`、`stopped`、`unknown` | 启动或停止它 |
| `guard` | 真机静默守卫：机器人侧进程（`ros2_control_node`、`franka_server`、`run_robotiq_client`、`policy_server`、`serve_policy`，按命令名识别，从不返回命令行本身）、压力 avg10 超过 CPU 25%、内存 5%、I/O 50% 或 swap 超过 50%、实时服务的门（`robot_quiet`）、`gpu.quiet_states`、`online.pressure_avg10_max`、产品正在运行的作业。`quiet` 为 `true`、`false` 或 `null`（无法判断）。FR3 健康文件显示手臂控制器已激活时，如果还有产品作业、swap 在用或主机压力过高，就给出 `banner` 黄色提示 | |

ROS 日志目录取 `LEVI_ROS_LOG_DIR`，其次 `ROS_LOG_DIR`，再次 `~/.ros/log`。记录器目录取 `LEVI_FR3_RECORDER_DIR`（未设置时记录器显示 `unknown`）。

`context` 表示配置（实时工作区及其 `live.toml`）是否读到了；读不到时，其余部分仍按 `/proc` 和环境变量回答。

**任何请求都不会被卡住。** 每个文件先用 `lstat` 确认是普通文件，再以 `O_NONBLOCK | O_NOFOLLOW` 打开、用 `fstat` 复核，并有读取上限：FIFO、设备、套接字、符号链接都报 `unknown`（“not a regular file”），从不读取。每个探针在自己的线程里运行，有截止时间（1.5 秒；GPU 2.5 秒，每次 `nvidia-smi` 查询 2 秒后被终止）；超时的报 `unknown`（“timed out”）。它仍卡着时（例如网络盘挂死），之后的采样直接对它报 `unknown`，不再重新启动它。读取配置也有 1.5 秒的截止时间。

**采样。** 后台从不采样。请求到来时才采样；2 秒内的下一个请求拿同一份结果（`cached: true`、`age_s`）。同一时间只有一个请求在采样，采样期间不持有锁：采样中到来的请求最多等 3.5 秒，之后拿上一份结果（`stale: true`）或 `{"state": "sampling"}`。开销大的部分有更长的间隔和上限：查找套接字所属进程的全量扫描最多每 30 秒一次（已知所属进程只做低成本复核，新出现的监听立即查找），机器人进程扫描每 5 秒，ROS 日志每 5 秒（文件没变就不重读），磁盘每 10 秒，手臂控制器激活期间 `nvidia-smi` 每 10 秒一次（`reused: true`）。最多扫描 4096 个进程、每个进程 4096 个文件描述符；扫描被截断时标 `truncated: true`（端口；守卫在没找到机器人进程时 `quiet` 随之为 `null`）。ROS 日志每个只读最后 256 KiB（`truncated: true` 表示计数只是下限）。返回内容不含命令行、环境变量、令牌或路径；`live.gate.reason` 是实时服务自己写的文字，原样转出（最多 200 字符）。

### marvel0（只读）

`GET /api/levi/marvel0/status`（本机鉴权与上面的接口相同）为 **marvel0** 页面（`/marvel0`；它是顶栏链接；命令面板 `Ctrl+K` 和跳转组合键 `G C` 也能打开）回答一个问题：现在看得到机器人活动吗？返回文档的 schema 是 `levi.marvel0.status.v1`，有四个部分。和上面一样，读不到的部分标 `unknown`（附 `detail`），不影响其他部分。

| 部分 | 来源 |
| --- | --- |
| `marker` | setup.md 规定手臂控制器运行期间要 `touch` 的文件：主检出的 `lab/levi-hub/.arm-controller-active`（LEVI 从 worktree 运行时也是主检出的这个文件；找不到带 `.git` 文件夹的检出时，用本检出的）。`LEVI_HUB_DIR` 指向的可选第二个标记文件不读。是否存在和年龄（`age_s`，按修改时间）只靠 `lstat` 判断，从不打开文件：普通文件即 `present`，哪怕读不了；符号链接、目录、FIFO 为 `unknown`（附 `detail`）。文件不存在时，只有它所在的文件夹存在才算 `absent`（用 `stat` 判断，所以指向空处的符号链接文件夹也算不存在）；那个文件夹本身不存在（检出找错了、磁盘没挂上）时为 `unknown`（“the marker folder does not exist”），因为文件夹不在并不能说明文件不在。标记文件从不被清除或 `touch` |
| `eval_sessions` | 评测客户端在 `<rollout 根目录>/.eval_sessions/*.json` 里写的会话文件（组、年龄、状态），根目录取实时服务配置里的 rollout 根目录。还没结束的会话（状态不是 `fault`、`stopped` 或 `finished`，或没有状态）在最近 1 小时内更新过就列出，已结束的在最近 10 分钟内更新过才列出；年龄取文件里的 `updated_at`，没有则取修改时间。最多列出最新的 50 个（附一条说明，该部分仍为 `ok`）。没有已知根目录、某个根目录列不出来或根本不存在、或近期的文件读不成会话时为 `unknown` |
| `units` | LEVI 自己的 `levi-aeri-*` systemd 用户单元，来自 `systemctl --user list-units --type=service,scope --all`（所以手动启动、名为 `levi-aeri-policy-manual-*` 的策略 scope 也看得到，inactive 和 failed 的单元也在内），附 active 和 sub 状态：运行中的排最前，其次是本页没有归类的状态，最后是安静的，一共最多 50 个；`systemctl` 不存在、失败或没有及时回答，或我们这边的某行输出读不出来时为 `unknown` |
| `robot_idle` | 下面的建议性结论 |

`robot_idle.state` 把这些部分和上文状态探针的 `guard` 部分合在一起判断：

- 满足下列任一条即为 **`busy`**：
  - 守卫发现了机器人侧进程（`guard.robot` 非空）；
  - FR3 健康文件显示手臂控制器已激活（`guard.controller_active` 为 `true`，或其他任何为真的值）；
  - 标记文件存在且不到 12 小时；
  - 有评测会话在最近 60 秒内更新过，不论它是什么状态（已结束的也算）；
  - 有 `levi-aeri-*` 单元处于 `active`、`activating`、`deactivating` 或 `reloading`（运行中、正在启动、正在停止或正在重载）。`inactive` 和 `failed` 的单元不算。
- 否则，满足下列任一条即为 **`unknown`**：
  - `marker`、`eval_sessions` 或 `units` 部分是 `unknown`（标记文件还包括：它不在，它所在的文件夹也不在；`eval_sessions` 还包括：配置的某个 rollout 根目录根本不存在，即磁盘没挂上；`units` 还包括：我们这边的某行 `systemctl` 输出读不出来）；
  - 标记文件存在，且已有 12 小时或更久：这里没有任何依据能分清它是崩溃留下的，还是一次很长的运行，所以**只要标记文件还在，结论就绝不是 `idle`**（手臂栈没起来时，请手动删掉它）；
  - 有评测会话还没结束，而最近一次更新在 60 秒以前（这样的会话最长列 1 小时）：它的客户端可能已崩溃或卡住，而机器人仍在动；
  - 有 `levi-aeri-*` 单元处于 `active`、`activating`、`deactivating`、`reloading`、`inactive`、`failed` 以外的 `ActiveState`（例如 `maintenance`）；
  - `guard.state` 不是 `ok`；
  - 没找到机器人进程而 `guard.quiet` 不是 `true`（即 `false` 或 `null`），因为这时守卫分不清“确实没有”和“没能查”；
  - `guard.controller_active` 既不是 `true`、`false`，也不是 `null`，而且为假（例如 `0` 或空字符串）：文件没有说“没有”；
  - 状态探针交回的是旧样本（`stale`，或 `age_s` 超过 10 秒），所以旧的“安静”守卫不会被当成现在机器人安静。`age_s` 缺失或不是数字的样本按旧样本处理。
- 其余为 **`idle`**，显示为“未发现机器人活动”。

60 秒和 12 小时的界限按精确的年龄判断；`age_s` 只在返回时才取整到 0.1 秒。所以一个还没结束、显示为 60.0 秒的会话，可能是 `busy`（59.96 秒），也可能是 `unknown`（60.04 秒）；显示为 43200.0 秒的标记文件，可能不到 12 小时（`busy`），也可能已满 12 小时（`unknown`）。页面只看得到取整后的年龄，在这 0.05 秒内可能和结论不一致（卡片显示“正常”而横幅说“无法判断”）。一个没结束的会话在最后一次更新满 1 小时后不再列出，这与客户端自己的“已崩溃”规则（`eval_session.py`）一致，“无法判断”到此为止，若没有别的迹象，结论可以回到 `idle`。

`reasons` 只列出处于 busy 或 unknown 的来源，busy 的在前。每项有 `source`（`guard`、`marker`、`eval_sessions` 或 `units`）、`state`（`busy` 或 `unknown`）、英文的 `text`、稳定的 `code` 和 `params`；状态为 `idle` 时为空。`text` 是服务器自己写的英文句子，原样显示，措辞可能改动。`code` 是这种情况的稳定名称（`来源.情形`），`params` 是只含字符串和数字的扁平对象，装着这种情况需要的值（名称、以秒为单位取整到 0.1 秒的年龄；从不含路径），所以页面可以自己翻译这种情况并填入这些值。`code` 和 `params` 是在不改 schema 字符串 `levi.marvel0.status.v1` 的情况下加的：旧的使用方仍能读到 `source`、`state` 和 `text`，使用方应把这两个新字段当成可选的。每种会话最多列 5 个，每种单元最多列 5 个，进程名最多列 5 个。

| `code` | 来源与状态 | 何时出现 | `params` |
| --- | --- | --- | --- |
| `marker.present` | `marker`，busy | 标记文件存在且不到 12 小时 | `age_s` |
| `marker.old` | `marker`，unknown | 标记文件存在且已有 12 小时或更久 | `age_s` |
| `marker.unreadable` | `marker`，unknown | 该部分因缺少文件夹以外的原因为 `unknown`（该位置是链接、目录或 FIFO，出错，或不知道位置） | 无 |
| `marker.no_folder` | `marker`，unknown | 标记文件不在，它所在的文件夹也不在 | 无 |
| `sessions.recent` | `eval_sessions`，busy | 有会话在最近 60 秒内更新过 | `group`、`age_s`，文件里有状态时还有 `status` |
| `sessions.silent` | `eval_sessions`，unknown | 有会话还没结束，最近一次更新在 60 秒以前 | `group`、`age_s`，文件里有状态时还有 `status` |
| `sessions.unreadable` | `eval_sessions`，unknown | 该部分为 `unknown`（可以和读到的会话产生的 `sessions.recent`、`sessions.silent` 项同时出现） | 无 |
| `units.active` | `units`，busy | 有单元处于 `active`、`activating`、`deactivating` 或 `reloading` | `name`、`active`、`sub` |
| `units.state_unknown` | `units`，unknown | 有单元处于这些状态以及 `inactive`、`failed` 之外的状态 | `name`、`active` |
| `units.unreadable` | `units`，unknown | 该部分为 `unknown` | 无 |
| `guard.robot_process` | `guard`，busy | 守卫发现了机器人侧进程 | `names`：最多 5 个不重复的进程名，排序后用 `, ` 连接 |
| `guard.controller_active` | `guard`，busy | `guard.controller_active` 为 `true` 或其他为真的值 | 无 |
| `guard.controller_value` | `guard`，unknown | `guard.controller_active` 为假，且既不是 `false` 也不是 `null` | 无 |
| `guard.old_sample` | `guard`，unknown | 状态探针的样本是旧的（规则见上） | 无 |
| `guard.truncated` | `guard`，unknown | 没找到机器人进程，`quiet` 不是 `true`，而且进程扫描被截断（`truncated`） | 无 |
| `guard.not_quiet` | `guard`，unknown | 没找到机器人进程，扫描没有被截断，`quiet` 为 `false` | 无 |
| `guard.unreadable` | `guard`，unknown | `guard.state` 不是 `ok`（且样本不是旧样本），或没找到机器人进程、扫描没有被截断而 `quiet` 为 `null`（或缺失） | 无 |

**这个结论只供显示，不限制任何操作。** LEVI 里没有任何地方读它来允许、拒绝、启动或停止什么，静默守卫和实时服务的门照旧工作。“未发现机器人活动”不等于“安全”：LEVI 没有文件、进程、会话或单元可看的运行，就看不到。具体来说：

- 这个结论不使用实验室的 `arm_guard.py`（在维护者工作区的 `lab/levi-hub/` 里）。它读内核的套接字表，找连到机械臂地址的连接（FCI 命令端口、UDP 流），并且要从 setup.md 取机械臂的地址。所以，没有标记文件、没有在运行的健康监视器、也没有守卫能按名字认出的进程时，启动起来的控制器看不到。
- 没挂上的磁盘，只有挂载点文件夹本身不存在时才看得到。挂载点是个空文件夹时，会被当作一个没有会话的根目录。
- 会话很多时，只判断最新的 50 个。

**只被动读取。** 这个接口从不连接机器人端口，不启动、不停止、不写入任何东西，也从不清除标记文件。它背后只有只读查询：`nvidia-smi`（经由状态探针）和针对 `levi-aeri-*` 单元的 `systemctl --user list-units`。marvel0 自己的各部分不读任何进程目录；返回内容不含路径。`guard` 部分就是状态探针自己的那一份（`probes.status()`，与 `/api/levi/setup/status` 共用缓存），所以请求 marvel0 接口会取用或复用那次采样，并沿用它的采样方式，包括上文所述的进程扫描和套接字所属进程扫描。marvel0 的每个部分在自己的线程里读取并有截止时间。`units` 和 `eval_sessions` 的结果复用 2 秒；某个部分正在读取时到来的请求，会等这次读取（最多等到该部分的截止时间）并使用它的结果，只有截止时间过了，才判定该部分卡住（报 `unknown`，立即回答）。

**终端 A、B、C 不受控制。** 状态接口是只读的（本页末尾的策略服务器面板是 marvel0 页面唯一会启动东西的地方，而且只启动它自己的策略服务器单元）。机械臂栈（A）、夹爪栈（B）和机器人服务器（C）按 setup.md 的说明手动启动和停止。终端 A 和 C 的卡片显示 FR3 健康文件和内核套接字表的内容；终端 B 的卡片把控制器状态显示为 `unknown`，因为夹爪栈没有页面可以读取的状态文件或端口。

**页面。** 页面按起栈的顺序从上往下排：一行说明本页性质（只读、仅供参考、不是急停；细节收在下面的折叠里）；一条栈状态条（终端 M 健康监视器、Desk、终端 A 机械臂栈、终端 B 夹爪栈、终端 C 机器人服务器、策略服务器、评测客户端，各是一块带标签的色块；健康监视器的文件停止更新或不存在时，色块上方先用一句话写出原因：“终端 M 没有更新，所以 Desk、A 和 M 无法更新”）；显示结论的横幅；然后是三个编号步骤：1 手臂栈（M、Desk、A、B、C 卡片），2 策略服务器（状态卡片在“策略服务器”卡片旁边），3 任务与命令（评测客户端卡片在“评测客户端命令”卡片旁边）；最后是本机的服务与 GPU（实时服务、视觉语言模型、GPU）。卡片有一个标签（正常、故障、已过期、未启动或未知）、一句话说明、依据的事实和一句简短提示；同一行的卡片等高，卡片下方的备注（原因和服务器写下的文字）折叠在“详情（n 条）”里，卡片为故障时才默认展开。状态条由同一批卡片生成（`StackStripData`），不读进程列表。A、B、C 卡片也不读进程列表（marvel0 只用被动来源），所以终端 B 仍是未知。“故障”指健康监视器亮红灯（镜像监视器的卡片，句子里仍写“红灯”）、没有策略服务器在监听时策略单元失败，或评测会话报告了故障。

*请求。* 标签页可见时每 3 秒请求一次 `/api/levi/setup/status` 和 `/api/levi/marvel0/status`，隐藏时不请求。6.5 秒内没有回答的请求会被放弃并中止，所以一个永远不回答的请求拖不住轮询；两个请求都失败的一轮之后，等待时间加倍，最长 30 秒。请求失败或标签页重新可见时，超过 10 秒的旧结果会被丢弃，不会当成当前状态显示；其采样在送达时已有的年龄也算在内（状态探针返回的 `age_s`），所以一份送达时已有 6 秒的缓存样本，再过 4 秒就会被丢弃。

*旧样本。* 状态探针的返回是旧样本时（服务器标了 `stale`，或其 `age_s` 超过 10 秒），页面上会有一条说明，依据它的每张卡片都显示“已过期”并写明样本多旧（除终端 B 夹爪栈卡片，它没有数据来源，和评测客户端卡片，它读的是 marvel0 状态）。样本当时的结论留作卡片的第一条事实（“当时的采样结果”），读数也保留在下面，但都不再是卡片现在的结论：旧样本里的红灯不显示为“故障”。本来就没有内容可显示的卡片仍是“未知”。状态探针的返回没有 `age_s` 时，页面不把它当作旧样本（上文的结论则把它当作旧样本）。

*原因。* 横幅按每条原因的 `code` 和 `params`、用页面当前的语言自己写出这条原因，所以中文页面上，页面认识的原因不会出现英文句子。页面不认识 `code`、`params` 缺了句子必需的值，或这条原因没有 `code`（旧版服务器）时，改为原样显示服务器的英文 `text`。原因的顺序是：先是显示有活动的，再是无法判断的，最后是其他的，每一类内部保持服务器的顺序，最多 12 条（上限在排序之后再取）。标记文件、评测会话或单元读不到时，对应卡片上的说明同样按 `marker.unreadable`、`marker.no_folder`、`sessions.unreadable` 或 `units.unreadable` 用页面的话写出；没有这些 `code` 时，原样显示该部分的 `detail`。

*横幅。* 同一次返回里的 `marker`、`eval_sessions` 或 `units` 部分是 `unknown`（说明写某个部分读不到），或返回里有 schema 而它不是 `levi.marvel0.status.v1`（说明写格式不认识），或返回说 `idle` 却仍列着原因（说明写返回列着原因，所以不采信；同时有部分没读到时，显示没读到的那条说明）时，横幅降为“无法判断”。每种情况下原因都照样列出。

*标记遗留提示。* 结论是 `busy`，每条原因都是控制器标记文件存在（`code` 为 `marker.present`），并且它已经存在至少 10 分钟（看它的 `age_s`；页面读不出年龄的标记不提示），同时状态探针的 `fr3_health` 部分是 `missing`，或是带年龄的 `offline`（健康文件停止更新，即健康监视器没在运行），而且这份状态样本不是旧的，原因下面多一行提示：如果机械臂栈已经停了，这多半是上次运行结束后没删的标记，按 setup.md 里“终端 A 退出后删除标记”的那一步处理。设 10 分钟是因为 setup.md 要求操作者先放标记、等 60 秒、再起手臂栈：这段时间里标记在、监视器没运行、也没有机器人进程，提示会让操作者删掉刚放好的标记。列着任何别的原因（机器人进程、评测单元或会话、已有 12 小时以上或读不到的标记文件）、健康文件是 `ok` 或红灯、健康文件只是读不了而显示 `offline`（没有年龄，监视器可能在运行）、状态样本是旧的、状态探针里没有可读的健康部分时，不显示这一行。页面只是这样提示，页面上没有任何东西会删除标记文件。

*卡片。* 评测客户端卡片取下列第一条成立的：最近 60 秒内更新过的会话报告了故障，为“故障”；有未结束的会话在最近 60 秒内更新过，为“正常”；有会话早先报告过故障，为“故障”（附该会话最后一次更新的时间，只要服务器还列着这个会话就一直是“故障”，即最后一次更新之后的 10 分钟内）；有未结束的会话最近一次更新在 60 秒以前，为“已过期”；否则为“未启动”（列出的会话都已结束，或没有会话）。一个会话也读不到，或者列表可能不完整而结果本该是“未启动”时，为“未知”。已有 12 小时或更久的标记文件显示为“可能是崩溃后遗留的，也可能属于一次很长的运行”。手臂栈没起来时，健康监视器自己会亮红灯；红灯的原因只有 `hardware_interface_missing`、`hardware_interface=`、`controller_manager_unreachable`、`robot_state_no_data` 或 `waiting_for_first_robot_state`（监视器在运行，但还没收到任何机器人状态），机器人没有报错，且硬件接口没有被报告为激活时，终端 A 的卡片显示“未启动”，并说明红灯是因为终端 A 没在运行。原因按完整名字认：`waiting_for_first_robot_state_extra` 是另一条原因，红灯照旧。硬件接口已激活时，这些原因是机械臂或其连接的故障，仍显示“故障”，不管控制器标志怎么说。健康文件不再更新（“已过期”）时，它最后写下的原因只列在终端 M 的卡片上：Desk 和终端 A 的卡片保留服务器关于它为什么停了的那句话，并写“原因同终端 M 的卡片。”，不再重复这些原因；亮红灯时三张卡片都保留原因。页面上没有任何东西是急停或安全检查，卡片显示绿色也不代表可以靠近机器人。

### marvel0：策略服务器面板和客户端命令面板

除了 `GET /api/levi/marvel0/status`，marvel0 页面还有两个为真机会话做准备的面板，以及它们背后的接口。**策略服务器面板**启动、显示和停止**一个**策略服务器：也就是人原本在策略服务器终端里手敲的那条命令（openpi 的服务脚本，加检查点、GPU 显存比例，以及可选的 RTC）。**客户端命令面板**为评测客户端终端生成命令（任务指令、步数、片段数、`--min-z`、RTC 执行步数、rollout 组、备注），按站点文件的界限校验后，作为**要复制的文本**展示。

在页面（`/marvel0`）上，它们是第 2、3 步，各自放在所对应组件的只读状态卡片旁边：**策略服务器**卡片和**评测客户端命令**卡片。页面上其余部分都不会执行任何操作。

它们用和其他 `/api/levi` 接口相同的本机鉴权（网页 UI 令牌；智能体的 Bearer 凭据会被拒绝），而且都先需要 **marvel0 站点文件**。两个写操作（部署和停止）只限本人。

| 接口 | 作用 |
| --- | --- |
| `GET /api/levi/marvel0/models` | 面板可以部署的检查点，每个带它的服务配置、是否接受 CFG 系数、是否只限已知任务指令、`unverified` 标签、默认的 rollout 组，以及 `skipped`：其他文件夹为什么没有列出 |
| `GET /api/levi/marvel0/site` | 去掉所有路径的站点文件：连接、序列号、起始位姿、界限、上限、固定的客户端参数、GPU 比例、单元设置，以及文件夹的最后一级名字 |
| `POST /api/levi/marvel0/command/preview` | 为一份表单生成评测客户端命令，校验后作为要复制的文本返回。不执行、不启动、不连接、不写入任何东西 |
| `GET /api/levi/marvel0/deploy` | 策略服务器面板的状态（`idle`、`starting`、`ready`、`failed`、`dead`、`unknown`、`foreign_listener` 或 `stopping`）。每次请求都根据单元状态、内核套接字表、单元日志和部署记录重新推出；还有部署会遇到的检查，以及服务器占用的 GPU 显存 |
| `POST /api/levi/marvel0/deploy` | 把一个策略服务器作为临时的 `systemd --user` 单元启动（只限本人） |
| `POST /api/levi/marvel0/deploy/stop` | 停止本面板启动的那个单元（只限本人） |
| `GET /api/levi/marvel0/deploy/log` | 该单元日志的末尾，只含可打印字符，每个路径只留最后一级名字（`?lines=`，1 到 200，默认 80） |

#### 面板从不做的事

面板只启动和停止**自己创建的策略服务器单元**（`levi-aeri-policy-manual-marvel0-<模型>`）。它从不启动机械臂控制器（终端 A）、夹爪栈（终端 B）、机器人服务器（终端 C）或评测客户端（健康监视器归下文的机械臂栈卡片管）；这些仍按 setup.md 在真实终端里手动启动，客户端命令只是文本。**页面上没有任何东西是急停**，没有任何控制被叫作急停，每一次会话手都必须放在物理急停上。策略服务器卡片会写明：“这张卡片只启动和停止它自己创建的策略服务器服务单元。它不启动机械臂栈、夹爪栈、机器人服务器或评测客户端，也不是急停。每次运行都请把手放在实体急停上。”（`GET /api/levi/marvel0/deploy` 的答复里有一句意思相同的较短 `estop.text`；页面显示的是它自己的这句。）停止按钮叫**停止策略服务器**，从不只叫“停止”。

评测客户端在它的策略服务器出故障之后不会自己回位或恢复手臂，所以面板对这种情况还有第二条提醒：会话可能还在进行时，服务器出了故障或消失了，请把手放在急停上。状态是 `failed`、`dead`、`stopping`、`foreign_listener` 或 `unknown`，并且机械臂控制器的标记文件存在或读不到，或者有评测会话没有结束或读不到时，会显示这条提醒（页面无法向机器人询问它是否已回位）。强制停止（输入了额外短语）切断了连着的客户端时，这件事记在部署记录里，之后即使状态是 `idle`，这条提醒也一直留着，直到标记文件消失且没有未结束的会话；刷新页面不会丢掉它。

#### marvel0 站点文件（`LEVI_MARVEL0_SITE`）

只属于这台机器的值（openpi 检出及其 python、检查点文件夹、rollout 根目录、锁文件、策略端口、两个相机序列号、起始位姿、安全界限、GPU 显存比例、策略服务器单元的 CPU 设置）放在一个 TOML 文件里，不写进仓库。`LEVI_MARVEL0_SITE` 指向它（`~` 和 `~/...` 表示运行 LEVI 的用户的主目录；`~名字` 从不展开，因为它会指到别的用户的文件夹，会被报为不是绝对路径；路径必须是绝对的）；没设这个变量时，用主检出的 `lab/marvel0/site.toml`（LEVI 从 worktree 运行时也是主检出的；`lab/` 被 git 忽略）。页面只读它，从不写它。它必须是运行 LEVI 的用户自己的普通文件，组和其他人都不能写（`chmod 644`），不超过 64 KiB；链接、文件夹和 FIFO 都会被拒绝。只有它的身份、修改时间或状态变更时间、大小、权限或属主变了，LEVI 才重新读。解析器吃不下的文件（不是 TOML 的文本、嵌套比它的栈还深、比 Python 能转换的更长的整数）报 `not_toml`，不会让接口出错。

没有这个文件时，上面每个接口都返回 `412`，`code: site_missing`，附 `looked_at`（它找过的路径；返回内容里除了要复制的命令之外，只有这一处会出现路径）和 `template`：一份完整的、路径写成 `/path/to/...` 占位符的文件，可以照着改。文件违反格式时返回 `412`，`code: site_invalid`，附 `errors`：一个 `{field, code}` 列表，从不重复具体的值。格式是封闭的：多出的键是错误，缺的键也是错误（只有 `server.port` 有默认值 8000）。`code` 有：`missing`、`unknown_key`、`type`、`range`、`not_absolute`、`not_normalized`、`unsafe_chars`（路径或名字里有 `$`、`%`、空白或控制字符）和 `inconsistent`（见表后的规则）；文件本身（`field: "file"`）的有 `not_regular`、`not_owner`、`writable`、`too_large`、`unreadable` 和 `not_toml`。读文件时不检查文件系统：路径是否存在由部署时的检查负责。

文件无效期间，**每个**接口都返回 `412`，策略服务器卡片的状态和它的“停止”也不例外：页面无法停止它自己启动的服务器。编辑之后最常见的原因是 `file: writable`（编辑器在 umask 002 下把文件存成了 0664）：对它运行 `chmod 644`。在那之前要停服务器，可在终端运行 `systemctl --user list-units 'levi-aeri-policy-*'` 看到单元名（`levi-aeri-policy-manual-marvel0-<模型>`），再运行 `systemctl --user stop <单元>`。页面上的“文件无效”提示也这样说。

下表的示例值取自 LEVI 自带的模板；模板里每个路径都是 `/path/to/...` 占位符，请写这台机器自己的路径。

| 段和键 | 含义 | 示例 |
| --- | --- | --- |
| （顶层）`schema` | 格式 | `"levi.marvel0.site.v1"` |
| `[paths]` `openpi_checkout` | openpi 检出：单元的工作目录，也是客户端命令开始的目录 | `/path/to/openpi` |
| `python` | 检出自带的 python，直接运行（不经 `uv run`） | `/path/to/openpi/.venv/bin/python` |
| `checkpoints_root` | 模型列表读取的文件夹 | `/path/to/openpi/checkpoints` |
| `rollout_root` | 评测客户端写入片段的唯一文件夹（`--rollout-root`）；任何表单都不能指定别的 | `/path/to/rollouts` |
| `gpu_lock_file` | 物理 GPU 锁文件（账本文件夹里的 `.gpu.lock`） | `/path/to/levi-hub/.gpu.lock` |
| `[live]` `unit` | 实时服务的视觉模型服务器存在期间持有 GPU 锁的 systemd 用户单元 | `"levi-live.service"` |
| `[ros]` `path_prefixes` | 策略服务器单元的 `LD_LIBRARY_PATH` 里位于这些目录下的项会被去掉 | `["/path/to/ros", "/path/to/franka_control"]` |
| `[server]` `port` | 策略端口，单元、套接字检查和客户端都用它（默认 8000；1024 到 65535，且不能是 5000、5001、5100 或 7470，它们属于机器人侧和 LEVI：客户端会被指向它们） | `8000` |
| `remote_host`、`robot_server_url` | 客户端到哪里找策略服务器和机器人服务器 | `"127.0.0.1"`、`"http://127.0.0.1:5000/"` |
| `[cameras]` `view`、`wrist` | 两个 D405 的序列号（6 到 16 位数字，且不相同），客户端命令里总是写出 | `"262622274256"`、`"262622275490"` |
| `[home]` `pose`、`duration_s` | 六个数的起始位姿，以及客户端回到那里用多长时间（1 到 30 秒：时间更短会让大幅度的移动变快） | `[0.481902, -0.011145, 0.235951, 3.119139, 0.008288, -0.883962]`、`3.0` |
| `[safety]` `min_z_floor`、`min_z_default` | `--min-z` 的下限（表单只能抬高它）和默认值；都大于 0，且低于起始高度。模板里的 `0.025` 低于客户端测得的桌面高度（0.04658 m；客户端自己的下限是螺丝命令 0.075、盘子命令 0.05325），所以默认表单会带 `min_z_below_task_table` 警告；是保留这个值，还是把 `min_z_default` 设成客户端的下限、只让 `0.025` 作下限，由 marvel0 的主人决定 | `0.025`、`0.025` |
| `max_xy`、`max_step_m`、`max_step_rad`、`rx_range`、`ry_range`、`rz_range` | 客户端的运动界限（范围写成 `[低, 高]`）；`max_xy` 须大于 0 且不超过 1.0、`max_step_m` 大于 0 且不超过 0.2、`max_step_rad` 大于 0 且不超过 0.5，否则整个文件被拒绝（客户端默认值分别是 0.85、0.10、0.35） | `0.85`、`0.10`、`0.35`、`[1.50, 4.03]`、`[-0.74, 0.96]`、`[-2.44, 0.77]` |
| `binarize_gripper`、`require_matching_server`、`allow_unknown_prompt` | 客户端的三个开关 | `true`、`true`、`false` |
| `[caps]` `max_steps_default`、`max_steps_cap`、`eval_num_default`、`eval_num_cap` | 每个片段的步数和片段数：默认值，以及表单不能超过的上限 | `500`、`3000`、`5`、`50` |
| `rtc_horizon_default`、`rtc_horizon_max`、`open_loop_default`、`open_loop_max` | RTC 执行步数，以及关掉 RTC 时的开环执行步数（各为 1 到 10） | `6`、`8`、`6`、`10` |
| `prompt_max_chars`、`note_max_chars` | 任务指令最长（至多 300）和备注最长（至多 200） | `300`、`200` |
| `cfg_scale_default`、`cfg_scale_max` | CFG 模型的优势 CFG 引导系数 | `1.0`、`1.5` |
| `[gpu]` `fraction_presets` | 面板提供的显存比例：一到四个值，0.05 到 0.45（约 15 GB，主人定的上限），递增 | `[0.22, 0.30, 0.45]` |
| `margin_mib`、`lock_hold_max_s` | 比例之外再留的空闲显存（至少 1024）；服务器加载期间 GPU 锁最长持有多久 | `1024`、`600` |
| `[unit]` `nice`、`cpu_affinity`、`cpu_quota_percent` | 策略服务器单元的 CPU 设置：nice 0 到 19，`taskset` 的核心列表（选机械臂控制器不用的核心；模板里的 `8-15` 是对没有 CPU 隔离的主机的猜测，安装站点文件之前由 marvel0 的主人确认），CPU 配额百分比（0 表示不限） | `10`、`"8-15"`、`0` |
| `stop_timeout_s`、`start_timeout_s` | 停止最多多久之后 systemd 强制结束单元（10 到 600）；启动多久还没完成就算 `failed`（30 到 1800） | `90`、`600` |
| `[catalog]` `known_configs`、`cfg_configs` | 面板接受的服务配置，以及其中接受 CFG 系数的 | `["pi05_fr3_all_state", "pi05_fr3_all_state_cfg"]`、`["pi05_fr3_all_state_cfg"]` |
| `restricted_prefixes`、`verified`、`excluded` | 只接受客户端那两条任务指令的模型系列；有真机 GPU 部署记录的检查点；永远不列出的检查点 | `["pi05_fr3_sp"]`、`["pi05_fr3_all_step49999"]`、`["reset_r1_step20000"]` |
| `[prompts]` `default`、`known` | 默认任务指令，以及表单为其他模型给出的提示列表（1 到 32 条） | `"pick the eggplant in the blue plate"` 和另外四条 |
| `[source]` `hash_files`、`manifest` | 部署时记录 sha256 的 openpi 检出内文件（相对路径）；必须与它们一致的清单文件，`""` 表示没有 | `scripts/` 和 `src/openpi/` 下的五个文件、`""` |

键之间的规则（`inconsistent`）：两个相机序列号不同；`min_z_floor` 和 `min_z_default` 都低于起始高度，且默认值不低于下限；每个默认值都不超过它的上限（步数、片段数、RTC 执行步数、开环执行步数、CFG 系数）；`cfg_configs` 是 `known_configs` 的子集。

**机械臂锁不是站点值。** AERI 运行使用机械臂期间持有的锁，是 `levi/automatic/fr3_run.py` 里固定的文件 `/tmp/levi-aeri-fr3-arm.lock`（有测试比对两处的字面量）。它曾经是站点文件的一个键；写错一个字母就会让“机械臂在使用中”的检查无声失效，所以现在文件里再写 `arm_lock_file` 会报 `unknown_key`。

**受限的任务指令也不是站点值。** 两任务专家模型（`pi05_fr3_sp...`）的客户端，在动作之前就会拒绝它的 `TASKS` 之外的任何任务指令：`pick screws of same size into the empty slot of screw box` 和 `stack the plates of same color together`。LEVI 保存了一份拷贝（客户端文件在 openpi 检出里，不在本仓库中），所以不管 `[prompts] known` 列了什么，页面对这种模型只提供、只接受这两条；`known` 是其他模型的提示列表。

#### 模型列表

一个检查点要同时满足下列条件才会列出：它是带 `params/` 的 JAX 检查点，且策略发现认为它可部署；角色是 `forward`；服务配置在 `known_configs` 里；站点没有排除它；文件夹名只含普通字符（字母、数字、`.`、`_`、`-`）且不超过 71 个字符（默认的 rollout 组名 `<名称>_rtc_demo` 必须不超过命令表单接受的 80 个字符，否则列出了这个模型，它的默认表单却会被拒绝）；**顶层**的 `norm_stats.json` 是普通文件。最后一条很关键：策略服务器只读检查点顶层自带的归一化统计，而策略发现也会接受 `assets/` 下的文件。没有列出的文件夹在 `skipped` 里写明原因，取第一条不满足的规则：`unsafe_name`、`name_too_long`、`excluded_by_site`、`not_deployable`、`not_jax`、`role_not_forward`、`config_unknown` 或 `no_toplevel_norm_stats`。时间反向的 reset 检查点是典型例子：它不是正向策略，归一化统计也只在 `assets/` 下，所以不会列出（站点文件另外也把它放在 `excluded` 里）。文件夹本身的问题（例如它不存在）在 `problems` 里。

只有 `cfg_configs` 里的配置接受 `--cfg-guidance-scale`。`pi05_fr3_sp` 系列（以及 `restricted_prefixes` 里的）只接受客户端那两条任务指令。不在 `verified` 里的选项带 `unverified` 标签：它没有真机 GPU 部署记录，部署它除了确认语（由确认弹窗发送），还要 `accept_unverified`。文件夹 30 秒内只读一次。

#### 策略服务器面板

**在页面上。** 策略服务器卡片显示状态徽标、对该状态的一句说明、这次部署的各项事实（模型、服务配置、RTC、比例、单元、已连接的客户端、GPU 显存）、日志里记录了什么（上面的就绪行，每一行标明已见到、尚未或没读到）、服务器占用的 GPU 显存和实时标注无法启动的提示、部署会遇到的检查，以及该单元的日志（最后 80 行；状态为失败或已退出时自动展开）。表单里有模型下拉框（没有真机 GPU 检查记录的模型标为 `unverified`）、RTC 开关（默认开）、带站点预设的显存比例下拉框（每个预设旁写着它需要多少空闲显存、现在是否够），以及 CFG 模型的引导强度输入框（留空用默认值）。没有列出的检查点收在“有 n 个检查点没有列出”下面，并写明原因。点**部署**会打开一个对话框，里面有模型、配置、RTC、比例和显存，手不能离开急停的提醒（页面上没有任何东西是急停），未验证模型要勾选接受，以及不需要输入任何东西：它是一个确认弹窗，勾选之后“确认”按钮才可用，页面自己发送短语 `deploy <model>`（后端仍然核对）。对话框开头还有一句提醒：不要在终端里手动启动策略服务的同时点“部署”；页面要等它开始监听才看得到它。卡片上的“部署”按钮在这些情况下是关闭的，并在旁边写明原因：状态不是 `idle`、`failed` 或 `dead`，有检查不通过，所选比例需要的显存超过空闲显存，或读不到状态。**停止策略服务器**只在服务器表示该单元可以停止时才出现；它的确认弹窗自己发送停止短语，有客户端连着时才要手输额外短语（仍然手输的两个覆盖之一），并写明停止最多要多久（`stop_timeout_s` + 30 秒）。手动启动的服务器没有“停止”，只提示在它的终端里按 Ctrl+C。标签页可见时，页面每 3 秒读一次状态（隐藏时不读；失败后等待时间加倍，最多 30 秒；超过 10 秒的答复不再当作当前状态显示）。没有得到答复的写操作会保留它的请求 id，再按同一个按钮就是重发同一个请求，不会被执行两次。

**一次部署做什么。** `POST /api/levi/marvel0/deploy` 接受 `request_id`、`model_id`、`rtc`、`fraction`（站点预设之一）、`cfg_guidance_scale`（不带 CFG 的模型留空；CFG 模型留空表示用站点默认值，且该参数总会传入）、`accept_unverified` 和 `confirm`，`confirm` 必须是确认语 `deploy <模型>`。检查按下面的顺序进行，第一个不通过的就带着它的 code 拒绝，此时什么都还没启动：

1. 请求 id、确认语、站点文件；
2. 部署文件夹可用（见下）；
3. 重发的请求直接得到已存的答复（见下）；
4. 同一时间只做一件事（第二个请求得到 `busy`，423）；
5. 表单（模型在列表里、比例是预设之一、CFG 系数在范围内、未验证的模型已被接受）；
6. 检查点：真实路径在 `checkpoints_root` 之内，`params/` 是文件夹，顶层 `norm_stats.json` 是普通文件（`checkpoint_invalid`）；python 可执行，服务脚本存在（`runtime_files_missing`）；
7. 源文件：读取 `[source] hash_files` 里每个文件的 sha256（`source_file_missing`）；有清单时，任何差异都拒绝（`source_changed`）；没有清单时哈希只记在部署记录里，面板给出 `source_hash_unmanifested` 的说明；
8. 套接字表读得到（`net_table_unreadable`：读不到绝不当作端口空闲）；策略端口上没有任何监听，IPv4 和 IPv6 都查（`port_taken`；手动启动的服务器从不去动；本面板自己正在运行的单元给出 `already_running`）；没有已建立的连接（`client_connected`）；
9. **任何状态**的 `levi-aeri-policy-*` 单元都不存在（`policy_unit_exists`；`activating` 算作预留；本面板自己的单元给出 `already_running`；问不了 systemd 给出 `systemd_unreadable`），并且没有未定的上一次启动（`start_unconfirmed`，409）：`systemd-run` 没有及时应答、或核心在它运行之前就停了的启动，会让记录停在 `intent`，它的单元可能稍后才出现。记录不满 30 秒（也就是状态从 `starting` 变成 `failed` 的时刻）之前，另一次部署（不论哪个模型）会在端口上放第二个服务器，所以被拒绝；那次启动的 GPU 锁在此期间保留；
10. 机械臂锁没有被持有（`arm_lock_held`；从内核的锁表读，从不打开这个文件，所以不会干扰 AERI 自己的加锁；无法得知给出 `arm_lock_unknown`），也没有登记在册且还活着的独立 AERI 运行（`aeri_run_live`；登记册读不了则以 `aeri_registry_unreadable` 拒绝）。运行登记册在按下部署时读，最终检查（第 15 步）里再读一次，**状态轮询不读**：它要用 LEVI 的密钥并读每个运行的日志，每 3 秒轮询一次的页面不该这样做。所以状态里的检查列表没有它的一行；轮询时看到的是机械臂锁那一行（运行使用机械臂期间持有这把锁）。LEVI 的登记册会跳过签名验证不过的条目，所以密钥读不了时根本看不到任何运行，这时起作用的是机械臂锁这一检查；
11. GPU 锁：没有人持有（启动时由面板去取），或者只有实时服务的视觉模型服务器持有（它存在期间一直持有锁，休眠时也是；面板不动它，由显存检查来决定），否则是 `gpu_lock_foreign`；
12. 用户管理器的环境读得到（`env_unreadable`）；
13. 空闲 GPU 显存（`nvidia-smi`，只取汇总的已用和总量，必须正好一行，否则 `gpu_unreadable`）至少是 `ceil(比例 x 总量) + margin_mib`（`gpu_insufficient`；数字见下）；
14. **部署记录**（阶段 `intent`）在 `systemd-run` 运行**之前**写好并 fsync；
15. 最终检查，紧挨着启动之前，面板取了 GPU 锁时在锁内进行：套接字表、单元列表、机械臂锁、运行登记册和空闲显存都重新读一遍；任何变化都会拒绝，记录标为 `aborted`；
16. 创建日志文件，`systemd-run --user` 启动单元，记录变为 `started`。`systemd-run` 失败时什么都没有运行，返回 `start_failed`（502）；它在限时内没有回答时，单元可能已经启动：记录停在 `intent`，请求保留下来（再发一次不会再启动），状态跟着单元走。

**显存规则。** 在 32607 MiB 的卡上、余量 1024 MiB 时：

| 比例 | 服务器预留 | 需要的空闲显存 |
| --- | --- | --- |
| 0.22 | 7174 MiB | **8198 MiB** |
| 0.30 | 9783 MiB | **10807 MiB** |
| 0.45 | 14674 MiB | **15698 MiB** |

拒绝信息会写全这些数字（“Fraction 0.30 of the 32607 MiB card needs 10807 MiB free (9783 + 1024); 5100 MiB are free. Put the live vLLM to sleep or stop it (/live), then try again.”）。0.30 和 0.45 要求实时服务的视觉模型已休眠或停止。面板在尝试部署之前，就会列出每个预设对应的数字。

GPU 锁只从第 15 步持有到服务器常驻显存为止（状态离开 `starting`，或连续三次读不到状态，或到了 `lock_hold_max_s`），绝不在服务器的整个生命周期里持有。服务器运行期间，面板会写出它占用多少 GPU 显存（所记录部署的 `ceil(比例 x 总量)`，是它预留的份额而不是实测值；不是本面板启动的服务器显示问号），以及实时标注无法启动。

**单元。** `levi-aeri-policy-manual-marvel0-<slug>`，slug 是小写的模型 id，`_` 换成 `-`（不能这样化简或超过 32 个字符的 id 会被截短，并附上整个 id 的 8 位哈希）。它是一个 `levi-aeri-policy-*` 单元，所以状态接口的单元部分和实时服务都会像看待 AERI 的任何策略服务器那样看到它。单元在 openpi 检出目录里直接运行检出自带的 python（不经 `uv run`）：RTC 开用 `scripts/serve_policy_rtc.py`，关用 `scripts/serve_policy.py`，`--port` 和（CFG 模型总会带上，值为默认值或所给值的）`--cfg-guidance-scale` 放在 `policy:checkpoint` 之前，随后是 `--policy.config` 和带绝对检查点目录的 `--policy.dir`。策略服务器终端没有 ROS 也没有 conda，单元也一样：`AMENT_*`、`ROS_*`、`RMW_*`、`COLCON_*`、`CMAKE_PREFIX_PATH`、`CONDA_*`、`VIRTUAL_ENV`、`PYTHONPATH` 和 `PYTHONHOME` 被取消设置，`LD_LIBRARY_PATH` 去掉 `[ros]` 前缀下的项，并设置 `XLA_PYTHON_CLIENT_MEM_FRACTION`（所选比例）、`PYTHONUNBUFFERED=1` 和 `no_proxy=localhost,127.0.0.1,::1`。它用 `KillSignal=SIGINT`（相当于终端里的 Ctrl+C）停止，`TimeoutStopSec` 取自站点文件，不重启、没有停止命令，用站点设定的 `Nice`、`CPUAffinity` 和可选的 `CPUQuota` 运行，并把标准输出和标准错误写进它的日志。单元带 `--collect` 启动，所以一结束 systemd 就把它忘掉，不保留退出状态：故障从日志里读，什么都没写就崩溃的显示为 `exited`。

**状态。** 每次 `GET /api/levi/marvel0/deploy` 都根据单元状态（`systemctl --user`）、监听情况（内核套接字表）和部署记录重新推出状态；答复复用 2 秒，也不会因为某个来源读不到而失败。

| 状态（页面上的徽标） | 含义 |
| --- | --- |
| `idle`（空闲） | 本面板没有策略服务器，端口上也没有监听 |
| `starting`（启动中） | 单元存在（或 `systemd-run` 正在进行，最多 30 秒），日志还没有出现它该有的全部内容；就绪标志显示进展到哪一步 |
| `ready`（已就绪） | 单元在运行，端口上监听的套接字属于该单元，日志里有全部就绪行 |
| `failed`（失败） | 会显示故障种类：`xla_oom`、`argparse`、`address_in_use`、`traceback`、`rtc_not_ready` 或 `cfg_not_confirmed`（服务器已在监听，但 5 秒后仍没有 RTC 或引导系数那几行）、`start_timeout`（运行超过 `start_timeout_s` 仍未就绪）、`exited`（没来得及监听就退出了）、`start_failed`，或 `intent_only`（记录写好了但没有启动单元）。仍在运行的单元可以停止；失败的部署不会挡住下一次部署 |
| `dead`（已退出） | 服务器日志里已经出现监听，随后退出了，可能是在本面板之外被停掉，也可能是崩溃 |
| `unknown`（未知） | 套接字表、systemd 或部署记录读不到（`net_unreadable`、`systemd_unreadable`、`record_unreadable`），或者这些事实对不上任何状态（`unclassified`）。不会假设端口空闲或服务器已停 |
| `foreign_listener`（手动启动） | 端口上有监听者，但它不是本面板的单元：`hand_started`（手动启动的服务器）、`listener_not_ours`，或 `owner_unknown`（读不到单元的进程，所以把监听者当作不是我们的）。面板从不去动它：请在运行它的终端里按 Ctrl+C |
| `stopping`（停止中） | 已请求停止而单元还没有消失，或者单元在没有本面板停止请求的情况下正在关闭（`stopped_elsewhere`）。单元仍在运行而没有任何停止在进行（核心在“停止中”标记与停止命令之间停了）时，可以再停一次 |

状态为 `idle`、`failed` 和 `dead` 时，只有与模型无关的检查全部通过，才提供部署（`can_deploy`）：状态文件夹、套接字表、端口空闲、没有客户端、没有策略单元、机械臂锁、GPU 锁、GPU 可读，以及最小的比例放得下（每一项是 `checks` 里的一行，级别为 `ok`、`refuse` 或 `unknown`；运行登记册由部署本身读，见第 10 步）；模型和比例由部署本身再检查一次。状态为 `starting`、`ready`，单元仍在运行的 `failed`，或单元仍在运行且本进程没有停止在进行的 `stopping` 时，提供停止（`can_stop`）。答复里还有就绪标志、`failure`、`gpu` 数字、每个预设的 `vram` 需求、`stop_blockers`（`client_connected`、`foreign_listener`、`net_unreadable`）、`live_annotation`、`estop`、`notes`、`confirm`（确认弹窗发送的确认语）和 `operation`（正在进行的部署或停止）。失败或已死的记录一直显示，直到下一次部署把它换掉。

监听的套接字属于谁，只根据本面板**自己**那个单元的进程来判断（先看单元的控制组，再看这些进程的套接字链接）；不读任何别人的进程目录。

**就绪是被动判断的。** 无论什么状态，页面都从不连接策略端口。服务器自己的日志说就绪了，并且端口出现在套接字表里，才算 `ready`：`Using norm stats shipped with the checkpoint:` 且点名的是这个检查点顶层的 `norm_stats.json`；RTC 部署还要 `RTC enabled: ...` 和 `RTC warm-up: plain N ms, rtc N ms`（预热被跳过不算）；CFG 模型要 `Advantage-CFG policy, guidance scale w=...`；最后是 `server listening on 0.0.0.0:<端口>`。**RTC 部署的服务器没有记录 RTC 就绪，就算部署失败。** 日志读开头和末尾各 256 KiB，因为长时间运行时连接日志会把启动行挤出末尾。日志里表示 GPU 显存耗尽的信息（`RESOURCE_EXHAUSTED`，或点名 OOM 或 memory 的 `XlaRuntimeError`）归为 `xla_oom`；XLA 分配器的警告“ran out of memory trying to allocate ... this is not a failure”不算，在已经 `ready` 的服务器上由一次请求引起的显存不足错误或 traceback 也不算；命令行错误归为 `argparse`，`Address already in use` 归为 `address_in_use`；服务器就绪之前出现的 traceback 归为 `traceback`。如果某个库改了日志措辞，健康的服务器可能一直停在 `starting`，随后以 `start_timeout` 失败：面板会显示日志末尾，让人自己判断。

**停止。** `POST /api/levi/marvel0/deploy/stop` 接受 `request_id`、`confirm`（`stop policy server`），以及仅用于在客户端连着时强制停止的 `client_override`（`stop with client connected`；任何其他非空值都被拒绝）。它只停部署时记下的那个单元：

1. `systemctl --user show` 必须报告同一个 `Id` 和完全相同的命令（`ExecStart`），否则返回 `unit_mismatch`，什么都不停；单元已经不在时返回 200，带 `already: true`；
2. 端口上有单元并不拥有的监听者时返回 `foreign_listener_not_stoppable`（本面板没有记录而手动启动的服务器同样如此：请在它的终端里按 Ctrl+C）；既没有记录也没有监听者时返回 `nothing_to_stop`；
3. **有客户端连着时绝不停止**：处于 ESTABLISHED 状态、由服务器接受的套接字（该端口是它的本地端口），或本机客户端经回环地址连到该端口的套接字，就以 `client_connected` 拒绝；本机向其他主机的 8000 端口发起的外连不算客户端。策略服务器一消失，客户端不会自己回位或恢复手臂，所以在客户端下面切断服务器，可能让手臂停在运动中途。这项检查做两次，间隔 0.3 秒，第二次在记录标为 `stopping` 之后、执行停止命令之前；如果直到这时才出现连接，记录退回到之前的阶段（phase），停止被拒绝。只有输入 `client_override` 才会继续，此时答复里带一条警告，提醒把手放在急停上；
4. `systemctl --user stop <单元>`：服务器收到 SIGINT，到了 `TimeoutStopSec` 之后由 systemd 结束该单元自己的进程。LEVI 自己不发任何信号，也绝不按进程号、端口或名字模式去停任何东西；
5. 接口最多等 `stop_timeout_s` + 30 秒，等单元消失、端口空闲，然后返回 `idle`（`stopped: true`），仍在停时返回 `stopping`。单元已经没了而端口还被占着，或再请求一次停止后仍在 deactivating，就是 `stop_incomplete`：请自己运行 `systemctl --user stop <单元>`。

到该端口的短暂连接（别的工具做的健康探测）同样会挡住停止，直到它结束。

**确认语。** 写操作的 `confirm` 必须与确认语完全一致，点名动作和模型，否则以 `confirm_required`（400）拒绝。页面的确认弹窗替人发送它，脚本自己发；仍然手输的只有有客户端连接时停止的额外短语：

| 动作 | 确认语 |
| --- | --- |
| 部署 | `deploy <模型 id>` |
| 停止 | `stop policy server` |
| 客户端连着时停止（`client_override`） | `stop with client connected` |

**请求。** 每个写操作都带 `request_id`（8 到 80 个字符，取自 `A-Z a-z 0-9 . _ : -`）。同一个 id、同样的内容（不比较确认语）返回已存的答复并带 `replayed: true`，不会重复执行，LEVI 重启之后同样如此；同一个 id 而内容不同是 `request_id_used`（409）。已被接受而答复没来得及保存的请求（LEVI 恰好在这之间停了）得到 `state: unknown` 和 `request_incomplete`，不会再启动任何东西。被拒绝的请求不保存：原因消除之后可以用同一个 id 再发。

**拒绝**（`detail` 是 `{code, message, ...}`；格式错误的请求体是 FastAPI 自己的 422）：

| 状态码 | code |
| --- | --- |
| 400 | `request_id_invalid`、`confirm_required` |
| 403、401 | `person_only`（Bearer 凭据）、`ui_token`（设了网页 UI 令牌而请求没带或带错） |
| 412 | `site_missing`、`site_invalid`、`state_dir_unusable`、`checkpoint_invalid`、`runtime_files_missing`、`source_file_missing` |
| 422 | `model_unknown`、`fraction_not_allowed`、`cfg_scale_not_allowed`、`cfg_scale_out_of_range` |
| 423 | `busy`（另一个部署或停止正在进行） |
| 409 | `unverified_not_accepted`、`request_id_used`、`source_changed`、`port_taken`、`already_running`、`client_connected`、`policy_unit_exists`、`start_unconfirmed`、`arm_lock_held`、`aeri_run_live`、`gpu_lock_foreign`、`gpu_insufficient`、`nothing_to_stop`、`foreign_listener_not_stoppable`、`unit_mismatch` |
| 503 | 某个来源读不到，所以什么都不假设：`net_table_unreadable`、`systemd_unreadable`、`arm_lock_unknown`、`aeri_registry_unreadable`、`env_unreadable`、`gpu_unreadable` |
| 502 | `start_failed`、`stop_failed`、`stop_incomplete` |

**记录和日志。** 它们放在产品工作区的一个私有文件夹 `outputs/LEVI/workbench/marvel0/deploy/` 里（目录权限 0700，文件权限 0600），绝不放在 rollout 根目录之下或源数据里；这个文件夹是链接，或会落在 rollout 根目录、检查点文件夹或 openpi 检出之内时，会被拒绝使用。`record.json` 是本面板知道的那一次部署：单元名、请求 id、模型、配置、RTC、比例、CFG 系数、完整的 `systemd-run` 和服务器命令、日志文件名、阶段 phase（`intent`、`started`、`start_failed`、`aborted`、`stopping`、`stopped`）、各时间、检查时的显存数字、GPU 锁的使用方式、源文件哈希，以及切断过连着的客户端的停止之后的 `stopped_with_client`。`requests/` 每个被接受的请求一个文件（保留最新的 200 个）。`logs/<单元>-<UTC 时间>.log` 每次部署一个日志，由 LEVI 创建、由单元追加写入（保留最新的 10 个，下一次部署时清理；长时间运行的日志在此之前一直增长）。`history.jsonl` 是审计，每个接受、拒绝、启动、停止或释放锁的事件一行，不含路径、令牌或密钥（256 KiB，保留一份轮转副本）。接口只用文件名回答日志；没有任何接口接受路径。

#### 客户端命令面板

在页面上，**评测客户端命令**卡片里有表单（模型、任务指令——以站点已知的任务指令作提示（受限模型则是它的那两条）、每个片段的最大步数、评测数量、最低高度、RTC 及其步数、数据分组、备注；任务指令、步数、评测数量和最低高度从站点的默认值开始；分组留空则用占位符里显示的默认值，备注留空则用一条写着模型、RTC 和执行步数的默认备注）、**由站点文件固定**的参数列表，以及**生成命令**按钮。生成的命令旁有**复制**按钮，以及“本页面没有运行它”的说明；命令生成之后表单又改了，命令会标为过期，必须重新生成。命令下面，卡片会说明策略服务器卡片里的服务器是否适合这条命令（模型和 RTC 模式一致、仍在启动、没有就绪、是别的模型、没有按 RTC 部署，或是手动启动的因而无从得知），并在“运行前请注意”下列出警告。

`POST /api/levi/marvel0/command/preview` 接受一份封闭的表单：`model_id` 和 `prompt`（任务指令），以及可选的 `max_steps`、`eval_num`（片段数）、`min_z`、`rtc`、`rtc_horizon`、`rollout_group` 和 `note`。表单以外的字段（`levi_mode`、`max_xy`、`allow_unknown_prompt` 等）一律拒绝；类型是严格的。默认值来自站点文件：步数和片段数、`min_z_default`、RTC 开并取站点的执行步数（关掉 RTC 时，`rtc_horizon` 是开环执行步数）、rollout 组 `<模型>_rtc_demo`（不用 RTC 时是 `<模型>_demo`）和备注 `marvel0 page: <模型> rtc=on horizon=<n>`。

界限（拒绝时返回 `422`，带 `{code, field, message}`）：`max_steps` 和 `eval_num` 为 1 到站点上限（`max_steps_out_of_range`、`eval_num_out_of_range`）；执行步数为 1 到 RTC 的上限，关掉 RTC 时为 1 到开环上限（`rtc_horizon_out_of_range`）；`min_z` 是有限的数，大于 0（`min_z_not_positive`），不低于站点下限（`min_z_below_floor`），并且低于起始高度（`min_z_above_home`）；rollout 组是由字母、数字、`.`、`_`、`-` 组成的短名，至多 80 个字符，以字母或数字开头，不含 `..`（`rollout_group_invalid`）。

**任务指令**是单独一个 argv 元素，从不经过 shell。为空、超过 300 个字符（或站点设的更低上限）、含引号、反斜杠、`$`、反引号或 `!`、含控制字符（普通空格以外的空白也算），或以短横线开头时会被拒绝（`prompt_invalid`，附 `reason`：`empty`、`too_long`、`forbidden_char`、`control_char`、`leading_dash`）；受限模型只接受客户端那两条任务指令（`prompt_unknown_for_model`）。备注遵守同样的规则，上限是它自己的（`note_invalid`）。

**固定参数从来不是表单字段。** 所有关系到机械臂安全的参数只来自站点文件，任何表单都改不了：相机序列号（两个都总会写出）、策略服务器和机器人服务器的地址、起始位姿及其时长、`--max-xy`、`--max-step-m`、`--max-step-rad`、三个角度范围、`--binarize-gripper`、`--require-matching-server`、`--allow-unknown-prompt`（站点设为 false 时写成 `--no-allow-unknown-prompt`）和 rollout 根目录。无人值守模式永远不会出现：`--levi-mode=off` 是常量，因为 `on` 会在没有人在起始关口把关的情况下启动。`--new` 和 `--no-step-by-step` 也是常量。`--min-z` 总会明确写出。

**命令是要复制的文本，从不执行。** 返回内容（`levi.marvel0.command.v1`）有 `argv`、`env`、`cwd` 和 `command`，其中 `command` 是形如 `cd <openpi 检出> && env -u VIRTUAL_ENV no_proxy=... uv run --no-sync --with pyrealsense2==2.55.1.6486 python examples/fr3_local/run_robotiq_client_collect_data.py ...` 的文本，每行一组参数，每个词都做了 shell 引用，供粘贴到评测客户端终端。这是 AERI 用的写法（openpi 的 `.venv` 里没有 `pyrealsense2`）。每个参数都是单个 `--flag=value`，只有 `--home-pose` 和三个范围的数字例外：它们作为单独的几项跟在参数后面（客户端的解析器就是这样读的），开关写成 `--flag` 或 `--no-flag`。命令里的 openpi 检出和 rollout 根目录是绝对路径，因为 shell 不会展开 `=` 之后的 `~`；除 `looked_at` 之外，返回内容里只有这两处有路径。命令返回之前会再按格式检查一遍（只有已知参数、没有重复、数字个数正确、表单的值再次通过校验）；和站点文件及表单允许的内容不一致的命令不会展示（`500 command_self_check`）。返回里还有：

- `server_requirements`：模型和配置，以及策略服务器是否必须按 RTC 部署（带 `--rtc` 的客户端需要 RTC 服务器）；
- `warnings`（从不拒绝）：`unverified_model`；`rtc_margin_low`，即执行步数加上约 3 拍推理延迟超过一个动作块的 10 个动作（执行步数大于 7）；`min_z_below_task_table`，即 `min_z` 低于客户端为这条任务测得的下限（螺丝命令 0.075，盘子命令和其他所有命令 0.05325）：显式的 `--min-z` 会盖过客户端自己的表，所以更低的值会降低那项任务的下限；
- `hands`：手放在物理急停上；本页面从不启动机械臂栈或这个客户端。

#### 限制

- **不是急停。** 见上文。机械臂栈和评测客户端留在终端里；手始终放在物理急停上。
- **端口上只有一个策略服务器。** 真正起约束作用的是端口表、策略单元列表和机械臂锁，它们是整台机器共有的。两个 LEVI 核心（产品版和测试 worktree）各有自己的部署文件夹，所以它们的操作锁互相排斥不了。
- **被动检查关不上的竞争。** 客户端可能在第二次套接字读取与停止的 SIGINT 之间连上来；手动启动的服务器可能在最终检查与单元自己绑定端口之间抢先绑定（单元的绑定随即失败，日志里是 `address_in_use`）。还在终端里加载的服务器（python 已启动、尚未监听）对所有检查都不可见，所以部署对话框写明：不要在终端里手动启动策略服务的同时点“部署”；页面要等它开始监听才看得到它。
- **状态接口。** 面板的单元运行期间，`GET /api/levi/marvel0/status` 会把机器人报告为忙，和任何正在运行的 `levi-aeri-*` 单元一样。
- **GPU 锁**只按上文所述的方式使用。面板不读任何按进程的 GPU 数据。
- **只有这台机器才能证明的事。** `systemd-run` 的各项属性、用户管理器的真实环境、显存占用的时间点，以及这台机器上服务器的确切日志文本，都需要有人监督着真实运行一次；测试只用假对象，不连接任何端口。第一次真实部署要在 marvel0 的主人在场、机器人空闲（没有客户端、没有机械臂栈）时进行，并且主人事先确认过 `cpu_affinity`；systemd 不接受的属性会以 `start_failed` 出现，并带着 systemd-run 自己的话，验证过的列表再写进站点文件。
- **前端样本。** `marvel0-deploy.samples.json`（有一个测试用页面自己的读取器解析它）是在假机器上由后端生成的；它与后端不一致时 `tests/marvel0/test_marvel0_deploy_sample.py` 会失败，并写出重写它的命令 `uv run --no-sync python tests/marvel0/regen_deploy_sample.py`。

### marvel0：机械臂栈卡片与健康监视器

`GET /api/levi/marvel0/stack`（`levi.marvel0.stack.v1`，同样的本地认证；缓存 2 秒）只用被动来源说明机械臂栈各成员的状态：健康监视器 M、机械臂控制器 A、夹爪栈 B、机器人服务器 C、策略服务器和评测客户端。它读取几个具名单元的 `systemctl --user show` 和用户管理器的默认限额（`DefaultLimitRTPRIO`、`DefaultLimitMEMLOCK`）、`loginctl show-user`（Linger）、内核的套接字表（由台账的 `arm_guard.py` 判断到机械臂的套接字，不计标记文件）、监视器写的健康文件（实时配置的 `fr3.health_file`）、机械臂控制器的标记文件（只 `lstat`；也给出 `ctime` 年龄，`touch -d` 改不回去）、评测会话文件，以及本进程自己的 `/proc/self/cgroup`。它不读任何其他进程的 `/proc/<pid>`，也不经设置探针的进程扫描间接读（不用 `guard.robot`），不连接任何端口，包括机械臂的。A 看到机械臂的套接字和新的健康文件；C 和策略服务器看各自端口的 LISTEN 行（5000/5001 和策略端口；不查套接字属主）；客户端看到策略端口的 ESTABLISHED 连接或未结束的评测会话（还没连上的看不到：`client.not_connected`，未知）。B 没有已知的被动证据（监视器也不问夹爪的控制器管理器），所以总是 `unknown`。**规则 R**：有任何一层在运行时 `verdict` 为 `busy`，否则只要有一层未知就是 `unknown`，只有每一层都看到已停才是 `idle`（未连上的客户端只在 C 和策略服务器都已停时按已停计）；M、A、B、C 有一层未知时每个启动门都拒绝（`gate.layers_known`，C 还有 `gate.b_known`）。客户端不计入 `gate.layers_known`：它只能在 C 和策略服务器之后连上，所以由 C 门的 `gate.no_client` 判断；又因为没连上的客户端看不到，C 门还有一项由人回答（`gate.person_no_client`：“确认没有评测客户端（终端 E）在运行或在等待连接；客户端连上之前页面看不到它”）。这时客户端一行写“连上之前看不到”，从不写“已停止”。套接字表只有在 `tcp` 读到、且 `tcp6` 读到或在没有 IPv6 的主机上不存在（ENOENT，且 `/proc/net/if_inet6` 也不存在）时才算读到；权限错误、其他错误、无法解码的内容，IPv6 开着却没有 `tcp6`，或者有一行解析不了（字段数不对，或端口、状态、inode、地址解析失败；表头和空行不算），都算未知，原因写 `N unparsable lines in <表名>`。C 已停但阻抗控制器仍在时是 `c.stopped_impedance`：A 停之前手臂仍被控制着。

对 A、B、C，答复带着**每个计划中的启动门的演练**（`gates.A|B|C`：`verdict` 为 `would_start` 或 `would_refuse`、拒绝的 `reasons`、每项检查的 `ok`（true、false 或 null）和 `level`：`required`、`warn` 或 `person`）。`route` 恒为 `false`：**没有任何路由能启动或停止 A、B、C。** A 之前的 FCI 检查（对机械臂的一次 TCP 连接）在主人决定之前不做，所以 A 的演练总是因 `gate.fci_probe` 拒绝。

写操作只能由人做（与策略部署相同的检查：拒绝 Bearer 凭据），带 `request_id`（重复请求只回答、不重做）和 `confirm` 里的短语（页面的确认弹窗发送）：

| 路由 | 短语 | 做什么 |
| --- | --- | --- |
| `POST /api/levi/marvel0/stack/monitor/start` | `start health monitor` | 以临时单元 `levi-fr3-health-monitor` 启动 M（`systemd-run --user`、`KillSignal=SIGINT`、`TimeoutStopSec=10`、`Nice=10`、`--collect`、`Restart=no`），运行健康文件 `run/` 目录旁的监视器脚本（`<franka_control>/scripts/fr3_health_monitor.sh`） |
| `POST /api/levi/marvel0/stack/monitor/stop` | `stop health monitor` | `systemctl --user stop levi-fr3-health-monitor.service`；A 或 C 在运行或无法排除时拒绝（M 最后停） |
| `POST /api/levi/marvel0/stack/marker/clear` | `clear marker` | 只删除 `lab/levi-hub/.arm-controller-active`；仅当 A 的单元不存在、到机械臂的套接字没有控制连接、且 A 显示已停（没有新的健康文件说硬件接口已激活）时 |

名为 `levi-fr3-health-monitor.service` 的单元，只有 `systemctl show` 报告的 `Id` 和 `ExecStart` 恰好是它和监视器脚本时才算页面的；这时页面认领它，从不替换。同名但命令不同的单元是 `foreign`，从不碰；没有该单元而健康文件是新的，说明监视器在别处运行（`elsewhere`：终端），不再启动第二个。

**M 的自动管理**（`LEVI_MARVEL0_MONITOR_AUTO`，默认 `off`；设为 `on` 才开启；在用户在场的真机上验证之前保持关闭）。开启时，产品 LEVI 每 5 秒检查一次：看到 A 或 C 在运行而 M 没在运行就启动 M；M 挂了就再启动（10 分钟内最多自动启动 3 次）；A 和 C 都显示已停满 120 秒后才停止 M。证据未知、单元是别人的、监视器在别处运行时什么也不做。监视器对机器人只读（订阅一个话题、读两个列表服务、写健康文件）。

**只在产品里生效，加一把整机锁。** 只有在主检出运行、属于 `levi-product.service` 的进程才对真机动作（profile `real`）；其他任何实例，包括 worktree 和临时的页面检查实例，都是 profile `fake`：只显示栈，每个写操作都答 `not_product`，自动管理也不运行。每个写操作和每个自动动作都持有主检出 `lab/levi-hub/.marvel0-stack.lock` 上的 `flock`（从不等待：第二个动作被拒绝 `stack_busy`，423）；文件里写着谁持有它，答复里的 `lock.state` 来自 `/proc/locks`。

**这些门的作用。** 它们只防误操作，防不了同一账号下的其他程序：任何以本用户运行的程序都能发同样的请求、伪造页面的请求头或改这里读取的文件。真正的屏障是 Franka Desk 和物理急停。`Linger=no`（`linger.state`）时，账号最后一次登出会在几秒内停掉所有用户单元，终端里起的栈也一样；A 可能在运行时答复带 `notice.logout_stops_stack`。
