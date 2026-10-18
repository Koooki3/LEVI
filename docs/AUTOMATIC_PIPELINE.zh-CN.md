# 自动测评流水线（AERI）

[English](AUTOMATIC_PIPELINE.md)

**状态：软件实现已做 fake I/O 验证，现场验收尚未完成。** 本页说明软件契约与显式执行桥。

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

**一致性规则（节选）。** `confirmed` 至少要有一个 required 谓词，且全部为真，没有 confirmed 或 undecided 的否决项；`rejected` 要有一个为假的 required 谓词或一个 confirmed 否决项。场景只有在所有 required 谓词都为真时才是 `ready`；`failed_predicates` 和 `unknown_predicates` 必须恰好列出为假和读不出的 required 谓词。`valid_until_ns` 晚于 `produced_ns`，且最多晚 `MAX_RESULT_VALIDITY_MS`（30 秒）；租约最长 `MAX_LEASE_MS`（10 分钟）。片段 ID 的格式是 `<run_id>.<forward|reset>.<NNNN>`，必须属于消息所在的运行。策略端点只能是回环地址，且不能是 5000、5001、5100、7470、8000 端口。片段结果严格跟随目标核实：`verified` 是 `success`，`contradicted` 是 `failure`，`undecided` 和 `unavailable` 是 `unknown`（没判出来的永远不算失败）。判定里带了在线判定的原值（`legacy_c5`）时，必须与 decision 一致：未定的回答不论 outcome 是什么都是 `unknown` / `model_undecided`（带未决否决项、有争议的豁免或缺少输入的成功也算未定），已定的成功是 `confirmed`，已定的失败是 `rejected`。运行事件里没有操作员标签的字段，状态只能是 AERI 的 15 个状态。这些跨字段规则无法写进 JSON Schema，快照看不到它们的变化，由 `E_INCONSISTENT` 夹具把守。

**时钟。** 一条消息里所有 `*_ns` 字段共用它的 `clock_domain`，本机单调时钟写作 `host-mono:<boot_id>`。`aeri.check_fresh(message)`（判定、场景评估或租约）读取的是**消费方自己的时钟**（`time.monotonic_ns()` 和本机时钟域；测试可以同时传 `now_ns` 和 `local`，不能只传一个）：时钟域不同抛 `E_CLOCK_DOMAIN`（无法比较，按已过期处理）；`produced_ns`/`granted_ns` 比这个时钟超前 `FUTURE_TOLERANCE_NS`（100 毫秒）以上抛 `E_FUTURE`；有效期已过抛 `E_EXPIRED`。它只是必要条件，不是充分条件：运行、片段、代次、请求这几道围栏另外要做。动作块应答里的 `received_ns` 由接收方用自己的时钟填写，从不由服务端填写，只作审计；动作块是否赶上截止时间由 `aeri.check_deadline(deadline)` 按接收方的时钟判断（硬截止已过抛 `E_EXPIRED`，软截止返回 `False`）。

**版本。** 消费方接受不超过自身的所有 `minor`，拒收更新的。minor 版本可以新增可选字段、登记新的事件类型、放宽上限、增加控制键。其他改动（删字段、新增必填字段、改动任何闭合枚举、类型、正则或默认值、缩短控制键名单）都要升主版本。

## Schema 快照

模型是唯一来源。`uv run levi dev check-contracts` 除了原有的 `docs/architecture/contracts.json`，还检查两件事：① `docs/architecture/aeri/v1/<contract>.schema.json` 与模型逐字节一致，并逐条列出破坏性差异；② 模型与 **`git merge-base HEAD <基线>` 处**的快照比较。基线依次取：`--base <ref>`，环境变量 `LEVI_CONTRACT_BASE`，本地 `main`，`origin/main`；显式指定的 ref 解析不到时不会换成别的。把破坏性改动连同重写后的快照一起提交，能通过第一项，第二项会在合并前的分支上把它抓出来（在 `main` 上跑等于 `main` 和自己比）。基线读不到（没有 git、不在检出里的源码树、ref 不存在、历史太浅算不出 merge base）时检查失败，不会放行，并给出修法。CI 运行 `levi dev check-contracts --base origin/main`，并取完整历史（`actions/checkout` 在 PR 和标签推送时不建本地 `main`）。基线上没有的快照算新契约。

v1 未发布期间 `aeri.RELEASED` 为 `False`：此时与基线相比的破坏性差异只打印为提示，不判失败。v1 发布时把它设为 `True`，从此这类差异一律失败，破坏性改动必须升主版本。

`uv run levi dev check-contracts --write` 重写快照（AERI schema 和 `contracts.json` 一起写；AERI 部分被拒时什么都不写）。遇到 v1 内的破坏性改动会拒绝，除非加 `--accept-breaking`；这个选项只用于 v1 发布前（`RELEASED` 设为真后被拒，不带 `--write` 时也被拒）。控制键名单、可重试错误码、禁止端口和有效期上限都写进每份快照（`x-levi-*`），改动会在审查时显出来。

每份契约的夹具在 `tests/automatic/fixtures/<contract>/`（`valid/`；`invalid/` 下的文件名是 `<用例>.<错误码>.json`），由 `tests/automatic/aeri_factory.py` 生成。

## 运行日志（`levi/automatic/journal.py`）

一次运行一个目录（`<rollout_root>/.aeri/runs/<run_id>/`，点目录，后台实时标注服务不扫描）：

| 文件 | 内容 |
| --- | --- |
| `state_journal.jsonl` | 唯一事实来源：追加写的 `levi.aeri.run_event.v1` 行；第 0 行是运行头（计划 sha256、契约版本、LEVI 提交号、复位模式和场景核对方式）；每行用 `prev_sha256` 链到上一行 |
| `plan.json` | 运行开始时的规范化计划及其 `plan_sha256`（与运行头相同）；在写运行头之前写一次（临时文件、fsync、替换、目录 fsync） |
| `journal.lock` | 唯一写者的进程身份；写者对运行目录本身持有 `flock`，第二个写者被拒（`JournalBusy`），即使这个文件被删掉也一样；持有者死后内核自动释放锁 |
| `state.json` | 每次提交后派生的快照（临时文件、fsync、替换、目录 fsync），从不用来做决定 |
| `torn/` | 撕裂末行的字节，在把文件截回完整行之前先保存下来 |

**持久性。** 每行用一次 `write` 写入，随后 `fsync`；创建文件时对目录 fsync。后台实时标注服务自己的 JSON 写入（`levi/live/jsonio`）不做 fsync，这里不用它。

**事务。** `prepared` 落盘之后调用方才能动作；`acknowledged` 记录控制器报告的结果（`yes`、`no`、`unknown`：只说明执行与否，不说明目标是否达成）；只有 `committed` 才改变状态。真实动作只有在 `executed: yes` 之后才能提交，其他情况一律 `aborted`。`none` 动作不需要确认。同一时刻只能有一个未结束的事务，`from_state` 必须等于当前状态，控制代次（control epoch）不能倒退，`COMPLETED` 之后不能再开事务。

**幂等：编排器必须遵守的规则。** 事务 ID 唯一。

- **物理动作**（`home`、`policy_steps`，包括启动复位）一律 `non_idempotent: true`，否则契约拒收。
- 每个不幂等动作都带 **`step`**，由编排器分配，**按（片段，动作种类）单调递增**。不幂等动作的 `step: null` 被契约拒收；不高于该片段、该种类上一次准备过的 step（即使从没用过）被日志拒收。
- `idempotency_key = sha256(run_id, episode_id, kind, step)`（辅助函数 `aeri.action_key`），有意**不含控制代次**；契约在每个 prepared 行上核对它。一个键只要准备过，之后任何代次、任何恢复之后都永远拒绝：FR3 服务端无法对命令去重，所以物理动作永不重发。
- 同一片段里两次合法的同种动作（第二次 Home）直接用下一个 step。
- **`executed: no` 之后的重试**：控制器报告动作没有执行，事务已 abort。重试是新的逻辑命令：用新的（更高的）step，并用 `retry_of: <事务 ID>` 指向那个已关闭的、同种类同片段的事务。`retry_of` 指向结果为 `yes`、`unknown` 或从未确认的动作时被拒；结果为 `unknown` 的动作转 `FAULT_LOCKED`，不重试。

`by_command(command_id)` 返回某条操作员命令已经做过的事；`expected_seq=` 让追加成为对下一行号的比较并设置（compare-and-set）。

**读回。** 只有没有换行、或无法按 JSON 解码的末行才算**撕裂**（崩溃造成：每行一次 write 加 fsync，崩溃最多留下一行没有换行的残行）：它被忽略，写者重新打开日志时把它挪到 `torn/`。能解码但与上一行哈希链对不上的完整行（这是改动，崩溃不会产生；比设计稿 X1 §5 的字面更严）、完整且链接正确却不符合契约的行（`minor` 更新、有未知字段、违反规则）、末行之前任何一行坏掉、或违反事务规则的行，都使日志成为**损坏**状态：什么都不截掉，只能只读打开，再也不写，`Journal.state` 和 `Scan.effective_state` 都是 `FAULT_LOCKED`。

**恢复。** 任何重启之后先 `Journal.open(...)`，再 `recover(authority=<recovery 主体>)`。恢复从不重放、从不发送任何东西：

- 运行已到 `COMPLETED`：什么都不做；
- 日志损坏：报告 `FAULT_LOCKED` / `journal_corrupt`，不写任何内容；
- 其他所有情况（有悬空事务，或运行停在任何状态，包括 `WAIT_HUMAN`）：悬空事务写 `aborted` 并附一条 `crash_before_commit` 备注，再以新的控制代次开一个事务，把运行转到 `FAULT_LOCKED` / `recovery_ambiguous`。新代次使崩溃前签发的所有运动令牌失效。

解除 `FAULT_LOCKED` 需要操作员命令：日志只接受带 `command_id` 的操作员发起、只转到 `PREFLIGHT`、动作为 `none` 的事务离开它（恢复只能重新进入 `FAULT_LOCKED`）。提交必须保持它准备时的原因、片段和策略代次。测试在每个崩溃点（`before_prepared`、`after_prepared`、`after_execute`、`after_acknowledged`、`after_committed`）用 SIGKILL 杀掉子进程，并在每个字节位置截断文件；所有情况下恢复结果一致，替代机器人命令的那一步从不重复执行。

**运行头和计划。** `Journal.create(..., reset_mode=, scene_check=, plan=)` 只在给了值时才把这两个模式（代码名）写进运行头；每行都按运行头的 minor 写：新运行写 `aeri.MINORS["run_event"]`（1），运行头的 `contracts` 列表按各契约自己的 minor 填写。编排器总是传入配置里的 `reset_strategy` 和 `scene_check`；dry run 还会传入作业的计划。计划的摘要必须等于 `plan_sha256`（`journal.plan_digest`，与 `load_job` 的规则相同）；运行目录里已有的 `plan.json` 必须是同一个 `plan_sha256`，创建和每次打开时都核对，从不覆盖。`journal.read_plan(run_dir)` 读回它（不是 JSON、或计划的摘要对不上时拒绝，`E_PLAN`）。从 JSON 读回后摘要会变的计划（键不是字符串）在写任何东西之前就被拒绝。`plan.json` 损坏时每次打开都被拒，运行在处理之前无法恢复：以运行头为准，把 `plan.json` 移走后重新打开（带 `plan=` 的打开会按运行头的 `plan_sha256` 核对后重写它）。

重新打开（`Journal.open(..., reset_mode=, scene_check=, authority=, plan=)`，`Orchestrator.restore` 用自己的配置调用它）时，配置里的模式与运行头不一致就拒绝：`E_PLAN`；给了 authority 时写一条 `run_header_mismatch` 备注（同一种不一致只记一次：监管进程反复重启也不会让日志变大）；运行头从不改写。运行头没有这两个字段（run_event minor 0 的日志）或调用方一个都没给时不核对。minor 0 的日志照常打开、追加和恢复，追加的每一行都保持 minor 0：同一个日志里不混写不同的 minor。重新打开时给 `plan=`，会给没有 `plan.json` 的旧运行补上。写运行头途中进程被杀，不会留下运行（目录已加锁、空文件、撕裂的运行头、或只有 `plan.json`）：`open` 报 `E_EMPTY`，可以重新创建；运行头写完后被杀，运行已存在，恢复到 `FAULT_LOCKED`。

读者拒收比自己新的 minor 写的日志（`E_SCHEMA_TOO_NEW`）：日志算作损坏，运行进入 `FAULT_LOCKED`。run_event minor 1 之前（提交 `acbb56c` 之前）的代码读新运行写的日志就是这样（失败即关闭：只读，不写）。`acbb56c` 到 `c8c9836` 之间的代码能读 minor 1，但早于这个写入方：它会接管新日志、追加 minor 0 的行，既不核对模式也不核对 `plan.json`。读者不强制“同一日志同一 minor”（那样会把回滚后的运行判为损坏），所以不要用旧代码重新打开运行。

**限制。** 创建时只对运行目录及其父目录 fsync，不对所有上级目录；运行期间日志不轮转（每次状态转换写几行，不是每个控制步写一行）。

## 状态机（`levi/automatic/state_machine.py`）

合同共有 15 个状态，包含 `FORWARD_JUDGING` 和 `RESET_JUDGING`。这两个状态表示动作已暂停、正在核实证据；界面应明确显示暂停和判定，运行仍在进行，进入 `WAIT_HUMAN` 之前不开放人工复位。显式事件暂停模式从 `*_ACTIVE` 进入 `*_JUDGING`，随后封存到 `WAIT_HUMAN`，或通过新的日志事务和策略代次恢复到 `*_ACTIVE`。

正常路径：

```
PREFLIGHT -> VERIFY_INITIAL -> FORWARD_ACTIVE -> FORWARD_STOPPING -> FORWARD_FINALIZE
  -> ROBOT_HOME -> SCENE_ASSESS -> FORWARD_ACTIVE（下一片段）... -> COMPLETED
VERIFY_INITIAL / SCENE_ASSESS -> RESET_ACTIVE -> RESET_VERIFY -> RESET_FINALIZE
  -> VERIFY_INITIAL（先 Home，再重新检查初始状态）
```

`TRANSITIONS` 为每个允许的 `(from, to)` 列出原因、权限主体（`orchestrator`、`operator`、`safety_guard`、`recovery`）和该转换能带的唯一动作类型；`check_transition` 拒绝其余一切（`E_ILLEGAL`、`E_REASON`、`E_AUTHORITY`、`E_ACTION`、`E_STEP`、`E_EPISODE`、`E_RESULT`），`check_journal` 按同一张表重读整份日志。最要紧的规则：

- 运动状态（`FORWARD_ACTIVE`、`RESET_ACTIVE`）由编排器以 `policy_steps` 动作进入：从场景判断（`VERIFY_INITIAL`、`SCENE_ASSESS`）开始，或在显式事件暂停模式从对应的 `*_JUDGING` 日志状态恢复；
- `policy_steps` 和 `home` 是物理动作、不幂等：必须标 `non_idempotent` 并带 `step`（`step=None` 被拒）。编排器给每个片段的物理动作编号（0：策略步，1：Home），片段号永不复用，所以日志的幂等键在任何代次、任何恢复之后都拒绝第二次尝试；
- 每个仍在运行的状态都可以转到 `FAULT_LOCKED`（编排器、安全守卫或恢复）。`WAIT_HUMAN` 和 `FAULT_LOCKED` 只能由带 `command_id` 的操作员命令转到 `PREFLIGHT`，不能直接回到运动；
- 操作员主体在任何规则上都必须带命令编号；
- 前向片段之后的 Home 是 `ROBOT_HOME -> SCENE_ASSESS`，原因为 `robot_home_reached`；操作员停止之后也可以改走 `ROBOT_HOME -> WAIT_HUMAN`（`operator_stop`，带操作员命令，不运动）；
- 片段结果写在 `FORWARD_FINALIZE -> ROBOT_HOME`、`RESET_FINALIZE -> VERIFY_INITIAL | WAIT_HUMAN` 的提交行上，以及从片段内状态转到 `FAULT_LOCKED` 的提交行上（以故障结束的片段：`task_outcome: unknown`，`stop_reason` 为故障原因。这里按流水线文档 §4.3 执行，X1 设计稿 §2.5 原本不给这类片段结果；停止原因为此增加了 `recorder_failed` 和 `home_failed`）。一步都没执行的片段丢弃录制（`recorder_abort`）。

**运动围栏。** `MotionFence` 只持有一个当前 `MotionToken`（`run_id`、`transaction_id`、`control_epoch`、`kind`、`episode_id`、`policy_epoch`，以及按编排器时钟计的到期时间）。机器人适配层每发一条命令前调用 `fence.check(token, kind, now)`。策略步令牌在提交进入运动状态之后签发；Home 令牌在 Home 事务的 prepared 行和 acknowledged 行之间签发。编排器在检查或准备任何其他事务之前先吊销令牌，所以迟到的线程、迟到的动作块或被拒的转换都不会留下运动授权。

## 编排器（`levi/automatic/orchestrator.py`）

`Orchestrator.create(run_dir, RunConfig, robot=, policy=, recorder=, events=, verifier=, scene=, clock=, fence=)` 开始一次运行；`run()` 一直驱动到需要人（`WAIT_HUMAN`、`FAULT_LOCKED`）或结束（`COMPLETED`）。每次状态变化都是 `prepare`（已 fsync）-> 动作 -> `acknowledge` -> `commit`；`executed: no` 中止并转 `WAIT_HUMAN` 或 `FAULT_LOCKED`，`executed: unknown` 一律转 `FAULT_LOCKED`。不做任何自动重试。

- **线程。** `run()` 只属于一个线程；`stop()` 和 `resume()` 可以来自任意线程。一把可重入的状态锁把每次“读状态、检查、prepare、commit”串行化，所以不会有事务从检查时以外的状态准备出来。`stop()` 从不等这把锁：它立刻把停止登记进一个登记表（有自己的小锁，控制循环每一步、每个片段开始前都会读）并返回 `stop_requested`，即使事务里的适配器调用很慢或挂死也一样；只有在 `WAIT_HUMAN` 中、并且 50 毫秒内拿到锁时，它才自己结束运行（`completed`）。`stop()` 只登记，不自己让机器人 hold（由循环的下一个安全点执行；适配器挂死时只能靠机器人侧看门狗和令牌过期）。停止一直保留到运行转到人工（无论因何原因进入 `WAIT_HUMAN`），届时所有已登记的停止一起被消费（第一个之外的编号记为 `stop_commands_merged`）：残留的停止不会挡住操作员下一次结束运行的停止。每次登记带代次：resume 只清除在它开始之前登记的停止（写成 `stop_command_lost` 备注）；resume 进行中到达的停止保留下来，在下一个决策点生效。
- **异常。** 任何从循环里逃出的异常（适配器或日志）都先吊销运动令牌，再让机器人 hold，把未结束的事务记为 `executed: unknown` 并关闭，把运行转到 `FAULT_LOCKED`（`watchdog_timeout`），然后才继续抛出。连这些都写不进去时，编排器在内存里停机（`halted`）：拒绝再运行或恢复，重启（`restore`）后从日志恢复。
- **备注。** 循环里产生的审计备注（被丢弃的动作块或判定、坏事件）只在内存里计数，到下一个事务边界按代码各汇总写一行；每个片段最多 `note_lines_per_episode` 行（超出后再写一行 `notes_suppressed`）。循环里不为单条备注做 fsync；`run()` 结束时再补写一次此后被压掉的计数。解释锁定或判定分歧的备注（`orchestrator_exception`、`hold_failed`、`motion_unacknowledged`、`recorder_error`、`early_stop_disputed`、`stop_command_lost`）不受预算限制。hold、quiesce 和转 `FAULT_LOCKED` 的事务先执行动作，积压的备注在其后写盘。

- **片段内**控制循环从不等判定：请求提交后以零超时收取，超过 `judge_request_timeout_ns` 撤回。动作块最多等到它的硬截止时间，并且只接受当前策略代次、当前请求和当前片段、按 C 自己的时钟准时、维度正确、`valid_from_action_index >= action_start_index` 的应答；其余丢弃并在日志里记一条备注（`chunk_dropped_*`）。连续 `chunk_failure_limit` 次失败则停止片段（`policy_error`）。
- **安全。** 守卫闩锁或红灯立即转 `FAULT_LOCKED`（`safety_stop`，由 `safety_guard` 发起）；机器人没有确认的命令（`unknown`）记为 `watchdog_timeout`；录制器失败记为 `recorder_failed`，rollout 改名为 `incomplete_*`，不写 `.complete`；Home 失败记为 `home_failed`。每次 Home 之前都先过安全检查（没有闩锁、没有红灯），否则记 `safety_stop`。
- **场景。** unknown 或 unavailable 从不跳过复位：`on_scene_unknown = "reset"`（默认）时运行复位策略，否则等人；`max_reset_attempts` 限制两次前向片段之间的复位次数。复位到达步数上限后封存、Home，然后等人（`reset_horizon_exhausted`）。
- **结果。** 前向结果在 Home 之前提交（此时 `robot_home: not_attempted`、`scene_reset: unknown`）；`task_outcome` 严格跟随 `goal_verification`，unknown 和 unavailable 永远不是成功。策略 quiesce 之后总会再请求一次终局判定；提前停止（`goal_verified`）只有在终局判定也确认时才记成功，否则记 `undecided`（或 `unavailable`），并写一条 `early_stop_disputed` 备注。终局判定必须覆盖片段的结尾：证据从第 0 步到最后一步，观测时间不早于请求（即 quiesce 之后）；否则丢弃（`judgement_dropped_stale`），目标核实记为 `unavailable`。相机帧连续 `camera_stall_limit` 次不变，或判定方因违约被停用时，终局判定为 `unavailable`。只有 rollout 封存为 complete 的片段才计入 `episodes`。
- **停止。** 停止在场景评估之前、之后、打开片段的事务里以及每一步都会检查：停止永远不会开出新片段。第一步之前就被停止截住的片段丢弃（`incomplete`、`unknown`），不计数、不 Home，运行等人。打断了执行的停止之后，按配置先 Home（`home_after_operator_stop = true`，设计稿的路径）或原地不动（`false`），然后等人。两条规则对前向和复位片段都适用（不 Home 的出口是 `ROBOT_HOME -> WAIT_HUMAN` 和 `RESET_FINALIZE -> WAIT_HUMAN`），也适用于执行结束后才到达的停止（quiesce、终局判定或封存期间）。
- **重启。** `Orchestrator.restore(run_dir, config, ...)` 打开日志并执行它的恢复：所有未完成的运行转到 `FAULT_LOCKED`（`recovery_ambiguous`），不重放任何东西。崩溃截断了某个片段时（最后提交的状态在片段内且该片段还没有结果），这次转换带上该片段的结果：`task_outcome: unknown`，`stop_reason: orchestrator_crash`；只有封存已提交时 rollout 才是 `complete`；Home 已准备但未提交时记 `robot_home: failed`（它可能执行过）。计数（片段号、策略代次、已完成片段数）从日志读回。
- **操作员命令。** `resume(command_id, expected_seq=, environment_handled=True, health_rechecked=True)` 把 `WAIT_HUMAN` 或 `FAULT_LOCKED` 转到 `PREFLIGHT`，运行停在那里直到再次调用 `run()`（resume 本身不推进任何东西）；重复的命令返回第一次的结果（被其他命令用过的编号被拒），过时的 `expected_seq` 被拒。`stop(command_id)`：同一个停止再发一次，在待生效时（`stop_requested`，标为重复）或已生效且下一次 resume 之前（`repeated`），都不改变任何东西，所以不会结束一个已在等人的运行；被 resume 用过的编号、或上一次 resume 之前用过的停止编号被拒（`command_used`），要换新编号重发。`stop(command_id)` 受控停止当前片段，在下一个决策点等人；在 `WAIT_HUMAN` 中则结束运行（`COMPLETED`）。

`RunConfig` 包含全部参数（片段数、步数上限、各种超时与上限、`termination`）。序号、step 和片段号都来自日志，所以 Fake 上固定种子的运行可以逐行重放。

## 终止裁决器（`levi/automatic/termination.py`）

四段，不跳过：**候选**（优先级为 `goal_candidate` 或类型在 `goal_event_types` 中的事件）-> **取证**（同一时刻只有一个请求，覆盖候选及其后 `settle_steps` 步）-> **确认**（属于本运行、本片段、本目标、本请求且未过期的 `confirmed` 判定，没有被撤回事件推翻；连续 `confirmations` 次）-> **停止请求**（`goal_verified`）。

永远不会导致停止的：`unknown`、`unavailable`、超时、属于其他运行或片段的判定、未请求或重复的应答、已过期的、来自其他时钟域或生成时间在未来的、证据没覆盖到稳定窗口的、违反契约的。这些一律丢弃并记备注，旧模式片段继续执行到步数上限（`on_unknown: continue_to_horizon`）。被丢弃的应答会撤回对应请求，裁决器不会无限等待；同一片段判定方违反契约达到 `violation_limit` 次后，本片段不再理会判定（也不做终局判定）；事件提供方违约同样次数后，本片段不再读它的事件；两种情况下运行都在下一个决策点等人（`contract_violation_limit`），优先于结束运行。迟到的旧事件不会把候选往前挪，确认必须覆盖候选（`observed_from_step` 不晚于候选）。

| `TerminationConfig` | 默认 | 含义 |
| --- | --- | --- |
| `allow_early_stop` | `true` | 为 `false` 时只记录确认，不据此停止 |
| `min_steps` | 10 | 此步之前不请求 |
| `settle_steps` | 8 | 证据要覆盖到候选之后这么多步 |
| `cooldown_steps` | 15 | 两次请求之间的步数 |
| `max_requests` | 6（`event_pause_judge` 为 20） | 每个片段的请求上限 |
| `confirmations` | 1 | 停止所需的连续确认次数 |
| `control_fraction` | 0.0 | 不提前终止的片段比例（按片段 ID 和 `control_seed` 确定），用来测量误提前终止 |
| `goal_event_types` | `object_settled` | 能成为候选的事件类型 |
| `violation_limit` | 3 | 违反契约多少次后不再理会判定 |

默认值没有在任何数据上标定过。

## 复位仲裁（`scene_assessment.py`、`reset_manager.py`）

机器契约与展示文案分离：`describe()`（含 `format`、`view_rule`）、
`plan.contract` 和计划摘要保持冻结的机器/审计数据，CLI JSON 保留原值。
计划 API 只投影契约标识、版本、状态，待办卡只投影标识、状态和谓词；
界面通过中英文词条展示状态，不翻译或重写冻结描述。目前这两个视图均不展示
`FORMAT_STATUS` 或 `VIEW_RULE`。此边界已有回归候选，但不代表已完成运行验证，
也不代表当前存在用户可见缺陷。

**只有证据足够的 `ready` 场景才能跳过复位。** 场景评估只是提供方的说法；`scene_assessment.arbitrate` 按任务的**初始状态契约**（Initial State Contract，`RunConfig.initial_state`）读它：

| 评估 | 结论 |
| --- | --- |
| `ready`，契约的每个必需谓词都读为真，且（`require_visible_evidence` 时）至少有 `min_evidence_refs` 个**不同的**帧或片段证据引用（同一引用写两次只算一次），并覆盖**每个**首选视角（`require_all_views`） | `ready`：开始下一个前向片段 |
| `ready`，但漏了或没读出某个必需谓词 | `unknown`（记 `scene_missing_predicate`） |
| `ready`，但缺少某个首选视角的证据 | `unknown`（记 `scene_missing_view`） |
| `ready`，但不同的证据引用不够 | `unknown`（记 `scene_insufficient_evidence`） |
| 任何观测时刻早于请求时刻（早于 Home 完成或复位结束）的评估 | `unavailable`（记 `scene_dropped_stale`） |
| 没有契约时的 `ready` | `unknown`（记 `scene_no_contract`） |
| 评估的是另一份契约 | `unavailable`（记 `scene_contract_mismatch`） |
| `reset_required`、`unknown`、unavailable | 原样 |

**没有契约时没有任何场景算 ready**（`initial_state = None`，默认）：ready 需要契约和证据，所以任何前向片段都开不了，也不运行复位（复位不可能让场景变成 ready）：每次场景核对都转人工。`levi automatic validate` 对这样的作业按真机运行直接拒绝（`validate --dry-run` 和 `run --dry-run` 仍可运行，并给出警告）。**契约文件格式是草案（HA-23）：** 下面是能承载流水线文档 §6.1 的最小格式，等用户确认；**视角规则**同样待确认：证据引用用第一个 `:` 之前的文字表示相机视角（`<视角>:<帧>`），视角名取自契约的 `observations.preferred`，代码里不写死任何相机名。

```yaml
initial_state:
  id: stack-plates-initial
  version: "1"
  status: draft            # 用户确认前为 draft（HA-23）
  robot:
    home_pose: fr3_safe_home
    gripper: open
  predicates:
    required: [object_at_source, gripper_open]
    optional: []
  observations:
    preferred: [side, wrist]
    require_visible_evidence: true
    min_evidence_refs: 1
    require_all_views: true   # 证据须覆盖每个首选视角（HA-23）
```

用 `scene_assessment.load_contract(text)` 读取。读取器只接受严格的 YAML 子集（不新增依赖）：用空格缩进的块映射、标量列表（`- a` 或 `[a, b]`）、带引号或不带引号的标量、`true`/`false`/`null`、有限数字和 `#` 注释；拒绝制表符、锚点、别名、标签、块标量、流式映射、多文档、重复键以及未知键。以 `{` 开头的文档按 JSON 读。谓词名用场景提供方的名字：契约把 `(id, version)` 和这些名字登记给 `aeri.parse`。

**场景不是 ready 时怎么办**由复位策略决定（`RunConfig.reset_strategy`，`reset_manager.py`）：

| 策略 | 场景不是 ready |
| --- | --- |
| `single_reset_policy`（默认） | `reset_required` 运行复位策略；`on_scene_unknown = "reset"` 时 `unknown`/`unavailable` 也运行，否则转人工；两个前向片段之间最多 `max_reset_attempts` 次复位，之后转人工 |
| `human_assisted` | 一律转人工（不运行复位策略） |

`human_assisted` 就是“仅测评 policy”模式（人工复位，AUT-22）；仲裁接口不依赖 `single_reset_policy`。人恢复之后系统会再核对一次场景（第二道确认，设计稿 X2 §1.2），**只有 `ready` 才开始前向片段**：`unknown`、unavailable、矛盾或契约对不上的回答、观测早于请求或证据不足、答的是别的片段，都会再次转人工，原因写在 note 里。永远答不出的场景核对会让运行在 `WAIT_HUMAN` 与 `VERIFY_INITIAL` 之间反复，所以没有场景提供方时拒绝启动该模式（`cli.launch_problems`；由人作为提供方的 `operator_attested`，即设计稿 X2 的方案，留给后续任务）。`atomic_skill_sequence` 和 `scripted_safe_reset`（流水线文档 §6.4）在 v1 中被拒绝。`reset_manager.check_plan` 拒绝在 `ready` 以外的场景上开始前向片段的策略，`check_after` 拒绝在复位到达上限、被停止或失去策略之后还继续、或在停止之后重试的策略。验证成功的复位总是进入下一次场景核对，晚到的停止（quiesce 或复位后场景评估期间到达）在那里生效：运行转人工，复位结果保持原样；除非 `home_after_operator_stop` 为 false，否则先 Home。违反规则的策略交给人处理（记 `strategy_refused`），不会锁住运行。复位到达上限时先封存（保留失败的 rollout，结果为 `failure` / `horizon_exhausted`），再 Home，然后转人工；Home 失败则锁定运行（`home_failed`），在操作员恢复之前什么都不再动；恢复要经过 `PREFLIGHT` 和一次新的初始状态检查。同一个命令 ID 重复恢复只恢复一次。

## 提供方适配层（`levi/automatic/adapters/`）

**Fake 提供方（`events.py`）。** `FakeEventStream`、`FakeGoalVerifier` 和 `FakeSceneAssessor` 交出契约字节，由 `aeri.parse` 读取；脚本可设定结论（`confirmed`、`rejected`、`unknown`、`ready`、`reset_required`）、提交或收取时的 unavailable 码、延迟、已过期/未来/其他时钟域的有效期、更新的 minor、控制键、互相矛盾的谓词、未登记的谓词、其他片段或请求。`make_request` 拒绝任何带操作员或评测字段、或机器人命令的请求。

**在线判定（C5）-> 判定（`legacy_live.py` 的 `from_c5`）。** 只读取 `levi.live.online` 的公开名字，不改动它。

| C5 `status` | 条件 | 结果 |
| --- | --- | --- |
| `ok` | `undecided`（包括未决的成功） | `unknown` / `model_undecided` |
| `ok` | `outcome=success` | `confirmed`；两个答案字段不都支持时为 `unknown` / `conflicting_predicates` |
| `ok` | `outcome=failure` | `rejected`（两个字段都正常时附一个 confirmed 的 `c5-rule` 否决项） |
| `unavailable` | `gate_closed`、`gate_pending`、`busy`、`service_busy` | `Unavailable`，可重试：属于降级，不是错误 |
| `unavailable` | `cold_start`、`vllm_starting`、`no_room`、`wake_failed`、`vllm_failed`、`shutting_down` | `Unavailable`，不可重试 |
| `error` | `timeout:` | `Unavailable(timeout)`：超时永远不是 `unknown` |
| `error` | `invalid_answer:`、`model_error:`、`internal_error:`、`invalid_request:` | `Unavailable(invalid_answer / model_error / provider_error / contract_violation)` |
| 其他 | | `Unavailable(contract_violation)` |

映射出的判定总是保留 C5 原值（`legacy_c5`），契约按原值复核映射：映射错误时返回 `Unavailable(contract_violation)`（原值写在 `detail` 里），绝不去掉原值把错误的判定放过去。

**AERI 状态 -> 客户端会话状态（C2）。** 只写客户端的 7 个状态；凡是策略可能在推理的状态一律写 `running`：

| AERI 状态 | 活动角色文件的 C2 状态 |
| --- | --- |
| `PREFLIGHT` | `standby` |
| `VERIFY_INITIAL`、`SCENE_ASSESS`、`WAIT_HUMAN`、`FORWARD_FINALIZE`、`RESET_VERIFY` | `waiting_reset` |
| `FORWARD_ACTIVE`、`FORWARD_STOPPING` | `running`（前向文件） |
| `RESET_ACTIVE` | `running`（复位文件；前向文件写 `standby`） |
| `ROBOT_HOME`、`RESET_FINALIZE` | `homing` |
| `FAULT_LOCKED` | `fault` |
| `COMPLETED` | `finished`（操作员停止后为 `stopped`） |

测试把每个状态都喂给后台实时标注服务自己的门控（`gpumgr.gate`）和冷启动保护：门恰好在策略可能推理时对推理关闭。若把复位策略执行状态写成 `waiting_reset` 或任何新状态名，门就会打开。

## 录制层（`levi/automatic/recorder.py`）

`RolloutRecorder(root, run_id=, run_dir=, group=, texts=, media=)` 把每个片段写成后台实时标注服务已经能读的 rollout 目录（接口 C1）：`<root>/<group>/<task_folder>/demo_NNNN`，`NNNN` 是片段编号，前向和复位片段各在自己的任务目录（`RunConfig.forward_folder`、`reset_folder`）。每次运行请用自己的任务目录：目录里已经用过的编号（`demo_`、`incomplete_` 或 `discarded_`）绝不覆盖，片段打不开，运行锁定（`recorder_failed`）。

**封存**沿用后台实时标注服务的规则（`levi.live.criteria`）：同步步骤文件（`aeri_steps.csv`），媒体写入器收尾，`events.csv` 写入 `episode_end` 行，`metadata.json` 写入 `stopped_at`、`media_storage.video_frames_match_csv` 和 `cameras.stall_detection.stalled`，每个文件整份写入并同步；再读回核对；**最后才创建 `.complete`**（临时文件、fsync、改名、fsync 目录）。标记之前任何一次写失败都会抛出，所以不会在不完整的 rollout 上留下标记；运行锁定（`recorder_failed`），目录改名为 `incomplete_NNNN` 并写 `eval.abort_reason`（安全停止写 `fr3_fault`，后台实时标注服务会把它读成 FR3 故障）。重复封存返回第一次的结果。

**标签。** `eval.outcome` 保持 `unlabeled`，`eval.verdict_by` 为 `aeri`，不写 `eval.agent_label`：自动结论只记在运行日志里，绝不写成操作员标签或 agent 标签。`eval.aeri` 记录运行和片段。

**运行 manifest**（`<run_dir>/manifest.json`，`levi.aeri.manifest.v1`）：本次运行打开过的每个片段、它的目录和状态（`opening`、`open`、`complete`、`incomplete`），复位片段的 `after_forward` 和前向片段的 `after_resets`。条目在目录创建之前写入。它是派生视图，唯一事实来源仍是运行日志。

**重启之后** `Orchestrator.restore` 调用 `recorder.recover(...)`：本次运行中封存事务没有被日志提交的 rollout 一律改名为 `incomplete_*`（`orchestrator_crash`）；如果崩溃发生在写标记和提交之间，先删除标记，保证不会出现“目录说完成、日志说未完成”。其他运行的目录从不触碰。

**会话文件（C2）。** `SessionFiles(root, run_id=, group=, folders=)` 作为编排器的 `listener`：每次提交状态后重写两个角色文件（`<root>/.eval_sessions/<group>__<task_folder>.json`），只用客户端的 7 个状态（见上表）、ISO 时间、`levi.reset_wait_s: null`，前向文件写 `levi.mode: unattended`，复位文件写 `levi.enabled: false`，另有 `episode_role` 和 `aeri{run_id, state, control_epoch}`。会话写失败只记一条 note（`session_write_failed`），不会停止运行。`heartbeat()` 重写最后的状态（后台实时标注服务读端在 10 秒没有更新且进程不在时判定会话崩溃）。请用 `watch.exclude` 把复位目录排除在后台实时标注之外，免得前向规格去标注复位片段。

**媒体。** `MediaSink`（`open`、`frame`、`finish`、`abort`）负责写采集格式（视频、位姿和夹爪 CSV）并报告事实。默认的 `NullMedia` 不写视频。某个相机的帧连续 `stall_limit`（10）次观测没有变化时，录制层自己把它封存为停滞，后台实时标注服务因此会拒收该 rollout，与客户端的 rollout 一样。

## 指标（`levi/automatic/metrics.py`）

`metrics.report(events, labels=, manifest=, termination=, max_steps=, reset_mode=, scene_check=)` 读取运行日志（步数取自运行 manifest），给出下表各组指标；每个比率都是 `{"n", "of", "rate", "wilson95"}`，区间用后台实时标注服务统计模块的 Wilson 95% 区间（`levi.live.stats.wilson`）。探索阶段只有 20–30 个片段时，要看区间，不要只看比率。

| 组 | 指标 |
| --- | --- |
| 自动结论 | 前向片段数、结局计数、`autonomous_success_rate`（unknown 留在分母里）、停止原因 |
| 提前终止 | 提前停止（`goal_verified`）对照真值的混淆表；精确率（真成功的提前停止 / 提前停止）、召回率（提前停止 / 检测器本可停下的真成功片段：提前停止或跑满的片段，不含被人、故障或策略结束的片段）、**只用对照组计算的误提前终止率**（流水线文档 §5.5：对照片段不允许提前终止、跑满上限，其中检测器本会停下的、占“真失败、跑满上限、封存成功且带对照记录”的对照片段的比例；其余在 `left_out` 中按原因单独计数：`cut_short` 是被人、故障或策略结束的，`not_recorded` 是没封存或没有对照记录的；没有这类片段时为 `available: false`，不给数字）、实验组的“真失败却提前停止 / 真失败”只作为下界 `treatment_false_early_stop_lower_bound`（停止掩盖了之后的情况）、节省步数（`max_steps` 减去实际步数，对提前停止求和）、对照片段单列、自动结论与真值的一致率和误判成功数 |
| 复位 | 复位次数、`autonomous_reset_success_rate`、场景决策与跳过次数；跳过准确率（在真就绪的场景上跳过、或在真需要复位的场景上复位 / 有标签的决策）、错误跳过率、多余复位率、复位耗时 |
| 自动化 | 干预次数（进入 `WAIT_HUMAN` 或 `FAULT_LOCKED`）及原因、恢复次数、最长无干预的连续前向片段数、人工等待时间（仅在时钟域没变时计） |
| 周转 | 从片段 k 回位到达（`ROBOT_HOME -> SCENE_ASSESS` committed）到片段 k+1 的 `FORWARD_ACTIVE` committed，按运行所处状态拆分：`scene_ms`（`SCENE_ASSESS`）、`reset_policy_ms`（`RESET_*`）、`human_reset_ms`（`WAIT_HUMAN`、`FAULT_LOCKED`）、`verify_ms`（`PREFLIGHT`、`VERIFY_INITIAL`），各部分之和等于 `turnaround_ms`。`with_person` 是需要人介入的周转所占比例。跨越时钟域变化的窗口记为 `unmeasured`；最后一个片段之后的窗口记为 `no_next_episode`；运行还停在其中的记为 `open`。片段没有回位到达就结束时（操作员停止后原地等待的 `ROBOT_HOME -> WAIT_HUMAN`、回位失败的 `ROBOT_HOME -> FAULT_LOCKED`）不开始窗口；`turnaround_unmeasured` 按原因统计所有没计入的周转（`clock_domain_changed`、`not_homed:<状态>:<原因>`） |
| 每个有效片段 | `time_per_valid_episode_ms`（日志在单调时钟上的时间跨度，按时钟域分段求和，除以封存完整的前向片段数；不含崩溃到重启之间的停机时间，`downtime_excluded: true`）和 `human_minutes_per_valid_episode`（在 `WAIT_HUMAN`/`FAULT_LOCKED` 中、以恢复或操作员停止结束的分钟数，除以同一片段数；运行还在等的记为 `open_waits`；计划内等待中途出故障时，故障之后的时间算计划外）；没有有效片段时 `value` 为 null |
| 由人做的场景决定 | `scene_check: operator_attested` 时由人回答场景核对：这些决定单列在 `scene_decisions_by_human`，不进复位组的跳过准确率（那是机器提供方的指标） |
| 一致性 | 自动判定与操作员标签的一致性，含合计和按片段结束方式分层（`by_ended_by`：`budget`、`early_stop`、`operator_stop`、`unknown`）；见下文“操作员标签与双标签对比” |

**跨复位模式可比**（设计稿 X2 §1.2）。报告头写明 `reset_mode`（`single_reset_policy`，或 `human_assisted`：仅测评策略，由人复位场景）和 `scene_check`（`provider` 或 `operator_attested`），`mode_source` 说明各自来源：调用方、运行头（契约 minor 1）、manifest（有由人复位场景的前向片段：`episodes[*].preceded_by: human_reset` 或非空的 `after_human_resets`）、日志（出现复位片段即为复位策略模式）、默认值（`provider`）或 `unknown`。`comparable` 列出两种模式含义相同的字段，跨模式只比较这些字段（其余字段列在 `mode_specific`）。在 `human_assisted` 下，场景核对（`VERIFY_INITIAL`/`SCENE_ASSESS`）因 `scene_reset_required`/`scene_unknown` 转入 `WAIT_HUMAN` 属于**计划内**干预：这就是该模式的复位方式。其余全部是**计划外**：`FAULT_LOCKED`、复位策略次数用尽、预检失败、操作员停止。`automation` 原有的键含义不变，新增 `planned`、`unplanned`（按原因，以及计划外占比及其区间）、`longest_run_without_unplanned`（可比）、`longest_run_without_any_human`（计入每次干预，与 `longest_run_without_intervention` 相同）和 `person_ms`。模式未知的运行（没有复位片段的 minor 0 日志，且调用方没给模式）把每次干预都算作计划外，不会美化结果。

**四类标签，互不混用**（流水线文档 §9.4）。`autonomous_verdict` 就是日志里的片段结果：不在别处写，也绝不称为真值（相应比率都叫 `autonomous_*`）。`posthoc_verdict`、`operator_label` 和 `adjudicated_ground_truth` 存在 `<run_dir>/labels/<kind>.jsonl`，每类一个只追加的文件（每行 fsync），用 `LabelStore.add(kind, episode_id, value, subject=, by=)` 写入。写一类标签从不改动别类的文件；同一类、同一片段、同一主题的第二个标签会被拒绝，除非写明 `supersede=True`，此时追加一行（第一行保留）。`by` 是不透明的主体 ID（不写姓名或邮箱）。主题有 `task_outcome`（`success`/`failure`；`operator_label` 还可以是 `discarded` 或 `unclear`，二者都不当真值）和 `initial_state`（`ready`/`reset_required`：开始这个片段之前场景是否需要复位）。比率用的真值：有裁定标签就用裁定标签，否则用操作员标签（`truth="adjudicated"` 只用裁定标签）；没有真值的片段计为 `unlabeled`，不进比率。

标签文件末行写到一半（崩溃）不会吞掉数据：下一条标签之前，撕裂的字节被移到 `labels/torn/`，文件截回到完整行；这一步和“是否已有标签”的检查在同一把锁下完成。不是最后一行的坏行会让文件不可用，直到有人检查（绝不跳过）。

**对照片段**（`termination.control_fraction`、`control_seed`：比例和抽取方式，参数待用户确认，HA-23）记在运行 manifest 里：每个封存的片段有 `control`，对照片段还有 `would_stop_step`（检测器本会停下的步，或 `null`）。

## 操作员标签与双标签对比

操作员给每个已结束的前向片段打自己的成败标签，与运行的自动判定分开存放，再做一致性对比（T-CL-14）。这与真机现有路径（`levi live`，`agreement.by_ended_by`）的双标签对比是同一回事，这里用于两种复位模式的 AERI 运行；`human_assisted` 下，操作员在复位场景的同时给刚结束的片段打标签。

**标签的取值。** `operator_label` 的主题 `task_outcome` 取 `success`、`failure`、`discarded`（这个片段不该计入）或 `unclear`（看不清）。`by` 是不透明的主体 ID（不写姓名或邮箱）。同一片段可以多次标注：每条都追加到 `<run_dir>/labels/operator_label.jsonl`，最新一条是当前值，旧的保留。`discarded` 和 `unclear` 永远不当真值：当前操作员标签是这两个值的片段没有操作员真值（裁定标签仍可给它真值）。

**什么时候能标。** `metrics.label_operator(run_dir, episode_id, value, by=)` 只接受运行日志里已经提交了结果的前向片段，不论它怎样结束（提前终止、用完预算、操作员停止、故障）；运行中、`WAIT_HUMAN` 期间、运行结束后都可以补标。尚未结束、不存在的片段和复位片段一律拒绝。标签通道不拿锁地读日志，从不写日志、manifest、rollout 或会话文件，只写标签文件（和标签的锁文件）。

**先盲标。** 操作员先下判断，再看运行的结论。一个片段的第一条 `success` 或 `failure` 标签会揭示它的自动判定；`discarded` 和 `unclear` 不揭示。每条操作员成败标签都在标签锁内记下 `verdict_revealed_before`（写入时判定是否已揭示）和 `reveals_verdict`（是否由这条标签揭示），揭示本身因此落盘。揭示按片段算，不按人算：此后谁看卡片都能看到判定。

等人时的待办卡（`recorder.pending_card`）有一个 `operator_label` 区块，针对最近结束的前向片段：当前标签（未标时为 null）、`blind_label`（第一条成败标签）、`revealed`、`revised_after_reveal`、已有几条标签、允许的取值、`ended_by`，以及揭示后才显示的自动判定；揭示之前 `automatic_verdict` 和 `ended_by` 为 null，`automatic_verdict_hidden` 为 `hidden_until_labelled`，`agrees`（盲标签与判定是否一致）为 null。卡片默认盲：揭示之前还隐藏 `last_episode` 里的 `task_outcome`、`goal_verification` 和 `stop_reason`（`verdict_hidden`），因为提前终止（`goal_verified`）从来不会是失败，等于告诉操作员检测器的结论。`pending_card(run_dir, blind=False)` 保留 `last_episode` 原来的样子（`operator_label` 区块仍隐藏判定）。`levi automatic label` 既不打印判定，也不打印片段怎样结束。

**片段的结束方式**（`ended_by`，取自日志里该片段的停止原因）：`budget`（`horizon_exhausted`）、`early_stop`（`goal_verified`）、`operator_stop`（`operator_stop`），其余都是 `unknown`（故障、策略、看门狗、崩溃、没有原因）。

**对比**（`metrics.report(...)["agreement"]`）。自动判定是日志里该片段的终局判定（`goal_verification`：`verified` 为成功，`contradicted` 为失败，`undecided` 为未决，`unavailable` 为无）。只比较操作员标为 success 或 failure 的片段，用的是**盲标签**（揭示之前写的第一条成败标签）；之后的改动保留在文件里，成为当前值和真值，但对比只把它计入 `revised_after_reveal`。没有盲标签的片段按当前值计入 `discarded`、`unclear` 或 `unlabelled`。`judged` 是参与对比、且运行也给出了成败的片段数。每个分层（`by_ended_by`：`budget`、`early_stop`、`operator_stop`、`unknown`）和合计 `total` 都给出：`episodes`、`unlabelled`、`discarded`、`unclear`、`operator_decided`、`matrix`（操作员 × 自动）、`judged`、`agree`、`agreement`、`agent_success_operator_failure`（操作员说失败而运行说成功）和 `agent_failure_operator_success`（反过来），二者都只算成败成对的样本，`undecided` 和 `none` 单列（从不算作一致），以及 `revised_after_reveal`。它们不是 `levi live` 的 `false_success`/`missed_success`：后者把未决判定算进分母（`missed_success` 把它算作漏判），这里不算，所以两边数字不能逐个对比。每个比例都带 Wilson 95% 区间；某分层成败成对的样本少于 10 个（`min_n`）时只给区间、不给点估计（`rate` 为 null，`small_sample` 为 true）。`min_n` 只是显示门槛，不是显著性门槛：10 对时区间宽度仍约 0.45。`agreement` 列在 `comparable` 中：两种复位模式下含义相同。

**怎样读分层。** AERI 里的提前终止由检测器触发，无人值守运行也会这样停。`budget` 是评判器在检测器没有叫停的完整片段上的一致性（经过检测器筛选，偏向失败；对照片段是唯一没被这样筛选的跑满片段）。`early_stop` 是检测器加评判器组合的一致性，对应无人值守 AERI 的实际用法。`operator_stop` 是人结束的片段，不能迁移到无人值守运行。（`levi live` 里提前结束靠操作员按键，所以那里只有 `budget` 能迁移。）

**局限。** 片段长度（视频）仍会让操作员知道它是否提前停下，所以 `early_stop` 分层对检测器的在线决定并不盲，卡片只是不再额外提示。探索阶段只有 20–30 个片段，每个分层都很小，要看区间。操作员标签是一个人的判断，不是裁定真值（计算比率时 `adjudicated_ground_truth` 优先）。HTTP 路由（`POST /runs/{id}/labels`）和页面是后续任务。

## 仅测评策略模式（人工复位）

`reset.strategy: human_assisted` 只测评前向策略：不运行复位策略，由人把场景复原。其余与有复位策略时同一套运行（同一状态机、提前终止、终局判定、封存、回位）；`RESET_ACTIVE`、`RESET_VERIFY`、`RESET_FINALIZE` 永远不会进入。本节取代上文关于 `operator_attested`“以后再做”的说法。

**行为。** 场景不是 `ready`（在 `VERIFY_INITIAL` 或前向片段之后）时，运行进入 `WAIT_HUMAN`（`scene_reset_required` 或 `scene_unknown`）。等待期间没有任何运动令牌。

**两道确认。** 第一道是操作员声明：`resume(command_id, expected_seq, environment_handled=True, health_rechecked=True)`，缺一项确认就拒绝（`confirmations_missing`），同一 command id 只生效一次（`repeated`），序号过时则拒绝（`stale_sequence`）。第二道是系统复核：`PREFLIGHT` 重新检查机器人，`VERIFY_INITIAL` 重新评估场景。只有仲裁给出 `ready` 才开始前向片段；`unknown` 和不可用都回到 `WAIT_HUMAN`。第二道复核没有任何绕过方式。

**谁来核对场景**（`reset.scene_check`）：

| `scene_check` | 由谁 | 说明 |
| --- | --- | --- |
| `provider`（默认） | 机器场景提供方 | 人工复位模式下，提供方的 `reachable()` 不返回 `True` 就拒绝启动（没有这个检查也按不可达处理；`E_SCENE_PROVIDER_MISSING`，`cli.scene_provider_problems`），避免在没人能回答时在 `WAIT_HUMAN` 和 `VERIFY_INITIAL` 之间来回 |
| `operator_attested` | 人（`adapters/human.py` 的 `HumanSceneProvider`，`provider: human`） | 只用于 `human_assisted`，需要初始状态契约 |

`operator_attested` 时，核对先抓取当前相机画面（在 resume 及其预检之后），先存入本次运行的证据库，再发出一道题：请求编号（只问一次）、随机 `nonce`（`secrets` 生成）、契约 `id@version` 和状态、每个谓词的文字，以及画面和它们的文件（`evidence/frames/<sha256>.<扩展名>`，相对运行目录）和 `frames_sha256`，人看的就是系统抓的帧。答复是 `{request_id, nonce, frames_sha256, predicates}`（`human.answer_for`）：原样带回 nonce 和帧摘要，每个必需谓词只回答 `true`、`false` 或 `null`（看不清）。提问之前就存在的答复（请求编号可以预测）、nonce 或帧摘要对不上的答复，一律不采纳。nonce 和摘要只能证明答复是在提问之后、并且针对这道题的帧；它们证明不了真有人看过画面（能写传输目录的程序也能原样带回），所以传输目录要和运行目录一样保持私有。结论由回答推出（有必需谓词为假：`reset_required`；有 `null`：`unknown`；全为真：`ready`，仍要经过仲裁，包括证据规则和视角规则）。回答里带结论或“ready”一律拒收。重复回答、回答从未发出、已答过或已撤回的请求，一律丢弃（`scene_answer_unsolicited`，`E_UNSOLICITED`）；格式不对的回答被拒收（`scene_answer_refused`），题目保持有效。超过 `reset.human_scene_timeout_s`（默认 600）无人回答，题目撤回并按不可用处理，运行再次等人。操作员的停止会立即结束等待中的核对。相机或传输出错按不可用处理，绝不放行。没能保存的帧（超过单帧上限、预算用完、没有证据库）不算证据：记录里列在 `frame_unsaved` 下，这次评估判为 `unknown`，绝不会是 `ready`。本版本的传输只有 `QueueTransport`、`ScriptedTransport`（试运行）和文件协议 `FileTransport`（`questions/<request_id>.json`、`answers/*.json`，都整体写入）；显示题目的页面由后续任务做。

**数据归属。** 人工复位不写 reset rollout、不建 reset 任务目录、不写 reset 会话文件（运行的 `SessionFiles` 只有 forward 角色，实时服务不会多出一个永远 `standby` 的会话）。复位事实都在日志里：`WAIT_HUMAN` 的提交行、操作员的 resume（`authority.principal_kind: operator`、不透明的 `principal_id`、`command_id`）。据此，下一个前向片段在运行清单里得到 `preceded_by` 和 `after_human_resets`（`[{wait_seq, resume_seq, wait_ms, principal_id, reason}]`），在 rollout 元数据里得到 `eval.aeri.preceded_by`（`none`、`reset_policy` 或 `human_reset`，取最近的一次），供训练池区分。只有为复位而等（`WAIT_HUMAN`，原因为 `scene_*`）并由 resume 结束的，才算人工复位；停止、故障或预检失败之后的恢复只留在日志里，不计入。

**证据。** 每次场景评估，无论是否被采纳（超时、过期或被拒的消息也算），都保存为 `<run_dir>/evidence/<assessment_id>.json`（提供方没给编号时用请求编号）：C 的结论和原因、提供方的结论、失败和看不清的谓词、谓词结果、引用，以及人工核对时抓取的画面（`evidence/frames/<sha256>.<扩展名>`，按内容寻址）。文件都整体写入（临时文件、fsync、改名、目录 fsync）；写入中被杀留下的临时文件在下次打开时删除。单帧超过 4 MiB 不保存，画面最多用每次运行 256 MiB 预算的 90 %，记录到预算为止，记录里写明跳过了什么。`recorder.pending_card(run_dir)` 只读不写，给出等人时需要的内容：原因、resume 要带的序号、已等待时长和这是第几次等人、上一片段的结果及其 rollout 路径和末帧、初始状态契约（`id@version`、状态：草稿标为“未经用户确认，HA-23”）、谓词文字，以及导致等待的那次评估。其中 `operator_label` 区块请操作员给最近结束的前向片段打标签，写入成败标签之后才显示自动判定，卡片默认盲（见“操作员标签与双标签对比”）。

**作业文件。** 两种模式共用 `levi.aeri.job.v1`（`levi/domain/aeri.py` 的 `JobSpec`，快照 `docs/architecture/aeri/v1/job.schema.json`）。`human_assisted` 时可以省略 `policies.reset`、`task.reset_instruction`、`recording.reset_folder`、`reset.enabled`、`reset.max_attempts`、`reset.on_unknown`；写了也会在计划旁列为 `ignored`（`validate`），并且不进计划，`plan_sha256` 不随它们变化。`single_reset_policy` 需要 `policies.reset.max_steps`（`reset.enabled: false` 时除外）。计划还包含解析后的 `rollout_root`（realpath，相对作业文件）、契约文件字节的 sha256、`reset_mode` 和 `scene_check`。策略别名（`single_policy`、`scripted_safe`、`atomic_skills`）只在作业文件里有效；计划、日志和契约里只写代码名。以后改用复位策略时，只需改 `strategy` 并补上 `policies.reset`；前向目录、契约、运行目录布局和指标都不变。

```yaml
reset:
  strategy: human_assisted
  scene_check: operator_attested   # 或 provider（默认）
  human_scene_timeout_s: 600
```

**契约版本。** `levi.aeri.run_event.v1` 升到 minor 1：`RunHeader` 新增可选的 `reset_mode` 和 `scene_check`（只写代码名；minor 0 的行带这两个字段会被拒收）。其他契约仍是 minor 0（`aeri.MINORS`）。minor 0 写的日志照常可读；`aeri.header_modes(header, plan)` 优先取 header 的值，没有时从作业计划推断；调用方可以用 `journal.read_plan` 从运行目录取得这份计划。新运行会写这两个字段（见运行日志一节的“运行头和计划”）。指标和 `cli report` 目前还不读 `plan.json`：遇到 minor 0 的日志仍按原来的办法推断模式（后续任务）。

**已知局限。** 还没有回答场景题的页面（只有协议和 Fake）。契约格式没有文字字段之前，谓词文字就是把名字里的下划线换成空格（HA-23）。人工核对的质量取决于相机给出的画面；仲裁的视角和证据规则同样适用。指标还不区分计划内和计划外的干预（模式矩阵任务）。

## 子任务循环（`reset_strategy: subtask_loop`）

不用复位策略的运行：只有测评策略和本地 VLM。操作员只设任务名（`task.instruction`）和轮数（`experiment.episodes`）；VLM 规划 pick-and-place 子任务，策略把每个子任务当作一个普通片段执行，逆向循环把场景恢复成本轮开始时的布局。代码：`levi/automatic/subtask_loop.py`（note 代码、模板、校验器、fake）、编排器的 `_decide_subtask` 及其后的方法、`levi/automatic/subtask_vlm.py`（真实 VLM 客户端与睡眠/唤醒）。**已接线，但没有在真机、真实策略服务或真实 vLLM 上验证过**：所有测试都跑在 fake 上。

**一次运行：**

1. 开始时 VLM 在两路相机画面上列出物体（`inventory`，用 Lynx 的提示和结构：`pink_plate_1` 这样的 id、各自位置、能否抓取）。终端 D 显示清单；`y` 确认，`n` 否决（`WAIT_HUMAN`，`subtask_inventory_rejected`）。确认后的清单加 `table` 冻结为本次运行的白名单（note `subtask_inventory_confirmed`，带 sha256 和旁路文件）。
2. 每轮：先做只读的起始位姿检查，再由操作员在终端 D 按 Enter 放行本轮。只有它能放行：页面、命令收件箱、VLM、超时都不能。本轮开始的画面作为证据保存（ref 和 sha256），就是参考布局。
3. 正向规划：VLM 拿到任务名、白名单和当前画面，回答 `{"steps": [{"pick", "place", "instruction", "done_when": [{"fact", "subject", "target"}]}]}`。`pick` 必须是白名单里可抓取的 id，`place` 是另一个白名单 id 或 `table`，每条事实（`on`、`in`、`in_gripper`、`gripper_empty`）只能引用白名单 id。策略指令**由代码**用 `pick`、`place` 和三个固定模板之一生成（`INSTRUCTION_TEMPLATES`，一张常量表）：`on` -> `put the {pick} on the {place}`，`in`（容器）-> `put the {pick} into the {place}`，放到桌上 -> `put the {pick} on the table`。关系取自计划里关于这一对 `pick`、`place` 的 `on`/`in` 事实；没有写关系的子任务被拒（`no_relation`）。id 变成词（`pink_plate_2` -> `pink plate`）。两个同色物体读起来一样：一份计划里若同一条指令对应两个不同的动作，整份计划被拒（`ambiguous_instruction`；2026-10-17 的取舍，白名单名称不改）。VLM 自己写的 `instruction` 只留作分析，从不发给策略。超出白名单的 id、格式错误、空的**正向**计划、超过 `max_subtasks_per_loop` 的计划、超时或异常：保持不动、不发动作、`WAIT_HUMAN`（`subtask_plan_refused`）。每条请求记录都保留本客户端的请求 id、输入画面 ref、超时、HTTP 回答结构是否正确及原因。桥在策略接受指令前再查一次（`prompt_allowed`：只接受冻结白名单生成的模板文本）。
4. 正向循环：每个子任务是一个正向片段，至多 `subtask_max_steps` 步（默认 200），用它自己的指令；按步数上限或操作员结束，走常规的 hold -> quiesce -> seal。子任务之间不回 Home。之后 VLM 在静止画面上回答这个子任务的事实（`check`），结论由代码推出：全部为 true -> 下一个子任务；有 false -> 重发同一个子任务（至多 `subtask_max_retries` 次，默认 5，超过则 `WAIT_HUMAN`，`subtask_retry_limit`）；null、格式错误、超时 -> `WAIT_HUMAN`（`subtask_check_unavailable`）。VLM 的回答只推动流程：它是写在 `subtask_check` note 里的模型判断，从不当作操作员标签、真值或指标里的成功（每个子任务片段的结果都写 `goal_verification: unavailable`）。
5. 正向循环在 Home 结束（沿用现有安全回位）。然后做起始位姿检查、逆向规划（当前侧视画面加本轮开始的侧视画面，校验相同；任务名由代码生成：`undo: <任务名>`），逆向子任务作为 `reset` 片段执行（同一个策略、同样的逐子任务循环），最后再回 Home。空的**逆向**计划表示场景已经复原：不跑逆向子任务，机械臂留在 Home，下一轮仍要做起始位姿检查并重新放行。正向结束提交 `subtask_forward_done`，逆向结束提交 `subtask_reverse_done`。

**任务完成后及时确认（`subtask.early_check`，默认关闭）。** 不开时，每个正向和逆向子任务都要跑满步数（200 步，2026-10-18 实测约 26 秒）才问 VLM 是否已完成。打开后，子任务很可能已经完成时就停下来问同一个问题：

- **触发。** `on_events: true`：绑定文件 `signal_events` 事件流（终止仲裁器读的同一个带画面的事件流；subtask_loop 的绑定只能为这个用途声明它，`on_events: true` 时必须声明）里出现 `event_types` 中的事件（默认 `gripper_open`，即松开夹爪），过 `settle_steps`（默认 8）步后检查到期，让物体落稳、机械臂移开。被撤回的事件不触发。`every_steps: N`（N ≥ 40）：从本次尝试开始或上次检查起每 N 步也到期，作为没有事件时的兜底。两者可同时开，至少开一个。本次尝试的前 `min_steps`（20）步内不查；距上次检查或距步数上限不足 `cooldown_steps`（20）步不查（那时普通检查马上就来）；每次尝试最多 `max_checks`（3）次。
- **停止。** 检查点在一个控制步的开头，排在操作员停止、锁存/红色健康检查和操作员 s/f/d 之后（它们的优先级都不让给它），在下一次观测之前。片段以停止原因 `subtask_early_check` 走普通的 hold → quiesce → seal 结束；没有新增运动路径、令牌或守卫。随后唤醒 VLM（照常检查显存），在静止画面上问该子任务的 `done_when`，提示词和结构与步数用完时完全相同。外部模式下，代理像回答其他请求一样回答它。
- **回答。** true：子任务完成，立刻进入下一个子任务（或循环结束、回 Home）。false、null 或没有回答（VLM 唤醒失败、回答格式不对）：同一个子任务、同一次尝试，在新片段里用剩下的步数接着跑（不回 Home，不计重发，转换原因 `subtask_continue`）；步数用完时的普通检查照常决定，重发和转人工规则不变。检查期间收到停止仍然优先（回 Home，再等人）；锁存或红色健康在续跑片段的第一步之前就被拦下。
- **记录。** `subtask_early_check` note（触发原因：`trigger` 为 `event` 时带 `event_type`、`event_id`、`event_step`，为 `interval` 时带 `every_steps`；本次尝试的 `step`、`n`）、带 `early: true`、`trigger`、`step` 的 `subtask_check` note，以及续跑时的 `subtask_continued` note（`after`、`segment`、`used_steps`、`remaining_steps`）。续跑片段的 `subtask_started` note 和 rollout 元数据带 `segment` 与 `continues`（前一个片段）；指标把它算作同一个子任务。每一段都是单独封存的 rollout。终端 D 只显示是什么触发了暂停，从不显示 VLM 的回答。

为什么结束片段而不是在片段里暂停：片段内暂停时策略片段仍未关闭（状态机认为策略可能在推理），却要问 VLM；还需要更长的或第二个运动令牌；一个 rollout 里会留下时间空档。结束再续跑只用已有的转换。代价是续跑的尝试会变成几段较短的 rollout。尚未在真机上验证。

**s / f / d。** 正向子任务片段进行中：`s` 或 `f` 给**本轮**打标签并结束正向循环。判定属于整轮，不属于某个子任务：它是 `<run_dir>/labels/round_label.jsonl` 里的一条记录（`metrics.label_round`：`round_id` 与 `subtask_round_released` note 一致，`success`/`failure`/`discarded`、`by`、时间，以及成员片段：本轮实际跑过的正向子任务片段，之后另有一行 `members` 写逆向片段）。子任务片段都没有自己的成败标签（`label_operator` 拒绝任何 subtask_loop 片段，无论从哪条途径来），所以不会有逐片段的结局流入 `levi export`、训练池或数据整理，`raw.demo_outcome`（它们读取的机器人标志）也从不给子任务 rollout 结局。一个片段都没跑过的轮次不留记录。成功率按轮计算，在报告的 `rounds` 小节（`success_rate` 只算 success 和 failure，`discarded` 不进分母；没有自动的轮次判定，所以没有一致率和提前停止）。操作员也可以运行中一个键都不按，运行后逐轮标注：`levi automatic label --run-dir RUN --list-rounds` 列出每轮的 ID、子任务（含各片段是否跑过）和 VLM 的判定；`levi automatic label --run-dir RUN --round ROUND_ID --value success|failure|discarded --by PRINCIPAL [--note TEXT]` 在终端再输入一次该值确认后，写入同样格式的记录（`source: post_run`）。`--by` 必填并记录在案；同一轮之后的标签覆盖之前的，两条都留在文件里；未知的轮次、或正向片段一步都没跑过的轮次被拒绝，subtask_loop 运行里任何片段级的成败标签也被拒绝。这是操作员的动作：agent 从不代人执行；机械臂回 Home，逆向循环照常复位场景。`d` 作废本轮：回 Home、跑逆向循环，本轮由新的一轮替换（不超过 `max_discarded_rounds`）。逆向循环中 `s`/`f`/`d` 不起作用（终端提示"逆向循环正在复位场景：这个键在这里不起作用"）；停止始终有效。停止在每个阶段都优先：清单之前、放行时、规划中、VLM 唤醒或睡眠时、策略运行时、判定时、重发前、逆向循环中；停止一经登记，已规划的子任务不再发出。

**VLM 和策略从不同时工作。** 每个策略片段前让 VLM 睡眠；每次 VLM 请求前唤醒它。唤醒要求空闲显存不少于 `vlm_wake_min_free_gpu_mib`（`nvidia-smi --query-gpu` 只读总量；不设时按 VLM 配置用 `levi live` 计算唤醒的同一公式 `gpumgr.need_mib(wake=True)`：预算减去睡眠中 vLLM 仍占的 2200 MiB，再加 850 MiB 唤醒余量，0.70 在 32607 MiB 的卡上约 21.1 GB；没有配置或读数时才用 23000）。唤醒阈值 = 这个需求 + `vlm_wake_headroom_mib`（默认也是下限 1024）。不够或读不到：不唤醒，`vlm_wake_refused` 记 `free_mib`、`need_mib`、`headroom_mib`、`threshold_mib` 和 `decision`，`WAIT_HUMAN`（`vlm_unavailable`）。每次 `/sleep` 和 `/wake_up` 之前都核对 VLM 端口的监听者是本方记录的单元（pid 和 starttime，R15）；不是则什么都不发（`not_our_unit`），运行等待人工。唤醒失败或超时：`vlm_wake_failed`，`WAIT_HUMAN`，策略不动。一级睡眠把权重（约 18.5-20 GiB）移到不可换出的锁页内存，所以 MemAvailable 必须不少于 `vlm_min_free_mem_gib`（默认 28）；低于它由 `vlm_sleep_fallback` 决定：`wait_human`（默认：`vlm_sleep_refused`，`WAIT_HUMAN`）、`level2`（丢弃权重，下次唤醒时重新加载；更慢，唤醒时间未实测）或 `stay_awake`（VLM 保持运行，策略运行期间不发请求；仅当显存还有 `vlm_stay_awake_min_free_gpu_mib`）。`vlm_alternation: co_resident` 用于足够小、能与策略同驻的 VLM：从不睡眠，策略运行时仍然不发请求，并且只有在运行的第一次检查发现所有模型加载后空闲不少于 `vlm_co_resident_headroom_mib`（默认 2048）时才允许。这个模式在日志里记一次（`subtask_alternation`）。每条 `vlm_sleep`/`vlm_wake` note 都记前后的 MemAvailable、唤醒前后 nvidia-smi 的 used/total 和耗时。

**录制。** 正向子任务片段录在 `<forward_folder>__subtask`，逆向的录在 `<forward_folder>__reverse`（可用 `subtask.forward_folder` / `subtask.reverse_folder` 改）；每个 rollout 的 `metadata.json` 有 `eval.aeri.subtask`（round、loop、index、attempt、instruction、task、done_when），其 `task_description` 是子任务的指令，所以实时标注服务和以后的数据集能把子任务 rollout 与整任务 rollout 分开。

**这个模式下终端 D 的等待。** 每种子任务等待都打印自己的一行，原因取自对应 note（计划被拒及原因、判定看不清、重发达到上限、VLM 没有唤醒并给出空闲和阈值 MiB、唤醒失败、睡眠被拒并给出 MemAvailable），再打印一行说明停在哪里：第 N 轮的正向循环中或其后（"本轮的逆向复位没有执行：按 Enter 之前请手动恢复场景（拿不准就按 q）"）、逆向循环中（"场景只复位了一部分"），或本轮还没有任何东西动过。`vlm_unavailable`（唤醒、睡眠、内存）和 `subtask_retry_limit` 需要先按 Enter 再按 `y`（只按 Enter 不会续跑）；计划被拒、判定看不清、清单被否决、本轮未放行只需一次 Enter。近期 note 行会显示 `subtask_*` 和 `vlm_*` note，`subtask_check` 除外（VLM 的回答在本轮标注之前不显示）。

**续跑从不接着上一轮**（`subtask_round_abandoned`）：下一轮从起始位姿检查和新的放行开始。重启后读回冻结的白名单和已结束正向循环的轮数；运行像任何崩溃之后一样处于 `FAULT_LOCKED`。

**日志 note**（详情都是 JSON；过长时保留短字段，并写出 `<run_dir>/subtasks/` 下旁路文件的路径和 sha256）：

| Note | 内容 |
| --- | --- |
| `subtask_alternation` | `mode`（`sleep` / `co_resident`）、`ok`、co_resident 的 used/total/余量 MiB |
| `subtask_inventory` | `items`（id）、`frames`（ref、sha256）；旁路文件：请求记录 |
| `subtask_inventory_confirmed` | `items`（白名单）、`by: terminal`、`sha256`；旁路文件：物体 |
| `subtask_inventory_rejected` | `by`（`terminal` / `validator`）、`reason` |
| `subtask_round_released` | `round`、`by: terminal` |
| `subtask_plan` | `round`、`loop`、`task`、`n`、`plan_file`、`sha256`（计划文件：子任务及其生成的指令、模板、参考画面、请求记录） |
| `subtask_plan_refused` | `round`、`loop`、`reason`（`not_whitelisted`、`bad_json`、`empty_plan`、`timeout` 等） |
| `subtask_started` | `round`、`loop`、`index`、`attempt`、`instruction`、`episode_id` |
| `subtask_check` | `round`、`loop`、`index`、`attempt`、`episode_id`、`value`（true/false/null）、`frames`；旁路文件：请求记录 |
| `subtask_early_check` | `round`、`loop`、`index`、`attempt`、`segment`、`episode_id`、`steps`、`n`、`trigger`（`event`：`event_type`、`event_id`、`event_step`；`interval`：`every_steps`）、`step` |
| `subtask_continued` | `round`、`loop`、`index`、`attempt`、`segment`、`after`、`value`、`used_steps`、`remaining_steps` |
| `subtask_retry` | `round`、`loop`、`index`、`attempt` |
| `subtask_loop_done` | `round`、`loop`、`subtasks`、`retries`、`checks`、`outcome`（`done` / `success` / `failure` / `discard` / `already_restored`） |
| `subtask_round_abandoned` | `round`、`phase`、`why` |
| `vlm_sleep` / `vlm_wake` | `ok`、`level`、`mem_available_gib`、`mem_after_gib`、`ms`（唤醒另有 `wake_s`、`free_mib`、`gpu_before`、`gpu_after`、`was`） |
| `vlm_wake_refused` / `vlm_wake_failed` / `vlm_sleep_refused` | `reason` 和读数 |

请求记录（旁路文件）包含分析所需字段（Lynx AERI-VLM-ANALYSIS-PLAN-20261017.md）：kind、round、index、attempt、画面、提示 sha256、请求与响应时间、延迟、提示与生成 token 数、原始回答、解析结果、是否合法、白名单越界、结果及原因、请求前 VLM 的状态和唤醒时间。页面从这些 note 读取循环阶段（`GET .../runs/{id}` 快照里的 `subtask`，来自 `metrics.subtask_progress`）；指标报告有 `subtasks`（轮数、循环、子任务、重发、判定回答；从不算成功）。

**任务文件**（`subtask:` 小节；每个键都可省略）：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `max_steps` | 200 | 一个子任务片段的步数 |
| `max_retries` | 5 | 判定为 false 的子任务最多重发次数 |
| `max_subtasks_per_loop` | 无 | 一份计划的子任务上限 |
| `vlm_min_free_mem_gib` | 28 | 一级睡眠前要求的 MemAvailable |
| `vlm_sleep_fallback` | `wait_human` | `wait_human`、`level2` 或 `stay_awake` |
| `vlm_wake_min_free_gpu_mib` | 按 VLM 配置（兜底 23000） | 唤醒所需的空闲显存 |
| `vlm_wake_headroom_mib` | 1024（下限） | 加在上面之上，即唤醒阈值 |
| `vlm_stay_awake_min_free_gpu_mib` | 4096 | `stay_awake` 要求的空闲显存 |
| `vlm_alternation` | `sleep` | `sleep` 或 `co_resident` |
| `vlm_co_resident_headroom_mib` | 2048 | co_resident 启动时的余量 |
| `home_after_loop` | true | 必须为 true（每个循环在 Home 结束） |
| `forward_folder`、`reverse_folder` | `<forward_folder>__subtask`、`__reverse` | rollout 文件夹 |
| `early_check` | 不写（关闭） | `on_events`（false）、`event_types`（`[gripper_open]`）、`settle_steps`（8）、`every_steps`（无，≥ 40）、`min_steps`（20）、`cooldown_steps`（20）、`max_checks`（3）：见上文“任务完成后及时确认”；不写时计划摘要不变 |
| `vlm` | | `model`、`served_name`、`port`（8100）、`gpu_memory_utilization`（0.70）、`max_model_len` / `min_model_len`（16384）、`sleep_mode`（true）、`prewarm`（true）、`fallback_gpu_memory_utilization`（0.72）、`request_timeout_s`、`wake_timeout_s`（60） |

`task.instruction` 必填；这里 `termination.allow_early_stop` 默认为 false（策略运行时没有判定），写 `true` 会被拒绝，`event_pause_judge` 和 `home_after_loop: false` 也一样。复位策略的键被忽略；其他策略忽略 `subtask:` 键（它们的计划摘要不变）。计划里保存任务名、模板和 VLM 配置，从不保存某个子任务的文本。

任务文件和绑定文件示例见英文版本节。绑定文件（`levi.aeri.fr3.binding.v1`）用 `subtask_vlm` 代替 `providers`，只有 `forward` 一个策略（两个循环都用它）：`"subtask_vlm": {"base_url": "http://127.0.0.1:8100", "model": "<服务名>", "service": {...}}`（可选 `extra_body`、`gpu_index`，以及图片上限 `max_images` 1–8、默认 2 和 `history_frames` 0–4、默认 0，见下文相机帧总线）；端口必须是任务文件的 `subtask.vlm.port`，`service` 必填（见下）。带只含 forward 的 `policy_swap` 块时，运行自己启动和停止策略服务。操作员在终端 D 按常规顺序运行：`python scripts/aeri.py check --config JOB --binding BINDING`，然后 `python scripts/aeri.py execute --config JOB --binding BINDING --expect-plan <计划 sha256> --expect-binding <绑定 sha256>`。

**终端 D 管理 VLM 服务**：依据绑定文件必填的 `subtask_vlm.service` 块（`unit` 为 `levi-aeri-vlm-*` 名称，`argv` 为绝对路径程序加普通参数，可选 `env`，`gpu_lock` 为 LEVI 的 `.gpu.lock` 绝对路径，`need_policy_mib`，可选 `ready_timeout_s`）；配只含 forward 的 `policy_swap` 时也管理策略服务（`levi/automatic/subtask_units.py`）。argv 必须与任务文件的 `subtask.vlm` 一致：`model`（这里必填）作为参数、`--port`、`--max-model-len`、`--gpu-memory-utilization`，任务文件写了服务名时还有 `--served-model-name`；不一致在任何东西启动前拒绝运行。每一步打印 `n/7 正在做什么 / 你要做什么 / 怎么停止` 和通过或失败，写入 `<rollout_root>/.aeri/units/<run>-steps.jsonl`，运行日志建立后也写成 `subtask_service_step` note：

1. 预检：LEVI 的 GPU flock（一直持有到结束）、策略加 VLM 所需的空闲显存（两个数都打印）、MemAvailable、端口 8000 和 8100；上次运行留下的本方 VLM 单元只在记录的身份仍然一致时才停止；
2. 以本方单元启动 VLM（`systemd-run --user`），单元处于 active、端口由它自己 cgroup 里的进程监听、`/health` 回答时算就绪；身份（单元、pid、starttime）立即记录；
3. 让 VLM 睡眠（先查 MemAvailable）；
4. 启动策略服务（只含 forward 的 `policy_swap` 单元），和以前一样在任何机械臂 I/O 之前；
5. 物体清单与确认；6. 各轮；
7. 收尾：停止策略单元和 VLM 单元（VLM 只在身份一致时停止，否则保留并由操作员决定），然后做释放核对（本方 pid 已不在 nvidia-smi 里、显存回落），核对之后才释放锁；核对不过会报告。

物体清单放在策略启动之后，而不是最初建议的之前：它需要相机，而桥只在策略服务启动并核对之后才打开机器人环境；峰值相同（策略加载时 VLM 在睡眠）。端口被任何不是本方已记录单元的进程占用时，拒绝启动、什么都不停：本运行和产品 `levi live` 的 vLLM 在 :8100 上互斥，停止实时服务（`levi live stop`）是操作员的步骤。任何一步失败只停止本运行启动的单元并打印是哪些。z 下限：每个子任务指令都用绑定文件的 `safety.min_z`（AERI 桥不用客户端按任务的下限表，VLM 的任何回答都到不了限位），与整任务运行相同。

### Rollout 集（`levi/automatic/rollouts.py`）

策略只在每个子任务片段里运行，每个片段各存成一个 demo 目录（正向在 `<forward_folder>__subtask/demo_NNNN`，逆向在 `<forward_folder>__reverse/demo_NNNN`）。所以一轮包含一个正向集和一个逆向集，各有一个或多个子任务；重试的子任务有多次尝试，也就有多个 demo。运行目录里的 `rollouts.json`（`levi.aeri.rollouts.v1`）把它们组织成：轮 → 集（`forward`、`reverse`）→ 子任务（序号、指令、pick、place）→ 尝试（片段 ID、相对录制根目录的 `demo`、帧数、起止时间、秒数、停止原因、`check`）。它是派生文件，随时可以删掉重建；不复制、不移动任何 demo。

- 只从运行已经写下的内容生成：运行日志（不拿锁读取，从不写入）、日志里点名的 `subtasks/` 附属文件、`plan.json`、`labels/` 和每个 demo 的 `metadata.json`。一次尝试对应一条 `subtask_started` note；它的 demo 是 `eval.aeri.episode_id` 与该片段 ID 完全相同的目录（存成 Python repr 的值用 `ast.literal_eval` 读取，从不 `eval`）。没有 demo 的尝试、对不上任何尝试的 demo（`unmatched_demos`）都列进 `problems`，不猜。仍叫 `incomplete_*` 的 demo（录制未封存）记为 `demo: null`，并在 `incomplete` 里说明原因。
- `check` 是 VLM 对子任务的判断（`subtask_check`，`by: vlm`），不是操作员标签，也不是结局。人的标签只在存在时出现：轮的 `operator_label` 来自 `labels/round_label.jsonl`，尝试的来自 `labels/operator_label.jsonl`。
- 轮的 `outcome` 取 `forward_done`、`forward_failed`、`discarded`、`abandoned`（`subtask_round_abandoned`，例如恢复之后）、`locked`（这一轮里运行进入 `FAULT_LOCKED`）或 `incomplete`（日志在正向循环结束前就结束了）；每个集另有自己的 `outcome`。`motion_refused` 等 note 留在发生时所在的轮里。
- 运行收尾时（subtask_loop 运行，`fr3_run`）尽力写一次该文件：失败只留一条 `rollouts_write_failed` 日志 note，不改变任何运动、状态或退出码。
- `levi automatic rollouts --run-dir D [--rollout-root R] [--write] [--json]`（也可 `python scripts/aeri.py rollouts ...`）打印摘要（各轮、每个集的子任务 × 尝试、problems）。只读；`--write` 才写 `rollouts.json`（先写临时文件再改名），不写别的。`--rollout-root` 默认取运行的 `launch.json` 或计划。读取方用 `rollouts.load_or_build`：文件比日志新时读文件，否则在内存里现算。


### 运行窗口里的 rollout 集

子任务循环运行每个策略推理段录一个 demo，所以一轮会留下很多 demo：正向集（每个子任务一个或多个 demo）和逆向集（同样，重试也各录一个）。这种运行的运行窗口有一个 **Rollout 集** 区块，默认折叠，打开后按 轮 -> 正向集 / 逆向集 -> 子任务 -> 尝试 显示。每次尝试显示指令、帧数、片段墙钟时间（从下发到结果，含检查时间，不是策略推理时间）、停止原因、模型的检查结果（一律写明是模型判断，从不写成成功；正向尝试的检查结果、停止原因和由它得出的结果，要等操作员给这一轮打标签后才显示）、操作员标签（有才显示），以及用查看器的视频组件播放的两路相机视频。这个区块只读，没有任何控件。

数据来自下面的 `GET runs/{id}/rollouts`：`levi.automatic.rollouts.load_or_build` 返回的 rollout 清单 `levi.aeri.rollouts.v1`（`rollouts.json` 比日志新时读它，否则在内存里现算，不写文件）。这个接口从不提供视频文件：按尝试的 demo 在 `metadata.json` 里的片段 ID，把它对到 LEVI 已经在提供的数据集（LEVI 自己的原始采集，或浏览视图已就绪的关联实时数据集 `live.<group>__<task folder>`），视频地址走数据集文件服务（`/api/levi/files/<名称>/videos/…`），由它做自己的检查。没有这样的数据集时，尝试只保留 demo 路径，并显示“视频不可用”和原因。

## 复位模式矩阵（`levi/automatic/modes.py`）

“仅测评策略”模式（`reset.strategy: human_assisted`）与有复位策略的模式是同一条流水线，不是另一条；靠一张登记表长期保持这一点。`MODE_MATRIX` 给运行的每项能力在每种复位模式下各写一格：`same`、`differs:<一句话>` 或 `n/a:<原因>`（原因必填）。能力由 `modes.SOURCES` 里的函数从代码枚举：状态机的每个转换（`transition:A->B`）、复位仲裁（`arbitration:plan:<决定>`、`arbitration:after_reset:<结果>`）、`metrics.report` 的每个顶层键（`metrics:<键>`）、`levi automatic` 的每个子命令（`cli:<名>`）。尚未枚举、由创建它们的任务追加到 `SOURCES` 的有：录制器的会话角色与 manifest 字段、`/api/levi/automatic/*` 路由、作业文件的键。

`modes.RESET_ONLY_TRANSITIONS` 写明 `human_assisted` 永不经过的转换（进入、处于和离开 `RESET_*` 的），是手写的，不是推导的。一条用 Fake 的测试在两种模式下跑同一组场景（场景就绪、场景不就绪、复位次数用尽或失败、预检失败、红灯、操作员停止、在每个 prepared 行之后和恢复过程中崩溃），要求每种模式恰好到达矩阵给它的转换：`human_assisted` 到达的等于 `single_reset_policy` 到达的减去这些转换。

**登记守卫**（`tests/automatic/test_mode_matrix_guard.py`）用子进程收集 `tests/automatic`，读取 `@pytest.mark.mode_matrix("<能力>", ...)` 标记。以下情形守卫失败：代码里有而矩阵里没有的能力（或矩阵里有而代码已没有的）；某个 `same` 或 `differs` 格在该模式下没有被收集的测试；某格缺原因；标记写错（没写能力、能力不存在、没说模式、声称覆盖 `n/a` 格）。测试的模式取自 `reset_mode` 参数（`tests/conftest.py` 里的夹具让它在每种模式下各跑一次），只为一种模式写的测试在标记上写 `modes=(...)`。永远不运行的测试（skip，或按 pytest 的求值条件成立的 `skipif`/`xfail`）不算覆盖。每种失败都有反例测试。

**新增能力时怎样登记**（一个转换、一条原因路径、一个指标键、一个子命令；以后的 API 路由或作业键也一样）：

1. 在 `MODE_MATRIX` 加一行，每种模式写 `same`、`differs:<怎样不同>` 或 `n/a:<原因>`；新的一类能力还要在 `SOURCES` 加一个枚举函数；
2. 给覆盖它的测试加标记，每个不是 `n/a` 的模式至少一个；两种模式测法相同时用 `reset_mode` 夹具；
3. 重新生成快照（`python -m levi.automatic.modes > tests/automatic/snapshots/mode_matrix.json`）和下表（`uv run levi docs sync`，中英两份一起写），再跑 `tests/automatic`。

AERI 改动合并前的独立审查要核对 `MODE_MATRIX` 已更新、两种模式都有测试。

<!-- levi:generated aeri-reset-modes -->
| 能力 | `single_reset_policy` | `human_assisted` |
| --- | --- | --- |
| `arbitration:after_reset:camera_stale` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:after_reset:operator_stop` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:after_reset:policy_error` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:after_reset:reset_horizon_exhausted` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:after_reset:reset_verified` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:after_reset:scene_reset_required` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:after_reset:scene_unknown` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:after_reset:watchdog_timeout` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `arbitration:plan:ready` | 相同 | 相同 |
| `arbitration:plan:reset_required` | 不同：次数未用尽时运行复位策略，之后转人工 | 不同：转人工（scene_reset_required） |
| `arbitration:plan:unavailable` | 不同：同 unknown：绝不跳过复位 | 不同：转人工（scene_unknown） |
| `arbitration:plan:unknown` | 不同：运行复位策略；on_unknown 为 wait_human 时转人工 | 不同：转人工（scene_unknown） |
| `cli:attach` | 相同 | 相同 |
| `cli:doctor` | 相同 | 不同：没有能作答的场景提供方时，启动检查不通过 |
| `cli:label` | 相同 | 相同 |
| `cli:plan` | 相同 | 不同：真实模式还会因为没有能作答的场景提供方被拒绝；试运行的计划相同 |
| `cli:report` | 相同 | 相同 |
| `cli:resume` | 相同 | 相同 |
| `cli:rollouts` | 相同 | 相同 |
| `cli:run` | 相同 | 不同：试运行中场景不就绪时，运行停在 WAIT_HUMAN |
| `cli:runs` | 相同 | 相同 |
| `cli:scene-answer` | 不同：不会请人核对场景：运行器回复 not_supported | 不同：scene_check 为 operator_attested 时转给人工场景核对；机器提供方时回复 not_supported |
| `cli:status` | 相同 | 相同 |
| `cli:stop` | 相同 | 相同 |
| `cli:validate` | 相同 | 不同：没有能作答的场景提供方时拒绝真机运行；--dry-run 可通过校验 |
| `metrics:agreement` | 相同 | 相同 |
| `metrics:automation` | 不同：所有干预都是计划外 | 不同：场景核对把运行交给人，属于计划内干预 |
| `metrics:autonomous` | 相同 | 相同 |
| `metrics:comparable` | 相同 | 相同 |
| `metrics:discarded_beyond_limit` | 相同 | 相同 |
| `metrics:early_termination` | 相同 | 相同 |
| `metrics:human_minutes_per_valid_episode` | 相同 | 相同 |
| `metrics:mode_source` | 相同 | 相同 |
| `metrics:mode_specific` | 相同 | 相同 |
| `metrics:note` | 相同 | 相同 |
| `metrics:operator_verdicts` | 相同 | 相同 |
| `metrics:reset` | 相同 | 不同：没有复位片段：复位次数和耗时恒为 0；跳过决策只统计前向开始 |
| `metrics:reset_mode` | 相同 | 相同 |
| `metrics:rounds` | 相同 | 相同 |
| `metrics:scene_check` | 相同 | 相同 |
| `metrics:scene_decisions_by_human` | 相同 | 相同 |
| `metrics:schema` | 相同 | 相同 |
| `metrics:subtasks` | 相同 | 相同 |
| `metrics:time_per_valid_episode_ms` | 相同 | 相同 |
| `metrics:truth` | 相同 | 相同 |
| `metrics:truth_labels` | 相同 | 相同 |
| `metrics:turnaround` | 不同：只有复位策略放弃时才有 human_reset_ms | 不同：reset_policy_ms 恒为 0 |
| `transition:FAULT_LOCKED->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FAULT_LOCKED->PREFLIGHT` | 相同 | 相同 |
| `transition:FORWARD_ACTIVE->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FORWARD_ACTIVE->FORWARD_JUDGING` | 相同 | 相同 |
| `transition:FORWARD_ACTIVE->FORWARD_STOPPING` | 相同 | 相同 |
| `transition:FORWARD_FINALIZE->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FORWARD_FINALIZE->ROBOT_HOME` | 相同 | 相同 |
| `transition:FORWARD_JUDGING->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FORWARD_JUDGING->FORWARD_ACTIVE` | 相同 | 相同 |
| `transition:FORWARD_JUDGING->FORWARD_FINALIZE` | 相同 | 相同 |
| `transition:FORWARD_JUDGING->WAIT_HUMAN` | 相同 | 相同 |
| `transition:FORWARD_STOPPING->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FORWARD_STOPPING->FORWARD_FINALIZE` | 相同 | 相同 |
| `transition:PREFLIGHT->FAULT_LOCKED` | 相同 | 相同 |
| `transition:PREFLIGHT->PREFLIGHT` | 不适用：只有子任务循环策略（subtask_loop，不在本表比较之列）才有 | 不适用：只有子任务循环策略（subtask_loop，不在本表比较之列）才有 |
| `transition:PREFLIGHT->VERIFY_INITIAL` | 相同 | 相同 |
| `transition:PREFLIGHT->WAIT_HUMAN` | 相同 | 相同 |
| `transition:RESET_ACTIVE->FAULT_LOCKED` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_ACTIVE->RESET_JUDGING` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_ACTIVE->RESET_VERIFY` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_FINALIZE->FAULT_LOCKED` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_FINALIZE->SCENE_ASSESS` | 不适用：只有子任务循环策略（subtask_loop，不在本表比较之列）才有 | 不适用：只有子任务循环策略（subtask_loop，不在本表比较之列）才有 |
| `transition:RESET_FINALIZE->VERIFY_INITIAL` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_FINALIZE->WAIT_HUMAN` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_JUDGING->FAULT_LOCKED` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_JUDGING->RESET_ACTIVE` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_JUDGING->WAIT_HUMAN` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_VERIFY->FAULT_LOCKED` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_VERIFY->RESET_FINALIZE` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:ROBOT_HOME->FAULT_LOCKED` | 相同 | 相同 |
| `transition:ROBOT_HOME->SCENE_ASSESS` | 相同 | 相同 |
| `transition:ROBOT_HOME->WAIT_HUMAN` | 相同 | 相同 |
| `transition:SCENE_ASSESS->COMPLETED` | 相同 | 相同 |
| `transition:SCENE_ASSESS->FAULT_LOCKED` | 相同 | 相同 |
| `transition:SCENE_ASSESS->FORWARD_ACTIVE` | 相同 | 相同 |
| `transition:SCENE_ASSESS->RESET_ACTIVE` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:SCENE_ASSESS->WAIT_HUMAN` | 不同：仅在复位策略停用、次数用尽或 on_unknown 为 wait_human 时（计划外干预） | 不同：场景不就绪时的正常路径：由人复位（计划内干预） |
| `transition:VERIFY_INITIAL->COMPLETED` | 相同 | 相同 |
| `transition:VERIFY_INITIAL->FAULT_LOCKED` | 相同 | 相同 |
| `transition:VERIFY_INITIAL->FORWARD_ACTIVE` | 相同 | 相同 |
| `transition:VERIFY_INITIAL->RESET_ACTIVE` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:VERIFY_INITIAL->WAIT_HUMAN` | 不同：仅在复位策略停用、次数用尽或 on_unknown 为 wait_human 时（计划外干预） | 不同：场景不就绪时的正常路径：由人复位（计划内干预） |
| `transition:WAIT_HUMAN->COMPLETED` | 相同 | 相同 |
| `transition:WAIT_HUMAN->FAULT_LOCKED` | 相同 | 相同 |
| `transition:WAIT_HUMAN->PREFLIGHT` | 相同 | 相同 |
<!-- /levi:generated aeri-reset-modes -->

## 命令行（`levi/automatic/cli.py`）

```
levi automatic doctor   [--config F] [--json]
levi automatic validate --config F [--json]
levi automatic run      --config F --dry-run [--episodes N] [--scenes S] [--keep DIR] [--json]
levi automatic status   --run-dir D [--json]
levi automatic report   --run-dir D [--config F] [--truth T] [--format md|json]
levi automatic label    --run-dir D --episode ID --value success|failure|discarded|unclear [--principal P] [--note N] [--json]
levi automatic rollouts --run-dir D [--rollout-root R] [--write] [--json]
levi automatic plan     --config F [--mode M] [--episodes N] [--scenes S] [--keep DIR] [--json]
levi automatic run      --config F --mode M --detach|--foreground [--expect-plan SHA] [--wait S]
levi automatic runs     [--json]
levi automatic stop     --run R | --run-dir D [--principal P] [--command-id C] [--wait S]
levi automatic resume   --run R | --run-dir D [--expected-seq N] [--principal P] [--command-id C]
levi automatic scene-answer --run R --request-id Q --predicates a=true,b=null
levi automatic attach   --run R | --run-dir D --detach|--foreground
```

`levi automatic …` 是同一个命令。每个选项的帮助都是中英双语。`plan`、`run --mode`、`runs`、`stop`、`resume`、`scene-answer`、`attach` 属于已启动的运行，见下文[启动运行](#启动运行launchpyrunnerpycontrolpy)。

- `doctor` 只读：检查契约快照、Fake、作业文件、rollout 根目录是否可写；并说明本版本没有真机适配层（非必需项，退出码仍为 0）。
- `validate` 读取作业文件，打印计划及其 `plan_sha256`（即日志里的计划哈希）；被拒时退出码 2 并给出原因。不加 `--dry-run` 时按真机运行校验：没有 `task.initial_state_spec` 的作业被拒绝（没有场景能算 ready，前向片段永远开不了；错误信息给出修法），没有场景提供方的人工复位模式也被拒绝（目前真机运行还配不了提供方）。加 `--dry-run` 时同样的作业可以通过，但给出警告；`doctor` 的 `launch` 检查报告同样的结论。
- `run` **没有 `--dry-run` 或 `--mode dry_run` 一律拒绝**（退出码 2）：本版本不能真机运行（`--mode shadow|assisted|autonomous` 也退出码 2）。试运行只驱动进程内 Fake，在临时目录里运行（结束后删除；`--keep DIR` 保留在一个新的或空的目录里），绝不写作业里的 `rollout_root`，运行期间拒绝任何 socket 连接。没有任何真实对象拥有运动权限：唯一的机器人是 `FakeRobot`。`--scenes reset_required,ready` 设定 Fake 先给出的场景结论。
- `status` 不拿锁、不写入地读取运行日志（撕裂的末尾只报告、不截掉；损坏的日志显示 `FAULT_LOCKED`）。
- `report` 以 Markdown 或 JSON 打印指标（见上）。
- `label` 给已结束的前向片段追加操作员标签（见“操作员标签与双标签对比”）。先核对片段，再在终端里确认：不在终端里一律拒绝（退出码 2，不写任何内容）；操作员输入该值才算确认。提示写到 stderr，列出当前标签，从不显示自动判定和片段怎样结束；写入前再核对一次片段已结束。`--principal`（默认 `operator`）是不透明 ID。

**作业文件**（`levi.aeri.job.v1`，与契约一样是草案，HA-23）使用同一个严格 YAML 子集；拒绝未知的节和键，v1 只读取下列内容：

```yaml
schema_version: levi.aeri.job.v1
experiment:
  name: r20261010-a           # 运行 ID（不含点）
  episodes: 30
  random_seed: 42
  execution_mode: shadow      # shadow | assisted | autonomous（只校验）
policies:
  forward:
    max_steps: 120
  reset:
    max_steps: 80
task:
  instruction: stack the plates
  reset_instruction: "Reset: stack the plates"
  success_condition: "All plates of one colour are stacked."   # 可选
  initial_state_spec: initial-state.yaml   # 相对本文件
termination:                  # TerminationConfig 的字段
  min_steps: 10
reset:
  strategy: single_reset_policy   # 也接受 single_policy；或 human_assisted
  max_attempts: 1
  on_unknown: reset               # 或 wait_human
recording:
  rollout_root: /data/rollouts
  group: aeri
  forward_folder: stack_plates__r20261010-a
  reset_folder: reset_stack_plates__r20261010-a
```

**成功条件。** `task.success_condition` 可选：一段话，至多 500 个字符，不能换行。只有写了才进冻结的计划（`plan.success_condition`，因此会改变 `plan_sha256`）；没写的作业，计划和摘要与这个字段出现之前逐字节相同（有测试固定）。**它目前只做记录与显示**（计划卡、运行窗口）：不参与任何判定。`human_assisted` 也保留它。

## 启动运行（`launch.py`、`runner.py`、`control.py`）

T-CL-07..09，设计 X2 §3–§5。命令行和以后的 HTTP API 共用一个启动核心：都调用 `launch.plan()` 和 `launch.launch()`，所以同一份作业文件两边算出的 `plan_sha256` 相同（有测试对每个入口断言）。**本版本只有 `dry_run` 能启动**：进程内 Fake，不联网，没有任何真实运动权限。

**计划**（`levi automatic plan --config F [--mode M] --json`，可启动时退出码 0）写明执行模式、复位模式和场景核对方式、角色、适配器、运行目录、初始状态契约（`id@version`、状态、sha256）、是否会让机器人动、每项检查和拒绝原因。`plan_sha256` 是规范化计划的 sha256：作业加载器的计划（键排序、填默认值、rollout 根目录取绝对真实路径、契约文件的字节哈希、去掉复位模式忽略的键），再套上执行模式和允许的覆盖项（`episodes`；试运行 Fake 的 `scenes`）。谁发起、从哪里发起（命令行或 API）、用什么方式承载都不进摘要。不给 `--mode` 和覆盖项时，它就是 `validate` 的摘要。做计划不写任何文件，只在第一次时创建核心密钥。

**检查与拒绝。** `E_NO_ROBOT_ADAPTER`（`dry_run` 之外的任何模式；不论计划怎么说，`launch()` 都再查一次）、`E_JOB_INVALID`、`E_OVERRIDE`、`E_RUN_EXISTS`（运行 ID 就是实验名，只能用一次：运行目录或索引条目已存在）、`E_ROLLOUT_ROOT`、`E_KEEP_DIR`、`E_NO_CONTRACT`、`E_SCENE_PROVIDER_MISSING`（人工复位模式，同前）、`E_ROBOT_BUSY`（另一个运行持有 `$LEVI_AERI_HOME/robot-<适配器>.lock`）和 `E_ROBOT_BUSY_CLIENT`（rollout 根目录下有不属于 AERI 的活评测会话：未结束、未崩溃；只读会话文件，不连任何端口）。后两项只针对真机；试运行的 Fake 机器人是它自己的。草案状态的契约只给警告（`W_CONTRACT_DRAFT`）。API 只能选 `LEVI_AERI_JOB_ROOTS` 下的作业文件（`E_JOB_OUTSIDE_ROOTS`）。

**启动令牌**（防止看过计划后文件被改再启动）：`v1.<到期>.<hmac>`，用核心密钥（`$LEVI_AERI_HOME/core.key`，0600，只生成一次，从不打印）对计划摘要、作业文件身份（设备、inode、大小、修改和变更时间、sha256）和十分钟有效期做 HMAC-SHA256。`launch()` 会重新计划：作业文件被改过甚至只被 touch 过、契约被改过、摘要不同，都返回 `E_PLAN_CHANGED`；过期返回 `E_TOKEN_EXPIRED`。`run --mode M --expect-plan SHA` 在计划不再是你看过的那份时拒绝。

**承载方式。** `run --mode dry_run --detach` 把运行器放进独立的 systemd 用户单元：`systemd-run --user --unit=levi-aeri-<run_id> --collect -p KillMode=control-group -p TimeoutStopSec=120 <python> -m levi.automatic.runner --run-dir D --plan-sha256 X`，**不设 `Restart`**：运行器崩溃后由人 `attach`。它不在产品的 cgroup 里，也从不登记进产品的 `processes.json`，所以停止或重启 LEVI 不会停掉运行。`--foreground` 在本终端前台运行。独占进程的运行器（systemd 或 `--foreground`）在拒绝一切 socket 连接的状态下驱动试运行（`cli.no_network`，与 `run --dry-run` 相同）；`inprocess` 后端（运行器是别的程序里的一个线程）无法在进程级拦截，因此不拦截。已启动的试运行写在 `$LEVI_AERI_HOME/dry-runs/<run_id>/`（或 `--keep DIR`），绝不写作业的 rollout 根目录。

**运行器**重新读取作业文件并重算摘要，不一致就在写任何东西之前拒绝（退出码 2）。它把自己写进 `<run_dir>/runner.json` 和索引 `$LEVI_AERI_HOME/runs/<run_id>.json`（pid、进程身份、状态）；`levi automatic runs` 列出索引以及每个运行器是否还活着。它驱动运行直到需要人或完成，然后等待命令；运行完成时退出，或收到 SIGTERM 时退出。

**SIGTERM 与 Ctrl+C**（`systemctl --user stop levi-aeri-<run_id>`、`--foreground` 下按 Ctrl+C、注销：`Linger=no` 时用户管理器会停掉该用户的所有单元，所以注销就是给运行器发 SIGTERM）。运行进行中（片段、场景核对、复位）时，它是 `system:sigterm` 发出的受控停止：片段在边界结束（停止、收尾、回位），运行转入等人（`WAIT_HUMAN`，`operator_stop`），不再开始新片段，运行器随后退出。运行已经处于等待（`WAIT_HUMAN`、`FAULT_LOCKED`）时不登记停止，也**不**结束运行：运行器直接退出，保持运行原样（结果 `exited_without_stop`）。只有输入确认的 `stop` 才会结束运行。这样留下的运行用 `attach` 接管，接管后锁定在 `FAULT_LOCKED`（`recovery_ambiguous`），等人恢复。信号处理函数只置一个标志，停止由命令线程登记。

**命令通道。** `stop`、`resume`、`scene-answer` 写入 `<run_dir>/control/inbox/<command_id>.json`（先完整写入并 fsync，再链接到位：不会出现半条命令，也不会覆盖已有命令；目录 0700）。运行器每 50 ms 轮询一次，调用编排器线程安全的 `stop()`/`resume()`，结果写到 `control/results/<command_id>.json`。同一个命令 ID 只生效一次：同一命令再发（连点、用 `--command-id` 重试）得到 `already_queued` 和第一次的结果；编排器对已执行过的 resume 回复 `repeated`；用过的 ID 换成别的命令返回 `command_used`。resume 写明它对应的日志行（`--expected-seq`，默认下一行），过时返回 `stale_sequence`。运行器崩溃后命令文件还在，由下一个运行器判定。同一个运行器对每个命令文件只处理一次：结果文件写不进去（磁盘满、只读）时退避重试，超过上限就放弃（`control.jsonl` 记 `result_write_failed`，日志记一条 `command_result_lost` 备注），命令不会再执行一遍。运行器主循环结束时仍在排队的命令一律回复 `E_RUNNER_EXITING`，不执行（之后没有东西驱动运行）：等有运行器接管后换一个新 ID 重发。每条命令审计两次：运行日志里一条 `operator_command` 备注（谁、命令 ID、结果、时间），以及 `$LEVI_AERI_HOME/control.jsonl` 里的发出和结果两行。ID、文件名和文件在使用前都要校验（ID 格式、不跟随符号链接、最多 64 KiB、严格 JSON、键必须完全一致）。

**输入确认。** `stop` 要求输入 `stop <run_id>`；`resume` 要求确认两项（环境已处理、健康已复查）并输入 `resume <run_id>`；`scene-answer` 要求输入 `answer <request_id>`。没有终端（标准输入不是 TTY）时一律拒绝且不写任何东西；没有跳过确认的参数。它们还要求运行器活着（`E_NO_RUNNER`：先 attach）。场景答复只转给人工场景核对（`scene_check: operator_attested`）；机器提供方回复 `not_supported`。

**接管。** `levi automatic attach --run R --detach|--foreground` 为运行器已退出的运行启动新运行器：`Orchestrator.restore` 先执行日志恢复，所以运行处于 `FAULT_LOCKED`（`recovery_ambiguous`），不重放任何动作，在操作员 `resume` 让它重新经过 `PREFLIGHT` 和初始状态核对之前，没有任何运动授权。运行器还活着时拒绝（`E_RUNNER_ALIVE`），运行已完成时也拒绝（`E_RUN_COMPLETED`）。这样恢复的试运行，Fake 会从脚本开头重新开始。

**设置。** `LEVI_AERI_HOME`（默认 `~/.levi-aeri`，以 0700 创建）：核心密钥、机器人锁、运行索引、命令审计和已启动的试运行。`LEVI_AERI_JOB_ROOTS`：用 `:` 分隔的目录，API 只能从这些目录里选作业文件（命令行可以用任意路径）。没设置时就是 `<LEVI_AERI_HOME>/jobs` 这一个目录，启动向导写第一份作业时创建。

## HTTP 接口（`levi/automatic/api.py`）

产品页面通过 `/api/levi/automatic` 下的路由使用本流水线（前端代理，带人类令牌）。这些路由只**读**运行目录并**调用**上面各层：这里的任何东西都不会让机械臂动。本版本只有 `dry_run` 能启动，其他执行模式一律返回 `501 no_robot_adapter`。错误体是 `{"detail": {"code", "message", ...}}`；请求体严格校验（`"yes"` 不是 `true`，`"3"` 不是 `3`，多余字段被拒绝）。时间是毫秒级 epoch。各种 ID（运行、命令、请求、证据）先匹配 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` 才会碰路径；返回的文字里所有绝对路径都会打码。

| 路由 | 谁 | 作用 |
| --- | --- | --- |
| `GET capabilities` | 读 | 适配器、执行模式（只有 `dry_run` 可用）、复位模式、场景核对方式、默认值 |
| `GET policies` | 读 | 从 `LEVI_AERI_POLICY_ROOT` 发现可部署的检查点（只读目录列表，从不读取或计算权重哈希）：`id`、`role`、`config`、`sha256_status`（`verified`：磁盘上有点名权重文件的哈希清单，LEVI 不重新计算；`recorded`；`none`）、`notes`；只有明确声明为复位策略的检查点才让 `reset_available` 为真（否则页面只提供人工复位） |
| `GET jobs` | 读 | `LEVI_AERI_JOB_ROOTS` 下的作业文件：不含路径的不透明 `id`、`name`、`valid`、`reset_mode`、`errors` |
| `POST jobs` | 人 | 把向导表单写成 `<第一个作业根>/wizard/` 下的**新**作业文件（绝不覆盖：`409 job_exists`；同一表单再提交得到同一文件）。检查点必须来自 `GET policies`；`human_assisted` 不带复位策略；`422 job_invalid` 带逐字段的 `errors: [{field, message}]`。所选检查点、服务配置与发现目录写入各策略的 `deployment` 块，并纳入计划摘要。由人核对场景（`operator_attested`）需要初始状态契约，表单目前还不能指定 |
| `POST plan` | 读 | 启动计划（`launchable`、`refusals`、`checks`、`launch_token`、`token_expires_at`）和 `requirements`（见下）；模式缺省为 `dry_run` |
| `POST runs` | 人 | 启动看过的计划（`plan_sha256`、`launch_token`、`confirm: "launch"`、`request_id`）：`412 plan_changed`/`token_expired`、`409 run_exists`/`robot_busy`、`501 no_robot_adapter`、`503 systemd_unavailable`。同一 `request_id` 返回同一运行，重启后也一样 |
| `GET runs`、`GET runs/{id}` | 读 | 摘要；快照：状态、`seq`、片段、计数、等待卡、打开的场景问题、一次性 `challenge`（绑定 `seq`，60 秒，只在内存里）、运行器，以及 `requirements` 和 `progress`（见下） |
| `GET runs/{id}/events?after=&limit=` | 读 | 某序号之后的日志转移和备注 |
| `GET runs/{id}/metrics` | 读 | `metrics.report` |
| `GET runs/{id}/subtasks` | 读 | `subtask_loop` 运行的笔记（`subtask_*`、`vlm_*`），detail 已解析：detail 指向 `subtasks/…-detail.json` 附属文件时，若该文件在运行目录之下且 SHA-256 一致，就读出完整 detail（否则标 `sidecar_problem`）；解析不了的 detail 返回 `null` 并标 `unparsed`。每个画面为 `{ref, sha256, stored}`（`stored`：在 `evidence/frames/` 下，可由 `GET frames/{sha256}` 取到）。`subtask_plan` 笔记指向的子任务计划文件逐个按 SHA-256 核对（`plans[sha256].ok`，否则 `problem` 为 `bad_path`、`missing`、`sha256_mismatch`、`bad_shape`）。其他运行返回空。只读：页面只显示一轮，放行在终端里由操作员做 |
| `GET runs/{id}/rollouts` | 读 | 运行的 rollout 集（`levi.automatic.rollouts.load_or_build` 返回的 `levi.aeri.rollouts.v1`，不写文件）：各轮（`outcome`、`notes`、`abandoned`、`locked`、本轮 `operator_label`），每轮有 `sets.forward` / `sets.reverse`（这一侧没执行时为 `null`；`plan_file`、`task`、`outcome`、`subtasks`），每个子任务有 `attempts`（`episode_id`、`demo`、`instruction`、`frames`、`seconds`、`started_at`、`ended_at`、`stop_reason`、`check`、`sealed`、`incomplete`、`operator_label`、`media`）。只输出这些字段：`check` 只留 `value`（模型判断）；私有请求证据文件名换成 `<private>`；文本里的绝对路径被替换。这一轮打标签前，检查结果、停止原因和结局照常显示：维护者 2026-10-18 关掉了 rollout 集的盲规则（L2-AERI-SUBTASK-CHECK-BLIND），这个接口不再给 `rollout_view.project` 传 `blind`（传了 `blind` 的调用方仍会得到隐藏后的结果：`check: {hidden_until_labelled: true}`、盲集停止原因和正向结局）。`media` 为 `{available: true, dataset, episode_index, viewer, videos: [{camera, url}]}`（地址走文件服务），或 `{available: false, reason}`（`no_demo`、`no_episode_id`、`no_dataset`、`no_browsing_view`、`not_in_view`、`no_video`）。未知运行 `404`，没有生成器 `503 rollouts_unavailable`，生成失败 `409 rollouts_unreadable` |
| `POST runs/{id}/stop` | 人 | `{command_id, confirm: "stop"}`，走命令通道：`result` 为 `applied`、`repeated` 或 `refused`（运行器三秒内没回复时为 `queued`，状态码 202）；`409 not_running`；该运行上另一个操作未结束时 `423` |
| `POST runs/{id}/resume` | 人 | `{command_id, expected_seq, environment_handled, health_rechecked, challenge}`：`409 not_waiting`/`confirmations_missing`/`stale_sequence`（challenge 错误或过期也是）；已登记的独立真机运行一律 `403 terminal_resume_required`（见下文） |
| `POST runs/{id}/labels` | 人 | 操作员对已结束的前向片段的盲标：`{label, revealed, card}` |
| `POST runs/{id}/scene-answer` | 人 | 回答打开的场景问题（`adapters.human` 的文件协议；请求 ID、nonce 和画面必须对得上：`409 unsolicited`）；已登记的独立真机运行一律 `409 scene_answer_terminal_only`（见下文） |
| `POST runs/{id}/attach` | 人 | `{request_id, confirm: "attach"}`：为运行器已退出的运行启动新运行器（`409 runner_alive`、`409 run_completed`）；`arm`/`disarm` 返回 `501` |
| `GET runs/{id}/evidence/{eid}`、`GET runs/{id}/frames/{sha256}` | 读 | 场景评估记录、采集的画面（只在 `evidence/` 之下，不跟随符号链接） |
| `GET setup-guide` | 读 | 来自操作手册配方和只读探针（`LEVI_SETUP_RECIPES`、`LEVI_SETUP_DOC`）的有序 `SetupStep` 列表；不执行任何东西，标为 `execute` 的配方按 `copy` 显示，手册哈希与记录一致时才显示命令 |

**谁能写。** 写路由拒绝任何 Bearer 凭据（agent 永远不能启动、停止、恢复、打标签或答复），并要求界面令牌。每个写操作带 `request_id` 或 `command_id`：同一个再发返回第一次的回复，已用 ID 换成别的内容是 `409 request_id_used`。每个运行同一时刻只允许一个操作（`423 busy`）。写操作记入 `$LEVI_AERI_HOME/control.jsonl`（`api_*` 行）。

**盲标。** 操作员给一个片段打成功或失败标签之前，任何地方都不显示自动系统怎么判、片段怎么结束：卡片的 `automatic_verdict` 是 `null`（`hidden_until_labelled`），事件里的 `goal_verified`、`horizon_exhausted`、`operator_stop` 换成 `hidden_until_labelled`，只要还有已结束片段没打标签，指标就扣下 `autonomous`、`early_termination` 和 `agreement.by_ended_by`（`null`，列在 `withheld` 里）。`discarded` 和 `unclear` 不揭示任何东西。

**`requirements` 与 `progress`。** 计划应答和运行快照带 `requirements`，只从冻结的计划里读：`task`（`instruction`、`reset_instruction`、`success_condition`）、`episodes`、`max_steps`（`forward`、`reset`）、`allow_early_stop`、`reset_mode`、`policies`（`forward` / `reset`：`checkpoint_id`、`config`，不回显目录）和 `inference`（`mode`：`legacy_async` 或 `event_pause_judge`；`physical_hold`）。旧计划缺的键为 `null`；没有计划的运行 `requirements` 为 `null`。运行快照还带 `progress`：正在跑的前向片段的步数，由它的 `aeri_steps.csv` 里已提交的行数得出。这个文件是分块写入的，所以 `step` 是**下限**（`lower_bound: true`）；读不到时 `step` 为 `null`，`reason` 写明原因（`no_open_episode`、`runner_not_alive`、`unreadable`），不做估算。这两个字段对客户端都是可选的：旧服务不发，页面就不显示。AERI 运行没有单独的事件智能设置，事件模式就是 `termination.inference_mode`。

**设置。** `LEVI_AERI_POLICY_ROOT`：策略检查点目录，`GET policies` 列出它，`POST jobs` 只接受其中的检查点（未设置：没有检查点，由人复位场景）。测试：`tests/automatic/test_api_*.py`。

## Fake（`integrations/fr3_automatic/fake.py`）

`FakeClock`（只在推进时走）、`FakeRobot`（只凭围栏的令牌运动；可脚本注入：连续三次 503 后闩锁、连续六次状态过期后闩锁、位姿冻结、红灯、闩锁、丢失应答、命令发出后进程崩溃、Home 超出容差、相机停滞）、`FakePolicy`（在 Fake 时钟上固定延迟；超时、服务端错误、NaN、维度或代次错误、过早的 `valid_from`、acquire 或 quiesce 失败、服务端崩溃）和 `FakeRecorder`（带写线程；写失败在下一次 commit 或 seal 时报出，所有写入成功后才写 `.complete`，`abort` 得到 `incomplete_*`）。该模块不 import 任何网络、进程或 LEVI 代码。

**Fake 与真机的差别**（`fake.FIDELITY`）：没有动力学和停止距离；Home 除非脚本指定否则总在容差内（真实的 `_go_home` 不核对到位）；急停对 Fake 可见，而真机 FR3 的软件状态过期联锁看不到急停；闩锁计数只部分模仿 `Fr3Guard`；健康状态与命令同一步读取（真实 C3 文件 2 Hz 写入）；没有 GPU 争用和冷启动；`FakeRecorder` 不写文件（`RolloutRecorder` 写，但它默认的媒体写入器不写视频）；相机帧只是计数；场景和目标 Fake 按脚本作答（不能说明真实模型的准确率）；会话文件只在状态变化时重写，长时间等待期间没有 2 秒心跳；没有网络。**这些测试通过只证明状态机逻辑，绝不代表真机行为。**

**测试**（`tests/automatic/test_aeri_*.py`）：状态表与围栏；各个 Fake；C5 映射表与 C2 门控如实性；裁决器；编排器在设计的故障清单上的行为（错误成功、Unknown、判定超时或离线、事件抖动、复位阶段迟到的前向动作块、策略服务崩溃、相机停滞、没有策略资源、复位到达上限、Home 失败、急停或 FR3 故障、录制器写盘和封存失败、每个事务每个阶段的重启以及 SIGKILL、两次 Resume），以及 150 次带崩溃和恢复的固定种子随机运行。

**端到端**（`test_aeri_e2e.py`，用 `aeri_world.py` 的完整 Fake 环境：编排器、录制层、manifest、会话文件、初始状态契约）：N 轮“前向 -> Home -> 场景 -> 复位 -> 下一轮”，每个 rollout 都用 `levi.live.criteria` 读回；固定种子的运行两次写出相同字节（日志、rollout、manifest、会话文件，去掉墙钟时间和进程身份），换种子则不同；两轮运行里每个事务的每个阶段都让编排器崩溃一次（崩溃点逐一计数），再由新进程接管：结果总是 `FAULT_LOCKED`（或 `COMPLETED`），不重放任何动作，每次 Home 只发一次，磁盘与日志一致，两个会话文件都是 `fault`；每第九个崩溃点由操作员恢复并跑完，不复用任何目录；另有子进程在封存事务的每个阶段以及 `.complete` 前后被 SIGKILL。故障清单（Fake 能测的 12 行：错误成功、Unknown、判定超时、事件抖动、复位中迟到的前向动作块、策略服务崩溃、相机停滞、没有策略资源、复位到达上限、磁盘写满、重启、两次 Resume）对每一行断言最终状态、记录的降级原因，以及没有任何动作在无令牌时执行或尝试。

**后台实时标注兼容**（`test_aeri_live_compat.py`）：Fake AERI 运行写出的 rollout 和会话文件被真实的后台实时标注服务接收（它的控制器和 worker 以 `once` 模式运行，对接假模型服务）：前向片段被标注；用 `watch.exclude` 排除的复位目录不被标注（不排除时它会成为一个单独的数据集，所以应当排除）。`levi/live` 及其测试都没有改动。

**尚未实现：** `atomic_skill_sequence` 和 `scripted_safe_reset`。显式 FR3 软件桥与页面已有实现，现场验收尚未完成。


## 测评 policy 发现（`levi/automatic/policies.py`）

`discover(policy_root)` 列出策略根目录（本机是 `openpi/checkpoints`）下的检查点目录，供启动页回答“部署哪个策略”“有没有复位策略、用哪个”。它只读：只列目录、读几个小的元数据文件，不 import openpi、不加载权重、不重算哈希、不打开端口、不跟随符号链接。`discover_report` 还返回被跳过的条目、条目数是否被上限截断、根目录一级的问题；`select(entries, role)` 取某个角色的可部署条目。

| 字段 | 来源 |
| --- | --- |
| `kind`、`deployable`、`is_jax` | 可部署的 JAX 目录要有 `params/`、`assets/` 目录（openpi 加载器读它）和 `norm_stats.json`（在顶层，或在 `assets/` 下，直接放或再深一层）；缺哪项就在 `problems` 里写明（`assets:missing`、`norm_stats.json:<原因>`）。有 `actor/` 是 PyTorch 源目录（列出但不可部署）；其余为 `unknown`，原因 `layout_not_recognised`。 |
| `config`、`config_source` | 先取 `VERSION.json` 的 `config`，再取 `README_DEPLOY.md` 里的 `--policy.config`，再取配方（`DEFAULT_RECIPE` 或 `recipe=` 参数），都没有则为 `none`。README 里训练机的配置名单独放在 `training_config_name`，从不用于部署。 |
| `role`、`role_source` | 只有明确声明才得到 `reset`：`VERSION.json` 的 `policy_role` 或 `role` 去空白、忽略大小写后等于 `reset`，或配方。`forward` 来自同样的明确声明，或按约定（`convention:config`）配置属于已知前向系列。目录名、README 标题、配置名、`VERSION.json` 的任何键或短字符串含 reset 类词（`reset`、`recovery`、`recover`、`return`、`home`）、`policy_role` 无法识别、声明互相矛盾，一律 `unknown`（原因在 `problems` 或 `warnings`）。`role: best` 这类是版本变体，不算角色声明。`select(entries, "forward")` 不返回 `unknown`，除非传 `include_unknown=True`。 |
| `variant`、`version`、`model`、`step`、`version_note`、`siblings`、`verified`、`not_verified` | `VERSION.json` 原文（`parallel_to` 对应 `version_note`）。 |
| `sha256_state`、`params_hashed`、`sha256_records` | 磁盘上已有的哈希：目录内或根目录的 `*.sha256` 清单（按相对路径的第一段归属，绝对路径、含 `..`、隐藏开头的路径不参与）和 `CONVERSION.json` 里的哈希。`recorded`：有本目录文件的清单；`indirect`：只有 `CONVERSION.json` 的哈希，它们属于 PyTorch 源文件和 `norm_stats_from` 指向的目录（见各记录的 `subject`），不是 `params/` 的；`missing`：没有。`params_hashed` 只在清单里有权重文件时为真。**页面不得把 `indirect`（或 `params_hashed` 为假的 `recorded`）显示为“已校验”**；`VERSION.json` 里“已核对”的说法不算哈希。`covers` 写明记录覆盖什么。 |
| `converted`、`size_bytes` | `CONVERSION.json` 存在且可读；按 `lstat` 累加的体积，最多数 `MAX_FILES_PER_ENTRY` 个目录项（文件、目录、链接都计，超过记 `size_truncated`）。 |

`VERSION.json` 或 `CONVERSION.json` 有问题（不是 JSON、太大、不是对象、符号链接、管道）时记入 `problems`，目录照常列出。大小写和 Unicode 折叠后同名、或 `(model, version, variant, step)` 相同的目录，两边都会被标记。根目录里的隐藏项、普通文件和符号链接列在 `skipped` 中，不读取。根参数为 `None`、空串或含 NUL 时返回空结果和 `root_invalid`。`VERSION.json` 与 README 的配置名不一致时给出 `config_sources_disagree` 警告。没有任何条目的角色是 `reset` 时，`select(entries, "reset")` 为空，调用方只给“人工复位”。按约定归类的角色和配方里的配置名是对本机的推断，不是检查点自己的声明，页面应显示 `role_source`。测试见 `tests/automatic/test_policies.py`。

### 引导式执行设置与布局卡回答

引导式评测计划沿用现有双标签客户端，支持 `human_assisted` 人工复位。`single_reset_policy` 引导式计划返回 `guided_reset_policy_unsupported`，不能启动；试运行仍验证两种复位策略。HTTP 运行器仍拒绝真实模式；下文显式 FR3 入口另设逐次人工确认。

复制的命令现在明确传递冻结步数 `--max-timesteps`、提前按键设置 `--dual-early-key` 或 `--no-dual-early-key`，以及收集数据的根目录 `--rollout-root`。已有参数只替换一次，重复参数会被拒绝。这些值保存在命令配置中，原始手册指令仍完整保留，用于检测手册变化。步数纳入共享设置摘要。旧客户端仍可能允许操作员在开始闸门改变步数；这样的片段偏离冻结计划，不能按原计划实验解读。

向导作业的策略会记录 `deployment.checkpoint_id`、`deployment.config` 和发现时的 `deployment.checkpoint_dir`，身份纳入归一化计划摘要。评测计划的子作业写入各组自己的部署身份。没有部署字段的旧作业仍可读取；记录身份并不证明策略服务实际上已经加载它。

`GET /api/levi/automatic/campaigns/<id>/cards` 为每个待确认片段返回 `candidate_cards`，含其所属段的全部卡位；保存时也按该段校验。`POST .../cards` 接受 `decision: confirm | no_card | withdraw`：确认必须带卡号，无卡和撤回必须带 null。明确无卡会跨重启保留、离开待确认，并且不进入成对分析；撤回会重新待确认。兼容旧客户端与旧日志：没有显式 decision 的 null 仍表示撤回。

上述保证用合成文件与假件验证，不表示真机人工验收完成。

向导 campaign 快照包含 `live_session`，也可通过
`GET /api/levi/automatic/campaigns/{id}/live-session?segment=N` 获取。状态为
`waiting`、`linked`、`ended`、`ambiguous`、`mismatch` 或 `unavailable`；唯一匹配时
包含现有公开 `session` 对象。关联先要求已完成片段精确证明 campaign、段号和组代码，
再核对数据根、组、任务目录、运行及会话身份、完整策略路径、配置、任务指令、双标签模式
和本段开始时间。不采用最新会话猜测；多个当前匹配明确报告歧义。公开对象不含内部完整
检查点路径，`dataset` 暂为 null，由实时服务解析镜像。该关联已用合成 I/O 测试覆盖，
尚未进行真机验证。


## 显式 FR3 真机执行桥


实际入口是 `python -B -m levi.automatic.fr3_run`，用 AERI 状态机执行
`human_assisted` 与 `single_reset_policy`。HTTP runner 仍拒绝真实模式；现有
向导复制的人工客户端指令属于另一条人工复位流程。两种桥接模式均未完成物理验收。

在线作业读取 `GET /api/levi/automatic/jobs/{job_id}/fr3-bridge`，实验读取
`GET /api/levi/automatic/campaigns/{id}/fr3-bridge?segment=N`。接口只读已保存的
子作业，返回 `entrypoint`、`check_command`、`execute_command_template`、
`plan_sha256`、`hardware_verified: false`、`manual_review_required: true`、
`requires_new_plan` 和代码列表 `blockers`，不会执行命令。模板中的 binding 路径
和摘要必须由操作者填入。未知作业或分段返回 404，保存的作业无效返回 422 `job_invalid`。

若提示需要新计划，应在已配置的作业根目录按上文格式建新作业，包含经人确认的
`task.initial_state_spec`、绝对 `recording.rollout_root`、发现到的各策略
`deployment.config` 与完整 `deployment.checkpoint_dir`，并使用新 `run_id`。
不能修改已冻结的作业，也不能替人把草稿合约改成 confirmed。桥拒绝 shadow 模式
和已有运行目录，不自动重放之前的运动。

操作者在冻结作业之外准备 JSON binding 文件。下表为必填项；顶层
`policy_warmup_timeout_s` 可选：

| 字段 | 实际来源 |
| --- | --- |
| `schema` | `levi.aeri.fr3.binding.v1` |
| `openpi_root` | 既有 openpi 检出的绝对路径；延迟导入其 FR3 客户端、健康守卫和环境 |
| `robot_url` | 已人工审阅的回环运动端点 `http://127.0.0.1:5000/` 或 `:5001/`，终端确认之后才连接 |
| `health_file` | 正在运行的 C3 监视文件绝对路径，创建环境前必须读到新鲜绿色健康结果 |
| `cameras` | 恰好 `side` 和 `wrist`，填写不同的真实数字序列号字符串 |
| `safety` | 必填 `min_z`、`max_xy`、`max_step_m`、`max_step_rad`、`home_pose`（六项米/弧度）、`home_duration_s`、`home_tolerance_mm`、`rx_range`、`ry_range`、`rz_range`（各为两项递增且跨度不超过 2π 的区间）、`binarize_gripper`（显式布尔值）；取现场审阅的范围，不猜值。归位时长加等待收敛预算必须小于冻结的归位超时。可选正有限值 `home_settle_timeout_s`（默认 5 秒）、`settle_poll_s`（默认 0.1 秒，最大 1 秒）、`gripper_confirm_timeout_s`（默认 2 秒）仅限制等待，不放宽阈值。相机帧总线的可选键（见下）：`max_frame_age_ms`（正数，最大 10000；复制进 openpi 每台相机的 `camera_kwargs`）、`camera_stale_steps`（整数 2–100，默认 3） |
| `policies` | 恰好 `forward`，策略复位另加 `reset`；每项为 `{"host":"127.0.0.1","port": PORT}`，指向已单独审阅并运行的策略服务（声明下文可选的 `policy_swap` 时，由本次运行自己启动） |
| `providers` | `{"online_url":"http://127.0.0.1:7882","file_root":"/absolute/provider-directory"}` |

**相机帧总线**（`levi/automatic/camera_meta.py`）。openpi 的 `RobotEnv.get_observation()` 返回 `image_meta`（每台相机的 `seq`、`t_capture_ns`、`t_arrival_ns`、`age_ms`）时，AERI 能知道每一步、每段录制、每次 VLM 请求用的是哪一帧。没有它时一切照旧。有它时：

- 桥接按 `side`/`wrist` 视角原样透传 `image_meta`；
- 同一台相机的 `seq` 连续 `camera_stale_steps` 个控制步（默认 3）不变：只记录，不停。每段连续重复记一条备注 `camera_seq_repeat`（写明哪台相机、`seq` 和帧龄），片段照常继续（CPU/GPU 负载下丢帧是正常的；维护者 2026-10-18 决定）；
- 只有设置了 `max_frame_age_ms`（AERI 作业都没设）时 openpi 才会抛出 `StaleFrameError`：在对这次观测采取任何动作之前结束本片段。片段照常封存，运行在**机械臂原地**（不回 Home）等人处理，原因 `camera_stale`（备注 `camera_stale` 写明哪台相机、它的 `seq` 和帧龄）。终端续跑前要按 `y` 确认；
- 设置了 `max_frame_age_ms` 而观测没有 `image_meta` 时拒绝该观测（旧版 openpi 会悄悄忽略这个上限）；
- `frames.csv` 和 `aeri_steps.csv` 在所有旧列之后增加 `side_seq`、`side_t_capture_ns`、`side_age_ms`、`wrist_seq`、`wrist_t_capture_ns`、`wrist_age_ms` 列（没有 `image_meta` 时为空）；
- VLM（`subtask_vlm`）和文件判定方按 `(view, seq)` 每帧只编码一次 JPEG，并在请求证据里给每帧记 `seq`/`t_capture_ns`（请求记录里的 `image_meta`）。每次请求最多发 `max_images` 张图；`history_frames` 大于 0 时，在同一上限内为每个已发视角补发至多这么多张来自 openpi `frame_history` 的更早帧（本地与外部模式走同一段代码）。

`t_capture_ns` 属于主机墙钟时间域，只与其他 `t_capture_ns` 比较；`t_arrival_ns` 和 `age_ms` 是单调时钟。openpi 现在每帧共享一个只读数组（`flags.writeable` 为 false）：LEVI 的 AERI 路径只读取或编码它。

在既有可运行 openpi 的 Python 环境（同时可导入 LEVI、OpenCV、NumPy、SciPy）
执行页面给出的 `--check`。它只读文件、检查 binding，不导入 SDK、不连接设备、
策略或 C5，也不启动服务，不能据此认定现场可用。保留输出的计划与 binding 摘要。
操作者审阅正在运行的机器人、健康监视器、相机、策略身份、限制和提供方后，把
这两个摘要填入 `--execute` 模板。每次启动都要求真实终端输入
`execute <run_id>`；确认后再读一次配置。配置项或旧日志不能替代这次人工确认。
桥不启动机器人、策略、健康或判定服务；唯一例外是可选 `policy_swap` 声明的策略服务（见下文“策略切换”）。

创建运行日志或录制器前，先读取并核对绑定的归位位姿与实测张开夹爪，再热身同一个
已验证的正向策略客户端，热身后再核对初态，防止等待期间位姿或夹爪变化。
**首轮回位候选（仅软件，默认关闭）。** 作业 `subtask.start_home: true` 仅支持
`reset.strategy: subtask_loop`，其他策略指定该设置会被拒绝。省略、null 或 false
保留旧 plan 哈希与普通只读启动拒绝；启用值进入冻结 plan，FR3 入口在取得机械臂锁前
拒绝配置与 plan 不一致，不能用单独 binding 开关绕过。Job schema minor 2 增加此字段。

可读但偏离 Home 的位姿可进入 PREFLIGHT，须依次新鲜确认：安全处理夹持物后按 `o`，
检查完整返回路径后按 `p`，确认回位会开爪并授权本次回位按 `h`。每步清除旧键，Enter
及续跑的 `y` 不算回位确认。终端、信号处理、停止探针与已成功且存活的心跳就绪前不允许
回位，等待超时不是就绪。命令先核对 command_id 与日志序号，再记 PREFLIGHT 自环的
非幂等 prepared，之后才可签 startup token 并调用唯一的 `home_under_closed_gate`；
没有伪造片段、结果或 rollout，退出必撤销 token。首轮准备生命周期内，停止登记永久置位该阶段停止探针
并撤销 token，回位途中也有效。成功独立放行或初态已在 Home 而无需回位后结束该生命周期，
不通过清除标记复活取消的首轮回位。后续普通轮的停止、续跑与恢复保持原语义；恢复时已有
片段历史即证明首轮准备结束。健康与位姿在停止锁外读取，随后仍核对原停止代次。

只能原子认领 OPEN→CLOSED 的门。成功后仍闭门，独立 `release_round` 才能凭原闭门
代次/时间与未变化的 stop generation 一次性重开，并重查位姿、停止、锁存和健康。
闭门变化、stop、resume 或重启不能刷新许可。计数证明零发送的拒绝进入 WAIT_HUMAN，
本候选不允许同运行重试或续跑，须结束运行、人工处理后新建运行重新确认。
部分/未知发送及 prepared/execute/ack 后崩溃均锁定，恢复不重放首轮回位。
不清除锁存，不在红灯下回位，不添加低位恢复策略；实际启用、部署与真机验收仍另审。

`Robot.start_pose()` 从同一次观测返回只读诊断：`target_pose`、`measured_pose`、
`error_mm`、`rotation_rad`、`gripper`、三个阈值 `tolerance_mm`、`rotation_tol_rad`、
`gripper_min`、具体未满足项 `unmet` 及 `unreadable_reason`。不可用或非有限诊断值
记为 null；原有阈值与 `ok` 判定不变。启动拒绝发生在 journal/session 创建之前，
不占用 run ID。提示明确未发送动作，回位会打开夹爪，须先安全处理夹持物并检查
返回路径；不增加自动归位或清除锁存。归位等待在返回成功前检查停止、锁存、健康及
当前未过期的 home token，即使同次观测已满足位姿与夹爪阈值也不能跳过。
顶层可选 `policy_warmup_timeout_s` 是正有限秒数（默认 30）；
拒绝布尔、零和非有限值。热身动作全部丢弃，不发送运动。失败或超时关闭环境，
不占用 run id。超时查询保持占用原查询槽，直到实际查询结束；超时不代表后端取消，
也不允许重发或切换到另一角色推理。该桥核对冻结的部署身份，不拥有或启动既有策略服务。

声明 `policy_swap` 时，正向服务由本次运行自己启动，其启动热身就是切换器的热身：
服务在任何会话出现前只被查询一次，切换器另核对动作、运动步数和运动围栏。
这里的拒绝与上文一样是启动拒绝（停掉该单元，run id 不占用）。
`policy_warmup_timeout_s` 在此不适用，两者同时给出时绑定被拒绝；请改用 `policy_swap.warmup_timeout_s`。

开始与归位检查保持绑定的位置容差、0.05 rad 旋转容差及归一化实测夹爪 >= 0.9，
拒绝非有限或超出归一化范围的测量。观测与解析后的目标位姿必须为既有的六项或七项表示，
聚合误差前逐项检查有限值；JSON 绑定的归位位姿仍为六项。最后一条归位指令后只读轮询，等待预算用完、
停止请求或健康转红即停止等待。夹爪排队文字本身不证明送达：仅识别已审阅的精确文字，
在后续运动前用稳定实测位置确认；最后一次应答也在保持、策略静默确认后，
终局判定与封存前完成同样的检查。终检不发运动，未证明送达即锁定收尾，
应答原文与实测值进入日志和终端。保留在队列的目标若原先就在目标侧，须实测到移动后
才能确认。故障摘要显示原因、最近备注细节和日志路径。确认预算默认仍为 2 秒；稳定目标采样的读取完成时间早于或恰好等于截止时间可以确认，超过截止时间则拒绝。停止、锁存、健康转红与非法读数优先于确认，截止时也如此；截止时尚未确认则失败，不再轮询。
动作守卫拒绝时，在 `action_guard_detail_NNNN` 日志备注中保存原动作、已审阅规范化动作、裁剪动作、发生变化的分量索引（从零开始）及客户端 clamp notes；按编号拼接即为 JSON 诊断。最多八段、每段 240 字符，加一条拒绝备注，通过有限白名单保留，不受普通片段备注预算耗尽影响。序列化前限制诊断输入规模；超限或非 JSON 输入改为明确的 JSON 省略摘要，不声称保留完整诊断。`action_guard_refused` 保存拒绝原因。诊断序列化失败时记录不可用标记，仍拒绝并锁存，绝不发送裁剪动作。
日志备注保持既有 300 字符上限。正向策略第一次执行推理抛出真实 `policy_error`、
且尚未执行任何动作时，先保持并确认策略静默，再转 `FAULT_LOCKED`，
不自动归位或重试。同时发生的操作员停止仍沿原人工处理路径；正常判定失败、
视觉 unknown 和已经执行动作后的异常保持原处理方式。

正向目标判定复用既有 C5 在线接口与客户端，提交实际已录制帧及静默后的新鲜末态
观测，不把操作者结局标签交给模型。所有角色的服务元数据都必须准确匹配冻结的
服务 config 和解析后的完整检查点目录，之后才创建机器人环境。

`scene_check: provider` 必须接入真实外部提供方，文件协议如下：

1. 提供方持续更新 `file_root/READY.json`：schema 为
   `levi.aeri.provider_ready.v1`，含 `contract_id`、字符串 `contract_version`、
   `capabilities`（`scene`；策略复位还需 `events`、`reset_judgement`）与整数
   `updated_wall_ns`，十秒内刷新。缺失、过期或身份不符即拒绝。
2. 监视 `<kind>/requests/<sha256(request_id)>.json`，kind 为 `scene` 或
   `reset_judgement`。`levi.aeri.provider_request.v1` 包含原始 AERI `request`、
   本地 `clock_domain`/`observed_ns`、当前 JPEG 帧引用 `frames`、`evidence_root`
   与合约必需/可选谓词。读取实际证据文件，针对这次请求判断。
3. 原子写入 `<kind>/responses/<同一 hash>.json`，内容为契约规定的 AERI
   `levi.aeri.scene.v1` 或 `levi.aeri.judgement.v1`。严格匹配运行、请求、片段、
   角色、合约、时钟域、观测时间步、谓词和证据引用。不可判断时如实返回。
   编排器仍验证响应；桥不制造答案。预写响应、符号链接和错误/过期响应不能建立就绪。
4. 事件提供方读取 `events/<sha256(episode_id)>/request.json` 中的运行、片段、
   角色和时钟身份，在同目录写唯一 `event-*.json` AERI 提案，并尊重
   `<kind>/cancelled/` 通知。事件只提出终止，末态/复位仍需独立判定。

仓库提供文件传输层，尚不提供外部场景/复位模型进程。缺该真实提供方时策略复位
被阻止，复制 READY 示例不能实现提供方。人工复位可选
`scene_check: operator_attested`，问题与当前帧保存在
`<rollout_root>/.aeri/runs/<run_id>/scene/questions/`。操作者在另一终端读取后，用
`python -B -m levi.automatic.fr3_run --scene-question <question.json> --predicates
'{"required_predicate":true}'` 回答实际合约的谓词，不确定填 null。
`WAIT_HUMAN` 只有操作者处理好场景后明确输入 `resume` 才继续。

只有已确认执行的时间步才提交视频/CSV。归位复用已守卫的笛卡尔 `_go_home`，
核验最终姿态与夹爪打开，不调用关节复位。每条运动 HTTP 调用检查当前 fence 和
停止请求；健康故障、归位失败、未知确认均锁住运动，不重试。SIGINT/SIGTERM
停止新指令并终止编排器，不能代替硬件急停。fake I/O 覆盖两种复位模式、复位失败、
身份不符、停机/故障门、未知确认、提供方时效和 `--check` 无连接。相机编码、服务
时序、物理停机行为和复位可靠性仍需现场监督验收。


绑定文件也冻结旋转范围与夹爪二值化设置。`--check` 拒绝范围外的归位旋转；只有
明确审阅的夹爪二值化可以改变动作，其余静默裁剪一律拒绝。测试通过
`LEVI_TEST_FR3_CLIENT_SOURCE` 指定当前客户端源码，只从 AST 执行三个纯裁剪函数，
不导入硬件模块。

执行在任何 SDK 导入或连接之前持有本机唯一物理臂锁
`/tmp/levi-aeri-fr3-arm.lock`，跨运行编号、记录根目录、工作树及 5000/5001 两个
端口共同排他。锁文件保留，关闭文件描述符释放内核锁。这只协调遵守桥协议的进程；
操作者仍须单独确认旧客户端和外部机器人控制器已经停止。

每条运动 HTTP 响应必须符合对应路线的已知确认（`Clear`、`Moved`、`Opened`、
`Closed`、`Moved Gripper` 或显式成功 JSON）。负答、空答或未知响应立即阻止后续
路线，并锁住不确定的执行，不重放。step 的嵌套响应也必须匹配已确认的姿态和
实际发出的夹爪指令；夹爪响应为 None 只有在没有新夹爪指令发出时才合法。

场景和复位目标响应只允许引用本次请求实际保存的帧：引用、SHA256 与捕获时间
必须准确相等，每个已读的必需谓词须有当前帧证据，文件仍须存在且 hash 一致。
编码/保存失败、旧引用、缺谓词证据或文件改动均返回 unavailable。传输层不能证明
模型判断正确。复位目标响应还须准确匹配已确认初始状态合约的编号和版本，完整
报告全部必需和可选谓词，且不能改动必需/可选标志。可选谓词为 false 或 null 不
决定复位成败；通用前进目标规范或缩减后的复位检查清单均返回 unavailable。
这类复位回答不会进入 judgement_ids，也不能启动前进流程，运行停在 WAIT_HUMAN。
独立的复位后场景检查若观察到初始状态已恢复，仍可记录
`goal_verification: verified` 和 `task_outcome: success`；这些字段保留场景观测，
不能当成复位回答已被接纳或整个运行已完成。
场景/复位模型 worker 须显式配置并匹配已批准规范，事件提供方是独立能力；
只有文件通道不能认定端到端真机可用。已消费文件超过 1000 个后事件仍可读取。媒体关闭会逐一尝试全部资源后
才报告首个错误。`--scene-question` 不能与 `--check`、`--execute`、作业或绑定
选项混用，保持 `--check` 只读。

### 策略切换（可选 `policy_swap`）

前向和复位两个策略服务按审定的显存比例 `.22` 合计约 15 GB，实时服务里清醒的
视觉模型约 24 GB，一张 32 GB 的卡放不下。所以 `single_reset_policy` 的 binding
可以声明可选的 `policy_swap` 块。声明后运行中最多只有一个策略服务：本次运行自己
启动前向服务；在转入 `FORWARD_ACTIVE` 或 `RESET_ACTIVE` 的事务里，先停掉正在运行
的服务，再启动另一个。视觉模型不受影响。不带这个键的 binding 仍按上文的约定：
接受的键、`--check` 输出不变，两个服务都要事先在运行。

每个字段都必填，没有默认值（下面的数值只是示例，不是实测或批准的设置）：

```json
"policy_swap": {
  "schema": "levi.aeri.fr3.policy_swap.v1",
  "services": {
    "forward": {"unit": "levi-aeri-policy-forward",
                "argv": ["/abs/openpi/.venv/bin/python", "scripts/serve_policy.py", "--port", "8000",
                         "policy:checkpoint", "--policy.config", "<冻结的 config>",
                         "--policy.dir", "<冻结的绝对 checkpoint_dir>"],
                "working_directory": "/abs/openpi", "mem_fraction": 0.22},
    "reset": {"unit": "levi-aeri-policy-reset", "argv": ["..."],
              "working_directory": "/abs/openpi", "mem_fraction": 0.22}
  },
  "vision_mem_fraction": 0.74, "max_total_fraction": 0.98, "gpu_margin_mib": 1024,
  "stop_timeout_s": 60, "start_timeout_s": 300, "handshake_timeout_s": 30,
  "warmup_timeout_s": 120, "start_retries": 1,
  "still_window_ms": 500, "still_tolerance_mm": 1.0, "still_tolerance_rad": 0.01
}
```

`--check` 只读文件检查这个块：两个单元名不同且以 `levi-aeri-policy-` 开头；
`argv[0]` 是绝对路径的可执行文件；`working_directory` 存在；argv 带该角色端点的
`--port`、该角色冻结的 `deployment.config` 和绝对 `checkpoint_dir`，且不含 `$`、
`%` 和控制字符（systemd 不做任何展开）；每个角色
`mem_fraction + vision_mem_fraction <= max_total_fraction <= 1`；`start_retries`
只能是 0 或 1；静止容差不超过回位容差。通过后输出多一个 `policy_swap` 键，列出
单元、端口、显存比例和超时。它不运行 `systemctl`、`systemd-run`、`nvidia-smi`，
不连接任何端口。

一次切换的顺序（`levi/automatic/fr3_policy_swap.py`）：

1. 没有运动令牌（事务开始时已撤销，片段的令牌在切换之后才签发）；桥不再发新的
   位姿指令（不向机器人发送任何东西）；手臂在审定的回位位姿，并在
   `still_window_ms` 内静止。不满足就在动任何服务之前拒绝。
2. 在 `<rollout_root>/.aeri/policy-swap/<run_id>.jsonl` 写 `prepared`。
3. 对旧单元执行 `systemctl --user stop`，等到单元不再 active、端口无监听者、
   事先从 `/proc/*/cgroup` 记下的进程都已退出，并且
   `nvidia-smi --query-compute-apps` 不再列出它们。只停 binding 声明的单元，
   从不按 PID 发信号。
4. `nvidia-smi --query-gpu=memory.total,memory.free`：空闲显存要够
   `mem_fraction × total + gpu_margin_mib`。
5. `systemd-run --user --unit=<unit> --collect --property=KillMode=control-group
   --property=TimeoutStopSec=<停止超时> --working-directory=<dir>
   --setenv=XLA_PYTHON_CLIENT_MEM_FRACTION=<比例> -- <argv>`；确定失败的启动最多
   重试 `start_retries` 次。
6. 被动就绪只读 `/proc`：单元 active，端口上每个监听进程都在该单元的 cgroup 里、
   命令行含冻结的 config 和检查点。之后才构造客户端（openpi 客户端在构造时连接），
   核对元数据、身份和客户端合同。
7. 用当前观测做一次预热推理，结果丢弃：冷启动的 JAX 服务第一次请求要编译，
   比默认 500 ms 的分块超时长。
8. 核对没有计入运动步、手臂没有移动，写 `committed`。

拒绝、超时、操作员停止或任何不确定，都会停掉本次切换启动的单元，记下服务状态
是否确定，并让运行转入 `FAULT_LOCKED`（`policy_error`，备注
`policy_switch_fault`）；本进程之后的切换一律拒绝，不重试运动。运行结束时停掉
本次运行启动过的单元并确认释放。启动时发现遗留单元或端口被占，就拒绝并给出
`systemctl --user stop` 命令让操作员处理，运行本身不去停它。若同一记录根目录下
有切换日志的事务没有结束（崩溃、断电）、有损坏行或最后的服务状态不是 `none`，
下一次 `--execute` 会先列出这些日志，确认短语变为
`execute <run_id> after policy switch check`，确认后在这些日志里记一笔已确认。
在任何机械臂 I/O 之前被拒绝的启动不会占用 run id。

场景在切换之前判定，切换后不再重判；切换只核对手臂没有移动，不能发现有人挪动
物体。实时服务把 `[gpu] policy_ports` 上的监听进程当作策略服务：复位端口与前向
不同时要加进去。`systemd-run --user` 启动的单元用的是用户管理器的环境，不是终端
的环境：argv 写绝对路径的解释器，不用 `uv run`。fake I/O 测试覆盖切换顺序、每次
调用单元工具时最多一个策略监听者、切换期间无令牌、幂等、端口和显存未释放、
显存预算、外来监听者、命令行和元数据不符、重试、超时、停止、中断的切换，以及
不带该键时约定不变。切换耗时、显存占用和单元本身都还没有在本机运行过。

## 有界复位后的最终验证

`reset.horizon_completion: wait_human` 仍是默认行为。复位达到步数上限仍记录
`stop_reason: horizon_exhausted`，到界本身不证明成功。操作员可批准一个**新计划**，
显式选择 `reset.horizon_completion: verify_final`；仅适用于启用的
`single_reset_policy`，且必须有已确认的初始状态合约、提供方场景检查和
`termination.allow_early_stop: false`。选项和全部稳定性参数进入计划摘要，旧计划
的批准不能用于新计划。既有 runner 仍拒绝真机，现场入口使用经过审核的 FR3
桥配方：先运行不连接设备的 `--check`，再由人员监督运行 `--execute`。

新模式必须明确填写 `reset.settle` 的六个字段：`sample_interval_ns`、
`stable_window_ns`、`max_wait_ns`、`position_tolerance_m`、
`rotation_tolerance_rad`、`gripper_tolerance`。时间须为正数，并满足采样间隔≤
连续稳定窗口≤最大等待；预算最多允许 1000 次观测。容差须为有限正数，夹爪
容差使用归一化到 0..1 的实测开口。没有默认物理阈值；操作员须核实位置观测
能力后，在新批准计划中冻结参数。测试的合成数值不是现场建议，物理阈值与
观测来源仍需 HA-23 监督验收。

正常到界后，策略须成功停止产生新动作，再读取覆盖连续稳定窗口的新鲜观测。
窗口内任意两次观测都须满足冻结的 XYZ 距离、绕回后的 Euler 角距离和实测
夹爪开口容差。至少两次观测的时间须在当前主机时钟域内严格递增，并落在本次
读取起止之间。SDK 的 `pose[6]` 是夹爪**指令**，不能证明夹爪稳定；必须另外
读取 `robot_state.gripper_position`，且本次观测已有的 `getstate` 响应须明确含有
`gripper_pos` 测量字段。桥保留读取代次和本机时序，并核对 SDK 输出与原始响应
一致。缺字段后由 SDK 填出的零值没有证明；明确实测为零可以有效，不增加设备
读取。缺值、非有限值、旧时间或乱序均不允许
继续。此门只检查笛卡尔/夹爪的观测稳定和策略不再发新指令，不声称证明关节
静止、速度或机械安全。

继续前须具备完整链条：保留带计划参数和时序的稳定观测；真正的 `reset_final`
判定，以当前帧证据确认准确、完整的初始状态合约；独立 `post_reset` 场景确认
ready；记录封存与归位成功；现有 `VERIFY_INITIAL` 再次确认当前场景 ready。
最终判定和场景证明分开保留。两次 ready 场景都必须真实保存判定记录和全部
引用帧；写入拒绝、I/O 失败、帧丢失或内容改变、存在未保存帧均阻断正向执行。
归位后的初态检查在人工恢复后仍执行同一留证门禁；其他复位模式保留原有的
留证失败记备注行为。模型截止时间保持不变（活动判定 3 秒、最终判定/
场景 5 秒）。采样和阻塞判定前后重查停止与故障；策略异常、超时、未知、证据
缺失、合约违规、封存或归位失败均保留人工或故障路径，原始到界仲裁禁令不变。

离线回归使用真实主机时钟、**没有事件文件**的 FileEvents、文件提供者、模型
worker、OnlineVerifier 和日志，只注入 SDK I/O 与模型回答。它验证软件门禁，
不能证明模型准确性或物理可用。真实 worker 仍须配置并准确匹配身份，只有文件
通道不够。现场执行仍需操作员当次终端确认及监督验收。

## 独立操作脚本与共享运行跟踪

`python scripts/aeri.py check --config JOB --binding BINDING` 复用
`python -m levi.automatic.fr3_run --check` 的只读检查。`execute` 保留原有
终端人工口令，以及 `--expect-plan` / `--expect-binding` 摘要核对。
`plan`、`runs`、`stop`、`resume` 等指令直接复用 `levi automatic`，执行核
和命令协议只有一套。脚本不会部署模型或启动服务。

经人工授权的独立运行登记到 `LEVI_AERI_HOME`，明确标记
`origin: operator_terminal`、实际执行模式，并绑定输入摘要、规范记录路径、
日志头、冻结计划和进程身份。原页面和 CLI 读取同一份经过验证的登记与日志。
关闭页面不停止运行器，重新打开会读取当前状态。停止和初态回答复用
原命令队列、序号检查、人工确认与幂等协议。这类运行不能通过 HTTP 恢复：
`POST runs/{id}/resume` 返回 `403 terminal_resume_required`（在动 challenge、
命令文件和日志之前拒绝，运行保持 `WAIT_HUMAN`；快照带
`resume_terminal_only: true`，页面据此显示提示而不是恢复表单）。须在终端恢复：
在 `scripts/aeri.py` 进程的终端输入 `resume`（驱动自己调用 `robot.health()`
复查）。不提供 `levi automatic resume` 作为这类运行的恢复方式：它只靠键入确认，
不会复查 `robot.health()`（问题 L2-SCRIPT-CLIRESUME）。这类运行的场景题也不能通过
HTTP 作答（取舍 UI-2）：`POST runs/{id}/scene-answer` 在写任何答复文件之前返回
`409 scene_answer_terminal_only`，快照带 `scene_answer_terminal_only: true`，页面据此只显示
题目、不给作答表单。须在终端键入 `y`/`n`/`u` 作答。场景核对为 `provider` 的真机运行同样
每轮由终端放行（L2-UI2-PROVIDER-RELEASE，用户 2026-10-15 决定；`console.ConfirmedScene`）：
forward 片段结束后先等复位提示的 `Enter`（之后才拍证据帧），再问 provider 并等它的答案
（`scene_timeout_s`），终端打印“模型判断：就绪 / 需复位 / 不确定 / 无答案（仅供参考……）”，
然后在终端问与 `operator_attested` 相同的场景题，只认终端的 `y`/`n`/`u`。运行采用的是人的
判断（`provider: human`）：每个必答条件都是 `y` 才开始这一轮，`n` 或 `u` 走原有路线（由
reset 策略复位，或进入 `WAIT_HUMAN`）。provider 的答案从不单独放行运动；与人的判断不一致时
记一条 `scene_model_disagreed` 备注（含双方答案 `model=... operator=...`），以人的判断为准。
这道题最多等 `human_scene_timeout_s`，分段等待，收到停止立即结束。离线和 fake 运行不变：
provider 的答案就是判断。判断依据是
经验证的登记（`backend: standalone`、`origin: operator_terminal`），不看名称
或路径；试运行和 HTTP 启动的运行恢复方式不变。真实运行不能自动重放或 attach；
故障仍走原人工审核，真实 HTTP 启动继续拒绝。

CPU SDK mock 仅证明接口接线，不证明真机就绪或模型互斥激活。forward、VLM、
reset 的静默交接仍依赖经审核的 Performance Architecture 集成。真机执行前
必须冻结成功判定规格和物理绑定；普通向导不能替代这些事实。

磁盘日志损坏或不完整时不能恢复。共享控制泵和终端恢复都会重新核对磁盘与当前写入器；损坏会锁住写入器，停止仍可提交。旧的未认证 dry-run 登记须保留计划及一致的有效日志头，才能由 CLI/API 读取。新的 dry-run 启动用现有核心密钥认证冻结计划，因此真正已启动但尚未写入日志头的短暂状态仍可查看。

## 真机运行的操作员终端（`levi/automatic/console.py`）

一个终端跑完整场真机运行：`python scripts/aeri.py execute ...`（即 `fr3_run --execute`
路径），标准输入输出都是终端时启用。先打印开始摘要，再要求键入原有的
`execute <run id>` 确认语句；在这里按 Ctrl+C 或输入结束会在连接任何设备之前取消
（退出码 130）。此后每个提示都是一行：`[第 2/5 轮] 机械臂已回位。请复位环境，完成后按
[Enter]（q 结束）`（第几轮、刚才发生了什么、现在要做什么）。这行字来自一张状态与提交
原因表（`STATUS`、`WAIT_REASONS`），所以回位验证分支的原因（`robot_home_reached`、
`robot_homed`、单片段成功后直接完成、多片段的 `scene_reset_required`）以及以后新增的原因
只是加一行，不用改逻辑。不含复位策略模式下每轮一次 Enter 加一次 `y`：复位环境，按
Enter（证据帧在此之后才取），回答场景题。

| 按键 | 位置 | 作用 |
| --- | --- | --- |
| `Enter` | 复位提示 | 环境已复位，此时才取证据帧。同时写一条日志备注 `operator_reset_confirmed`（操作员身份，写明本轮片段），因为 `y` 直接进入下一轮，不再有 `WAIT_HUMAN` 能证明人复位过环境。 |
| `Enter` | `WAIT_HUMAN` | 续跑，先重新做健康检查；监控为红则不续跑（提示说明原因，继续等待）。原因为 `operator_stop`、`contract_violation_limit`、`policy_error` 时一次 `Enter` 不够，见下一行。 |
| `Enter`，再按 `y` | 原因为 `operator_stop`、`contract_violation_limit`、`policy_error` 的 `WAIT_HUMAN` | 第一次 `Enter` 先清空键盘缓冲（`keys.flush()`），再提示确认。只有 `y` 才算确认（`Enter` 和其他键都忽略；`n` 取消并继续等待，`q` 结束）。所以快速连按两次 `Enter`，或把 `y` 和 `Enter` 一起按下，都不会续跑。确认后的续跑与原来一样（`resume_checked`，再经 `PREFLIGHT`）。其他等待原因仍是单键。 |
| `y` `n` `u` | 场景题 | 条件成立 / 不成立 / 看不清，每个必答条件按一个键，`b` 返回上一项。`Enter` 从不作答。`y` 放行下一轮，`n` 或 `u` 进入 `WAIT_HUMAN`（复位后按 `Enter`）。只有这些按键能作答（取舍 UI-2）：页面照样显示题目和证据帧，但页面上的回答会被移走、记一条 `scene_answer_page_ignored` 备注，不会放行运动，终端也会提示这一点。第 1 轮的 `initial_state` 题同样如此。 |
| `s` `f` `d` | forward 一轮进行中（`FORWARD_ACTIVE`） | 对正在进行的这一轮给出人工判定：成功、失败或作废。它只提前结束**这一轮**，运行接着进入下一轮（见下文“按人工判定提前结束一轮”）。始终可用；开轮前敲的键、其他键，以及回位、复位、等待人工时敲的键都不起作用。 |
| `s` `f` `u` `d` | 复位提示、`WAIT_HUMAN` | 仅在 `LEVI_AERI_CONSOLE_LABELS=1` 时可用：对刚结束的一轮打可选的操作员标签（成功、失败、不确定、作废），先于 `Enter`。 |
| `q` | 各提示 | 结束运行（停止，不是故障）。 |
| Ctrl+C | 任何时候 | 入口保持机械臂并登记停止，不再下发运动指令。控制台打印一行并写入 `operator_interrupt` 备注（信号、人当时在哪个提示、状态、轮次）；退出码为 128 加信号号。在提示处，运行以 `WAIT_HUMAN` 或 `COMPLETED`（`operator_stop`）结束。策略运行中，运动指令在停止标志下被拒绝，目前会以 `safety_stop` 锁定运行（屏幕会说明这是 Ctrl+C 引起的）；已锁定的运行需用新的 run id。 |

`Enter` 声明了什么（上述三种原因是 `Enter` 加 `y`）：真机运行里，在 `WAIT_HUMAN` 按这个键一律以
`environment_handled=True`、`health_rechecked=True` 调用续跑，也就是说按下它就是声明场景
已复位；这个声明由在机器人旁的操作员负责，AERI 不核验。续跑仍会经过 `PREFLIGHT`，它重新
检查健康监控与锁存，任一不通过就不会开始运动（运行回到 `WAIT_HUMAN` 的
`preflight_failed`，机器人已锁存则以 `safety_stop` 锁定；日志里保留这次续跑和拒绝），但
`PREFLIGHT` 不检查场景是否真的复位。复位提示处的 `Enter` 同理：随后的证据帧和场景题才是
对场景的核对，`y` 是操作员的回答。

**按人工判定提前结束一轮**（L2-AERI-OPERATOR-EARLY-STOP，沿用旧测评客户端的
`s`/`f`/`d`）。forward 一轮进行中，控制台按单键读取（cbreak，对文件描述符 `os.read`，
由入口的心跳线程每 50 ms 做一次不阻塞的 `select`）。一轮开始时（`FORWARD_ACTIVE` 提交）
先清空开轮前敲进的键，再打印一行说明哪些键有效；运行离开 `FORWARD_ACTIVE` 时终端退出
cbreak 并再清空一次，所以回位、复位或等待人工期间敲的键不会结束任何一轮。`s`、`f`、`d`
调用 `Orchestrator.episode_verdict`（label 为 `success`、`failure` 或 `discard`，来源
`terminal`），它和停止一样只做登记。循环在下一步、在停止检查和锁存/健康检查之后（两者
优先级不变）以停止原因 `operator_verdict` 结束这一轮，走的是普通的 `FORWARD_STOPPING`
保持、静默、封存和回位：不新增运动权限，不跳过任何检查。之后运行像判定模型提前结束后
那样进入下一轮，不进入 `WAIT_HUMAN` 或 `COMPLETED`。停止仍然结束整次运行，并且优先于
尚未生效的人工判定（返回 `stop_pending`，或该判定被丢弃并记一条
`operator_verdict_dropped` 备注）；在这一轮最后一步之后才到的按键也这样丢弃。每轮只收
一次判定；事件暂停判定（`event_pause_judge`）运行不收（`not_supported`）。

人工判定与模型判定分开存放。日志里记一条 `operator_verdict` 备注，内容是 JSON：`label`、
`source`、`episode_id`、`step` 与 `frame_index`（按键时的控制步和最后一帧的索引）、
`taken_step`、`wall_ns`、`command_id`。片段结果仍是运行自己的判定
（`label_kind: autonomous_verdict`，终局判定照常进行），`stop_reason` 为
`operator_verdict`。结果提交后，这个判定还会写成该轮的操作员标签
（`labels/operator_label.jsonl`：`success`、`failure`，`d` 写 `discarded`）；作废的一轮没有
真值，不进任何比率，指标报告的 `operator_verdicts` 列出各次判定，作废的标 `excluded: true`。
作废的一轮不计入运行的轮数，自动补跑一轮（用户 2026-10-16 裁定），第一步之前就给出的判定结束的是一轮没跑过的，不计入轮数，运行照常继续；也不写操作员标签（记 `operator_verdict_not_labelled` 备注）；也不进自动成功率、一致率和真值统计。补跑有上限 `max_discarded_rounds`（运行配置，默认等于运行轮数；作业文件暂不能设置，默认值不改变计划摘要）：作废永远不算真值，也不进任何分母；超过上限后，作废的一轮只是不再补跑：它占掉运行的一轮，记一条 `discard_limit_reached` 备注（“not replaced (limit X); the run ends with fewer valid rounds”），运行照常结束，有效轮数少于 N，指标报告用 `discarded_beyond_limit` 给出这个数。运行进程在一轮中被强杀或崩溃后，终端可能停在 cbreak（不回显、不能行编辑）：在该终端运行 `stty sane` 恢复。双标签对比里 `operator_verdict` 与 `operator_stop` 归为一类
（由人结束）。页面能显示这条备注和停止原因，但不能发出判定：没有对应的接口，指令通道对
任何运行都拒绝 `episode_verdict` 这种指令（`terminal_only`），与取舍 UI-2 的场景题作答相同。

盲性：操作员对某一轮的标签落盘之前，不显示自动判定，也不显示该轮如何结束。允许提前
结束时，轮次行在此之前既不显示步数也不显示原因，任何时候都不显示内部状态名
（`ROBOT_HOME`、`FINALIZE`）。不允许提前结束时，与旧客户端一样显示步数和结束方式。

允许提前结束时，运行中那一轮的进度行在该片段的标签落盘之前只写“第 n/N 轮 运行中”，
不带步数（步数不到上限就会暴露已提前结束）。`WAIT_HUMAN` 提示下面和故障摘要里打印的
备注来自一张可显示的备注码白名单（`console.py` 的 `DISPLAY_NOTES`：机器人、录制器、
策略分块、会话文件等运行事实）；`early_stop_disputed`（含终局判定）这类判定备注只留在
日志和页面里，新增的备注码在加入白名单之前不显示。策略适配器或策略服务给出的错误原文
（`policy_unavailable`，例如 `websocket closed (1011)`）是单独的一类
（`POLICY_ERROR_NOTES`）：控制字符（0x00–0x1f、0x7f）先换成空格，再截到 300 字符（`POLICY_ERROR_LIMIT`）后显示在控制台故障摘要、
`WAIT_HUMAN` 提示下面以及旧手敲路径的 `print_fault` 里。

**`scripts/aeri.py execute` / `fr3_run --execute` 的退出码**（行为变化：被 Ctrl+C 或
SIGTERM 安全停止的运行以前和其他未完成的运行一样退出 1）：

| 退出码 | 含义 |
| --- | --- |
| 0 | 运行以 `COMPLETED` 结束，包括在等待时被人停止的运行（`COMPLETED` `operator_stop`；没有单独的“已停止”状态）。**退出码 0 不等于跑完了全部片段**：在等待处输错（不是 `resume`，或确认时不是 `y`）也会停止运行并得到 0，要看最终状态的原因是不是 `operator_stop` |
| 1 | 运行以其他状态（`WAIT_HUMAN`、`FAULT_LOCKED`）结束，且不是信号造成的 |
| 2 | 在运行之前或之外被拒绝或失败（摘要不一致、没有终端、确认语句不对、作业有误） |
| 130 | 在 `execute <run id>` 确认语句处按 Ctrl+C 或输入结束：连接任何设备之前取消 |
| 128 + n | 运行被信号 n 安全停止（SIGINT 为 `130`，SIGTERM 为 `143`）；stderr 写“已被信号 n 停止；最终状态 S”（随控制台语言 `LEVI_AERI_CONSOLE_LANG`，默认中文） |

运行结束时的收尾顺序：先通知心跳线程停止并让机械臂保持（`emergency_hold`）；接着
`FAULT_LOCKED` 的运行把故障摘要打印到 stderr，这次打印失败（stderr 已断开，`EIO`）会被
忽略；然后等心跳线程结束、关闭控制台，再释放运动围栏、编排器和录制器（前一项失败也照样
尝试后一项），最后恢复 SIGINT/SIGTERM 处理器。控制台关闭失败（终端已断开）时其余资源照样
释放，能写就先在日志里补一条 `console_close_failed` 备注，再往 stderr 写警告；stderr 也
断开时警告直接丢弃。这两种失败都不改变运行状态和退出码（`COMPLETED` 的运行仍是 `0`）。

**手敲的旧路径。** 没有控制台时（stdout 或 stdin 不是终端，例如 `... | tee run.log`，
或 `LEVI_AERI_CONSOLE=0`），真机运行无论场景核对方式都在加载或连接任何东西之前被拒绝：
`operator_attested` 的场景题否则只能在页面上作答（取舍 UI-2），`provider` 的场景否则会只凭
provider 的答案放行（L2-UI2-PROVIDER-RELEASE，用户 2026-10-15 决定）。所以真机运行不再走到
下面这条路径；代码仍在，测试照旧覆盖。它的 `WAIT_HUMAN` 打印一行提示并读一行输入；stdin 不是终端时直接
返回。stdin 是终端时先冲刷输入（`termios.tcflush(TCIFLUSH)`），运行期间预先敲进去的行
（比如提前输入的 `resume`）不会回答这次等待，并在这一刻记下这次等待的日志序号。输入
`resume` 续跑（带着这个序号调用 `resume_checked`，再进 `PREFLIGHT`）；其他任何输入、空行或
输入结束（EOF）都停止运行（`terminal-stop`：`COMPLETED` `operator_stop`）。原因属于
`console.py` 的 `CONFIRM_REASONS`（`operator_stop`、`contract_violation_limit`、
`policy_error`）时只输入 `resume` 不够：提示要求二次确认，再冲刷一次输入，只有输入一行
`y`（或 `Y`，即 `CONFIRM_KEYS`）才续跑；其他任何输入、空行或输入结束都停止运行。其他原因
仍是一次 `resume`。由 `main()` 启动的运行总会登记，所以两行输入都用 `select` 等待，而不是
阻塞在 `input()`：页面恢复了运行、状态或日志有了变化（故障锁定、写入了新行）、停止探针或
信号让机械臂停下时，这次等待直接结束，本终端不会续跑；日志变化之后才输入的 `y` 会被
`stale_sequence` 拒绝。冲刷依赖终端的规范（行）模式（ICANON）：这时预先敲入的行还在内核
队列里，冲刷能清掉。终端关闭了 ICANON 时 stderr 会给一条警告，因为 Python 可能已经把这些
行读进了自己的缓冲；这时请只在提示出现后输入。

环境变量：`LEVI_AERI_CONSOLE=0` 关闭控制台（回到上面手敲的旧路径），
`LEVI_AERI_CONSOLE_LABELS=1` 启用可选的标签键，`LEVI_AERI_CONSOLE_PRINCIPAL` 指定标注者
（默认 `operator`），`LEVI_AERI_CONSOLE_LANG=en` 把整个终端切成英文（默认 `zh`）。没有终端
就没有控制台。已知限制：录制器的 `preceded_by` / `after_human_resets` 元数据目前只读
`WAIT_HUMAN` 行，所以用 `Enter` 确认的复位暂时以 `operator_reset_confirmed` 备注为准，
直到录制器也读取它。

## 显式事件暂停判定（默认关闭）

`termination.inference_mode` 默认是 `legacy_async`，保留异步判定时继续动作的原行为。显式选 `event_pause_judge` 后，模式和 `termination.physical_hold` 的全部稳定条件进入规范化计划摘要，不能复用旧批准摘要。必须明确提供采样间隔、稳定窗口、最长等待、位置/旋转/夹爪容差；不设物理容差默认值。该模式仅允许 `confirmations: 1`、开启提前停止、无对照组，每片段最多 400 个**已确认执行**的动作。`on_unknown: continue_to_horizon` 只控制旧模式；新模式的 unknown、超时、证据或保持未确认均闭锁，不能按失败继续动作。

新模式复用原 `RuntimeOwnership` 和 `WorkloadBroker` 的唯一角色请求槽，不建立另一个调度器。初始场景判断、forward/reset 请求与目标判断都通过同一角色授权。 每次场景判断在角色授权前先分配独立策略代次并持久写入日志；实际策略片段再申请严格更新的代次，包括初始场景需要 reset 的情况。重启从这些日志记录恢复代次计数。候选一出现就不再发起新的策略请求和新动作（`max_requests` 在新模式下是每片段暂停判定次数上限，默认 20（legacy 仍为 6），达到后策略继续跑到自然结束或 `max_steps`，再走终局判定；`min_steps`、`settle_steps`、`cooldown_steps` 在新模式忽略；新候选的 step 必须大于上一次判定覆盖的步）；被判 rejected 的候选即作废，只有更新的事件才能再次触发暂停。先废弃动作缓存、提高策略代次并关闭新策略请求，进入 `FORWARD_JUDGING` 或 `RESET_JUDGING` 日志状态。VLM 开始前分别需要按冻结条件取得的独立物理保持证明与全部受控后端的静默证明；VLM 返回后仍须后台请求停止、全部本地请求 worker 退出和整个受控群组静默，才可以接受判定。保持和判定证据必须保留。操作员停止会锁存推理周期；只有没有证明失败且请求槽已确认释放时，`resume` 才会重新打开它，否则以 `intervention_latched` 拒绝；证明失败仍保持锁存（resume 被接受，下一次运行再次 FAULT 且不产生动作）。证据准备、证据留存、判定后新观测和策略准入共用不可恢复的证明失败锁存；操作员同时停止也不能把这些失败变成可恢复的普通停止。

过期的恢复序号不会重新开放推理周期：只有日志 prepare 的序号比较交换及恢复事务持久提交成功后，才在停止代次检查下 rearm。后到的停止保留锁定；rearm 时发现证明失败则重新进入故障隔离，不发送动作。已提交的事件模式恢复接受及拒绝结果都在返回前持久关联到准确事务及命令；重试（包括重开日志后）保留已记录结果，不依赖后续周期状态。缺少结果标记（包括接受/拒绝标记写失败或旧的未标记事件模式日志）返回 `command_used`：该 ID 已用但结果未知，不重放动作。legacy 保留原有未标记重复合同。存储失败仍抛异常，并可能阻止后续日志写入；故障隔离撤销运动权限，但不保证结果已保存。

暂停的既有 `policy_quiesce` 事务还复用 `_quiesce`，在目标判定和封存前确认策略已静默，并终检最后一个 pending 夹爪命令的 ACK。quiesce 结果未知或末 ACK 未证实时，已准备的暂停事务记执行未知并 abort，周期闭锁；不授权目标请求、封存或 Home。适配器 quiesce 不等于 native 模型／后端静默证明；原 `InferenceCycle` 权限仍须独立验证请求停止、worker 退出及整个群组静默。

**终局 Home（仅软件）。** 暂停判定结束的前向片段（确认成功，或动作数达到上限）现在走边 `FORWARD_JUDGING -> FORWARD_FINALIZE`（`policy_quiesce`，再次确认末次夹爪 ACK），再 `FORWARD_FINALIZE -> ROBOT_HOME`（封存录像；片段结果在这里按已保留的暂停判定写入，此时 Home 尚未执行，所以 `robot_home: not_attempted`），再 `ROBOT_HOME -> SCENE_ASSESS`（Home，由 `Robot.home_under_closed_gate` 在关闭的准入闸下执行）。不再发第二次目标请求：不调用旧 `_verification`。回位完成后日志里有 note `robot_homed`（片段、`sealed`，不含任何判定结论（结论只在片段结果里，操作员标注之前不会随 note 显示）；机器人有 `admission_snapshot` 时附闸门事实 `gate`、`home_only_from_ns` 以及本窗口的 `home_pose_released`、`home_gripper_released`、`home_gate_rejected`（是本窗口的增量，不是机器人的累计值；快照读取失败写 `gate=unreadable`），否则只写 `gate=closed`；最后是 `next=scene_assess`）和已提交的原因 `robot_home_reached`；之后由既有场景评估决定下一步：场景就绪则开始下一片段（恢复钩子以更新的代次重开准入），场景需要复位则交给复位策略或人（`WAIT_HUMAN`，`scene_reset_required`），片段数够了则结束（`COMPLETED`）。封存之后登记的停止让手臂留在原地（`ROBOT_HOME -> WAIT_HUMAN`，`operator_stop`），不受 `home_after_operator_stop` 影响。Home 失败或结果不明则以 `home_failed` 锁定，保留已封存的结果，不重试；唯一的例外是机器人在第一条命令之前就拒绝的 Home，它进入等待（`home_not_executed`，见下文恢复钩子一节）。unknown、超时或证据不足的判定不变：`FAULT_LOCKED`（`judgement_unproven`），不封存、不回位。复位角色同样不变：暂停的复位判定仍封存后进入 `WAIT_HUMAN`，不回位。机器人没有可调用的 `home_under_closed_gate` 时编排器拒绝构造（`ConfigError`）。旧边 `FORWARD_JUDGING -> WAIT_HUMAN` 为旧日志保留合法，但不再有代码走它。真实事件模式入口仍然拒绝（`test_real_binding_refuses_mode_before_reading_binding_or_robot`）：以上内容没有在真机上运行过。

以下内容没有被证明：

1. “自暂停起准入连续关闭”只覆盖到 `home_only_from_ns`；回位后闸门重新关闭，是新的关闭区间（新的代次和关闭时间）。
2. 物理保持证明只对被判定的那一帧成立。Home 会让手臂运动，所以没有“事件之后手臂一直静止”的证明。
3. Home 计数记录的是命令开始通过闸门，不证明已送达或手臂到位；到位只靠 `_settle_home` 的实测。
4. 本地闸门只约束本进程的 `env._post`（窗口内只有打开它的线程能通过），不排除其他进程直连机器人服务（已有的局限）。
5. 不证明后端静默，Home-only 窗口也不提供新的后端证明。
6. 暂停判定针对回位前的画面；回位可能扰动场景，之后的场景评估是新的请求。
7. 真实客户端 `_go_home` 实际发哪些路由：只读过检出的客户端源码（`examples/fr3_local/run_robotiq_client.py` 的 `_go_home` 经 `RobotEnv.step` 逐步发送，它 POST `clearerr`、`pose`，只有测得夹爪尚未张开时才 POST `open_gripper`；Home 位姿要求夹爪张开，所以不会走到 `close_gripper`、`move_gripper`）。没有在真机上运行过，其他版本的客户端可能不同；`pose`/`clearerr`/`open_gripper` 之外的路由会在闸门被拒，运行以 `home_failed` 锁定。
8. 真实的 `stop_admission`／`resume_admission` 钩子绑定现在已有软件实现（`levi/automatic/fr3_intervention.py`，见下一节），但只在测试里装配，真实入口不使用。Home 之后的场景评估所用的保持钩子，需要用新的封闭区间建立见证，因为 Home 之前建立的见证会永久失败；绑定只在自封闭起恰好发生过一次封闭准入下的 Home 窗口时才接纳这个新区间。
9. `Robot._sending` 和 `Robot._step_notes` 是 `step()` 与 Home 共用的实例字段。目前只有编排器线程一个调用者；机器人适配器不支持两个并发调用者，也没有对此加保护。
10. Home 期间崩溃时，恢复把运行锁定为 `FAULT_LOCKED`（`recovery_ambiguous`），该片段保留唯一的一份结果，`robot_home` 仍是 `not_attempted`，与旧路径一致，不会改写为 `failed`；未完成的 Home 事务可在日志中看到。
11. 停止在受闸 Home 发出第一条命令之前撤销了 Home 令牌，结果是 `no`，不会锁存机器人，所以停止之后人工的 `PREFLIGHT` 可以通过；命令已发出之后再撤销则是 `unknown`，会锁存且不重试。旧的 `Robot.home()` 对任何撤销仍然锁存。窗口打开期间再次调用 `home_under_closed_gate`（无论是否同一线程）会在任何其他检查之前被拒绝，不改变任何状态（不做围栏检查，不读健康状态）。Home 是否已发出命令，按“通过围栏的 POST 计数”判断，而不是 `_sending` 标志（别的线程调用 `step()` 会清掉它）。
12. 需要知道的行为变化：停止在第一条命令之前撤销令牌后，机器人不再被锁定，所以人工恢复之后手臂可能不在 Home 位姿。`robot.start_pose()` 只在 `fr3_run.py` 调用一次，`VERIFY_INITIAL` 不会重查，因此这依赖人工的 `environment_handled` 声明。令牌过期或被撤销、没有登记操作员停止、且按机器人自己的计数在第一条命令之前就被拒时，现在进入等待（`home_not_executed`，见下文恢复钩子一节），不再锁定；发出过命令之后仍记 `home_failed` 并锁定。这种等待里机械臂没有回 Home，仍停在上一片段结束的位置。所以续跑时会用 `Robot.start_pose`（启动关口的检查和容差）只读检查一次起始位姿，不在 Home 就回到等待（`home_pose_not_reached`）；终端提示按 `setup.md` 手动回 Home。
13. `Robot.stop_admission()` 本身不是异常安全的（先改模式和代次，最后才读时钟），是既有代码，未改动。Home-only 窗口关闭时如果读不到时钟，闸门以“没有关闭时间”的状态关闭，因此无法在这一区间上建立见证。窗口即使被打断（例如 `KeyboardInterrupt`）也会先关闭闸门并清理状态，之后才重新抛出该中断，此时 `hold()` 同样会执行。非 `Exception` 的中断会使清理重试，最多三次；清理中的普通 `Exception` 不重试，因此调用方的 `hold()` 一定会执行到。打开窗口时会清除 `closed_from`，所以窗口内 `stop_admission()` 读时钟失败时不会留下窗口之前的关闭时间（而是没有时间），离开窗口时再写入当前时间；见证不可能从 Home 命令之前开始。重试用尽后闸门保持拒绝策略命令（fail-closed）：窗口标志仍在，`reopen_admission()` 被拒，再次调用 `home_under_closed_gate` 也被拒，只能重启进程恢复。
14. 上述时钟失败之后闸门已经是 `closed`，`stop_admission()` 因此是空操作，无法开始新的区间：关闭时间保持 `None`，必须先 `reopen_admission()` 再 `stop_admission()` 才能得到新区间并建立见证。这是当前行为，由测试固定；改变规则需要作者决定。
15. `self._motion_posts += 1` 不在准入锁内。并发的 `step()` 可能使计数落后，或使 Home 被误判为“已发出命令”而实际是那个 step 发的（偏保守的方向：调用结果是 `unknown` 并锁定，不会误报干净的 `no`）。

确认成功按上文封存并回位。明确未成功且实际动作数不足上限时，重新取观测并暂存，重新认领新代次的策略；`inference_resumed` 是一个新的 `policy_steps` 日志事务，只从它签发新的运动令牌。旧缓存、旧请求和旧代次的迟到应答不能复用。判定等待不计动作；达到上限后在保持状态做终局判断，再像确认成功一样封存并回位（复位片段仍封存后等待人）。停止注册立即撤销新模式的运动围栏和模型结果权限，不等模型完成或后台排空；证明不足仍保留后台所有权。

证明 hook 抛异常或异常逃出编排循环后，注入周期永久标记失败，立即撤销当前请求的结果权限，不等后台排空。所有者和退出证明不足的请求槽继续保留。人工恢复可以整理环境，但不能复用这个失败周期或恢复旧模型权限；需要新的冻结运行或重新建立真实权限。已准备的暂停/恢复事务先记录未知应答并中止，再进入故障状态。旧 C2 协议的分时门中，两种判定状态保守映射为 `running`，其中 `RESET_JUDGING` 写 reset 角色文件；后台标注继续关闭，判定由原角色权限内核执行。这里的旧状态值表达判定期间的关门。

注入式 `MeasuredPhysicalHold` 观察器在完整冻结的 `SettleConfig` 窗口内采集新鲜 `Robot.observe` XYZ／Euler 姿态和独立实测夹爪位置。任意两次采样都必须满足位置、跨 ±π 归一化的 Euler 旋转及夹爪容差；采样起点和测量时间的间隔不能超过冻结值。每次采样以及最终发布前均核对单调的 `getstate/gripper_pos` route／generation／读取时间来源、与实测机器人状态相等的值、非布尔有限数值、时钟身份，以及整个窗口持续关闭的策略入口和运动权限。`AdmissionClosedProof` 回调是明确的注入合同，构造一个声明对象本身不构成证明。缺失、过时、中断或异常的观测会永久拒绝该观察器。现有 FR3 源码暴露实测夹爪来源，但实际 `cartesian_position` 仍需要另行审查的姿态 route 与持续关闭绑定；该软件适配器没有安装这种真实绑定。

**准入闸与 `AdmissionClosedProof` 来源（仅软件）。** `fr3_bridge.Robot.stop_admission()` 关闭可选的本地闸：此后所有 `pose`、`clearerr`、`open_gripper`、`close_gripper`、`close_gripper_slow`、`move_gripper` 的 POST 都在发出 HTTP 之前以 `AdmissionRefused` 拒绝；`Robot.step` 返回 `executed="no"` 且不锁存，`Robot.home` 在尚未发出任何命令时同样返回 `no`。姿态（含 `clearerr`）与夹爪命令各有单调递增的尝试数和闸拒绝数；`admission_snapshot()` 原子读取闸状态、代次和计数，`reopen_admission()` 重新打开闸并改变代次。闸默认打开，旧路径不变。`adapters/physical_hold.py` 的 `RobotAdmissionWitness`／`assemble_measured_hold` 只有在以下条件全部成立时才签发 `AdmissionClosedProof`：闸仍是 `stop_admission` 所返回标记对应的那一次关闭（从未重开，重开后再关也不行）、已放行命令计数与标记相同、`MotionFence` 当前没有令牌、时钟域一致且时间不倒退、片段与策略 epoch 不变。任何一项不满足即抛出 `InterventionUnproven` 并保持失败。有效期和全部 `SettleConfig` 限值都由调用方注入，没有默认值。限制：计数记录的是命令通过闸的开始，因此该见证只证明没有新命令开始；不能证明最后一个已放行的动作已经完成或机械臂已静止。`Robot.hold()` 只设置标志，`Robot.observe()["observed_ns"]` 是主机读取完成的时间，不是物理采集时间。`MotionFence` 不记录吊销时间，其吊销状态只是时间点事实。静止只能由 `MeasuredPhysicalHold` 的实测窗口证明。未使用也未审查任何真机、端口或绑定。

**Home-only 准入（仅软件）。** 闸门有三种模式：`open`（默认）、`closed`（`stop_admission()`）和 `home_only`，后者只存在于 `Robot.home_under_closed_gate(*, token)` 内部。进入需要：模式为 `closed`；令牌是 `MotionFence` 接受且为当前令牌的 `kind="home"` 令牌；没有停止、没有锁存、健康为绿。否则在发送任何 HTTP 之前返回 `executed="no"`，也不锁存；闸门开着同样被拒。窗口内只放行 `pose`、`clearerr`、`open_gripper`，并且只来自这次 Home 自己的发送路径、持该令牌、且来自打开窗口的那个线程（本进程内其他线程的命令一律被拒并计数）；`Robot.step`（策略动作）、`close_gripper`、`close_gripper_slow`、`move_gripper` 和恢复类路由一律被拒，旧的 `Robot.home()` 在闸门关闭时仍然返回 `no`。窗口的命令单独计数（`home_pose_released`、`home_gripper_released`、`home_gate_rejected`），不影响 pose／gripper 计数；`AdmissionSnapshot` 增加 `mode` 和 `home_only_from_ns`，窗口内 `closed` 为假。只要 `home_under_closed_gate` 调用还在进行（即使窗口内先调用过 `stop_admission()`），`reopen_admission()` 就被拒；窗口内调用 `stop_admission()` 立即关闭闸门。无论成功、失败、异常或停止，`finally` 都把闸门恢复为 `closed`：若此时模式不是 `closed`，则强制关闭为新的区间，并换新代次和新的 `closed_from_ns`，再调用 `hold()`。进入和离开各改变一次代次，所以窗口之前建立的 `RobotAdmissionWitness` 永久失败；见证还检查 `mode == "closed"` 和 Home 计数。Home 之后场景评估所需的关闭证明，必须从新的 `stop_admission()` 标记开始。`MotionFence`、开启或关闭状态下的 `stop_admission`，以及 `legacy_async` 路径均不变。

**实现边界：**当前只提供可注入的软件协调核和进程内 fake 回归。实际 FR3 工厂没有提供 owned policy 群组、真实后台 barrier、独立物理稳定 observer，以及暂停帧与 FileEvents/OnlineVerifier 事件窗口的可信绑定；缺少任一接口，构造编排器前就拒绝新模式。`Robot.held`、`Policy.ticket.done`、HTTP 成功或 client 返回不是这些证明。真实 paused 帧必须进入当前片段的保留证据窗口，不能复用触发前画面。实际配置、HTTP 真机启动、GPU 模型和机器人均未启用或验证；真实接线和跨分支共用内核组合回归仍需独立验收。

注入式 `PausedFrames` 适配器复用 `EventObserver` 和原有事件 `Ledger`，不建立另一份帧历史，也不提交 HTTP。暂停事务提交后，`stage_paused` 仅以时间和序号严格更新的观测替换当前动作步端点。恢复前的新观测仍属于暂停；恢复事务提交并预留新的策略代次后，`stage_resumed` 在同一步替换为新的动作前观测。等待不增加动作步。每次刷新仍保留片段起始帧、连续窗口校验及原有 16 MiB JPEG 总字节上限；已冻结的请求字节和来源记录保持不变。`Media.use_event_source(..., stage_adapter=...)` 必须显式注入，证明异常进入原闭锁流程；默认保留旧路径行为。暂停证据冻结前核对注入的实际日志、时钟、片段、角色及已撤销的动作围栏，生成供已获授权 worker 使用的 CPU 请求。这是离线软件合同；适配器尚未安装真实相机／物理来源绑定及借用 worker 授权的接线，FR3 新模式继续拒绝。

保持采样把目标节奏安排在冻结的最大采样间隔内，给时钟、关闭观察器、测量和睡眠开销留出余量。实际采样起点和测量时间戳的间隔仍必须分别满足原上限；截止时间、完整稳定窗口、任意两采样的容差及最多1000次观测均严格保留。内部节奏使用精确整数运算，让长窗口能够在有界采样数内覆盖；不保证任意慢I/O或没有证明开销余量的窗口都能成功。`SettleConfig` 接受的配置仍可能不可达：采样间隔1 ms、稳定窗口及最长等待999.5 ms时，1000次观测最多只有999个间隔，只能覆盖999 ms。本观察器明确拒绝这一边界，不修改冻结字段或计划摘要。真实FR3入口继续拒绝。

**FR3 介入钩子（切片 D，仅软件）。** `levi/automatic/fr3_intervention.py` 把 FR3 对象放到 `InferenceCycle` 的七个 `InterventionHooks` 后面；它不增加调度器或权限。`Fr3InterventionHooks(...).as_hooks()` 返回该数据类；`bindings()` 列出每个钩子绑定到什么。

| 钩子 | 绑定到 | 何时失败（抛 `InterventionUnproven`） |
| --- | --- | --- |
| `stop_admission` | `Robot.stop_admission()`（封闭标记）、旧代次的 `Policy.quiesce`、唯一客户端证明 | 标记不是本时钟下的封闭快照、仍有运动令牌、旧代次未被隔离、证明失败、参数不对。闸门先关（在检查参数之前），失败后也保持关闭 |
| `resume_admission` | 只用 `Robot.reopen_admission_if(generation, closed_from_ns)` | 有角色请求占着槽、证明失败、闸门被别人关着、区间内放行过策略命令、区间不是本次介入关的那个（重开又关；或自封闭起不是恰好一次封闭准入下的 Home 窗口）、观测早于区间、run 不同。封闭准入下的 Home 一条命令都没发出时改抛 `InterventionStopped`（不闭锁，见下） |
| `observe_hold` | `MeasuredPhysicalHold` 加 `RobotAdmissionWitness`，每次封闭一个观测器（`hold_factory`；真实的由 `measured_hold_factory` 构造） | 观测器失败或没返回 `PhysicalHoldProof`、保持超过截止时间。闸门开着且没有封闭时，先关闸再保持；不属于本次介入的封闭一律拒绝 |
| `observe_runtime` | `runtime_probe`（可信 `RuntimeObservation`）、唯一客户端证明 | 观测不属于 `RuntimeOwnership` 运行时令牌的租约和 worker、探针失败 |
| `role_window` | 给 `RuntimeOwnership.begin_role_request` 的 `RoleWindow` | 角色未知、`judge` 在开着的闸门上或 `forward`/`reset` 在关着的闸门上、窗口超出租约、证明失败 |
| `stop_proof` | `stop_probe` 与槽中那个请求的 `RequestStopProof` | 请求不是槽里的、探针答复不恰好是 `(True, True)`（绝不返回假标志）、超过截止时间 |
| `prepare_evidence` | `PausedFrames.prepare_evidence`（`signal_events` 账本） | 返回不是 `True` |

`physical_hold` 是 `termination.physical_hold` 的限值加 `MeasuredPhysicalHold`／`RobotAdmissionWitness` 这一对；`signal_events` 是 `SignalEvents` 经 `event_window.Ledger` 进入 `PausedFrames`。

`resume_admission` 是特定重开。`Robot.reopen_admission_if` 只在闸门仍是按代次和关闭时间指名的那个封闭区间时才重开；绑定从不调用泛化的 `reopen_admission()`（测试让它抛异常来保证）。没有被本次介入关闭任何东西、闸门开着时，只是允许开始。封闭准入下的 Home 之后闸门是新区间（代次 +2）；只有自封闭起确实打开过 Home-only 窗口（`home_only_from_ns` 不早于封闭时间）、Home 的 pose 计数增加且代次恰好移动 2，绑定才跟进。Home 在发出首条命令前被拒（executed=no，臂没动；2026-10-14 21:1x 决定）时，只凭桥自己的 Home-only 计数器判断：代次恰好 +2，`home_pose_released`、`home_gripper_released` 都没变，且自封闭起确实打开过 Home-only 窗口（先用通用重开再 stop 同样是 +2、没有 Home POST，但没有窗口，照旧锁存）。此时 `resume_admission` 和 `observe_hold` 抛 `InterventionStopped`：绑定不闭锁、不跟进、不重开，闸门保持关闭。其余任何差异都闭锁，包括窗口放行了夹爪命令却没有 pose 的情形。绑定的区间不变，所以经同一绑定的 resume 仍以同样方式被拒，直到绑定接管新区间（`adopt_unexecuted_home`，见下）或开新 run。编排器（L2-HOME-EXECNO-ORCH）按同样的事实处理：有操作员停止时照旧进入等待（`ROBOT_HOME -> WAIT_HUMAN`，`operator_stop`）；没有操作员停止时，若机器人的 Home 确实被调用并返回 executed=no，且机器人自己的 Home-only 计数（`home_pose_released`、`home_gripper_released`、`home_gate_rejected`，Home 前后各读一次）都是可读的整数且没有变化、闸门已重新关闭、没有故障闩锁、红灯或机器人停止/信号（`Robot.stopped()`）、介入周期未闭锁且没有占用中的请求，也进入等待（`ROBOT_HOME -> WAIT_HUMAN`，原因 `home_not_executed`，终端显示“Home 在发出首条命令前被拒：机械臂没有回 Home，仍停在上一片段结束的位置”，并提示续跑前会自动检查是否在 Home，请按 `setup.md` 手动回 Home，然后按 Enter 再按 `y`），拒绝原因记在 note `home_failed_detail`。等待期间没有任何东西重开闸门。操作员续跑后，第一次用到钩子时（`VERIFY_INITIAL` 的场景核对，或下一片段开始）先请绑定接管闸门当前的封闭区间（`adopt_unexecuted_home`，FR3 绑定的额外入口，不是第八个钩子）：只接受未变的区间，或那一个没有发出命令的 Home 窗口，并且 pose、gripper 放行计数都等于封闭点的值；否则关闸并闭锁。随后场景保持或 `resume_admission` 跟进接管的区间。绑定没有这个入口、抛异常或返回 `True` 以外的任何值，运行都以 `FAULT_LOCKED` 锁定，闸门保持关闭。片段开始处的重建在当前流程里走不到（每次开始都在场景核对之后，场景核对已先重建），由直接测试覆盖。在这之前，这种等待之后的第一次 `PREFLIGHT` 会用 `Robot.start_pose` 只读检查一次起始位姿（与 `fr3_run` 启动关口相同的检查和容差：绑定的 Home 位姿、Home 的位置与旋转容差、测得夹爪张开；不发任何命令），测量结果记在 note `start_pose_checked`。只有 `ok` 恰好为 `True` 才继续（`VERIFY_INITIAL`）；不在 Home、读不到位姿、抛异常、机器人没有 `start_pose` 或其他任何返回值，都走 `PREFLIGHT -> WAIT_HUMAN`，原因 `home_pose_not_reached`（终端显示“机械臂不在 Home 位姿（误差见下方 start_pose_checked），未续跑”，按 Enter 再按 `y`），下次续跑再查一次。重启之后（重启先锁定，`recovery_ambiguous`），只要最后一次提交进入 `WAIT_HUMAN` 的原因是 `home_not_executed` 或 `home_pose_not_reached`，且之后没有开始过片段，`Orchestrator.restore` 就把两项重新置上，所以续跑时仍会检查位姿并重建区间。计数增加、结果不明、机器人闩锁，或计数读不到、前后不一致，一律按已执行处理：`home_failed`，`FAULT_LOCKED`，与原来相同。重建标记和位姿检查在重启后从日志恢复（见下）。

探针。`runtime_probe` 和 `stop_probe` 在 daemon 线程里调用；该线程不持有绑定、机器人或内核的任何锁，也不向机器人发任何东西。探针在 `probe_wait_s`（默认 `budget_ns`；`stop_probe` 最多到它的截止时间）内没有返回，绑定闭锁；操作员停止会立即结束等待并抛 `InterventionStopped`（不闭锁），所以编排器循环和停止路径不会被挂起的探针阻塞。Python 线程无法强杀：迟到的答复落进每次调用独有、没人再读的一次性槽，不改变任何状态。绑定按钩子记住它放弃的探针线程；同一钩子下次调用时若该线程仍在运行，无论是否已过等待时限，本次调用都被拒绝并闭锁，不再启动新的探针（过了时限即判为挂住，决定 4）。该线程结束后，下次调用才运行自己的探针。

失败方向。钩子抛异常、超出时间上限（`budget_ns` 或它自己的截止时间）、或其事实未被证明，都会先调用 `robot.stop_admission()`（所以任何看到闸门开着的拒绝都会把它关上；闸门已关时没有影响），再让该绑定闭锁：之后每个钩子都会再关一次闸并抛异常。若 `stop_admission` 本身失败（包括 `BaseException`），抛出的仍是原来的拒绝，文本末尾写明 “the gate could not be closed: <类型>”。`InferenceCycle` 把它变成不可逆闭锁，编排器进入 `FAULT_LOCKED`、撤销围栏并保持机械臂，不再发出任何步（每个钩子各有一个测试，另有内核拒绝窗口、超出时间上限、运行中失去证明三个测试）。操作员停止（`InterventionStopped`）不闭锁；停止期间 `stop_admission` 仍然关闸，且没有任何东西会把它重开。

唯一客户端（G1），用计数比对。账号与他人共用，所以守卫从不查看别人的进程。`N` 是内核全局表 `/proc/net/tcp{,6}` 里连到策略端口的连接数；`M` 是其中客户端一端被本次运行自己的进程（本进程及其子进程，只读它们的 `/proc/<pid>/fd`）打开的个数。`N != M` 即有外来客户端，证明失败。另外要求：端口不是共享端口（8000、5000、5001、5100、7470、8100 和 LEVI 的端口在构造时即被拒绝）；每个监听都只绑定回环；监听套接字和每个已接受的套接字都在本次运行启动的单元（`policy_swap` 单元，读该单元的 `cgroup.procs`）的进程里打开；每个连接都是回环连接且有本地客户端一端。只统计对端是回环地址的客户端一端（连到别的主机上同号端口的连接不算客户端）；孤儿套接字（最后一个描述符关闭后留下、inode 为 0、处于 FIN_WAIT1、FIN_WAIT2、LAST_ACK 或 CLOSING）既不计数，也不当作“单元之外的服务端一端”，所以任一方正常关闭连接都不会让证明失败（FIN_WAIT1、CLOSING 的孤儿只在发送队列为空、`tx_queue` 为 0 时才排除，队列里还有数据就计数）；`tcp6` 中的 IPv4 映射地址按 IPv4 比较。单元名必须是 `levi-aeri-policy-<name>`（`policy_swap` 的命名）；从会话 scope 启动时找不到单元的 `cgroup.procs`，证明会写明原因并要求传入 `unit_procs_file`。不列 `/proc`，也不打开其他任何 `/proc/<pid>/`（测试记录它触碰的每个 pid）。读不到表、`cgroup.procs` 或本方 pid，证明失败。`SlotBoundPolicy` 包装策略，使 `request` 只在证明成立、且 `RuntimeOwnership` 槽里有本请求自己角色、片段和代次的、已授权未撤销的请求时运行。`stop_admission`、`observe_runtime`、`role_window`、`stop_proof` 都会重复该证明。这是时间点证明：两次检查之间连上来的进程由下一次检查发现，而不是被阻止；它也不证明服务端行为正确。手工启动的服务无法被证明是私有的。已知限制：守卫不核对单元的 `InvocationID`，所以无法区分在本次运行之外重启的同名单元；这需要 `policy_swap` 交出它启动的那次 invocation。

`--check --hooks` 保留为独立开关（裸 `--check` 的输出逐字节是既有契约，不变）。没有封闭时，`observe_hold` 在闸门开着的情况下先关闸再保持（第一次场景判定发生在 `resume_admission` 之前，更早无法证明封闭；这是安全方向）。`fr3_run.py --check --hooks` 先以 JSON 打印这份报告（`hooks_report`：七个钩子的 `implemented`、`bound_to`、各自需要什么以及作业／binding 是否声明，另有 `physical_hold`、`signal_events`、`sole_client`、`complete`、`real_entry`）。它不连接任何东西。真实入口不变：`load_binding` 仍拒绝 `event_pause_judge`，所以对事件模式作业运行 `--check --hooks` 会先打印报告，再以退出码 2 给出拒绝。钩子只在测试里装配，用 `GateFakeRobot`（带闸门记录的 fake）。

`InferenceCycle.invoke` 必须显式提供且仅提供一个执行入口：兼容的无参数 `call()`，或 `request_call(request)`。forward、reset和judge角色在入口缺失、重复或不可调用时，均在模型授权前拒绝。原 `RuntimeOwnership.begin_role_request` 授权后，`request_call` 收到它直接返回的那个 `RequestToken` 对象，包含角色、片段和策略代次；回调只能借用该请求，不能再次begin或finish。Cycle仍独自负责begin、停止证明和finish。回调异常（包括 `TypeError`）沿原不可逆闭锁撤销结果发布，保留尚无真实停止证明的原请求槽，不做签名猜测或重试。这个接口为已审查owned worker的CPU请求分发准备接线，不安装SDK worker、transport或真实FR3绑定；真实入口继续关闭。

可选的显式 `completion_adapter(request, returned_completion)` 只能与 `request_call` 同时使用，非法组合在授权前拒绝。它必须返回冻结的 `CompletionEvidence(request, result, stop_proof, observation)`，收据及停止证明均绑定本轮授权的原请求对象。Cycle独自把证明交给原权限内核，最终核对租约、角色、完整组、时钟及新鲜空闲观测后才发布结果。不提供适配器时保留原证明hook。回调或适配器异常沿原闭锁保留已撤销的请求槽，不接受tuple、bool、隐式识别或失败回退。SDK ACK来源由注入的借用执行器检查，不能从HTTP或客户端结束推断。

显式注入 `Orchestrator(owned_calls=OwnedCalls(...))` 后，场景、暂停目标与策略chunk均经 `request_call` 借用同一原Cycle权限、runtime和租约。必填CPU准备回调返回不可变 `PreparedCall` bytes，包含原AERI请求、观测来源时间、片段/角色/代次/时钟及payload哈希。场景在保持与授权后采集新帧，分发前保留JPEG证据；目标准备必须使用现有 `PausedFrames.freeze` 窗口；策略明确接收当轮刚stage的新观测，不暗取后来覆盖的 `robot.raw`。借用执行器检查SDK ACK来源。CPU解码只在Cycle完成后运行，输出场景/目标bytes或chunk dict，再交原AERI解析、仲裁、日志和动作检查。显式注入后的绑定、准备、完成或codec错误均不回退旧provider或HTTP；不注入时保留原路径。生产冻结prompt/spec、场景/暂停窗口/策略codec及SDK factories尚未安装，注入合同和CPU fake不构成真实硬件证明；真实FR3入口继续关闭。

`levi/automatic/adapters/vlm_codec.py` 提供 judge 角色的场景与暂停目标回调（`VlmCodec.prepare_scene/prepare_goal/decode_scene/decode_goal`），与外部 C5 和文件 provider 共用同一份冻结合同：`provider_model` 的 `FrozenSpec`、`question`、`model_result` 和 `validate_result`。冻结规格、时钟（本机时钟域）、场景视角、图像最长边、JPEG 质量和 `PausedFrames.freeze` 回调全部由调用方注入，没有默认值，缺失或非法在构造时拒绝（并重新校验规格哈希与规范形式）。场景帧由刚观测的 BGR 数组缩小（不放大）后编码为 JPEG；目标帧取自保留的暂停窗口图片，并对照冻结证明哈希与暂停绑定（run、片段、角色、策略代次、时钟域、提交时间）。跨 IPC 的调用是一个 `bytes` 信封（规范 JSON 头加 JPEG 字节，worker 内工厂用 `unpack_payload` 读取），`PreparedCall` 携带原请求、`SourceBinding` 与保留帧。解码时从 payload 重新推导问题、schema、规格哈希、请求身份、帧和来源，只接受一条文本答案，并只返回 `model_result` 与 `validate_result` 都通过的结果：读不出的谓词保持 `unknown`（`model_undecided`）；字段缺失、多余或格式错误，规格、模型、观测时间或帧不符，超过截止时间或观测过旧，一律抛 `InterventionUnproven`，不会折叠为成功或失败。编解码器无状态、不写文件、不记日志，自动判定只返回给 orchestrator。冻结的复位规格只判复位目标，前向目标会被拒绝。前向判定必须显式启用：`provider_model.freeze_forward(job, settings)` 消费真实 `cli.load_job` 返回值，在同一 `FrozenSpec` 类型中生成 `levi.aeri.forward_goal_model_spec.v1`。规格冻结完整作业计划及其准确摘要、前向任务原文，并将操作员的 `task.success_condition` 原文作为唯一必需谓词 `task_success_condition`。来源缺失或空白会拒绝，不用 instruction、策略文件夹名或 ISC 谓词代填成功条件。显式 `forward_final`、`forward_active` 期限必须同时等于冻结计划和实际 `RunConfig` 的对应限制。此规格只接受当前 forward 片段的 `forward_goal`，拒绝 scene 和 reset 请求；保留既有暂停证据、原 owned 请求、模型／帧／时间检查及三值答案。编解码器只提供判定，不生成状态迁移、操作员标签或物理证明。本模块仅 CPU，并用 fake 执行器与 worker 验证：真实 worker 内 VLM 工厂、真实冻结规格和物理绑定尚未安装，真实 FR3 入口继续关闭。

暂停后的目标判定取帧只能在暂停事务提交后开始。观测时间戳必须是整数，并落在调用 Robot.observe 前后读取的原运行时钟区间内；等于调用前时间合法。旧帧、完整回放旧观测、未来时间、缺时间或布尔值均在 recorder 入库、证据准备及判定授权前拒绝。owned 目标来源以独立记录的取帧开始时间为下界，不能用帧自身时间证明自己新鲜。

## 新鲜采集源合同（`adapters/fresh_capture.py`）

`fr3_bridge.Robot.observe` 的图像来自 `env.get_observation()`。其下的读取线程只缓存最新一帧，`read()` 不返回帧号、硬件时间戳或到达时间，所以本地读完时间或图片哈希不能证明图像是授权之后才采集的。`levi.automatic.adapters.fresh_capture` 规定采集源必须提供什么才能证明，并拒绝其余一切。它不 import 任何相机 SDK，不打开设备，不使用机器人端口；`fr3_run` 目前不使用它。

- `CapturedFrame`：相机 id（serial）、单调 `frame_number`、整数 `device_timestamp_ns`（None 表示没有）、`timestamp_domain`、`host_clock_domain`、`host_arrival_ns`（帧到达进程时的主机单调时钟）、`image_sha256` 和可选 `image_bytes`（必须与哈希一致）。
- `FreshCaptureBinding`：相机 serial 集合、主机时钟域、时间戳域、时间戳语义（例如曝光开始）、`max_latency_ns`、`max_frame_interval_ns`、`min_frames_per_camera` 和 `DeviceClockAlignment`（偏移、偏移不确定度、ppm 漂移界、参考设备时刻）。所有字段由调用方注入、没有默认值；字段缺失、非法或未声明对齐，构造时即拒绝。
- `FreshCaptureVerifier(binding).verify(grant_ns, host_clock_domain, frames, previous=None)` 返回 `FreshCaptureProof` 或抛 `FreshCaptureUnproven`。拒绝的情形：缺少或非整数的设备时间戳；时间戳域或主机时钟域与 binding 不同；缺相机或出现未绑定相机；帧数不足；帧号或设备时间戳不递增；帧间隔超过 `max_frame_interval_ns`；帧在授权之前到达；最早可能采集时刻（`device_ns + offset - uncertainty`）早于授权；采集时刻晚于到达；延迟超过 `max_latency_ns`；图像哈希与上一帧相同（停滞）。`previous` 是每个相机授权前最后一帧的可选基线，只用于序列检查。证明对象按相机列出帧号、设备时间戳、到达时间和图像哈希，并带 binding 哈希。
- `FakeCamera` 是测试用的确定性内存相机，不证明任何硬件事实。

RealSense 映射（只是对照表，这里不 import `pyrealsense2`）：

| `CapturedFrame` 字段 | RealSense 来源 | 说明 |
| --- | --- | --- |
| `camera_id` | `device.get_info(camera_info.serial_number)` | 不用 USB 路径或枚举序号 |
| `frame_number` | `frame.get_frame_number()` | 每路流各自计数；跳号表示丢帧；管线重启会归零，重启后须重建基线 |
| `device_timestamp_ns` | `round(frame.get_timestamp() * 1e6)` | `get_timestamp()` 是毫秒浮点数；拿不到则为 None |
| `timestamp_domain` | `frame.get_frame_timestamp_domain()`（`hardware_clock`、`system_time`、`global_time`） | 与 serial 一起记录；只接受 binding 指定的域 |
| `host_arrival_ns` | 帧到达进程时读取的主机单调时钟 | 不是 `get_timestamp()`；必须用 `host_clock_domain` 指定的时钟 |
| `image_sha256` / `image_bytes` | `get_data()` 缓冲区的哈希 | |

必须在现场测量并人工审核，不能假设：每台相机的 serial 及其在机位中的位置；已部署固件和驱动实际报告哪个时间戳域、标记哪个时刻（曝光开始、曝光中点、读出）；设备时钟与主机单调时钟之间的偏移、不确定度和漂移界（用 `global_time` 时还要确认设备与主机同步已开启）；真实负载下的最大延迟和最大帧间隔；采集路径交付的是每一帧及其元数据，而不是缓存的最新帧。binding 必须先经人工审核才能使用；本模块只拒绝与声明相矛盾的情形，不验证声明本身。

`OwnedCalls` 的 forward（策略）编解码器在 `levi/automatic/adapters/forward_codec.py`：`ForwardCodec(ForwardCodecConfig(...))` 的 `.prepare_chunk` / `.decode_chunk`，以及 `make_completion_adapter(verify_ack=..., roles=...)`。仅用 CPU 与 NumPy，所有物理和模型参数都没有默认值：图像尺寸、状态和动作维度、horizon 上限、prompt、`model_version`、`action_contract` 与墙钟都必须由调用方注入，缺失或格式不对即拒绝。`prepare_chunk` 用当轮刚 stage 的观测构造与评测客户端 `_query_policy` 相同的 infer 输入（`view1`、`hand` 由 BGR 转 RGB，`observation/state` 取自 `robot_state.cartesian_position`，加 `prompt`），返回 `PreparedCall`：payload 为 bytes（worker 内用 `decode_infer_input` 还原字典），`SourceBinding` 的来源类型为 `policy`。它拒绝非 forward 片段、`obs_ns` 不是本次观测、状态新鲜度未被证明、图像尺寸或 dtype 不符、状态非有限值以及任何前缀条件。`decode_chunk` 按 `fr3_bridge.Policy.request` 的字段把 worker 结果转成 `chunk_response`，并拒绝 `unavailable`、形状错误、超过 horizon 上限、非数值/NaN/无穷的动作、`action_contract` 或 `model_version` 不一致，以及在请求截止时间或之后才到的答案。适配器只在结果对应的请求就是 `InferenceCycle.invoke` 传入的同一对象（身份而非相等）、注入的 `verify_ack` 接受 SDK ack、停止证明指向同一请求且结果为 CPU 数据时才返回 `CompletionEvidence`。这是用 fake worker 测过的软件合同；prompt、部署参数、真实 JAX/Torch worker、相机时间戳语义与 binding 审核均未安装也未证明，真实 FR3 入口继续关闭。


时间戳语义目前只支持准确值 `start_of_exposure`（曝光开始）。曝光结束、曝光中点、读出、到达或未知语义在创建绑定时拒绝；准入后的时间戳不能证明曝光在准入后才开始。其他语义需要另行审核能够计算曝光开始下界的映射，本契约尚未提供。

### 逆向子任务中明确请求人工复位

`subtask_loop` 终端的 **m** 请求停止自动逆向复位并等待人工处理。
当前反向 episode 的动作、策略停用、记录封存和 VLM 判定期间均可请求；
Enter 和 s/f/d 不触发此请求。请求绑定 run、轮次、episode 和 policy epoch；
已关闭或旧窗口不能停止下一 episode。同命令及完整参数重发只返回原接受结果，
不会再次登记停止。

这是普通停止，**不是急停**：已准入的 step 可以完成，其中可能包含多次机器人
POST；请求后不再准入下一 step。**接受请求不代表可以接近机械臂**。
后续沿用策略停止、记录封存和 `home_after_operator_stop` 配置规定的保持或回位；
安全守卫或确认失败会锁定运行。结果保持 `scene_reset=unknown`，不产生成功或失败
标签。人工处理场景并检查安全后，resume 放弃中断的本轮、进入 PREFLIGHT，仍需独立
放行下一轮。此入口不添加自动无进展判断，不修改重试策略，不自动清锁存，也不新增
运动路径。
