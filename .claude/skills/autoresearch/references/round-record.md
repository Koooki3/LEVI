# Round record

## results.tsv (inner loop, untracked, one file per experiment branch)

Tab-separated (commas break in descriptions). Header and columns:

```
commit	metric	delta_vs_kept	wall_s	local_tokens	external_tokens	status	description
```

- `commit`: short hash of the experiment commit. `metric`: the primary metric on tune data. `delta_vs_kept`: against the last kept commit, same sign convention as the metric's direction.
- `local_tokens`, `external_tokens`: counted apart; `external_tokens` takes the last usage line per message id, because the first line under-counts several times over.
- `status`: `keep`, `discard` or `crash` (use 0 for the numbers of a crash). `description`: one line, the hypothesis.

`results.tsv` is the working log. The authoritative record of a run is its `experiments.py` row (R13); the two must agree on commit and metric.

## Round report (Chinese, `levi-hub/reports/<topic>-<date>.md`)

1. 问题与指标（含方向、噪声下限、停止规则、预算）。
2. 数据划分：tune 与 confirm 各是什么、哈希、与冻结/留出集的核对结果。
3. 基线与最佳保留的改动；每个保留项的 delta。
4. confirm 结果（只跑一次，带区间）；与教师/直接调用的三组对比表（质量、耗时、本地 token、外部 token）。
5. 丢弃项及原因；没有核实的事；下一轮要不要开（R10）。
6. 复现命令：分支、提交、评测命令、登记的运行编号。
