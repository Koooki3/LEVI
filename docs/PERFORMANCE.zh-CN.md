# 性能基线

[English](PERFORMANCE.md)

LEVI 已经保存了三类耗时与成本记录：实时标注服务的 `live/stats.jsonl`（见[实时标注服务](LIVE.zh-CN.md)）、Agent 用量样本（`levi/agent/usage.py`）、每次运行的 harness 成本（`levi/harness/cost.py`）。本页说明建立在它们之上的只读视图和 CPU 基准。两者都不另存数据，不用 GPU，不调用模型，也不接触机器人。

## 追踪（trace）

```
python -m levi.performance trace --live-dir <工作区>/live --workspace <工作区> [--since EPOCH] [--limit N] [--summary-only]
```

`--workspace` 是 LEVI 工作区（即 `LEVI_WORKSPACE` 文件夹）：用量样本读自其中的 `outputs/LEVI/workbench/agent/workbench.sqlite3`，成本记录读自 `outputs/LEVI/datasets/*/tasks/*/ledger.json`；直接给 workbench 文件夹也可以。

输出一份 JSON（`levi.performance.trace.v1`），由 `live/stats.jsonl`、`live/gate.jsonl`、Agent 用量样本和已关闭运行的成本记录合并而成，每个来源都可省略。对每个已标注的片段，它给出 `trace_id`（由数据集、demo、会话、批次和片段序号哈希得到，不落盘）、各阶段耗时（镜像、规划、首个请求、提交、判定）、模型耗时与 token、不在模型里花掉的时间，以及片段期间 GPU 闸门关闭了多久。`summary` 给出各项的中位数、p90、最大值和均值。`unmeasured` 列出目前没有任何记录的项目（帧解码、显示时间戳扫描、哈希、排队时间、内存峰值）。用量样本以只读方式打开；闸门文件里的会话名不会被复制。

## 基准（bench）

```
python -m levi.performance bench [--case hash|pts|pixels] [--repeat 3] [--frames 182] [--size 640x480] [--fps 10] [--cores 4] [--scratch 目录]
```

用 ffmpeg 在临时目录生成合成 H.264 视频（结束后删除），在至多 4 个 CPU 核、低优先级下计时：

| 项目 | 计时内容 |
| --- | --- |
| `hash` | 整个文件的 SHA-256 |
| `pts` | 用解码帧和用数据包两种方式列出全部显示时间，并报告两份列表是否完全相同 |
| `pixels` | `levi.conversion.media.inspect(path, pixels=True)`，即转换时的像素统计，两种 `LEVI_PIXEL_STATS` 方法都测（耗时、加速比、最大差值） |

结果（`levi.performance.bench.v1`）含参数、机器、库版本，以及每项的中位数、95 分位和最小秒数。请在同一台机器上比较两次运行；绝对数值不可跨机器比较。

## 显示时间戳扫描（`LEVI_PTS_SCAN`）

证据帧是按视频的显示时间戳找到的，不是用帧号除以 fps。LEVI 对每个视频只列一次这些时间，并把列表缓存在证据旁边（`*--video-index.json`，以文件的 SHA-256 为键）。

| `LEVI_PTS_SCAN` | 含义 |
| --- | --- |
| `frame` | 原有做法：`ffprobe` 解码每一帧并给出 best-effort 时间戳。设 `LEVI_PTS_SCAN=frame` 即可切回。 |
| `packet`（默认） | 改读容器的数据包时间戳，不解码：在实测的合成视频上快 4 到 80 倍，对普通文件（B 帧、可变帧率）列表相同。它信任容器，所以只要容器里的数据包与显示的帧不是一一对应就让位：数据包带丢弃标志（`D`）或损坏标志（`C`，MP4 编辑列表或起点早于 0 会产生前者）；时间戳为负；ffprobe 在 stderr 报了任何问题（文件被截断或损坏）；某个数据包没有时间戳；没有数据包；时间不严格递增。这时改走帧扫描，并记一条含文件名和原因的警告日志。缓存文件会额外记下 `"scan": "packet"`，另一种扫描写的缓存不会被使用。 |

其他取值会被拒绝。两种模式下，同一视频的列表都按内容哈希保存在内存里（16 个视频），所以被许多片段共用的 v3 文件在一个进程里只扫描一次；每个片段每个相机只对文件算一次哈希（哈希值传给扫描，不再重算）。检查不变：缓存仍与源文件哈希绑定，后面的时间不一致容差照常生效。

在真实的开发集视频上比较过两种扫描之后，`packet` 已成为默认值（2026-10-10：1520 个文件、278952 帧，列表全部相同，快约 7 倍；结果见下）。切换前写下的缓存没有 `"scan"` 键，按帧扫描缓存处理：第一次读取会重扫一次，并改写成数据包格式。要重复这次比较：

```
python -m levi.performance pts-compare <视频或文件夹> [...] [--cores 4]
```

两个比较命令都会把自己限制在至多 4 个 CPU、低优先级（`--cores`，0 表示不处理）。`pts-compare` 还会逐文件列出数据包为什么不能用。

它每个文件打印一行；列表有差异或文件无法扫描时退出码为 1。

在开发集和实时镜像的 1520 个 H.264 640x480 文件上实测（1400 种不同内容；含 B 帧，其中许多是可变帧率）：数据包列表与帧列表逐值相同的有全部文件，帧数与 OpenCV 解码出的帧数相同的有全部文件，没有文件需要回退；数据包扫描耗时中位 21 毫秒，帧扫描 148 毫秒（约 7 倍；当时页缓存是热的，冷盘未覆盖）。开发集里没有带编辑列表、负时间戳或损坏的视频，没有其他编码，也没有共享的 v3 视频文件；这些情形靠 `tests/test_pts_scan.py` 里的合成夹具和回退到帧扫描来保证。

## 源视频被哈希的次数

凡是保护证据不可变性的哈希都保留。对每个文件，快照会哈希复制前的源文件、副本、复制后的源文件（从而发现复制过程中的改动）；计划时的哈希在任何复制之前比较；提交前的检查再对源文件哈希一次；每张被复用的缓存证据 PNG 在复用前都会重新哈希。`levi/agent/media.py` 里唯一发现的冗余在 `sample()`：它先为缓存键哈希一次视频，又在时间戳索引里再哈希一次，现在把哈希值传了下去。这些次数在 `tests/test_media_hashing.py` 中有断言，旁边是对各阶段做篡改的测试。

## 转换的像素统计（`LEVI_PIXEL_STATS`）

`levi.conversion.media.inspect(path, pixels=True)` 统计所有解码像素每个通道的最小值、最大值、均值和标准差，结果进入 `meta/stats.json` 和训练归一化。

| `LEVI_PIXEL_STATS` | 含义 |
| --- | --- |
| `float` | 原有方法：每帧转成 float64 再求和。设 `LEVI_PIXEL_STATS=float` 即可切回。 |
| `histogram`（默认） | 按通道数每个 8 位取值出现的次数（3 x 256 个整数），再由计数一次性算出四个统计量。求和是精确的整数，方差没有相消误差。每帧约快 11 到 17 倍；min、max、mean、count 与 float 方法相差约 1e-11 或更小。标准差按方差比较：float 方法把方差算成 `mean(x^2) - mean(x)^2`，通道为常值或接近常值时会相消，开方后会把 1e-12 的误差放大成最大约 1e-6 的噪声（常值 720p 通道得到 2e-6，真值为 0）。histogram 的结果是精确的。不是 8 位的帧仍用 float 方法。 |

其他取值会被拒绝。在 61 个真实开发集视频上比较过两种方法之后（2026-10-10），`histogram` 已成为默认值：min、max、mean、count 的最大差值为 9e-13，方差为 1.2e-12（std 本身 1.6e-12），histogram 方法耗时中位快 23 倍（3.5 到 30 倍）。所以 `meta/stats.json` 的末几位会与 float 方法写出的文件不同，差别远小于训练归一化能感知的程度；对常值通道，新值才是精确值。要重复这次比较（只要有文件的 min、max、mean、count 或方差（std 的平方）相差超过 1e-9，或帧数、span、首帧哈希不同，命令就退出 1；std 本身的差值会报告但不作判据）：

```
python -m levi.performance pixel-compare <视频或文件夹> [...] [--cores 4]
```

`bench --case pixels` 在合成视频上给两种方法计时，并报告加速比和最大差值。

## 有硬上限的缓存目录（`DiskBudget`）

`levi.performance.budget.DiskBudget` 让一个缓存目录自己守住字节上限（可选再加条目数上限）。原因是只增不减的缓存迟早写满磁盘：几百个片段的证据是几 GB，几千个是几十 GB，而每出现一个新的 schema、模型或提示词版本，就又是一份。上限在写入时由写入者自己执行，而不是靠一个可能来得太晚的清理器。

它只是一个库。**LEVI 里目前没有任何地方使用它**，不改变任何行为，也没有设置项。只用标准库，不导入 LEVI 的其他模块。需要本地文件系统（用到 `flock` 和 `rename`），不支持网络文件系统。

```python
from levi.performance.budget import DiskBudget, BudgetRejected

cache = DiskBudget.from_cap_gb("/path/to/evidence-cache", 20, version="schema-3")   # 20 GiB
cache.put("episode-17", png_bytes)                  # 先预留空间，需要时淘汰最久未使用的条目
cache.read("episode-17")                            # 返回字节或 None；命中算一次使用

with cache.writing("episode-18", reserve=40_000_000) as tmp:    # 一个空目录，往里写
    with cache.open_file(tmp / "0001.png") as f:                # 写到预留量就停
        f.write(frame)
# 正常退出时，目录被改名成为条目

with cache.get("episode-18") as lease:              # 读租约：持有期间条目不会被改动或删除
    first = (lease.path / "0001.png").read_bytes()

try:
    cache.put("x", data)
except BudgetRejected as why:       # why.reason：too_large | busy | no_space | no_output | reclaimed | unavailable | lock_timeout
    ...                             # 调用方不用缓存继续往下走
```

读取也可能被拒绝（`get`、`read` 会抛 `BudgetRejected("lock_timeout")` 或 `"unavailable"`）；每次调用都要当作可能说“不”。

| 行为 | 说明 |
| --- | --- |
| 上限也管正在写的数据 | 写入者先申请空间再写：`put` 预留 `len(data)`，`writing(reserve=N)` 预留 `N`（默认预算的四分之一），再各加上条目 `.meta` 文件的实际大小（键和版本字符串的 UTF-8，最多 4 KiB，所以长键或非 ASCII 的键占得更多）。已提交的条目加上所有存活的预留不会超过 `max_bytes`，所以会淘汰最久未使用的条目来腾地方；腾不出来的写入在写任何数据之前就被拒绝（`too_large`：永远放不下；`busy`：其余空间被别的写入者的预留或读者的租约占着）。通过 `open_file` 写的字节到预留量就停；直接往目录里写的内容在提交时测量（也可随时用 `charge(tmp)` 测），超出就拒绝。所以不论有多少进程在写，磁盘上这个目录最多是 `max_bytes` 加每个存活写入者几百字节的记账文件，有两个例外：无视自己预留的写入者（不用 `open_file` 也不调 `charge`）在提交之前仍能占用磁盘空间；已被淘汰但尚未删完的条目按占用计算（见“崩溃安全”），而不是算作空闲。 |
| 条目 | 条目是一个目录，名为 `<键哈希>.<版本哈希>.<代>.<大小>`，里面是内容和 `.meta`。名字里带着记账需要的信息，所以没有会与目录不一致的索引。条目提交后不可修改：提交时测量大小，之后的检查把变小的条目视为损坏。重写同一个键会生成新的一代；旧的一代在下一次提交或整理发现没人读时删除。`put`/`read` 在条目里使用一个文件 `data`。`entries()` 返回完整的键和版本字符串。 |
| LRU | 条目最后一次使用的时间，是它的目录和其中 `.meta` 文件的修改时间；`get`、`read` 会同时刷新两者，所以只看文件时间的工具也能看到使用。 |
| TTL | 闲置超过 `ttl_s`（默认 14 天）的条目视为过期：不会被返回，在下一次整理或需要腾地方时删除。闲置时间在缓存被使用时执行；没人再打开的缓存不会被触碰（见“被弃用的缓存”）。 |
| 版本 | 每个条目都带版本字符串（schema、模型或提示词版本）写入。其他版本的条目是过期版本：不会被返回，先于有效条目被淘汰，闲置超过 `stale_grace_s`（默认 7 天）后删除，或由 `purge_stale()` 立即删除。 |
| 读租约 | `get` 返回 `Lease`（条目上的共享 `flock`）。有租约的条目不会被本类淘汰、替换或删除；如果租约占得太多导致写入放不下，写入会以 `busy` 被拒绝。用 `close()` 或 `with` 释放租约，被垃圾回收时也会释放，`fork` 出的子进程不会继承租约。以其他方式拿到的路径只是个提示。 |
| 原子写入 | 数据先写入 `.budget/w` 下的私有目录，`fsync`（`durable=True`，默认）后一次改名到位。读者要么看到完整条目，要么看不到。`fsync` 失败会中止提交（`ENOSPC`、`EDQUOT` 报为 `no_space`，其他错误原样抛出）；改名之后对缓存目录的 `fsync` 失败时，先让条目重新不可见再抛出错误；不支持对目录 `fsync` 的文件系统则忽略这一步。重写失败时，同一个键的旧一代可能已经被删除（对缓存来说是一次未命中）。 |
| 崩溃安全 | 每个写入者活着的时候，在 `.budget/w` 下对自己的记录持有一把独占 `flock`。能拿到这把锁，就说明写入者已死（包括被 SIGKILL；与 pid 复用、文件时间、时钟都无关），它的记录和数据会被回收，没有记录的临时目录也一样；存活的写入者无论写多久都不会被回收。删除条目时先改名进 `.budget/trash`（大小写在名字里，删完之前仍计入上限），所以删到一半崩溃不会留下看起来有效的残缺条目。缺 `.meta` 的条目，或比提交时变小的条目（被其他工具原地删了一部分），被视为损坏，由 `maintain()` 和 `get` 丢弃。整理在创建对象时、`maintain()` 中、以及读写过程中至多每 `housekeeping_s`（默认 10 分钟）一次，所以长期运行的进程也会回收死去的写入者。`maintain()` 还会按当前上限修剪。 |
| 并发 | 所有改动都在缓存目录本身的独占 `flock` 下进行。线程之间、进程之间互斥；进程死亡时内核释放锁。没有可供清理器删除的锁文件。`fork` 出的子进程会立即关掉自己继承的所有锁描述符。 |
| 磁盘满 | 写入时遇到 `ENOSPC` 或 `EDQUOT` 为 `BudgetRejected("no_space")`；其他存活写入者的预留也写满后，磁盘剩余空间会低于 `min_free_bytes`，也是（只在写入前检查一次，并计入淘汰能还回的空间）。不会留下残缺文件。目录或其隐藏子目录被删掉后，下一次调用会重建；临时目录被删的写入者得到 `BudgetRejected("reclaimed")`。 |

规模。每次写和读都会列出缓存目录，每个条目一次 `stat`，所以开销随条目数增长：几千个（每个片段一个条目）没问题，每帧一个条目不行。请保持条目粒度粗。创建对象时会运行 `maintain()`，在锁内遍历每个条目的所有文件一次；频繁启动的短命进程可以传 `recover=False`，由一个长期运行的进程调用 `maintain()`。

**被弃用的缓存。** 闲置时间和上限由使用缓存的一方执行，所以没人再打开的缓存会一直占着它的字节。`claim_abandoned(root, idle_s)` 是留给外部清理器的接口，用来整体删除这样的缓存：只有在该目录是 `DiskBudget` 缓存、没有存活的写入者、没有被租用的条目、并且其中的一切（目录本身、每个条目目录、每个 `.meta`）在 `idle_s` 秒内都没被碰过时，它才在持有缓存锁的情况下产出 `True`；清理器随后用一次 `rename` 把目录改成同级的另一个名字再删除，下一次使用会重建缓存。`is_abandoned(root, idle_s)` 回答同样的问题但不保持锁（只是提示）。

**从外部清理这个目录。** 定时清理脚本仍可作为兜底，前提是遵守两条：跳过以 `.` 开头的名字（那是记账用的 `.budget`），并把整个条目目录当作一个整体。由于最后一次使用既记在条目目录上，也记在条目内的文件上，按条目内最新文件时间排序的脚本看到的顺序与本类一致。脚本删掉整个条目时，本类只会发现它不见了；只删了一部分时，该条目会被判为损坏并清掉剩余部分。

## 显式启用的工作负载准入内核

`levi.live.workload_broker.WorkloadBroker` 是供既有实时控制器使用的进程内
准入内核，没有新增守护进程或资源调度系统。它不探测 GPU、不拿 flock、
不启动 worker、不停止进程、不调模型，也不连接机器人。物理所有权仍由既有
控制器和资源 driver 管理。这一检查点尚未接入控制器或运行服务。

内核复用 `aeri.WorkloadSpec`、`Deadline`、`ResourcePressure`、`ResourceLease`、
`AdmissionRejected` 和 `WORKLOAD_PRIORITY`。有界队列依次优先处理
`policy_realtime`、`judge_critical`、`resident_group`、`vision_light`、`background_annotation`、
`batch`；同类先看最近的截止时间，再按到达顺序。此 GPU 内核明确拒绝只请求
CPU 的工作；`any` 选择 GPU，不暗中回退 CPU。P0 安全、watchdog 和停止路径
不调用或等待这个对象。

`submit` 冻结工作负载的运行、片段、profile SHA-256、完整 p99 耗时与显存
估计。`admit` 要求同运行、同时钟的新鲜压力、既有 GPU gate 开放，以及调用者
授权的同片段、同 profile `SafeWindow`。窗口和所有权证明还须绑定当前 broker
身份。VRAM、策略身份或模型状态未知、模型尚在加载、旧或未来压力、gate
关闭或自相矛盾、缺少内存或耗时估计，都会拒绝准入。flock 可用不代表显存可用
或控制窗口安全。关键判定不允许冷启动；完整耗时估计必须包含启动/唤醒、
推理和结果投递，p99 加余量须同时落在截止时间、包含排队耗时的预算及安全
窗口内。

同一时刻只有一个 GPU 租约或预约占有物理槽，正向与复位轮换也不能并行。
新 broker 默认所有权未知。首次准入前，`prove_idle` 要求可信控制器通过实际
原 worker、子进程及 GPU 检查提供 `IdleProof`；不能使用模型回复，也不能因
队列为空就假定退出。`bind_worker` 记录准确 PID、进程启动 ticks 和 boot
身份；`accepts_result` 要求已有 worker 绑定、准确当前租约/epoch、结果权限
未撤销且截止时间与租约未过期。

取消、过期、压力失效和优先级抢占只撤结果权限，不释放物理槽。
`preemption_request` 向所有者提供 `request_boundary` 或 `cooperative`
请求，不发信号或指令。低优先 worker 只有在 `release_after_exit` 收到同一
worker、全部子进程与本人 GPU 分配的新鲜 `ExitProof` 后，才实际让出。
活着或状态未知的 owner 持续占槽。`release_unstarted` 也须明确核实预约
从未启动 worker；取消后迟到的 worker 仍可绑定，避免丢失所有权。
正在运行的策略不会因模型 gate 被抢占。租约 ID 包含新 broker 身份，每次
发放递增 epoch；旧结果或退出证明不能释放新 owner。

队列默认最多 128 项，上限 1024；满队列明确拒绝并施加背压。过期请求产生
有界拒绝通知，溢出计数可见。快照与抢占信息只是本地状态，没有新增持久
调度状态。调用在线程间原子执行；跨进程仍依赖既有 driver 和 GPU 所有权。

**验证范围**：CPU/mock 测试覆盖并发、优先级、抢占、实际退出证明门禁、
期限与排队耗时、显存余量、压力/窗口新鲜度、旧 epoch 与重启未知状态。
这些测试不能证明实际策略 quiesce、GPU 显存交接或可用的 p99。真实
`running` 仍关闭既有模型 gate，没有删除 busy state 或伪装成 waiting。
可信的运行时策略 quiesce 证明入口尚未接通，因此步数上限前自主介入仍未
验证。控制器接线、真实压力/退出探针、GPU 测量和真机测试是后续独立门禁。

所有权证明还必须绑定当前租约代次，实际 worker 退出证明同时绑定租约 ID；旧空闲证明不能释放新预留。


## 常驻模型与串行请求的所有权（显式启用）

`levi.live.runtime_ownership.RuntimeOwnership` 在原准入 broker 之外维护单独的
请求槽。常驻模型绑定原物理 `ResourceLease`、准确 PID/启动 ticks/boot 身份，
以及本实例 ID 和 runtime epoch。同 broker 只允许一个请求权限管理器；
丢失 Python 对象引用不会解除绑定。请求 token 绑定同一个 runtime 和递增的
请求 epoch，截止时间受物理租约约束。可信的新鲜观察须确认准确模型进程
存活、实际资源 driver/flock 租约仍被持有。身份未知或变化后保持关闭，直到
物理退出被核实；重启后的所有者不能接纳旧 token。

HTTP 完成、取消、超时和调用线程结束只能通过 `revoke_request` 撤销结果权限。
请求槽持续占用，直到 `finish_request` 收到该请求专属的 `RequestStopProof`：
实际后台请求已经停止，**全部**请求 worker（包括线程）已经退出。后台忽略
取消时不能发下一个请求。迟到完成可以清请求槽，但不能提交结果。同 runtime
轮换请求不释放常驻 GPU 物理所有者。真正交接仍委托原 broker 的严格
`ExitProof` 核实进程、全部后代和本人 GPU 分配退出；活动请求还须先有自己的
停止证明。不把 HTTP 状态或调用线程结束当成后台 ACK，不伪造 GPU 释放标记。

RuntimeOwnership 本身只是进程内合同基础。它不启动探针、线程或
服务，不改 gate 或配置。可信集成须提供真实后台完成 ACK、跟踪全部
实际请求 worker，并让生命周期变更与这些调用串行；本地对象锁不能替代跨
进程资源所有权。既有请求所有者须保持占用，直到全部实际工作线程结束。CPU
fake 覆盖活 worker 忽略取消、过期、PID 复用、旧 epoch、重启未知，以及请求
轮换后常驻所有权仍保留。这些测试不提供真实 GPU 交接或策略 quiesce 证据；
真实 `running` gate 保持关闭。

### 显式有界 runtime 桥

`levi.live.runtime_bridge.RuntimeBridge` 组合上述既有合同；控制器参数
`runtime_bridge=None` 保持默认行为。没有 CLI、环境变量、服务配置或真实
端点自动安装这座桥。可信组合入口必须注入实际资源 driver、内核 flock、
准确 PID/启动 ticks/boot/端口所有者的观察，以及冻结的独占单引擎后端能力。
证据未知或 driver 代次变化时关闭准入；不 import lab 调度器，不把环境字符串
当作所有权证明。

可信启动器先通过原 broker 调用 `admit_runtime`，再为实际本人 leader 调用
`bind_runtime`。strict 控制器不走旧启动、外部模型接管或 manual 放行分支。
后台模型 worker 明确暂停，等它能使用同一权限入口后再接；不宣称全部请求
生产者已经接通。

普通在线判定与 AERI 入口都须 join 全部实际本地工作线程，并收到同 runtime、
同请求 epoch 的后端 barrier，才能提交成功。`VllmPauseBackend` 只适配经独立
核实实现的同步 `/pause` wait/abort barrier 与 `/resume`，通过注入的固定回环
transport 调用。断连、`/abort_requests`、`/is_paused` 和 sleep 都不是完成证明。
barrier 前后再次核实准确权限，barrier 时间计入请求截止时间。超时或 ACK 未知
撤销结果，但保留 Judge busy、请求槽和物理 owner；迟到有效 barrier，或严格
进程/全部后代/GPU 退出加本地线程退出，可清槽，不能复活结果。P0 本地撤权
与物理释放尝试不等待生命周期 mutex 或 drainer。

首版仅支持有限物理 lease，过期不能靠新 wrapper 或 broker 延长。strict sleep
hook 结束本轮准入，保留物理 owner 和 flock；不释放 GPU 所有权，也没有实现
长期睡眠、唤醒与续租。物理释放仍须原 broker 的严格退出证明。永久常驻以及
策略/复位角色组另需合同。专属测试是 CPU/内存 fake，覆盖两个实际在线入口；
真实 CPU IPC/flock 组装、后端能力核实、GPU 时延和机器人验证仍是独立检查，
本轮没有这些证据。


## 确认常驻模型退出

既有实时控制器只有在 `Vllm.stop()` 确认原 PID、启动时钟计数和 boot 身份的
所有者已退出、完整 `/proc` 扫描确认进程组没有活成员、成功的 `nvidia-smi`
计算进程扫描确认记住的 PID 不再占用 GPU 后，才释放 GPU flock。PID 被复用
不授权向同号的新进程发信号。身份、boot、进程组或 GPU 证据读不到，GPU PID
行不完整，或所有者记录读写失败，都保留“停止未确认”。原记录和身份保留；
能写入时还保存待退出成员。再次启动必须等待后续完整停止确认，重试仍重新
检查进程组和 GPU。只用于状态展示的 GPU 列表保持原来的宽松接口。

执行启动命令前，先在内存和原子 pending 记录中保留启动所有权；记录无法读写时
不启动。启动器返回的 PID/start/boot/进程组与新服务的 PID/身份，在可读取时保留。
超时、非零退出码、PID 文件缺失或未变化、身份未知、最终记录写入失败，都保持
所有权未确认。不完整启动不向仍活或身份未知的服务发信号，也不允许再次启动。
已记录所有者退出后，还须完整检查进程组与 GPU，包括观察到的启动器及其组，
才能恢复。没有服务 PID 时，启动器退出仍不足以证明脱离其组的子进程全部退出。
执行命令前的准备错误继续使用普通 backoff。让步探针只枚举核实后的记录进程组，
并再次核实身份；PID 文件不一致或不可读会产生保守 probe error，文件中另一个
进程不会被当作本服务的 GPU 使用者而豁免。

关闭监督进程时，只要控制器仍活着，就保留未解决的记录及本进程的锁描述符。
进程真正退出必然关闭这个描述符；继承同一打开文件描述的活子进程仍可能
继续持有 flock。关闭返回或 HTTP 响应都不代表物理释放或后端空闲 ACK。
本改动不挂载可选运行时桥，不开放 `running` 闸门，也不证明后端请求已停止。

CPU 回归使用明确的 fake GPU 查询来源、自己创建的 CPU 子进程和临时 flock，
包括父进程退出后仍存活的子进程。它们验证保守保留所有权及之后的恢复，
不证明实际显存交接、后端暂停 ACK 时限或真机行为。

监督进程拿不到锁或无法核实继承描述符时，将仍存活的原所有者记为未解决，
保留原记录。构造时不停止旧服务，也不把它改称外部服务；再次启动仍须
等待后续显式停止或 tick 完成确认。

这些既有检查覆盖进程组及记住的 PID。后续运行时桥仍须单独核实完整后代
所有权，包括已离开该进程组的工作进程。


## 受控常驻群组与有界续签（显式启用）

`resident_group` 是正式物理预约分类，优先级低于策略和关键判定，高于轻量视觉、
后台标注与批处理。`ResidentGroupSpec` 声明各角色和不同的 owned capability；
`RuntimeOwnership.bind_group` 绑定一个 supervisor 与完整成员的 PID/start/boot 身份。
`GroupObservation` 必须绑定原 driver 租约、代次、会话，证明 driver/flock 持有和成员
清单完整，并核实全部成员都是本人管理且运行中的后端。外部服务的 PID 不能代替这些
能力；真实适配器须实际提供证明。内核不接管外部服务，也不自行探测。

`begin_role_request` 让正向、判定、复位及可选后台角色共用一个请求槽。每次请求都要
新的 `RoleWindow`，绑定当前运行、profile、片段、接收端时钟、角色和策略代次；独立的
`authorization_epoch` 每次请求严格递增，跨片段还须提升策略代次。全部成员须有后端
静默 barrier 和全部本地请求 worker 退出证明。完成请求还要晚于 stop proof 的完整群组
观察，调用线程结束不能代替它。模型 gate 不撤销群组的物理预约，但这不能产生角色
窗口、批准请求或改变既有 gate。

`renew_resident` 在原 owner 与 broker 两把锁内明确重新授权：旧许可仍有效、未撤销、
身份可信，无活动请求，全部成员静默，新的物理 `SafeWindow`（与片段角色窗口分开）、
截止时间、新鲜压力及显存余量有效，且没有更高优先级交接。冻结的群组内存估计须包含
全部成员、启动/编译/切换峰值和余量；续签保留这一预约并核对额外可用余量。所有验证
与 token 构建完成后才同时改租约及 runtime。每段仍最多十分钟，两个代次都递增；旧
token 无权取消或释放新代次。心跳、过期或未知身份不能续签、复活所有权。

`GroupExitProof` 要证明每个原成员及其全部后代退出并释放本人 GPU 分配，另有 supervisor
完整退出证明；原 broker 也执行这一要求。完整物理退出可清理悬置请求，但不发布结果。
内核不等待模型 mutex、不执行后端动作。专属 fake 回归覆盖角色竞争、续签竞争、拒绝与
构建失败时的原子性、旧 token、不完整静默/退出证明和旧单 worker 兼容。真实适配器、
进程/flock 组装、GPU 内存与 barrier 时延、机器人联测仍是后续验收门；未显式组合时关闭。


原权限内核公开 `owns_exact_request` 查询原对象的排空归属，以及 `accepts_exact_request` 对原对象沿用原许可检查。等值令牌副本不会获得精确许可；旧按值接口不变。此增量不安装真实模型适配器，也不证明真机就绪。

## 受控 SDK 运行时适配（软件候选，显式启用）

`levi.live.owned_runtime` 提供私有 IPC 模型 worker 启动器和
`OwnedGroupObserver`；`levi.automatic.owned_runtime.OwnedRoleRuntime`
供脚本组合既有 `RuntimeOwnership` 与 `WorkloadBroker`，不建立第二套调度器。
现有推理循环也可以把 worker 绑定到同一个 authority，用已授予的角色 token
提交请求，再用群组 observer 取得原请求收尾证明。适配层默认不安装，不能接管
任何已经运行的外部策略或 VLM 服务。

启动必须已有原 broker 许可、准确且有效的 canonical 资源 driver 租约及已登记
的活子进程、继承的 GPU 独占 flock 描述符，以及既有委派的 cgroup v2 supervisor
根和每个角色各自的空叶目录。启动器先把自己实际创建的子进程记入显式运行时
目录中的 `levi.children`，再打开模型导入门。观察静默或退出时，会复核完整根与
叶目录成员、PID/启动时间/boot 身份和已见过的 engine 后代。额外进程、containment
更换、未知身份或尚未解决的启动会拒绝后续启动。进程或 HTTP 返回、空闲显存
都不能替代这些证明。

Torch worker 在推理后同步实际 CUDA 设备，Python 请求线程未结束时不发 idle ACK。
私有 vLLM worker 等唯一 generate 协程退出，再调用
`AsyncLLM.pause_generation(mode="wait", clear_cache=False)`；冻结实现的调度器
Future 经 `collective_rpc("synchronize_device")` 完成。已安装版本与 engine、executor、
worker 源码清单必须匹配独立审查过的版本。这条 SDK 路径仅替代本受控适配中的
HTTP `VllmPauseBackend` 依赖，不开放 HTTP 服务或外部 generation 入口。源码哈希
只核对代码一致性，实际私有启动收据、containment 和 SDK ACK 才证明所有权与静默；
factory 及完整 dispatch 闭包仍须独立审查。当前只支持一个 engine/device 拓扑，
拒绝外部 KV connector，也不让 GPU 对象跨过 CPU IPC 边界。

SDK 超时、ACK 代次不符或观察未知时，原执行槽继续保留。已撤销的晚到结果可以
在取得证明后排空槽，但不能发布。关闭入口和撤销 token 不等待模型 mutex。
常驻续租仍交给原内核，更新同一租约与 runtime 代次，并要求同样的新鲜全成员
静默证据；不会自动续租、部署或重启服务。

首轮使用的 JAX 策略提供者仍不可用。
`levi.live.owned_jax.JaxDependencyFence` 可等待输出、存留状态、live-array 依赖
及 JAX side effect，但明确不产出 backend-idle ACK。标准 infer 的完整 dispatch
闭包（包括存留 RNG 与所有 transforms）尚未冻结，数组就绪不足以证明整个后端
静默。这些适配也不提供机械臂物理保持或暂停相机帧的证明。

当前操作会话不在可写的 user-manager 委派根中，可信委派 supervisor 的外层
bootstrap 尚未安装。后续显式部署必须在获准的委派 supervisor 中保留 canonical
driver/子进程祖先关系与已锁定的 open-file descriptor；本软件候选不创建 cgroup
或 systemd 单元。CPU fake 回归覆盖 SDK ACK 与原群组内核，没有运行 GPU 模型、
真实进程组、已安装服务或机器人。

Torch 私有 factory 新合同是 `build(device=..., sdk=...)`。显式
`source_root` 与完整 Python `source_hashes` 清单冻结 factory、父包初始化和本地
依赖。owned loader 直接编译同一份已验证 bytes，不读取可碰撞的 .pyc。
预加载模块、不同 origin/loader、未冻结依赖，以及解析到解释器目录之外的
同名标准库均拒绝。SDK 对象显式注入；source hash 本身不是受信 loader 或
完整调用闭包的审查批准。本轮没有安装生产 factory。

SDK 执行现在必须提供真实已安装 Python 包的完整 `torch_source_hashes` 清单；
vLLM 还须完整 `sdk_source_hashes`，八个 barrier hash 必须属于该完整清单。
factory 执行前及 SDK idle ACK 前后，真实包解析路径、loader 与实际函数 code
均须绑定已验证 bytes，预加载或 shadow 包拒绝。vLLM factory 同样接收
`build(device=..., sdk=...)`。未绑定的 native/第三方导入拒绝。实际完整
SDK/native 闭包和 owned SDK 子进程 bootstrap 尚未安装，真实入口保持关闭。
仅测量小 CPU 包的绑定软件；完整真实包清单成本及 worker 来源尚未测量。

冻结模块的静态、相对、fromlist 与普通动态导入现在使用同一 resolver，
包括缓存命中。每个 bundle 的 importlib/builtins facade 核实返回依赖；
未绑定的低层动态加载 API 拒绝。标准库来源可信不代表其返回第三方模块已获准。

关键 SDK 绑定在 factory 模块初始化前封存。factory 返回后和每个 idle ACK
前后复核准确 module/class 属性、函数 code、globals 别名、defaults、闭包及
引用容器，包括实际 Torch synchronize。无法保留这些绑定的动态关键状态
拒绝；封存不证明尚未实现的 remote/native SDK worker 来源。专属小假包测试
直接使用普通 factory.load 到 serve 链，不替换 load 或 require_function。

八个声明的 vLLM barrier 模块在封存前全部经 owned loader 导入，factory 不能
先导入某个延迟加载的 barrier 模块再改绑；已封存模块离开 `sys.modules` 时，
下一次检查拒绝。Torch idle 检查从 loader 持有的 model 句柄读取 bundle 与封存，
不读 factory 返回对象的属性。worker 解释器以 `-B -X pycache_prefix=os.devnull`
启动：`-B` 只禁止写缓存，同 mtime/size 的旧 `__pycache__` 仍会替换 loader 与
屏障检查本身。worker 入口在读取 bootstrap 前拒绝其他解释器配置。不覆盖：
替代已删除 `.py` 的无源旧式 `.pyc`、native 扩展、延迟导入的非 barrier 模块，
以及 supervisor 自身的解释器。

worker 不再经继承的工作目录、`PYTHONPATH` 或用户 site 解析 `levi`：它以隔离
模式（`-I`）运行，从 supervisor 自身实际解析到并显式传入的检出根导入 `levi`。
读取 bootstrap 前，只要有任何已加载的 `levi` 模块不是该根下的普通源码模块，
子进程即拒绝。因此 shadow `levi` 包不能替换绑定 SDK 来源的 loader。解释器、
其 site-packages `.pth` 文件和 native 扩展仍属受信范围。

工厂与 SDK 源码加载采用同一事务。钩子安装、模块初始化、工厂构建或
引擎校验失败时，撤销全部本次钩子及其确切创建的模块。私有 worker
正常或异常结束时按安装逆序关闭同一生命周期；直接调用 vLLM 的软件
入口须调用 `close_engine(engine)`。外部预加载及替换对象保持原样，
关闭后的 bundle 拒绝继续导入。这里清理的是 Python 源码生命周期；
物理设备所有权仍须原内核完整且新鲜的群组退出证明才能释放。

vLLM 关键方法须解析到工厂执行前的原类描述符，`__func__` 与 `__self__` 均须匹配；实例遮蔽即使使用同一 bundle 编译的方法也拒绝。合法类方法、静态方法语义保留。函数 globals、默认值与闭包中的确切被动 `SimpleNamespace` 可绑定传递至 namespace/dict/list/tuple 的 callable 路径，普通数值计数仍可变化。主动对象、未支持容器或未绑定关键 callable 明确拒绝。这只校验已审调用点；loader 不是 Python 安全沙箱，没有据此声明真实 SDK/native/子 bootstrap 可用。

Owned worker 提供 `collect_sdk_completion(request)`，返回原提交 token、严格匹配的 `SdkIdleAck` 和 CPU 结果。超时保留 pending；坏 ACK/payload 或库存未知永久撤销该 pending 结果。真实 ACK 缓存仅可在新鲜探针成功后用于同一 request 的 drain。worker 不发起或结束内核 request，也不把调用返回当停止证明。

`BorrowedRoleExecution(authority, runtime, lease, observer)` 用 `dispatch_existing(request, payload, role=..., episode_id=..., policy_epoch=...)` 与 `collect_existing(request)` 执行 Cycle 已签发的 grant，不调用 begin/finish/renew。`BorrowedCompletion` 返回原 request、SDK ACK、stop proof、新鲜整群观测及 CPU 结果，由 Cycle 再独立观测后唯一 finish。超时保留 pending；坏 ACK/探针撤销结果但保留同 request 的真实缓存 drain。迟到、撤销或关闭后的结果为 None。只有真实 SDK ACK 加新鲜完整整群 idle 能形成 stop proof；当前 running-group 合同不把整 worker 退出伪装成该证明。旧 `OwnedRoleRuntime.begin/collect` 独立入口保持兼容。没有 HTTP fallback、生产挂载或新真实 SDK 提供者。

专属 `test_borrowed_cycle_execution.py` 检查要求新解释器和显式 `LEVI_JOINT_KERNEL_ROOT`，从记录的实际路径加载原 Kernel/Cycle 与候选 owned 文件。CPU 假 IPC 验证原 begin/finish 各一次、准确匹配的 SDK 收据结构，以及最后独立的完整整群观测。收据之后其他成员变忙会阻止发布并保留原槽。这是软件组合证据，未安装生产提供者、真实 SDK/native bootstrap、物理保持或 Orchestrator 挂载。

RuntimeOwnership 新增 `owns_exact_request(request)` 纯身份查询，覆盖仍待排空的撤销或迟到请求；它不授予许可或证明静默。`accepts_exact_request(request, observation, now_ns=...)` 在同一锁下先用 `is` 确认原活动对象，再沿原结果许可校验。既有 `accepts_result`、revoke 与 finish 对旧调用者保留原公开按值语义。

Borrowed 执行必须使用上述原 Kernel 精确查询。等值 token 副本在 observer、worker 或 revoke 副作用前拒绝。派发和结果许可使用 `accepts_exact_request`；收集及撤销须由 `owns_exact_request` 确认同一原对象。撤销或迟到的原对象仍可用真正 ACK 排空，结果为 None。缺少精确公开合同的 Kernel 拒绝，不回落到按值许可。
