# 评测记录：教师–学生三组对比

本页只写教师–学生三组对比这一节（`levi/eval/compare.py`、`levi/eval/tokens.py`）。子任务标注的人工与 agent 评测记录见 [English](EVALUATION.md)。

教师–学生做法里，每一轮都用三种方式做同一批题：同题、同解码、同评分，然后放进一张表比较。

| 臂 | 谁作答 |
| --- | --- |
| `teacher` | 外部 agent：提供范式和经验条目。它的答案是参考，不是金标准 |
| `local_harness` | 本地 VLM，经 LEVI 的 harness |
| `local_direct` | 同一个本地 VLM，直接调用，不经 LEVI |

主指标是：`local_harness` 的质量不低于教师，同时更快、花的外部 token 更少。所以表里除了质量，还有每秒质量和每外部 token 质量。

## 一个臂记什么（`levi.eval.arm_record.v1`）

| 字段 | 含义 |
| --- | --- |
| `arm`、`task`、`model` | 哪一臂（`teacher`、`local_harness`、`local_direct`）、任务名、作答的模型 |
| `episode_set`，或 `episode_set_hash` 加 `episode_count` | 题目集。`episode_set_hash` 是排序、去重后按字符串写出的 episode 编号的 SHA-256；两种写法都给时必须一致 |
| `decoding`、`scoring` | 本轮声明的解码和评分（JSON 对象；`scoring` 也可以是字符串），按所写内容比较。外部 agent 实际的采样无法观测也无法强制，所以教师臂记的是声明的解码 |
| `quality`、`quality_ci`、`quality_ci_method` | 对**金标准**的主指标（指标名写在 `scoring` 里）、区间 `[下界, 上界]` 和算法。`null` = 没测 |
| `agreement_with_teacher` | 本臂复现教师答案的比例，0 到 1。教师臂不填 |
| `wall_s` | 墙钟秒数，大于 0 |
| `local_tokens` | 本机 GPU 上花的 token |
| `external_tokens` | `input_new`、`output`、`cache_read`、`cache_write`（即 `levi.aeri.external_report.v1` 的四个字段），分开记。整个对象或某个字段为 `null` = 未知；`0` = 测得为零 |
| `external_tokens_source` | 外部数字的来源（一行） |
| `blocked` | 有什么阻塞（一行） |
| `per_episode` | episode 编号到分数。这是唯一的逐条字段，confirm 表里不允许 |

记录里出现表外的字段会被拒绝，逐条内容没法夹带进去。

## 一条命令

```bash
python -m levi.eval.compare teacher.json harness.json direct.json            # Markdown
python -m levi.eval.compare teacher.json harness.json direct.json --format json
python -m levi.eval.compare ... --confirm        # 只允许聚合分数
```

每个文件可以是一条记录、一个记录列表，或 `{"records": [...]}`。命令只读这些文件并打印；不起服务、不用 GPU、不开端口。在 Python 里：`compare.compare(records, confirm=False, token_basis=...)` 返回 `Table`，有 `to_markdown()` 和 `to_json()`。

Markdown 有三张表，每臂一行（质量与速度；本地与外部 token；相对教师和相对直接调用的差），外加 `local_harness` 对教师的结论：质量不低于教师、更快、外部 token 更少。结论只比点估计；差异小的时候先看区间。JSON 每臂一行，带全部列。

## 表强制的规则

- **同题同条件。** `task`、题目集哈希、`decoding` 或 `scoring` 不同的记录被拒绝，报错写明每一处不同。同一臂有两条记录也拒绝。
- **未知不是 0。** 为 `null` 的测量显示 `未知`，依赖它的比值和差也显示 `未知`。分母恰好为 0（例如本地臂没花外部 token）的比值显示 `不适用`，不是 `未知`。输入缺失时，结论写 `未知`，不写“是”。
- **每外部 token 质量**默认用 `input_new + output + cache_write` 作分母：外部模型新读入和新写出的部分。`cache_read` 是对缓存前缀的低价重读，单独一列、不加进去（沿用实验台账的口径；`AERI_EXTERNAL.md` 也说缓存维度不求和、不折算价格）。基准里有一个字段未知，合计就未知。用 `token_basis` 或 `--basis` 改口径；表会打印所用口径。
- **confirm 保持盲。** `confirm=True` 或 `--confirm` 时，带 `per_episode`、带明确的 `episode_set`（改给哈希和数量）、或在 `blocked`、`external_tokens_source` 里写自由文本（只允许 `gpu_busy` 这样的原因代码）的记录被拒绝。confirm 片段的内容和标签不给教师和总管看。

## 从 Claude 会话取外部 token（`levi/eval/tokens.py`）

教师的外部 token 取自它自己的会话记录。Claude 会话文件把一条 assistant 消息写成共享同一个 `message.id` 的多行，上面的 usage 一行比一行大。**每个消息 id 只计一次，取它的最后一条 usage**；取第一行会把一条消息少算好几倍（一份子 agent 记录上实测：第一行 output 是 16，最后一行是 415）。

```bash
python -m levi.eval.tokens session.jsonl [more.jsonl ...] --since 2026-11-06T09:00:00Z --until 2026-11-06T10:00:00Z
```

- **文件由调用方点名。** 代码不列目录、不展开通配符、不看 home 目录，所以不会扫进不属于本轮的会话。续接过的会话按从旧到新传（后面文件里的同一条消息替换前面的）。
- 一条消息按第一行的时间落入时间窗，落入后按它的最终 usage 计。
- 解析不了的行、既没有 `message.id` 也没有 `uuid` 的 assistant 行、设了时间窗却没有有效时间的消息、缺失或不是计数的 usage 字段，都会被**计数并报告**，不静默丢掉。这时 `exact` 为假，合计是下限。没有任何被计入的消息带某个字段，该字段是 `null`，不是 0。
- `Usage.external_tokens()` 和 `Usage.source` 用来填教师记录的 `external_tokens` 和 `external_tokens_source`。

## 还没做

还没有把已有的评测记录、`levi-vlm-eval` 的结果、AERI 队列的 `tokens.jsonl` 转成 `levi.eval.arm_record.v1` 的适配器；一轮的记录由调用方自己组装。教师臂的解码是声明的，不是强制的。这张表不是显著性检验。
