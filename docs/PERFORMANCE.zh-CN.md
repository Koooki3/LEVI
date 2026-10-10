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
