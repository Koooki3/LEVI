# 上机向导：配方与状态探针

[English](SETUP_WIZARD.md) | 中文

LEVI 可以引导操作者准备一次真机会话：下一步运行哪条命令、它会动到什么、它要启动的组件是否已经健康运行。本页说明目前已有的两部分：**配方注册表**（把每一步和本机操作手册对应起来）和 `GET /api/levi/setup/status` 背后的**只读状态探针**。两者都不启动、不停止、不连接任何东西。只读的**机器人台**页面建立在探针之上，见“状态探针”一节末尾。

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

### 机器人台（只读）

`GET /api/levi/rig/status`（本机鉴权与上面的接口相同）为**机器人台**页面（`/rig`；它不是顶栏链接，从命令面板 `Ctrl+K` 或跳转组合键 `G C` 打开）回答一个问题：现在看得到机器人活动吗？返回文档的 schema 是 `levi.rig.status.v1`，有四个部分。和上面一样，读不到的部分标 `unknown`（附 `detail`），不影响其他部分。

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

`reasons` 只列出处于 busy 或 unknown 的来源，busy 的在前。每项有 `source`（`guard`、`marker`、`eval_sessions` 或 `units`）、`state`（`busy` 或 `unknown`）、英文的 `text`、稳定的 `code` 和 `params`；状态为 `idle` 时为空。`text` 是服务器自己写的英文句子，原样显示，措辞可能改动。`code` 是这种情况的稳定名称（`来源.情形`），`params` 是只含字符串和数字的扁平对象，装着这种情况需要的值（名称、以秒为单位取整到 0.1 秒的年龄；从不含路径），所以页面可以自己翻译这种情况并填入这些值。`code` 和 `params` 是在不改 schema 字符串 `levi.rig.status.v1` 的情况下加的：旧的使用方仍能读到 `source`、`state` 和 `text`，使用方应把这两个新字段当成可选的。每种会话最多列 5 个，每种单元最多列 5 个，进程名最多列 5 个。

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

**只被动读取。** 这个接口从不连接机器人端口，不启动、不停止、不写入任何东西，也从不清除标记文件。它背后只有只读查询：`nvidia-smi`（经由状态探针）和针对 `levi-aeri-*` 单元的 `systemctl --user list-units`。机器人台自己的各部分不读任何进程目录；返回内容不含路径。`guard` 部分就是状态探针自己的那一份（`probes.status()`，与 `/api/levi/setup/status` 共用缓存），所以请求机器人台接口会取用或复用那次采样，并沿用它的采样方式，包括上文所述的进程扫描和套接字所属进程扫描。机器人台的每个部分在自己的线程里读取并有截止时间。`units` 和 `eval_sessions` 的结果复用 2 秒；某个部分正在读取时到来的请求，会等这次读取（最多等到该部分的截止时间）并使用它的结果，只有截止时间过了，才判定该部分卡住（报 `unknown`，立即回答）。

**终端 A、B、C 不受控制。** 页面是只读的。机械臂栈（A）、夹爪栈（B）和机器人服务器（C）按 setup.md 的说明手动启动和停止。终端 A 和 C 的卡片显示 FR3 健康文件和内核套接字表的内容；终端 B 的卡片把控制器状态显示为 `unknown`，因为夹爪栈没有页面可以读取的状态文件或端口。

**页面。** 顶部横幅显示上述结论，下面每个组件一张卡片：机器人侧（Desk：模式与 FCI、终端 A 机械臂栈、终端 B 夹爪栈、终端 C 机器人服务器、终端 M 健康监视器、策略服务器、评测客户端）和本机的服务与 GPU（实时服务、视觉语言模型、GPU）。卡片有一个标签（正常、故障、已过期、未启动或未知）、一句话说明、依据的事实，以及这个组件怎么启动（setup.md、AERI.md）。“故障”指健康监视器亮红灯（镜像监视器的卡片，句子里仍写“红灯”）、没有策略服务器在监听时策略单元失败，或评测会话报告了故障。

*请求。* 标签页可见时每 3 秒请求一次 `/api/levi/setup/status` 和 `/api/levi/rig/status`，隐藏时不请求。6.5 秒内没有回答的请求会被放弃并中止，所以一个永远不回答的请求拖不住轮询；两个请求都失败的一轮之后，等待时间加倍，最长 30 秒。请求失败或标签页重新可见时，超过 10 秒的旧结果会被丢弃，不会当成当前状态显示；其采样在送达时已有的年龄也算在内（状态探针返回的 `age_s`），所以一份送达时已有 6 秒的缓存样本，再过 4 秒就会被丢弃。

*旧样本。* 状态探针的返回是旧样本时（服务器标了 `stale`，或其 `age_s` 超过 10 秒），页面上会有一条说明，依据它的每张卡片都显示“已过期”并写明样本多旧（除终端 B 夹爪栈卡片，它没有数据来源，和评测客户端卡片，它读的是机器人台状态）。样本当时的结论留作卡片的第一条事实（“当时的采样结果”），读数也保留在下面，但都不再是卡片现在的结论：旧样本里的红灯不显示为“故障”。本来就没有内容可显示的卡片仍是“未知”。状态探针的返回没有 `age_s` 时，页面不把它当作旧样本（上文的结论则把它当作旧样本）。

*原因。* 横幅按每条原因的 `code` 和 `params`、用页面当前的语言自己写出这条原因，所以中文页面上，页面认识的原因不会出现英文句子。页面不认识 `code`、`params` 缺了句子必需的值，或这条原因没有 `code`（旧版服务器）时，改为原样显示服务器的英文 `text`。原因的顺序是：先是显示有活动的，再是无法判断的，最后是其他的，每一类内部保持服务器的顺序，最多 12 条（上限在排序之后再取）。标记文件、评测会话或单元读不到时，对应卡片上的说明同样按 `marker.unreadable`、`marker.no_folder`、`sessions.unreadable` 或 `units.unreadable` 用页面的话写出；没有这些 `code` 时，原样显示该部分的 `detail`。

*横幅。* 同一次返回里的 `marker`、`eval_sessions` 或 `units` 部分是 `unknown`，或返回里有 schema 而它不是 `levi.rig.status.v1`，或返回说 `idle` 却仍列着原因时，横幅降为“无法判断”。

*卡片。* 评测客户端卡片取下列第一条成立的：最近 60 秒内更新过的会话报告了故障，为“故障”；有未结束的会话在最近 60 秒内更新过，为“正常”；有会话早先报告过故障，为“故障”（附该会话最后一次更新的时间，只要服务器还列着这个会话就一直是“故障”，即最后一次更新之后的 10 分钟内）；有未结束的会话最近一次更新在 60 秒以前，为“已过期”；否则为“未启动”（列出的会话都已结束，或没有会话）。一个会话也读不到，或者列表可能不完整而结果本该是“未启动”时，为“未知”。已有 12 小时或更久的标记文件显示为“可能是崩溃后遗留的，也可能属于一次很长的运行”。手臂栈没起来时，健康监视器自己会亮红灯；红灯的原因只有 `hardware_interface_missing`、`hardware_interface=`、`controller_manager_unreachable` 或 `robot_state_no_data`，机器人没有报错，且硬件接口没有被报告为激活时，终端 A 的卡片显示“未启动”，并说明红灯是因为终端 A 没在运行。硬件接口已激活时，这些原因是机械臂或其连接的故障，仍显示“故障”，不管控制器标志怎么说。页面上没有任何东西是急停或安全检查，卡片显示绿色也不代表可以靠近机器人。
