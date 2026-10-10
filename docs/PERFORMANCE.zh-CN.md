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
| `frame`（默认） | 原有做法：`ffprobe` 解码每一帧并给出 best-effort 时间戳。 |
| `packet` | 改读容器的数据包时间戳，不解码：在实测的合成视频上快 4 到 80 倍，对普通文件（B 帧、可变帧率）列表相同。它信任容器，所以只要容器里的数据包与显示的帧不是一一对应就让位：数据包带丢弃标志（`D`）或损坏标志（`C`，MP4 编辑列表或起点早于 0 会产生前者）；时间戳为负；ffprobe 在 stderr 报了任何问题（文件被截断或损坏）；某个数据包没有时间戳；没有数据包；时间不严格递增。这时改走帧扫描，并记一条含文件名和原因的警告日志。缓存文件会额外记下 `"scan": "packet"`，另一种扫描写的缓存不会被使用。 |

其他取值会被拒绝。两种模式下，同一视频的列表都按内容哈希保存在内存里（16 个视频），所以被许多片段共用的 v3 文件在一个进程里只扫描一次；每个片段每个相机只对文件算一次哈希（哈希值传给扫描，不再重算）。检查不变：缓存仍与源文件哈希绑定，后面的时间不一致容差照常生效。

只有在真实的开发集视频上比较过两种扫描之后，`packet` 才会成为默认值：

```
python -m levi.performance pts-compare <视频或文件夹> [...] [--cores 4]
```

两个比较命令都会把自己限制在至多 4 个 CPU、低优先级（`--cores`，0 表示不处理）。`pts-compare` 还会逐文件列出数据包为什么不能用。

它每个文件打印一行；列表有差异或文件无法扫描时退出码为 1。

## 源视频被哈希的次数

凡是保护证据不可变性的哈希都保留。对每个文件，快照会哈希复制前的源文件、副本、复制后的源文件（从而发现复制过程中的改动）；计划时的哈希在任何复制之前比较；提交前的检查再对源文件哈希一次；每张被复用的缓存证据 PNG 在复用前都会重新哈希。`levi/agent/media.py` 里唯一发现的冗余在 `sample()`：它先为缓存键哈希一次视频，又在时间戳索引里再哈希一次，现在把哈希值传了下去。这些次数在 `tests/test_media_hashing.py` 中有断言，旁边是对各阶段做篡改的测试。

## 转换的像素统计（`LEVI_PIXEL_STATS`）

`levi.conversion.media.inspect(path, pixels=True)` 统计所有解码像素每个通道的最小值、最大值、均值和标准差，结果进入 `meta/stats.json` 和训练归一化。

| `LEVI_PIXEL_STATS` | 含义 |
| --- | --- |
| `float`（默认） | 原有方法：每帧转成 float64 再求和。 |
| `histogram` | 按通道数每个 8 位取值出现的次数（3 x 256 个整数），再由计数一次性算出四个统计量。求和是精确的整数，方差没有相消误差。每帧约快 11 到 17 倍；min、max、mean、count 与 float 方法相差约 1e-11 或更小。标准差按方差比较：float 方法把方差算成 `mean(x^2) - mean(x)^2`，通道为常值或接近常值时会相消，开方后会把 1e-12 的误差放大成最大约 1e-6 的噪声（常值 720p 通道得到 2e-6，真值为 0）。histogram 的结果是精确的。不是 8 位的帧仍用 float 方法。 |

其他取值会被拒绝。默认仍是 `float`，因为 `meta/stats.json` 的末几位会随方法变化。在 `histogram` 成为默认值之前，请在真实视频上比较两者（只要有文件的 min、max、mean、count 或方差（std 的平方）相差超过 1e-9，或帧数、span、首帧哈希不同，命令就退出 1；std 本身的差值会报告但不作判据）：

```
python -m levi.performance pixel-compare <视频或文件夹> [...] [--cores 4]
```

`bench --case pixels` 在合成视频上给两种方法计时，并报告加速比和最大差值。

## 有硬上限的缓存目录（`DiskBudget`）

`levi.performance.budget.DiskBudget` 让一个缓存目录自己守住字节上限（可选再加条目数上限）。原因是只增不减的缓存迟早写满磁盘：300 个策展片段的证据约 7 GB，1,616 个 robotiq 片段约 40 GB，三个 schema 版本就是 120 GB（单价来自清理审计的实测，约每个片段 25 MB）。所以上限在写入那一刻执行，而不是靠一个可能来得太晚的清理器。

它只是一个库。**LEVI 里目前没有任何地方使用它**，不改变任何行为，也没有设置项。只用标准库，不导入 LEVI 的其他模块。

```python
from levi.performance.budget import DiskBudget, BudgetRejected

cache = DiskBudget.from_cap_gb("lab/perf-cache/evidence", 20, version="schema-3")   # 20 GiB
cache.put("episode-17", png_bytes)              # 需要时淘汰最久未使用的条目
cache.read("episode-17")                        # 返回字节或 None；命中算一次使用
with cache.writing("episode-18") as tmp:        # 一个文件或整个目录，结束时原子改名到位
    tmp.mkdir()
    ...
try:
    cache.put("x", data)
except BudgetRejected as why:                   # why.reason：too_large | no_space | no_output | lock_timeout
    ...                                         # 调用方不用缓存继续往下走
```

| 行为 | 说明 |
| --- | --- |
| 上限 | `max_bytes`（硬上限）和 `max_entries`。放不下的写入会淘汰条目直到放得下；如果无论如何都放不下，会在淘汰任何东西之前就被拒绝（`too_large`）。单位是 GiB（2**30），与 janitor 配额表里 `cap_gb` 的单位一致。 |
| LRU | 条目的修改时间就是它最后一次使用的时间，`get` 和 `read` 会刷新。janitor 的 `lru_cap` 查找器按同一个时钟排序，所以两者对“谁最旧”不会有分歧。 |
| TTL | 闲置超过 `ttl_s`（默认 14 天，即 janitor `perf-cache` 的期限）的条目视为过期：不会被返回，在下一次写入或 `maintain()` 时删除。`put(..., ttl_s=)` 可给单个条目设自己的期限。 |
| 版本 | 每个条目都带版本字符串（schema、模型或提示词版本）写入。其他版本的条目是过期版本：不会被返回，先于有效条目被淘汰，闲置超过 `stale_grace_s`（默认 7 天）后删除，或由 `purge_stale()` 立即删除。用新版本写同一个键，会替换旧条目。 |
| 原子写入 | 数据先写入 `.tmp-<pid>-<随机串>`，`fsync` 后改名到位；读者要么看到完整条目，要么看不到。条目可以是文件，也可以是目录。 |
| 崩溃安全 | 以目录本身为准；`.budget.index.json` 只记原始键、版本字符串和单个条目的 TTL。它缺失、损坏或与目录不符时，会按目录重建。打开时，已死进程（或超过一小时）留下的 `.tmp-*`、`.trash-*` 会被清除。删除条目时先改名为 `.trash-*`，所以删到一半崩溃不会留下看起来有效的残缺条目。 |
| 并发 | 所有改动都在 `.budget.lock` 的独占 `flock` 下进行。线程之间、进程之间互斥；进程死亡（包括 SIGKILL）时内核会释放锁。 |
| 磁盘满 | 写入时遇到 `ENOSPC` 或 `EDQUOT`，或写入后磁盘剩余空间少于 `min_free_bytes`，会得到 `BudgetRejected("no_space")`，不会留下残缺文件。 |

设计的局限：正在进行的写入只在提交时才计入，所以目录可能短暂超出上限，超出量不超过正在写的数据。`min_free_bytes` 是防范同一磁盘上其他使用者的唯一手段。名字不是 `<32 位十六进制>.<8 位十六进制>` 的文件（点开头的文件、外来文件）不归它管。

**与 janitor 的关系。** janitor（`janitor.py`，不在本仓库内）是兜底：它的 `perf-cache` 条目有相同的 20 GB 上限和 14 天期限，把每个缓存条目（`<类别>/<条目>`）当作一个整体。`DiskBudget` 是第一道防线：在写入时就执行上限，产品工作区里也是如此，而那里 janitor 从不碰。janitor 删掉某个条目，`DiskBudget` 只会看到它不见了；janitor 排序时用的是同一个修改时间。
