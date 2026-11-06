# Round record

## results.tsv (inner loop; one file per experiment branch, under `<worktree>/.state/tmp/`, git-ignored)

Tab-separated, because commas break in descriptions. The columns and the `keep|discard|crash` convention follow Karpathy's `program.md` (MIT), with cost columns added. Header:

```
commit	metric	delta_vs_kept	wall_s	local_tokens	external_tokens	status	description
```

- `commit`: short hash of the experiment commit. `metric`: the primary metric on tune data. `delta_vs_kept`: against the last kept commit, in the metric's own direction.
- `local_tokens`, `external_tokens`: counted apart. For external tokens take the last usage line per message id (the first line under-counts several times over); `LEVI/lab/aeri-external/tools/agent_tokens.py` does it today, and `levi/eval/tokens.py` once it is merged. `experiments.py` still takes the first line (issue EXPERIMENTS-CLAUDE-USAGE-FIRST-LINE), so do not take its Claude token columns as external-token evidence.
- `status`: `keep`, `discard` or `crash` (use 0 for the numbers of a crash). `description`: one line, the hypothesis.

`results.tsv` is the working log. The authoritative record of a **LEVI annotation evaluation** is its `experiments.py` row (R13), regenerated from `levi-vlm-eval/results/*.json`; they must agree on commit and metric. Replay studies and other tuning are not in `experiments.py`: their record is this file plus the round report below, with the evaluator command, the commit and the data-split hash.

## Round report (Chinese, `levi-hub/reports/<topic>-<date>.md`)

1. 问题与指标（方向、噪声下限、守卫指标与上限、停止规则、预算）。
2. 数据划分：tune 与 confirm 各是什么、编号清单的哈希、与冻结/留出集的核对方式和结果。
3. 基线（重复 k 次）与最佳保留的改动；每个保留项的 delta。
4. confirm 结果（只开一次，带区间）；有教师时的三组对比表（质量、与教师一致率、耗时、本地 token、外部 token），confirm 上只放聚合数。
5. 丢弃项及原因；没有核实的事；下一轮要不要开（R10）。
6. 复现命令：分支、提交、评测命令、登记的运行编号。
