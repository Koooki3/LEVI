# AERI 原生经验落地：后续验收计划

[English](AERI_HARVEST_VALIDATION.md)

## 状态与范围

这是验收计划，不是测试报告。下列运行检查全部为 **未运行**。
用户已确认机械臂仍在使用：不得执行或排队测试、类型检查、schema 生成/检查、
构建、浏览器、服务、GPU 任务或机器人动作。静态审查、解析检查、已有测试函数，
均不能代替回归运行通过。

本计划核对的产品候选：`020007fee9e89543025df5617cf9ee615aeda0c8`。
比较基线：`bfdee2cf53365d8e0065b3f019e471213fe871fe`。
文档提交将是候选的后代；后续集成须冻结新的候选并更新证据清单。

本地协调来源：`LEVI/lab/levi-hub/programs/levi2/NATIVE-HARVEST-20261018.md`。
实验来源：`LEVI/lab/aeri-external/REPORT-20261018.md`、`EXPERIENCE.md`，
以及允许读取聚合内容的 `LEVI/lab/aeri-1017-user-session/harness-opt/CHANGES.md`。
下面的合成检查不需要真实会话、队列、留出答案、源数据或机器人端口。
实验中的模型判断不是人工标签；外部模式结果不能证明本地模型质量或物理安全。

## 执行前冻结证据

集成人记录完整候选 commit/tree SHA、基线、工作树干净状态、精确范围差异、
依赖锁文件摘要、环境/工具版本、测试选择器、资源租约、门禁实现版本及独立审查引用。
分支名必须解析成完整 SHA，不能以移动分支名作为证据身份。运行结束再次记录实际
检出版本；存在未提交源码或夹具变化时，不得把结果归给被冻结 SHA。

每项检查记录命令和参数、起止时间、原始日志位置与摘要、选择/收集/通过/失败/
错误/跳过数量、子命令退出码、包装器/门禁退出码及中断状态。跳过、未收集、
取消、超时、日志不完整、外层成功掩盖内层失败，都不是通过。核对必需选择器及
参数化分支，不能只看总数。失败尝试与原因保留。
源码、测试、schema 或依赖变化后，旧结果不能证明新候选；旧日志仅作历史证据。
重跑受影响检查并说明未受影响范围，才能作组合验收结论。

## 当前禁止解除后的执行门禁

遵循 `LEVI/lab/AGENTS.md`、hub `COORDINATION.md`、
`manual/robot-activity.md` 和当前 `heavy_gate.py`。
先取得用户关于机器人已结束使用的确认，再由登记的负责人核实既有稳定静默窗口
与资源租约；未知/忙碌不算静默。使用独立任务工作树；全量套件必须同时取得
`full-test` 和该工作树租约，同一时刻只跑一套。
不得缩短门禁、删除标记、绕过准入，或在仍禁止执行时提交自动重试队列。

heavy_gate 仅支持其允许的可重启作业，其文件头明确排除构建、服务和 GPU 作业；
这些必须单独授权和调度。中断尝试永远不算验收证据。检查门禁结果与记录的子命令
结果；后续 shell 命令成功不能抵消先前失败。
独立 hub 命令链候选是预防工具，不证明门禁通过或历史在途发布已完成。

## 定向 Python 验收矩阵

路径相对产品仓库。每个 `文件::函数` 都存在于冻结候选源码中。
这些是定位用例，不是穷举；定向通过后还应执行相关完整文件。以下全部 **未运行**。

| 范围 / 产品入口 | 已有测试定位 | 必须保持的不变量 |
| --- | --- | --- |
| 夹爪截止边界；`levi/automatic/fr3_bridge.py` | `tests/automatic/test_fr3_real_gripack.py::test_confirmation_uses_sample_completion_deadline`；同文件 `test_unsteady_sample_at_deadline_is_refused_without_another_poll` | 正确处理截止点合格采样；迟到/不稳定采样及停止/红灯仍拒绝，不悄悄改默认等待时间。 |
| 裁剪诊断；同桥 | `tests/automatic/test_fr3_bridge.py::test_clamp_refusal_preserves_diagnostics_without_motion`；`test_guard_diagnostics_survive_journal_note_budget` | 诊断失败或预算耗尽不得放行动作或解除锁存。 |
| 回位稳定和启动；`fr3_run.py`、`orchestrator.py` | `tests/automatic/test_fr3_real_home.py::test_home_at_target_cannot_succeed_with_concurrent_safety_failure`；`tests/automatic/test_startup_home.py::test_preflight_requires_fresh_confirmation_before_home`；`test_crash_at_home_transaction_never_replays` | 默认关闭的首轮回位需新鲜确认；崩溃/未知执行状态不重放动作；到位不能覆盖停止/红灯。 |
| 首轮回位到反向人工入口 | `tests/automatic/test_startup_home.py::test_startup_then_reverse_human_resume_requires_new_release`；`test_startup_then_reverse_human_red_guard_locks_without_recovery` | 生产 execute 搭配假模块的链路需再次独立放行；红灯不能转成自动恢复。 |
| 反向请求身份；`orchestrator.py`、`console.py` | `tests/automatic/test_reverse_human_entry.py::test_closed_old_window_cannot_stop_next_reverse_episode`；`test_failed_seal_closes_window_and_locks`；`test_resume_requires_an_independent_round_release` | 旧请求不能停止后续片段；封存失败锁定；人工请求不产生结局标签。软停止不是急停或可接近证明。 |
| 冻结配置和提示契约；`subtask_loop.py`、`subtask_vlm.py` | `tests/automatic/test_aeri_subtask_job.py::test_unset_inventory_budget_preserves_the_exact_legacy_plan`；`tests/automatic/test_aeri_subtask_loop.py::test_plan_gripper_fact_contract`；`test_zero_provider_budget_keeps_gpu_and_headroom_guards` | 未提供配置保留旧摘要；target规则与校验一致；真实零与未知分开，不能绕过守卫。 |
| 截断与适配器组合 | `tests/automatic/test_fr3_subtask_entry.py::test_truncated_answers_wait_even_when_the_json_is_valid`；`test_extra_body_cannot_bypass_the_frozen_output_budget`；`test_external_queue_504_waits_and_late_answer_cannot_cross_requests` | length结束留证后等待；迟到答案不能跨请求或绕过冻结预算。 |
| 原生外部桥；`external_bridge.py` | `tests/automatic/test_external_bridge.py::test_publish_complete_unique_and_preserve_input`；`test_caller_budget_timeout_releases_slot_before_caller_deadline`；`test_invalid_report_does_not_change_a_valid_answer` | 完整发布、有界期限/容量、可选报告隔离；这些合成用例不需要真实服务。 |
| 外部自报；`external_report.py` | `tests/automatic/test_external_report.py::test_exact_utf8_binding_preserves_unknown_and_true_zero`；`test_client_revalidates_binding_without_promoting_self_report` | 请求/答案绑定，各字段独立null/零；自报不升级为实测用量或人工真值。 |
| 报告投影和标签；`request_evidence_view.py` | `tests/automatic/test_fr3_subtask_entry.py::test_optional_report_persists_privately_without_changing_standard_usage`；`tests/automatic/test_request_evidence_view.py::test_production_fake_orchestrator_and_label_writers_bind_each_request` | 按精确轮次/成员关系和标签控制可见性，discard/撤回隐藏证据。API定位替换为临时目录，不覆盖HTTP认证或浏览器。 |
| 私有证据路由 | `tests/test_dataset_asset_boundary.py::test_raw_dot_components_and_nested_private_runs_are_refused`；`test_asset_symlink_canonical_destination_must_remain_public`；`test_public_assets_keep_get_head_range_and_internal_symlinks` | 公开资源入口不能访问私有AERI证据，保留公开资源服务行为。 |
| 细化图像上限；`levi/agent/observations.py` | `tests/test_harness_cost.py::test_a_split_rechecks_its_new_singleton`；`tests/test_events_planning.py::test_event_refinement_batches_respect_the_actual_request_image_cap` | 新singleton重新校验；较粗spacing从头重试；全部不适配则拒绝，不重写已完成shard或候选算法。 |

还需覆盖已有文件 `tests/automatic/test_aeri_contracts.py`、
`test_api_subtasks.py`、`test_subtask_units.py`、`test_fr3_real_startpose.py`、
`test_console.py`、`test_console_subtask.py`、`test_aeri_cli.py`、
`test_human_assisted_evidence.py`，以及 `tests/test_workbench.py`。
它们覆盖变化路径周边的权限和旧行为；只选择新增用例不能证明真实调用方仍可组合。

## 前端、Parquet、schema 与文档矩阵

以下全部 **未运行**。本计划未执行格式器、类型检查、生成夹具、浏览器或schema命令。

| 已有源码/检查 | 验收要求 |
| --- | --- |
| `src/components/automatic/__tests__/subtask-loop.test.tsx`：“each token keeps its own null and zero value (%s)”；“open evidence is removed on same-instance visible → hidden → missing updates (%s)” | EN/ZH参数化均覆盖，token不串字段，原已呈现证据完整撤回。组件手写夹具不是生产API生成夹具。 |
| 同文件：“the view has no control: no button, input, select or textarea”；“the API client has no write for a subtask or a round” | 只读展示不取得生命周期或机器人控制权。 |
| `src/utils/__tests__/parquetCache.test.ts`：“relocates once when an old firstIndex would clip a same-size rewrite to empty”；“a correct first row cannot conceal a wrong middle index” | 过期global index仅重建一次，验证完整请求区间，保留底层row-offset裁剪契约。 |
| `src/app/[org]/[dataset]/[episode]/__tests__/fetch-data.test.ts`：“unordered shared file fallback excludes neighbouring charts, language and timestamps”；“cross-episode fallback validates selected ranges without requiring unsampled gaps” | 共享文件整读回退只有在实际global index可验证并逐片段选择时才成立；缺失/重复/不可定位则拒绝，不能混入邻片段。未选间隔无需完整。 |
| `package.json`：`type-check`、`lint`、`format:check`、`test`、`validate` | 产品和测试两套tsconfig都须通过，保留隔离测试运行器；工具缺失属于阻塞/未验证，不算通过。 |
| `levi dev check-contracts`；已有 `tests/automatic/test_aeri_contracts.py` | 记录精确比较基线及实际命令参数，核对冻结计划的schema漂移。静态JSON解析不足；不能靠默默重生成快照抹去不一致。 |
| `levi docs check`；双语链接与示例 | 命令、配置、结论与固定源码一致；示例不授予机器人权限。本清单不证明其他文档已核验。 |
| 后续浏览器/构建阶段 | 单独批准并调度。用生产API生成的合成投影贯通UI，验证语言、隐藏/撤回生命周期及缺字段；执行禁止期间不得伪造生成夹具。 |

## 五项扩展：分别管理证据

下面五项在上述候选之后获准，属于 **当前固定矩阵以外：独立候选、后续集成或仅设计**。
展示保护独立审查开发提交d4f01aaa，已在020007fe之后组合为98c2ab6e；
运行测试仍未运行。这是来源记录，不是后续组合的验收证据。合入前记录实际受审SHA与文件/测试清单，再冻结新候选；
本计划不为未完成工作虚构测试选择器。

| 扩展 | 所需证据与限制 |
| --- | --- |
| 冻结契约展示保护 | 保留原始 `FORMAT_STATUS`/`VIEW_RULE`、plan摘要和旧记录，证明已有公开投影不泄露内部措辞；无实际消费者不新建展示模块。 |
| 显式脱敏日志导出导入 | 对明确提供的脱敏导出做有界解析与合成夹具；拒绝歧义身份/用量，保留来源和未知。不得读真实会话/队列、自动导入或虚构操作员标签。 |
| 自动无进展检测设计 | 明确观测新鲜度、状态转换、误报与后续测试；不接控制、不新增默认阈值。人工反向入口不等于自动检测。 |
| 可信策略声明设计 | 定义可信生产者、身份/摘要覆盖和只读消费者；不恢复MODELREG、不扫描训练数据、不从实验成功推断能力。 |
| 跨模块审查与本验收计划 | 冻结受审SHA，保留发现及处理记录。本轮综合审查仅覆盖外部证据链与startup/reverse链，不是全项目审查；静态无阻塞不等于运行、集成、用户验收或物理证明。 |

hub的导入幂等、采样参数边界和命令链分支仍是独立hub候选。
产品检查不验证它们；各自需要精确SHA、独立审查和后续运行记录。
不能通过导入真实经验条目验证工具。

## 人工与真机验收单列

软件验收需完整受审候选、获准执行且通过的检查、明确残余限制。
合并、部署/重启、物理验收是不同决定，本文件不授权其中任何一项。
服务重启前仍按既有规则确认没有活动真机评测。

后续真机实验必须另获明确批准，由人在场审核冻结绑定、相机身份、场景可达性、
策略/任务兼容性和停止流程。验证默认关闭的首轮回位启用与新鲜确认、独立轮次放行、
实测回位容差、夹爪截止行为和裁剪诊断，不能放宽锁存。
验证反向人工入口及红灯/停止优先；软停止确认不是可接近机械臂的证明。
故障/未知/部分动作不授权自动回位或重放。

操作员标签必须由人通过原生轮次标注入口提供，外部模型报告分开保留。
本地/外部成对质量、指令措辞效果、自动复位可靠性、无进展阈值及token/费用节省，
均需独立获准实验；本候选没有这些结果。

基线之后的设计来源：无进展设计385f562c→68c284c6；可信策略声明设计825ee844→3bb498bc。
两者均仅设计，不带可执行实现。

独立aeri-startup-fault-chain树已有
`tests/automatic/test_startup_home.py::test_startup_reverse_policy_identity_mismatch_waits_without_motion`
及同文件`test_startup_reverse_session_heartbeat_failure_cannot_resume_or_home`。
故障链候选fd770b65→组合94dc8be1已通过固定SHA独立静态审查；未运行，不在020007fe中。策略身份不匹配预期WAIT_HUMAN/policy_error；session心跳不是driver lease失效覆盖。
脱敏日志导入候选6d77d5a4→组合05010a49同样已通过固定SHA独立静态审查，未运行；两项集成都不自动扩展020007fe矩阵的证据。
