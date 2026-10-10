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
| `pixels` | `levi.conversion.media.inspect(path, pixels=True)`，即转换时的像素统计 |

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
