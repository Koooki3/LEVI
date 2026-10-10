# 自动测评流水线（AERI）：契约与事务日志

[English](AUTOMATIC_PIPELINE.md)

**状态：只在 Fake 上运行。** 本页写集成契约（`levi/domain/aeri.py`）、持久化事务日志（`levi/automatic/journal.py`）、状态机与编排器（`levi/automatic/state_machine.py`、`orchestrator.py`）、终止裁决器（`termination.py`）、提供方适配层（`levi/automatic/adapters/`）和进程内 Fake（`integrations/fr3_automatic/fake.py`）。真实机器人适配层、命令和页面都还没有；这里的代码不会让机器人动、不会启动模型、不会开端口，在这里通过的测试都不算真机验证。

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
| `state_journal.jsonl` | 唯一事实来源：追加写的 `levi.aeri.run_event.v1` 行；第 0 行是运行头（计划 sha256、契约版本、LEVI 提交号）；每行用 `prev_sha256` 链到上一行 |
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

- **线程。** `run()` 只属于一个线程；`stop()` 和 `resume()` 可以来自任意线程。一把可重入的状态锁把每次“读状态、检查、prepare、commit”串行化，所以不会有事务从检查时以外的状态准备出来。`stop()` 从不等这把锁：它立刻把停止登记进一个登记表（有自己的小锁，控制循环每一步、每个片段开始前都会读）并返回 `stop_requested`，即使事务里的适配器调用很慢或挂死也一样；只有在 `WAIT_HUMAN` 中、并且 50 毫秒内拿到锁时，它才自己结束运行（`completed`）。`stop()` 只登记，不自己让机器人 hold（由循环的下一个安全点执行；适配器挂死时只能靠机器人侧看门狗和令牌过期）。停止一直保留到运行因它转到人工，届时所有已登记的停止一起被消费（第一个之外的编号记为 `stop_commands_merged`）。每次登记带代次：resume 只清除在它开始之前登记的停止（写成 `stop_command_lost` 备注）；resume 进行中到达的停止保留下来，在下一个决策点生效。
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
| `ready`，契约的每个必需谓词都读为真，且（`require_visible_evidence` 时）至少有 `min_evidence_refs` 个帧或片段证据引用 | `ready`：开始下一个前向片段 |
| `ready`，但漏了或没读出某个必需谓词 | `unknown`（记 `scene_missing_predicate`） |
| `ready`，但可见证据不够 | `unknown`（记 `scene_insufficient_evidence`） |
| 评估的是另一份契约 | `unavailable`（记 `scene_contract_mismatch`） |
| `reset_required`、`unknown`、unavailable | 原样 |

不配契约（`initial_state = None`，默认）时沿用提供方的结论，和以前一样。**契约文件格式是草案（HA-23）：** 下面是能承载流水线文档 §6.1 的最小格式，等用户确认。

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
```

用 `scene_assessment.load_contract(text)` 读取。读取器只接受严格的 YAML 子集（不新增依赖）：用空格缩进的块映射、标量列表（`- a` 或 `[a, b]`）、带引号或不带引号的标量、`true`/`false`/`null`、有限数字和 `#` 注释；拒绝制表符、锚点、别名、标签、块标量、流式映射、多文档、重复键以及未知键。以 `{` 开头的文档按 JSON 读。谓词名用场景提供方的名字：契约把 `(id, version)` 和这些名字登记给 `aeri.parse`。

**场景不是 ready 时怎么办**由复位策略决定（`RunConfig.reset_strategy`，`reset_manager.py`）：

| 策略 | 场景不是 ready |
| --- | --- |
| `single_reset_policy`（默认） | `reset_required` 运行复位策略；`on_scene_unknown = "reset"` 时 `unknown`/`unavailable` 也运行，否则转人工；两个前向片段之间最多 `max_reset_attempts` 次复位，之后转人工 |
| `human_assisted` | 一律转人工（不运行复位策略） |

`atomic_skill_sequence` 和 `scripted_safe_reset`（流水线文档 §6.4）在 v1 中被拒绝。`reset_manager.check_plan` 拒绝在 `ready` 以外的场景上开始前向片段的策略，`check_after` 拒绝在复位到达上限、被停止或失去策略之后还继续的策略。复位到达上限时先封存（保留失败的 rollout，结果为 `failure` / `horizon_exhausted`），再 Home，然后转人工；Home 失败则锁定运行（`home_failed`），在操作员恢复之前什么都不再动；恢复要经过 `PREFLIGHT` 和一次新的初始状态检查。同一个命令 ID 重复恢复只恢复一次。

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

`metrics.report(events, labels=, manifest=, termination=, max_steps=)` 读取运行日志（步数取自运行 manifest），给出三组指标；每个比率都是 `{"n", "of", "rate", "wilson95"}`，区间用后台实时标注服务统计模块的 Wilson 95% 区间（`levi.live.stats.wilson`）。探索阶段只有 20–30 个片段时，要看区间，不要只看比率。

| 组 | 指标 |
| --- | --- |
| 自动结论 | 前向片段数、结局计数、`autonomous_success_rate`（unknown 留在分母里）、停止原因 |
| 提前终止 | 提前停止（`goal_verified`）对照真值的混淆表；精确率（真成功的提前停止 / 提前停止）、召回率（提前停止 / 真成功的片段）、误提前终止率（真失败却提前停止 / 真失败的片段）、节省步数（`max_steps` 减去实际步数，对提前停止求和）、对照片段单列、自动结论与真值的一致率和误判成功数 |
| 复位 | 复位次数、`autonomous_reset_success_rate`、场景决策与跳过次数；跳过准确率（在真就绪的场景上跳过、或在真需要复位的场景上复位 / 有标签的决策）、错误跳过率、多余复位率、复位耗时 |
| 自动化 | 干预次数（进入 `WAIT_HUMAN` 或 `FAULT_LOCKED`）及原因、恢复次数、最长无干预的连续前向片段数、人工等待时间（仅在时钟域没变时计） |

**四类标签，互不混用**（流水线文档 §9.4）。`autonomous_verdict` 就是日志里的片段结果：不在别处写，也绝不称为真值（相应比率都叫 `autonomous_*`）。`posthoc_verdict`、`operator_label` 和 `adjudicated_ground_truth` 存在 `<run_dir>/labels/<kind>.jsonl`，每类一个只追加的文件（每行 fsync），用 `LabelStore.add(kind, episode_id, value, subject=, by=)` 写入。写一类标签从不改动别类的文件；同一类、同一片段、同一主题的第二个标签会被拒绝，除非写明 `supersede=True`，此时追加一行（第一行保留）。`by` 是不透明的主体 ID（不写姓名或邮箱）。主题有 `task_outcome`（`success`/`failure`）和 `initial_state`（`ready`/`reset_required`：开始这个片段之前场景是否需要复位）。比率用的真值：有裁定标签就用裁定标签，否则用操作员标签（`truth="adjudicated"` 只用裁定标签）；没有真值的片段计为 `unlabeled`，不进比率。

## Fake（`integrations/fr3_automatic/fake.py`）

`FakeClock`（只在推进时走）、`FakeRobot`（只凭围栏的令牌运动；可脚本注入：连续三次 503 后闩锁、连续六次状态过期后闩锁、位姿冻结、红灯、闩锁、丢失应答、命令发出后进程崩溃、Home 超出容差、相机停滞）、`FakePolicy`（在 Fake 时钟上固定延迟；超时、服务端错误、NaN、维度或代次错误、过早的 `valid_from`、acquire 或 quiesce 失败、服务端崩溃）和 `FakeRecorder`（带写线程；写失败在下一次 commit 或 seal 时报出，所有写入成功后才写 `.complete`，`abort` 得到 `incomplete_*`）。该模块不 import 任何网络、进程或 LEVI 代码。

**Fake 与真机的差别**（`fake.FIDELITY`）：没有动力学和停止距离；Home 除非脚本指定否则总在容差内（真实的 `_go_home` 不核对到位）；急停对 Fake 可见，而真机 FR3 的软件状态过期联锁看不到急停；闩锁计数只部分模仿 `Fr3Guard`；健康状态与命令同一步读取（真实 C3 文件 2 Hz 写入）；没有 GPU 争用和冷启动；`FakeRecorder` 不写文件（`RolloutRecorder` 写，但它默认的媒体写入器不写视频）；相机帧只是计数；没有网络。**这些测试通过只证明状态机逻辑，绝不代表真机行为。**

**测试**（`tests/automatic/test_aeri_*.py`）：状态表与围栏；各个 Fake；C5 映射表与 C2 门控如实性；裁决器；编排器在设计的故障清单上的行为（错误成功、Unknown、判定超时或离线、事件抖动、复位阶段迟到的前向动作块、策略服务崩溃、相机停滞、没有策略资源、复位到达上限、Home 失败、急停或 FR3 故障、录制器写盘和封存失败、每个事务每个阶段的重启以及 SIGKILL、两次 Resume），以及 150 次带崩溃和恢复的固定种子随机运行。

**端到端**（`test_aeri_e2e.py`，用 `aeri_world.py` 的完整 Fake 环境：编排器、录制层、manifest、会话文件、初始状态契约）：N 轮“前向 -> Home -> 场景 -> 复位 -> 下一轮”，每个 rollout 都用 `levi.live.criteria` 读回；固定种子的运行两次写出相同字节（日志、rollout、manifest、会话文件，去掉墙钟时间和进程身份），换种子则不同；两轮运行里每个事务的每个阶段都让编排器崩溃一次（崩溃点逐一计数），再由新进程接管：结果总是 `FAULT_LOCKED`（或 `COMPLETED`），不重放任何动作，每次 Home 只发一次，磁盘与日志一致，两个会话文件都是 `fault`；每第九个崩溃点由操作员恢复并跑完，不复用任何目录；另有子进程在封存事务的每个阶段以及 `.complete` 前后被 SIGKILL。故障清单（Fake 能测的 12 行：错误成功、Unknown、判定超时、事件抖动、复位中迟到的前向动作块、策略服务崩溃、相机停滞、没有策略资源、复位到达上限、磁盘写满、重启、两次 Resume）对每一行断言最终状态、记录的降级原因，以及没有任何动作在无令牌时执行或尝试。

**后台实时标注兼容**（`test_aeri_live_compat.py`）：Fake AERI 运行写出的 rollout 和会话文件被真实的后台实时标注服务接收（它的控制器和 worker 以 `once` 模式运行，对接假模型服务）：前向片段被标注；用 `watch.exclude` 排除的复位目录不被标注（不排除时它会成为一个单独的数据集，所以应当排除）。`levi/live` 及其测试都没有改动。

**尚未实现：** 真实 FR3 适配层、真机运行命令、`/automatic` 页面；`atomic_skill_sequence` 和 `scripted_safe_reset`。

