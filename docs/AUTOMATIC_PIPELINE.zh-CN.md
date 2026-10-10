# 自动测评流水线（AERI）

[English](AUTOMATIC_PIPELINE.md)

**状态：只在 Fake 上运行。** 本页写集成契约（`levi/domain/aeri.py`）、持久化事务日志（`levi/automatic/journal.py`）、状态机与编排器（`levi/automatic/state_machine.py`、`orchestrator.py`）、终止裁决器（`termination.py`）、复位仲裁（`scene_assessment.py`、`reset_manager.py`）、录制层（`recorder.py`）、指标（`metrics.py`）、命令行（`cli.py`）、提供方适配层（`levi/automatic/adapters/`）和进程内 Fake（`integrations/fr3_automatic/fake.py`）。真实机器人适配层和页面都还没有，命令行只能在 Fake 上试运行；这里的代码不会让机器人动、不会启动模型、不会开端口，在这里通过的测试都不算真机验证。

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

**一致性规则（节选）。** `confirmed` 至少要有一个 required 谓词，且全部为真，没有 confirmed 或 undecided 的否决项；`rejected` 要有一个为假的 required 谓词或一个 confirmed 否决项。场景只有在所有 required 谓词都为真时才是 `ready`；`failed_predicates` 和 `unknown_predicates` 必须恰好列出为假和读不出的 required 谓词。`valid_until_ns` 晚于 `produced_ns`，且最多晚 `MAX_RESULT_VALIDITY_MS`（30 秒）；租约最长 `MAX_LEASE_MS`（10 分钟）。片段 ID 的格式是 `<run_id>.<forward|reset>.<NNNN>`，必须属于消息所在的运行。策略端点只能是回环地址，且不能是 5000、5001、5100、7470、8000 端口。片段结果严格跟随目标核实：`verified` 是 `success`，`contradicted` 是 `failure`，`undecided` 和 `unavailable` 是 `unknown`（没判出来的永远不算失败）。判定里带了在线判定的原值（`legacy_c5`）时，必须与 decision 一致：未定的回答不论 outcome 是什么都是 `unknown` / `model_undecided`（带未决否决项、有争议的豁免或缺少输入的成功也算未定），已定的成功是 `confirmed`，已定的失败是 `rejected`。运行事件里没有操作员标签的字段，状态只能是 AERI 的 13 个状态。这些跨字段规则无法写进 JSON Schema，快照看不到它们的变化，由 `E_INCONSISTENT` 夹具把守。

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

正常路径：

```
PREFLIGHT -> VERIFY_INITIAL -> FORWARD_ACTIVE -> FORWARD_STOPPING -> FORWARD_FINALIZE
  -> ROBOT_HOME -> SCENE_ASSESS -> FORWARD_ACTIVE（下一片段）... -> COMPLETED
VERIFY_INITIAL / SCENE_ASSESS -> RESET_ACTIVE -> RESET_VERIFY -> RESET_FINALIZE
  -> VERIFY_INITIAL（先 Home，再重新检查初始状态）
```

`TRANSITIONS` 为每个允许的 `(from, to)` 列出原因、权限主体（`orchestrator`、`operator`、`safety_guard`、`recovery`）和该转换能带的唯一动作类型；`check_transition` 拒绝其余一切（`E_ILLEGAL`、`E_REASON`、`E_AUTHORITY`、`E_ACTION`、`E_STEP`、`E_EPISODE`、`E_RESULT`），`check_journal` 按同一张表重读整份日志。最要紧的规则：

- 运动状态（`FORWARD_ACTIVE`、`RESET_ACTIVE`）只能从场景判断（`VERIFY_INITIAL`、`SCENE_ASSESS`）由编排器以 `policy_steps` 动作进入；
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

永远不会导致停止的：`unknown`、`unavailable`、超时、属于其他运行或片段的判定、未请求或重复的应答、已过期的、来自其他时钟域或生成时间在未来的、证据没覆盖到稳定窗口的、违反契约的。这些一律丢弃并记备注，片段继续执行到步数上限（`on_unknown: continue_to_horizon`，v1 唯一的策略）。被丢弃的应答会撤回对应请求，裁决器不会无限等待；同一片段判定方违反契约达到 `violation_limit` 次后，本片段不再理会判定（也不做终局判定）；事件提供方违约同样次数后，本片段不再读它的事件；两种情况下运行都在下一个决策点等人（`contract_violation_limit`），优先于结束运行。迟到的旧事件不会把候选往前挪，确认必须覆盖候选（`observed_from_step` 不晚于候选）。

| `TerminationConfig` | 默认 | 含义 |
| --- | --- | --- |
| `allow_early_stop` | `true` | 为 `false` 时只记录确认，不据此停止 |
| `min_steps` | 10 | 此步之前不请求 |
| `settle_steps` | 8 | 证据要覆盖到候选之后这么多步 |
| `cooldown_steps` | 15 | 两次请求之间的步数 |
| `max_requests` | 6 | 每个片段的请求上限 |
| `confirmations` | 1 | 停止所需的连续确认次数 |
| `control_fraction` | 0.0 | 不提前终止的片段比例（按片段 ID 和 `control_seed` 确定），用来测量误提前终止 |
| `goal_event_types` | `object_settled` | 能成为候选的事件类型 |
| `violation_limit` | 3 | 违反契约多少次后不再理会判定 |

默认值没有在任何数据上标定过。

## 复位仲裁（`scene_assessment.py`、`reset_manager.py`）

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
| `metrics:early_termination` | 相同 | 相同 |
| `metrics:human_minutes_per_valid_episode` | 相同 | 相同 |
| `metrics:mode_source` | 相同 | 相同 |
| `metrics:mode_specific` | 相同 | 相同 |
| `metrics:note` | 相同 | 相同 |
| `metrics:reset` | 相同 | 不同：没有复位片段：复位次数和耗时恒为 0；跳过决策只统计前向开始 |
| `metrics:reset_mode` | 相同 | 相同 |
| `metrics:scene_check` | 相同 | 相同 |
| `metrics:scene_decisions_by_human` | 相同 | 相同 |
| `metrics:schema` | 相同 | 相同 |
| `metrics:time_per_valid_episode_ms` | 相同 | 相同 |
| `metrics:truth` | 相同 | 相同 |
| `metrics:truth_labels` | 相同 | 相同 |
| `metrics:turnaround` | 不同：只有复位策略放弃时才有 human_reset_ms | 不同：reset_policy_ms 恒为 0 |
| `transition:FAULT_LOCKED->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FAULT_LOCKED->PREFLIGHT` | 相同 | 相同 |
| `transition:FORWARD_ACTIVE->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FORWARD_ACTIVE->FORWARD_STOPPING` | 相同 | 相同 |
| `transition:FORWARD_FINALIZE->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FORWARD_FINALIZE->ROBOT_HOME` | 相同 | 相同 |
| `transition:FORWARD_STOPPING->FAULT_LOCKED` | 相同 | 相同 |
| `transition:FORWARD_STOPPING->FORWARD_FINALIZE` | 相同 | 相同 |
| `transition:PREFLIGHT->FAULT_LOCKED` | 相同 | 相同 |
| `transition:PREFLIGHT->VERIFY_INITIAL` | 相同 | 相同 |
| `transition:PREFLIGHT->WAIT_HUMAN` | 相同 | 相同 |
| `transition:RESET_ACTIVE->FAULT_LOCKED` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_ACTIVE->RESET_VERIFY` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_FINALIZE->FAULT_LOCKED` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_FINALIZE->VERIFY_INITIAL` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
| `transition:RESET_FINALIZE->WAIT_HUMAN` | 不同：只有该模式运行复位片段 | 不适用：不运行复位策略：场景不就绪时等人处理 |
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

**设置。** `LEVI_AERI_HOME`（默认 `~/.levi-aeri`，以 0700 创建）：核心密钥、机器人锁、运行索引、命令审计和已启动的试运行。`LEVI_AERI_JOB_ROOTS`：用 `:` 分隔的目录，API 只能从这些目录里选作业文件（命令行可以用任意路径）。

## Fake（`integrations/fr3_automatic/fake.py`）

`FakeClock`（只在推进时走）、`FakeRobot`（只凭围栏的令牌运动；可脚本注入：连续三次 503 后闩锁、连续六次状态过期后闩锁、位姿冻结、红灯、闩锁、丢失应答、命令发出后进程崩溃、Home 超出容差、相机停滞）、`FakePolicy`（在 Fake 时钟上固定延迟；超时、服务端错误、NaN、维度或代次错误、过早的 `valid_from`、acquire 或 quiesce 失败、服务端崩溃）和 `FakeRecorder`（带写线程；写失败在下一次 commit 或 seal 时报出，所有写入成功后才写 `.complete`，`abort` 得到 `incomplete_*`）。该模块不 import 任何网络、进程或 LEVI 代码。

**Fake 与真机的差别**（`fake.FIDELITY`）：没有动力学和停止距离；Home 除非脚本指定否则总在容差内（真实的 `_go_home` 不核对到位）；急停对 Fake 可见，而真机 FR3 的软件状态过期联锁看不到急停；闩锁计数只部分模仿 `Fr3Guard`；健康状态与命令同一步读取（真实 C3 文件 2 Hz 写入）；没有 GPU 争用和冷启动；`FakeRecorder` 不写文件（`RolloutRecorder` 写，但它默认的媒体写入器不写视频）；相机帧只是计数；场景和目标 Fake 按脚本作答（不能说明真实模型的准确率）；会话文件只在状态变化时重写，长时间等待期间没有 2 秒心跳；没有网络。**这些测试通过只证明状态机逻辑，绝不代表真机行为。**

**测试**（`tests/automatic/test_aeri_*.py`）：状态表与围栏；各个 Fake；C5 映射表与 C2 门控如实性；裁决器；编排器在设计的故障清单上的行为（错误成功、Unknown、判定超时或离线、事件抖动、复位阶段迟到的前向动作块、策略服务崩溃、相机停滞、没有策略资源、复位到达上限、Home 失败、急停或 FR3 故障、录制器写盘和封存失败、每个事务每个阶段的重启以及 SIGKILL、两次 Resume），以及 150 次带崩溃和恢复的固定种子随机运行。

**端到端**（`test_aeri_e2e.py`，用 `aeri_world.py` 的完整 Fake 环境：编排器、录制层、manifest、会话文件、初始状态契约）：N 轮“前向 -> Home -> 场景 -> 复位 -> 下一轮”，每个 rollout 都用 `levi.live.criteria` 读回；固定种子的运行两次写出相同字节（日志、rollout、manifest、会话文件，去掉墙钟时间和进程身份），换种子则不同；两轮运行里每个事务的每个阶段都让编排器崩溃一次（崩溃点逐一计数），再由新进程接管：结果总是 `FAULT_LOCKED`（或 `COMPLETED`），不重放任何动作，每次 Home 只发一次，磁盘与日志一致，两个会话文件都是 `fault`；每第九个崩溃点由操作员恢复并跑完，不复用任何目录；另有子进程在封存事务的每个阶段以及 `.complete` 前后被 SIGKILL。故障清单（Fake 能测的 12 行：错误成功、Unknown、判定超时、事件抖动、复位中迟到的前向动作块、策略服务崩溃、相机停滞、没有策略资源、复位到达上限、磁盘写满、重启、两次 Resume）对每一行断言最终状态、记录的降级原因，以及没有任何动作在无令牌时执行或尝试。

**后台实时标注兼容**（`test_aeri_live_compat.py`）：Fake AERI 运行写出的 rollout 和会话文件被真实的后台实时标注服务接收（它的控制器和 worker 以 `once` 模式运行，对接假模型服务）：前向片段被标注；用 `watch.exclude` 排除的复位目录不被标注（不排除时它会成为一个单独的数据集，所以应当排除）。`levi/live` 及其测试都没有改动。

**尚未实现：** 真实 FR3 适配层、真机运行命令、`/automatic` 页面；`atomic_skill_sequence` 和 `scripted_safe_reset`。


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
