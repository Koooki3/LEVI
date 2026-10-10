# 自动测评流水线（AERI）：契约与事务日志

[English](AUTOMATIC_PIPELINE.md)

**状态：只有地基。** 本页只写已经存在的两部分：集成契约（`levi/domain/aeri.py`）和持久化事务日志原语（`levi/automatic/journal.py`）。编排器、状态机、机器人适配层和命令都还没有；这里的代码不会让机器人动、不会启动模型、不会开端口，全部只用 Fake 测试。

流水线由三部分组成，彼此只通过带版本的消息交互：

- **A** 事件智能：提出事件，判定目标和场景；
- **B** 性能运行时：分配资源，提供策略动作块；
- **C** 自动流水线：唯一有运动权限的一方，也是运行日志的唯一写者。

## 契约（`levi.aeri.*.v1`）

| Schema | 方向 | 模型 |
| --- | --- | --- |
| `levi.aeri.event.v1` | A → C | `EventProposal` |
| `levi.aeri.judgement.v1` | A → C | `Judgement` 或 `JudgementUnavailable`（按 `kind` 区分） |
| `levi.aeri.scene.v1` | A → C | `SceneAssessment` 或 `SceneUnavailable` |
| `levi.aeri.runtime.v1` | B ↔ A/C | `WorkloadSpec`、`ResourceLease`、`AdmissionRejected`、`ResourcePressure`、`PolicyHandle`、`ChunkRequest`、`ChunkResponse`、`QuiesceAck`、`RuntimeUnavailable` |
| `levi.aeri.run_event.v1` | 只由 C 写 | `RunEvent`（日志的一行） |

每条消息都带 `schema`、`minor`、`run_id` 和 `emitted_wall_ns`（墙钟，只作审计，从不参与比较）。

**读消息**一律用 `aeri.parse(raw, contract)`（在 Python 里构造的消息用 `aeri.validate(dict, contract)`）。拒收时抛 `AeriError`，`code` 说明原因：

| 错误码 | 原因 |
| --- | --- |
| `E_TOO_LARGE` | 超过 256 KiB |
| `E_JSON` | 不是 JSON |
| `E_DUPLICATE_KEY` | 同一对象里键重复 |
| `E_NONFINITE` | `NaN`、`Infinity` 或溢出成无穷的数（`1e400`） |
| `E_CONTROL_FIELD` | A 或 B 的消息里任何位置出现机器人或复位命令键（`robot_stop`、`execute_reset`、`go_home`、`resume`、`command`，以及 `robot_`/`execute_`/`cmd_`/`force_` 前缀等）。判断前先规范化：兼容字符折叠，去掉零宽字符、软连字符和组合附加符，拆开 camelCase，大小写折叠，空格、`-`、`.`、`/`、`:` 都当作 `_`（所以 `robotStop`、`robot.stop`、`ｒｏｂｏｔ＿ｓｔｏｐ` 都会被拒） |
| `E_SCHEMA` | schema 标识不对、类型不对（严格模式：`true` 不是 `1`，`"1"` 也不是 `1`）、缺字段、取值越界 |
| `E_SCHEMA_TOO_NEW` | `minor` 比本读者认识的新（失败即关闭） |
| `E_UNKNOWN_FIELD` | 任何层级出现契约没定义的字段 |
| `E_INCONSISTENT` | 字段之间互相矛盾（见下） |
| `E_SPEC_MISMATCH` | 传了 `specs=` 时：谓词名不在所引用的规格里 |

不用 `model_validate_json`：它遇到重复键会静默保留最后一个。pydantic 默认的宽松模式会把 `true` 和 `"1"` 转成整数 1；AERI 模型是严格的，两者都拒。这两种行为都有测试固定。

**三种结果，互不混用。** `decision: confirmed | rejected`；`decision: unknown`（看过证据仍定不了，必须给 `unknown_reason`）；`kind: unavailable`（根本没有判定：门关、忙、超时、模型出错……）。“unknown”和“unavailable”都不算成功。只有 `gate_closed`、`busy` 和带 `retry_after_ms` 的 `admission_rejected` 可以标为可重试。

**一致性规则（节选）。** `confirmed` 至少要有一个 required 谓词，且全部为真，没有 confirmed 或 undecided 的否决项；`rejected` 要有一个为假的 required 谓词或一个 confirmed 否决项。场景只有在所有 required 谓词都为真时才是 `ready`；`failed_predicates` 和 `unknown_predicates` 必须恰好列出为假和读不出的 required 谓词。`valid_until_ns` 晚于 `produced_ns`，且最多晚 `MAX_RESULT_VALIDITY_MS`（30 秒）；租约最长 `MAX_LEASE_MS`（10 分钟）。片段 ID 的格式是 `<run_id>.<forward|reset>.<NNNN>`，必须属于消息所在的运行。策略端点只能是回环地址，且不能是 5000、5001、5100、7470、8000 端口。片段结果严格跟随目标核实：`verified` 是 `success`，`contradicted` 是 `failure`，`undecided` 和 `unavailable` 是 `unknown`（没判出来的永远不算失败）。判定里带了在线判定的原值（`legacy_c5`）时，必须与 decision 一致：未定的回答是 `unknown` / `model_undecided`，已定的成功是 `confirmed`，已定的失败是 `rejected`。运行事件里没有操作员标签的字段，状态只能是 AERI 的 13 个状态。这些跨字段规则无法写进 JSON Schema，快照看不到它们的变化，由 `E_INCONSISTENT` 夹具把守。

**时钟。** 一条消息里所有 `*_ns` 字段共用它的 `clock_domain`，本机单调时钟写作 `host-mono:<boot_id>`。`aeri.check_fresh(message)`（判定、场景评估或租约）读取的是**消费方自己的时钟**（`time.monotonic_ns()` 和本机时钟域；测试可以同时传 `now_ns` 和 `local`，不能只传一个）：时钟域不同抛 `E_CLOCK_DOMAIN`（无法比较，按已过期处理）；`produced_ns`/`granted_ns` 比这个时钟超前 `FUTURE_TOLERANCE_NS`（100 毫秒）以上抛 `E_FUTURE`；有效期已过抛 `E_EXPIRED`。它只是必要条件，不是充分条件：运行、片段、代次、请求这几道围栏另外要做。动作块应答里的 `received_ns` 由接收方用自己的时钟填写，从不由服务端填写，只作审计；动作块是否赶上截止时间由 `aeri.check_deadline(deadline)` 按接收方的时钟判断（硬截止已过抛 `E_EXPIRED`，软截止返回 `False`）。

**版本。** 消费方接受不超过自身的所有 `minor`，拒收更新的。minor 版本可以新增可选字段、登记新的事件类型、放宽上限、增加控制键。其他改动（删字段、新增必填字段、改动任何闭合枚举、类型、正则或默认值、缩短控制键名单）都要升主版本。

## Schema 快照

模型是唯一来源。`uv run levi dev check-contracts` 除了原有的 `docs/architecture/contracts.json`，还检查两件事：① `docs/architecture/aeri/v1/<contract>.schema.json` 与模型逐字节一致，并逐条列出破坏性差异；② 模型与 **`git merge-base HEAD main` 处**的快照比较（`--base` 可换成别的分支）。把破坏性改动连同重写后的快照一起提交，能通过第一项，第二项会把它抓出来。基线读不到（没有 git、不在检出根目录、分支不存在、历史太浅算不出 merge base）时检查失败，不会放行，所以 CI 必须连同历史取到 `main`。基线上没有的快照算新契约。

v1 未发布期间 `aeri.RELEASED` 为 `False`：此时与基线相比的破坏性差异只打印为提示，不判失败。v1 发布时把它设为 `True`，从此这类差异一律失败，破坏性改动必须升主版本。

`uv run levi dev check-contracts --write` 重写快照（AERI schema 和 `contracts.json` 一起写；AERI 部分被拒时什么都不写）。遇到 v1 内的破坏性改动会拒绝，除非加 `--accept-breaking`；这个选项只用于 v1 发布前（`RELEASED` 设为真后被拒，不带 `--write` 时也被拒）。控制键名单、可重试错误码、禁止端口和有效期上限都写进每份快照（`x-levi-*`），改动会在审查时显出来。

每份契约的夹具在 `tests/automatic/fixtures/<contract>/`（`valid/`；`invalid/` 下的文件名是 `<用例>.<错误码>.json`），由 `tests/automatic/aeri_factory.py` 生成。

## 运行日志（`levi/automatic/journal.py`）

一次运行一个目录（`<rollout_root>/.aeri/runs/<run_id>/`，点目录，后台实时标注服务不扫描）：

| 文件 | 内容 |
| --- | --- |
| `state_journal.jsonl` | 唯一事实来源：追加写的 `levi.aeri.run_event.v1` 行；第 0 行是运行头（计划 sha256、契约版本、LEVI 提交号）；每行用 `prev_sha256` 链到上一行 |
| `journal.lock` | 唯一写者的进程身份；写者对运行目录本身持有 `flock`，第二个写者被拒（`JournalBusy`），即使这个文件被删掉也一样；持有者死后内核自动释放锁 |
| `state.json` | 每次提交后派生的快照（临时文件、fsync、替换、目录 fsync），从不用来做决定 |
| `torn/` | 撕裂末行的字节，在把文件截回完整行之前先保存下来 |

**持久性。** 每行用一次 `write` 写入，随后 `fsync`；创建文件时对目录 fsync。后台实时标注服务自己的 JSON 写入（`levi/live/jsonio`）不做 fsync，这里不用它。

**事务。** `prepared` 落盘之后调用方才能动作；`acknowledged` 记录控制器报告的结果（`yes`、`no`、`unknown`：只说明执行与否，不说明目标是否达成）；只有 `committed` 才改变状态。真实动作只有在 `executed: yes` 之后才能提交，其他情况一律 `aborted`。`none` 动作不需要确认。同一时刻只能有一个未结束的事务，`from_state` 必须等于当前状态，控制代次（control epoch）不能倒退，`COMPLETED` 之后不能再开事务。

**幂等。** 事务 ID 唯一。动作的 `idempotency_key` 是 `sha256(run_id, episode_id, kind, step)`，有意不含控制代次：不幂等的动作（Home、启动复位、策略步）只要它的键出现过一次，就永远拒绝再次准备，之后任何代次、任何恢复之后都一样（FR3 服务端无法对命令去重）。`by_command(command_id)` 返回某条操作员命令已经做过的事；`expected_seq=` 让追加成为对下一行号的比较并设置（compare-and-set）。

**读回。** 只有没有换行、无法按 JSON 解码、或与上一行哈希链对不上的末行才算**撕裂**（崩溃造成）：它被忽略，写者重新打开日志时把它挪到 `torn/`。完整且链接正确、却不符合契约的行（`minor` 更新、有未知字段、违反规则），末行之前任何一行坏掉，或违反事务规则的行（文件被改过），都使日志成为**损坏**状态：什么都不截掉，只能只读打开，再也不写，`effective_state` 为 `FAULT_LOCKED`。

**恢复。** 任何重启之后先 `Journal.open(...)`，再 `recover(authority=<recovery 主体>)`。恢复从不重放、从不发送任何东西：

- 运行已到 `COMPLETED`：什么都不做；
- 日志损坏：报告 `FAULT_LOCKED` / `journal_corrupt`，不写任何内容；
- 其他所有情况（有悬空事务，或运行停在任何状态，包括 `WAIT_HUMAN`）：悬空事务写 `aborted` 并附一条 `crash_before_commit` 备注，再以新的控制代次开一个事务，把运行转到 `FAULT_LOCKED` / `recovery_ambiguous`。新代次使崩溃前签发的所有运动令牌失效。

解除 `FAULT_LOCKED` 需要操作员命令：日志只接受带 `command_id` 的操作员发起、只转到 `PREFLIGHT`、动作为 `none` 的事务离开它（恢复只能重新进入 `FAULT_LOCKED`）。提交必须保持它准备时的原因、片段和策略代次。测试在每个崩溃点（`before_prepared`、`after_prepared`、`after_execute`、`after_acknowledged`、`after_committed`）用 SIGKILL 杀掉子进程，并在每个字节位置截断文件；所有情况下恢复结果一致，替代机器人命令的那一步从不重复执行。

**限制。** 创建时只对运行目录及其父目录 fsync，不对所有上级目录；运行期间日志不轮转（每次状态转换写几行，不是每个控制步写一行）。
