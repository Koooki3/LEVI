# 子任务标注评测记录

LEVI 用同一种格式记录一个人或一个 agent 标注子任务的好坏和快慢，两者可以逐项对比。English: [EVALUATION.md](EVALUATION.md)。

| 谁 | 何时写记录 | 文件 |
| --- | --- | --- |
| 人 | 在标注视图点**停止记录** | `<workspace>/eval/<dataset>_human_<YYYYmmddTHH>.md` |
| agent | 一次已提交、且覆盖数据集**每个片段**的子任务标注运行（例如“给 `<dataset>` 全部标注子任务”） | `<workspace>/eval/<dataset>_agent_<YYYYmmddTHH>_<driver>.md` |
| agent，按需 | 对其他已提交的子任务标注运行（批量、子集）执行 `uv run levi agent eval <run-id>` | 同上 |

小时取完成时间（UTC），与运行编号用同一个时钟。同一小时内再写同名文件，是追加一节，不会覆盖第一份。`driver` 记录谁做的：

| driver | 含义 |
| --- | --- |
| `external-mcp` | 外部 agent（Claude Code、Codex 等）通过 LEVI 的 MCP 工具工作 |
| `external-pilot` | 由 LEVI 作为受管 Pilot 运行的外部 agent |
| `api` | LEVI 调用的在线模型（OpenAI 兼容端点） |
| `local-vlm` | Ollama 或本地 OpenAI 兼容服务（`openai-local`，例如 vLLM）上的本地模型，独自工作 |
| `local-vlm-teacher` | 每个阶段都有外部教师审阅的本地模型 |
| `native-external`、`native-local-vlm`、`native-local-vlm-teacher` | 同样的标注者，但**不经 LEVI** 工作；做完的标注事后导入 |

要把不经 LEVI 的工作放在同等条件下比较，走常规路径导入：规划一次外部运行，把 `imported_from` 设为某个 `native-*` driver，准备证据，用 `annotations.propose_segments` 暂存做完的时间片段（LEVI 引用它准备好的帧），通过各道关卡再提交。这次运行在 `outputs/LEVI/workbench/agent/datasets/<dataset>/` 下的文件夹与 agent 运行的产物相同，记录在提交时按该 driver 写出。提交前用 `runs.report_usage` 报告原标注者的 token；导入本身不消耗标注者 token。

## 记录一个人的工作

在数据集的**标注**页：

1. **● 开始记录。** 会话保存在服务端，刷新页面或开第二个标签页都不丢。
2. 标注。有**子任务词表**时，为每个时间片段选子任务及其结局；描述可以留空。
3. 一个片段做完，点**确认片段完成**。未保存的修改会先保存。只有确认过的片段才计入。
4. **■ 停止记录。** 摘要显示用时、已确认片段数、子任务数和覆盖率，以及记录文件的路径。**放弃**会结束会话但不写记录。

子任务词表按数据集在**子任务词表**里设置。要在规划 agent 运行之前设好：它和数据集的标注存在一起，运行进行中改词表会改变这次运行的基线，运行的提议随即被拒绝。每行一个子任务：`id | label | definition`。如果该数据集上一次 agent 规划定义过子任务，编辑器会把它们作为起点。有词表时，人工标注的时间片段与 agent 的一样带 `levi.subtask_id` 和 `levi.outcome`；没有词表时，编辑器和以前一样（自由文本子任务）。

## 记录里有什么

第 1–6 节对人和 agent 相同，都由 `levi/eval/metrics.py` 从数据集标注文件里的子任务原子测量：

1. **概览**：数据集、标注者、起止时间，以及片段怎么计数。
2. **完成情况**：已确认的片段（agent：该次运行已提交的片段）、子任务数、每片段子任务数、覆盖的视频秒数。
3. **覆盖**：整体和每片段的秒级、帧级覆盖（均值、中位数、最小值），覆盖率不低于 95 % 的片段，以及长于 0.2 s 的空隙。一个时间片段覆盖 `[timestamp, to)`；没有 `to` 就延伸到下一个子任务或最后一帧。
4. **语义丰富度**：
   - 不同标签数；
   - 带词表 id 的占比和用到的词表项；
   - 带结局的占比和结局分布；
   - 带描述的占比、每条描述的词数、不同描述的占比、type/token 比；
   - 只是重复标签的描述。
5. **正确性**：
   - 结构有效性：空的或越界的时间片段、重叠、词表之外的 id；
   - 与数据集**同内容副本**的一致性：时间片段 F1（相同子任务、IoU ≥ 0.3）、结局一致率、时间准确率（参照标注时间中相同子任务、以及相同子任务且相同结局所占的比例）、边界误差。所有度量与任务无关：不依赖词表、任务或帧率。副本是目录里与本数据集同名干（`<base>_human`、`<base>_agentVLM` 等）、片段和帧数都相同的其他数据集；
   - 覆盖最少的三个片段，以及留给测试者写意见的空行。
6. **效率**：用时、每片段用时、每分钟子任务数、每分钟标注的视频秒数。

agent 的记录多一节 **7. agent 效率与成本**：

- **外部 token**：不在本机上的计算。`api` 是 LEVI 的计量；外部 agent 是它们自己的报告，没有报告时是 LEVI 测得的下限（它交付给对方的量）；`local-vlm-teacher` 是教师的 token。
- **本地 VLM token 与模型时间**（仅本机 GPU）。
- LEVI 交付给 agent 的 token、工具调用及其耗时。
- 模型请求数和缓存命中。
- 教师的决定，以及通过的人工关卡（计划批准、试点审阅、提交）。

每个数字也都在文件末尾的 JSON 块里，方便之后汇总。

## 比较各种方式

要在同一批数据上比较各标注方式，在工作区下为每种方式复制一份数据集（例如 `<base>_human`、`<base>_agentCode`、`<base>_agentVLM`、`<base>_agentMix`）。LEVI 会登记每份副本，每条记录会报告它与其他副本在同样片段上的标注的一致性。

## 教师、harness 与直接调用：三组一轮

教师–学生做法里，每一轮都用三种方式做同一批题：同题、同解码、同评分，然后放进一张表比较（`levi/eval/compare.py`；[English](EVALUATION.md)）：

| 臂 | 谁作答 |
| --- | --- |
| `teacher` | 外部 agent：提供范式和经验条目。它的答案是参考，不是金标准 |
| `local_harness` | 本地 VLM，经 LEVI 的 harness |
| `local_direct` | 同一个本地 VLM，直接调用，不经 LEVI |

主指标是：`local_harness` 的质量**不比教师差超过事先声明的界**，同时更快、花的外部 token 更少。所以表里除了质量，还有每秒质量和每外部 token 质量。

### 一个臂记什么（`levi.eval.arm_record.v1`）

| 字段 | 含义 |
| --- | --- |
| `arm`、`task`、`model` | 哪一臂、任务名、作答的模型 |
| `split` | **必填**：`tune`、`confirm` 或 `other`，这一臂跑的数据划分。`split` 为 `confirm` 的记录在任何表里都按下面的 confirm 规则处理，不看调用方传什么 |
| `episode_set`，或 `episode_set_hash` 加 `episode_count` | 题目集。`episode_set_hash` 是排序、去重后按字符串写出的片段编号的 SHA-256；两种写法都给时必须一致 |
| `scored_count` | `quality` 是对其中多少题算出的。表里它必须等于 `episode_count`：这一臂被阻塞或答失败的题仍留在分母里，按失败计分。给了 `quality` 就必须给 |
| `decoding` | 本轮声明的解码，非空的 JSON 对象。外部 agent 实际的采样无法观测也无法强制，所以教师臂记的是声明的解码 |
| `scoring` | 本轮声明的评分，JSON 对象，**必须**写明 `higher_is_better`（`true`/`false`：`quality` 越大越好吗；MAE、P90 这类误差写 `false`）和 `non_inferiority_margin`（非劣效界 δ，不小于 0 的数，在本轮开始前写下）。按所写内容比较，`0` 与 `0.0` 算同一个值 |
| `quality`、`quality_ci`、`quality_ci_method` | 对**金标准**的主指标（指标名写在 `scoring` 里）、区间 `[下界, 上界]` 和算法。`null` = 没测 |
| `quality_delta_ci_vs_teacher`、`quality_delta_ci_method` | **配对差**（本臂质量减教师质量，同一批题）的区间 `[下界, 上界]` 和算法。这是聚合量，由能逐题看到结果的计分方算出，表只读它。教师臂不填 |
| `agreement_with_teacher` | 本臂复现教师答案的比例，0 到 1。教师臂不填 |
| `wall_s`、`wall_s_basis` | 墙钟秒数（大于 0），以及它含不含排队：`includes_queue`（子 agent 的 duration 含排队）或 `excludes_queue`。`null` = 没说明 |
| `local_tokens` | 本机 GPU 上花的 token |
| `external_tokens` | `input_new`、`output`、`cache_read`、`cache_write`（即 `levi.aeri.external_report.v1` 的四个字段），分开记。整个对象或某个字段为 `null` = 未知；`0` = 测得为零 |
| `external_tokens_exact` | `true` = 计数完整，`false` = 只是下限（见下），`null` = 没说明。需要同时有 `external_tokens` |
| `external_tokens_source` | 外部数字的来源（一行） |
| `blocked` | 有什么阻塞（一行） |
| `per_episode` | 片段编号到分数。这是唯一的逐条字段，confirm 记录里不允许 |

记录里出现表外的字段会被拒绝。

### 一条命令

```bash
python -m levi.eval.compare teacher.json harness.json direct.json            # Markdown
python -m levi.eval.compare teacher.json harness.json direct.json --format json
python -m levi.eval.compare ... --confirm              # 断言：每条记录都是 split=confirm
python -m levi.eval.compare ... --verdict-basis fresh  # 事先登记的 token 口径
```

每个文件可以是一条记录、一个记录列表，或 `{"records": [...]}`。命令只读这些文件并打印；不起服务、不用 GPU、不开端口。在 Python 里：`compare.compare(records, assert_confirm=False, verdict_basis="both")` 返回 `Table`，有 `to_markdown()` 和 `to_json()`。没有一个可能被忘掉的 `confirm` 开关：一张表是不是 confirm 表由记录的 `split` 决定，`--confirm` / `assert_confirm` 只是多加一条要求“每条记录都是 confirm”。

Markdown 有三张表，每臂一行（质量与速度；本地与外部 token；相对教师和相对直接调用的差），外加 `local_harness` 对教师的结论。JSON 每臂一行，带全部列。

### 表强制的规则

- **同题同条件。** `task`、题目集哈希、片段数、`decoding` 或 `scoring` 不同的记录被拒绝，报错写明每一处不同；`decoding` 和 `scoring` 写明第一个不同的键和各臂的值。`local_direct` 的 `model` 必须与 `local_harness` 相同（直接调用组是同一个模型、不经 LEVI）。每一臂的 `scored_count` 必须等于 `episode_count`。同一臂有两条记录、或记录的 `split` 不同，也拒绝。
- **未知不是 0。** 为 `null` 的测量显示 `未知`，依赖它的比值和差也显示 `未知`。分母恰好为 0（例如本地臂没花外部 token）的比值显示 `不适用`，不是 `未知`；误差除以秒或 token 也是 `不适用`（越小越好时，这类比值不是越大越好的比率）。
- **质量按区间做非劣效判断。** 一轮 confirm 只有约 6 个片段，一个片段翻转就让比率变 0.14 到 0.17，两个点估计几乎说明不了什么。用 `scoring` 里的界 δ 和方向，以及 harness 记录的 `quality_delta_ci_vs_teacher`（调成越大越好；误差类指标要翻转）：区间最差的一端仍在 δ 之内（下端 ≥ −δ）为**是**；连最好的一端也超出 δ（上端 < −δ）为**否**；区间跨过界为 `未定`；没有区间为 `未知`。点估计只是辅助行（`quality_point_not_worse`），不进“三项全满足”。表拒绝不包含自己点估计差的区间（多半是符号写反了）。
- **外部 token 并列两个合计。** `fresh` = `input_new + output + cache_write`（外部模型新读入和新写出的部分），`all` = 四项全加（含 `cache_read`，即对缓存前缀的低价重读）。两个口径各有每千 token 质量和相对教师的差；口径里有一个字段未知，合计就未知。“外部 token 更少”两个口径都答：都说是才是“是”，都说否才是“否”，其余写 `未知`，两个口径给出相反答案时 `fewer_external_tokens_basis_dependent` 为真（反复重读大缓存前缀的学生，在一个口径下更少，在另一个口径下更多）。如果在本轮开始前登记过口径，`verdict_basis="fresh"` 或 `"all"` 按那一个判，表里写明；两个合计仍都在表里。
- **下限不是计数。** `external_tokens_exact: false` 把计数标成下限：表里写 `≥N`，每 token 质量写 `≤x`，含下限的差写 `（含下限）`。学生臂是下限时，永远不判“更少”（写未知）；教师臂是下限时，学生低于这个下限才判“更少”，高于它写未知，不写“否”。
- **时间同口径才比。** 时间差和“更快”只在两臂的 `wall_s_basis` 已声明且相同时给出；口径不同或没说明就写未知，表里说明原因。在外部 agent 的轮次里量到的子 agent duration 含排队，本地调用的时间不一定含。
- **直接调用组属于这一轮。** 缺 `local_direct` 时表仍会生成，但结论标为不完整，“三项全满足”那一行不会是“是”（“否”仍是“否”）。
- **confirm 保持盲。** `split: "confirm"` 的记录只能带聚合数和短代码：不能有 `per_episode`，不能有明确的 `episode_set`（改给哈希和数量）；`task`、`model`、`quality_ci_method`、`quality_delta_ci_method`、`blocked`、`external_tokens_source` 各自是一个代码（最多 64 个 `A-Za-z0-9_.:+-` 字符）；`decoding` 和 `scoring` 各自是不超过 16 项、256 个字符的扁平对象，键是代码，值是代码、数字或布尔值。这个检查在报告任何差异之前运行，报错信息也不会重复被拒内容。它做不到的是判断意图：调用方把某个片段专属的词写成“代码”（`ep7_flipped`）能放过一个短代码，但放不过分数列表、编号列表或一句话。confirm 片段的内容和标签不给教师和总管看。

### 从 Claude 会话取外部 token（`levi/eval/tokens.py`）

教师的外部 token 取自它自己的会话记录。Claude 会话文件把一条 assistant 消息写成共享同一个 `message.id` 的多行，上面的 usage 一行比一行大。**每个消息 id 只计一次，取它的最后一条 usage**；取第一行会把一条消息少算好几倍（一份子 agent 记录上实测：第一行 output 是 16，最后一行是 415）。

```bash
python -m levi.eval.tokens session.jsonl [more.jsonl ...] --since 2026-11-06T09:00:00Z --until 2026-11-06T10:00:00Z
```

- **文件由调用方点名。** 代码不列目录、不展开通配符、不看 home 目录，所以不会扫进不属于本轮的会话。续接过的会话按从旧到新传（后面文件里的同一条消息替换前面的）。
- 一条消息按第一行的时间落入时间窗，落入后按它的最终 usage 计。
- **`exact` 只对给出的文件成立，不代表本轮的全部花费。** 教师经子 agent 作答时，主会话里留下一条结果行（`type: user`，`toolUseResult` 里有 `agentId`），子 agent 自己的消息在它自己的文件里（`<session>/subagents/agent-<id>.jsonl`）。这些文件也要传：没给对应文件的子 agent 结果行会被计数（`subagent_results`、`subagents_uncovered`），它的汇总 usage 不加进合计（子 agent 的文件里有逐条消息的计数，两边都加会算两次），`exact` 为假，`source` 写明。
- 解析不了的行、写到一半的末行（`truncated_tail`）、既没有 `message.id` 也没有 `uuid` 的 assistant 行、因没有 `message.id` 而用 `uuid` 作键的消息（`uuid_fallback`；一条消息的几行会分开计）、设了时间窗却没有有效时间的消息、缺失（`usage: {}` 四个字段都缺）或不是计数的 usage 字段，都会被**计数并报告**，不静默丢掉。这时 `exact` 为假，合计是下限（`reasons` 列出代码）。没有任何被计入的消息带某个字段，该字段是 `null`，不是 0。
- `Usage.record_fields()` 返回要填进记录的三个字段：`external_tokens`、`external_tokens_exact` 和 `external_tokens_source`（不超过 200 个字符的一句话）。confirm 记录用 `record_fields(confirm=True)`：那句话里有空格和标点，confirm 记录不接受，所以换成 `source_code`（`claude_jsonl.last_usage:exact`、`:lower_bound`、`:not_exact` 或 `:nothing_counted`）。

### 还没做

还没有把已有的评测记录、`levi-vlm-eval` 的结果、AERI 队列的 `tokens.jsonl` 转成 `levi.eval.arm_record.v1` 的适配器；一轮的记录由调用方自己组装。教师臂的解码是声明的，不是强制的。表不计算配对差区间（它看不到逐题结果），也不核对 δ 是不是真在本轮之前写下的。这个模块没有读过真实的 Claude 会话里的子 agent 结果行；它的格式（`toolUseResult.agentId`）参照 AERI 的 token 工具，测试用的是手写夹具。
