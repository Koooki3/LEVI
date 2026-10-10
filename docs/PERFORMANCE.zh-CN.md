# 性能基线

[English](PERFORMANCE.md)

LEVI 已经保存了三类耗时与成本记录：实时标注服务的 `live/stats.jsonl`（见[实时标注服务](LIVE.zh-CN.md)）、Agent 用量样本（`levi/agent/usage.py`）、每次运行的 harness 成本（`levi/harness/cost.py`）。本页说明建立在它们之上的只读视图和 CPU 基准。两者都不另存数据，不用 GPU，不调用模型，也不接触机器人。

## 追踪（trace）

```
python -m levi.performance trace --live-dir <工作区>/live --workspace <工作区> [--since EPOCH] [--limit N] [--summary-only]
```

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
| `packet` | 改读容器的数据包时间戳，不解码：在实测的合成视频上快 4 到 80 倍，列表相同。如果某个数据包没有时间戳、没有数据包，或时间不严格递增，就改走帧扫描。缓存文件会额外记下 `"scan": "packet"`，另一种扫描写的缓存不会被使用。 |

其他取值会被拒绝。两种模式下，同一视频的列表都按内容哈希保存在内存里（16 个视频），所以被许多片段共用的 v3 文件在一个进程里只扫描一次；每个片段每个相机只对文件算一次哈希（哈希值传给扫描，不再重算）。检查不变：缓存仍与源文件哈希绑定，后面的时间不一致容差照常生效。

只有在真实的开发集视频上比较过两种扫描之后，`packet` 才会成为默认值：

```
python -m levi.performance pts-compare <视频或文件夹> [...]
```

它每个文件打印一行；列表有差异或文件无法扫描时退出码为 1。

## 源视频被哈希的次数

凡是保护证据不可变性的哈希都保留。对每个文件，快照会哈希复制前的源文件、副本、复制后的源文件（从而发现复制过程中的改动）；计划时的哈希在任何复制之前比较；提交前的检查再对源文件哈希一次；每张被复用的缓存证据 PNG 在复用前都会重新哈希。`levi/agent/media.py` 里唯一发现的冗余在 `sample()`：它先为缓存键哈希一次视频，又在时间戳索引里再哈希一次，现在把哈希值传了下去。这些次数在 `tests/test_media_hashing.py` 中有断言，旁边是对各阶段做篡改的测试。

## 转换的像素统计（`LEVI_PIXEL_STATS`）

`levi.conversion.media.inspect(path, pixels=True)` 统计所有解码像素每个通道的最小值、最大值、均值和标准差，结果进入 `meta/stats.json` 和训练归一化。

| `LEVI_PIXEL_STATS` | 含义 |
| --- | --- |
| `float`（默认） | 原有方法：每帧转成 float64 再求和。 |
| `histogram` | 按通道数每个 8 位取值出现的次数（3 x 256 个整数），再由计数一次性算出四个统计量。求和是精确的整数，方差没有相消误差。每帧约快 11 到 17 倍；结果与 float 方法相差约 1e-12（即 float 方法自身的舍入）。不是 8 位的帧仍用 float 方法。 |

其他取值会被拒绝。默认仍是 `float`，因为 `meta/stats.json` 的末几位会随方法变化。在 `histogram` 成为默认值之前，请在真实视频上比较两者（只要有文件的 min、max、mean、std 或 count 相差超过 1e-9，或帧数、span、首帧哈希不同，命令就退出 1）：

```
python -m levi.performance pixel-compare <视频或文件夹> [...]
```

`bench --case pixels` 在合成视频上给两种方法计时，并报告加速比和最大差值。
