# 分级作答：决策层

`levi/decision/` 决定一道题**由谁来答**：本地模型、外部面板，还是人。它是纯核心：不调模型、不起服务、不开端口，`levi/automatic` 目前也没有使用它。English: [DECISION.md](DECISION.md)。

现状：契约、置信度抽取、校准和路由已实现，并在合成数据上测过。另已实现（纯 CPU，测试里用假面板）：升级队列、外部 token 台账和泄漏门。还没做：向模型服务请求 logprobs 的路径、对比记录的字段扩展，以及在真实数据或 AERI 里的使用。严格 JSON 的服务端返回的 logprobs 是语法掩码之前还是之后，**未核实**（要 GPU 测试），两种情况都支持。

## 路由顺序

按顺序判，第一条成立即定。

| # | 条件 | 结果 | 标签 |
| --- | --- | --- | --- |
| 1 | 该问题类型在此语境下属于物理安全，或未登记 | 交人 | 无 |
| 2 | 置信度未知、本地答案为 `null`（弃答）、选项外概率质量超限、有物理否决、分布外标志 | 外部面板 | `agent-reviewed` |
| 3 | 校准后概率 `p >= tau` | 本地 | `automatic` |
| 4 | 其余（`tau` 为 `ESC_ALL` 时一律） | 外部面板 | `agent-reviewed` |

面板不可用或答案不合格时，题目停在 `pending_external`：不写标签、不用本地答案、不找人。两名审阅者不一致时必须有裁定，否则这份响应不算作答。

物理安全表（`levi/decision/safety.py`）是静态的，键为（问题类型，离线或实时）。实时的“完成判断 / 可以放行”算物理安全，录好数据上的同类题不算。没登记的组合一律算物理安全。

## 置信度

闭式字段（布尔、枚举、字母选项）取值开头那个 token 的前几名候选，按允许取值归一化得到各取值概率，并给出落在所有允许取值之外的概率质量。字段定位不到、token 拼不回原文、某个候选同时是两个允许取值的开头、概率不合理，都记为**未知**，按升级处理。模型自报的置信度从不读取。物理一致性规则由调用方以否决函数注入；规则本身抛异常也算否决。

## 校准与阈值

`levi/decision/calibration.py` 只在 **tune** 条目上拟合一个温度：`split` 不是 tune 一律拒绝，`confirm` 有专门的错误（`ConfirmDataError`）。用来选 `tau` 的概率来自逐片段留一拟合。`tau` 取网格中外部 token 最少的值，约束是：分级系统相对纯教师臂的配对质量差，按片段聚类自助法（固定种子）的 90% 单侧下界不低于 `-delta`；`ESC_ALL` 的差恒为 0、永远可选，所以本地模型很差时退化成纯教师臂。并列时取更大的 `tau`。产物（`levi.decision.calibration.v1`）绑定模型、提示词和 harness 快照的 sha256，key 对不上就拒绝运行；同样输入得到逐字节相同的产物。

## 升级队列

`levi/decision/queue.py`。升级后的题目在面板不可用、或响应不合格时在此等待。文件只追加（JSON 行，`levi.decision.queue.v1`：`enqueue`、`rejected`、`answered`、`failed`），状态由重放文件得到。幂等键是**教师可见**请求的规范化 sha256，同一请求入队两次只写一行。教师可见请求里没有本地答案、置信度、问题类型、split 和条目引用；`EscalationRequest` 也没有能装下它们的字段。

状态只有三个：`pending_external`、`answered_external`、`external_failed`。队列不写标签、不退回本地答案、不找人。面板不可用（`send` 返回 `None` 或抛连接类错误）不算事件：本轮在此停下，其余题目仍是 pending。响应在重放时校验：严格 JSON（重复键、NaN/Infinity、嵌套超过 32 层都拒绝）、契约、`request_id`、标签档 `agent-reviewed`、答案和每个审阅者答案按请求的响应 schema、面板规则（两名审阅者不一致必须有裁定，且答案必须是裁定者的）。问题码：`not_json`、`panel`、`contract`、`request_id`、`label_tier`、`adjudication_missing`、`answer_schema`、`reviewer_schema`、`answer_not_panel_result`。不合格响应用掉一次尝试，达到 `max_attempts`（默认 3）后为 `external_failed`。第一份合格答案生效，之后的一律忽略。重放时对每条已存的 `answered` 行再按同一规则校验：不是对应请求的合格响应的行（例如手工追加的）会让重放抛 `QueueCorrupt`，不会被算作答案。`enqueue(request, context)` 还需要题目所在的语境（`offline` 或 `live`），遇到物理安全题就抛 `PhysicalQuestion` 拒绝（查 `safety.py` 的静态表；没登记的 kind 或未知语境按物理安全算）：这类题交给人，永不送面板。路由本来先做这个判断；队列再查一次，直接调用也绕不过去。pending 和 failed 的题留在分母里（`counts()["in_denominator"]`）。schema 检查支持 `type`、`enum`、`const`、`properties`、`required`、`additionalProperties: false`、`items`；其他关键字入队时就拒绝（`UnsupportedSchema`），不会有未经检查就被接受的答案。已记录的教师答案只有在请求 sha256 完全相同时才能复用（`reuse_reference_answer`）。

## 外部 token 台账

`levi/decision/ledger.py`。每个面板成员、每次升级尝试一行：请求 sha256、任务标签、purpose、split、角色、四个计数、`fresh`（`input_new + output + cache_write`）、`with_cache`（四项之和）、`exact`、来源码。两种口径和标签规则取自 `levi/eval/token_monitor.py`。**未知写 `null`，不写 0**；有未知字段的行永不 `exact`，含未知行的合计只是下限（`exact` 为 false）。被拒响应花掉的 token 同样记账（键：请求 sha256、purpose、角色、尝试序号；重放不会写两遍）。`report()` 按 split 和任务汇总；`confirm`、`frozen`、`heldout` 的记录只出现在聚合里，不出逐请求行。

## 泄漏门与 confirm 只开一次

`levi/decision/leakcheck.py` 在教师可见请求存入或发出之前检查（`Queue.enqueue`，`Queue.drain` 发送前再查一次）：任何键或字符串（含 request id）里不得出现被禁的片段 id、被禁的 sha256，也不得有解码后哈希命中被禁 sha256 的图片。名单由调用方注入（`Denylist.from_ledger_rows` 从 confirm 台账构造），本包不读任何金标准、frozen、heldout 目录。命中抛 `LeakFound`，退出码 **4**（`python -m levi.decision.leakcheck --request R --denylist D`；输入读不了为 2）。校准收到 confirm 数据是退出码 3；同一 confirm 片段被第二次认领是退出码 5。

`levi/decision/confirm.py`：`claim(rows, entry)` 是纯函数，`claim_ledger(path, entry)` 加锁追加。行格式和语义与 `levi.events.causal_eval.claim_ledger` 相同（复用其 `confirmed_overlap`，有测试双向核对）：id 或任一采集 sha256 已在台账里的片段，抛 `ConfirmReused` 拒绝，且不写入。

## 导入边界

`levi.decision` 不导入 `levi.automatic`、`levi.curation`，它们也不导入它。`tests/test_decision_import_boundary.py` 用 AST 检查。

## 测试

`tests/test_decision_*.py`（不需要 GPU 和网络）。每条不变量有测试，且各用一个去掉规则的变异验证过。
