# AERI 无进展观测设计

[English](AERI_NO_PROGRESS.md)

状态：仅设计，基线 `020007fe`，任务 `T-C-NO-PROGRESS-DESIGN`。
本文没有实现分类器、runner 钩子、配置默认值、界面或任何自动机器人行为。
下文测试是待实现规格，尚未运行。本片不满足 `L2-AERI-EARLY-HUMAN-RESET`
原验收中自动进入 `WAIT_HUMAN` 的要求。

## 依据及局限

工作区汇总 `LEVI/lab/aeri-external/EXPERIENCE.md` 第2、4、5节及
`REPORT-20261018.md` 第1、3、6节区分了不适用的逆向指令、容器换位、连续判定失败、
安全守卫中断和人的解释。这些材料支持更早提示协助，但没有给出场景距离阈值、
可达性模型或检测准确率。本设计未读取会话、请求队列、盲集或源 rollout。
原问题提出连续两次失败且场景无变化；次数是既有产品提案，不能当作校准结果。
场景比较器及其阈值仍未验证。没有完整审查过的 profile 和验收依据前保持禁用。
历史计划仅供理解；当前行为以该基线源码为准。

源码入口（除特别说明外均在 `levi/automatic/`）：

- `orchestrator.py` 的 `Episode`、`reverse_human_scope`、`reverse_human_request`、
  `_finish_reverse_subtask`、`_reverse_stop_guard`、`_next_subtask`：false 按既有
  上限重试；判定缺失走既有 unavailable 分支；人工请求绑定作用域且不等于成败标签。
- `adapters/signal_events.py` 的 `CachedObserver` 缓存既有观测，`SignalEvents`
  检查 stream 身份、serial、时效、时钟及实测状态；`levi/events/stream.py` 检测信号事件，
  不判断任务进展。
- `console.py` 使用新鲜作用域发起逆向人工请求。接受请求不等于已停止运动、已回位，
  也不表示可以接近机械臂。

## 观测信号与误报边界

| 输入 | 可以说明 | 不能说明／误报来源 |
| --- | --- | --- |
| host 的 episode、step、policy epoch、plan/subtask 身份 | 证据属于哪次执行、是否发生已确认 step | 步数或耗时不能代表物理／语义进展 |
| 新鲜实测 pose/gripper，含单位和来源 | 有效窗口内活动或静止 | 保持、接触、慢速运动、柔顺抓取、稳定等待可能有益；命令值不是实测值 |
| 有 capture 身份及冻结相机／布局／标定的图像 | 供独立验证过的场景比较器使用 | 曝光、阴影、遮挡、相机／腕部运动可能隐藏或制造变化 |
| 验证过的任务相关场景比较 | 在精确契约下 changed / unchanged / unknown | 像素相同不证明新鲜；像素变化不证明接近目标或可达 |
| 已完成子任务 check | check 返回 true / false / unavailable | 连续 false 不证明不可达、不安全或不可能；模型答案不是人标 |
| 既有 guard/fault/stop | 抑制或作废提示 | 检测器没报警不能削弱守卫或认证安全 |

本片没有确认可用的在线语义比较器、经验证阈值和专用只读消费者，因此不实现孤立分类器。
不能用速度、图像摘要、超时或外部 agent notes 偷换语义比较器。不得增加 observe、
模型调用、服务或机器人轮询；仅复用已有、有界的保留证据，不从任意路径或日志取数据。

## 可落地的纯接口与归属

未来模块（本片不创建）：`levi/automatic/no_progress.py`。

```python
observe(state, sample, *, profile, now_ns) -> (state, observation)
finish_attempt(state, result, *, profile, now_ns) -> (state, observation)
close(state, *, reason) -> (state, observation)
```

函数纯粹且确定：不读时钟、不 sleep、不 I/O、不调 adapter、不建线程、不发命令、
不迁移 runner 状态。host 注入单调时钟，并在构造前验证数据上限。
拒绝 bool 冒充整数、非有限数、负时间、无界文本／集合和多余字段。
上限来自经过审查的 profile/契约，不从运行现场猜值。

| 记录 | 必须字段及来源 |
| --- | --- |
| `scope` | host 提供 `run_id`, `round_id`, `episode_id`, `policy_epoch`, `loop=reverse`, `plan_sha256`, `subtask_id`, `attempt_id`, `process_generation`；不接受模型关联 |
| `sample` | scope、严格递增 `serial`、已确认 `step`、`observed_ns`, `received_ns`, `clock_domain`, `source_generation`, `quality`；可选实测活动及比较结果，附保留证据 ID／摘要 |
| `comparison` | `contract_id`, `version`, `profile_sha256`、baseline/current 精确证据 ID 与 capture 身份、`relation=changed|unchanged|unknown`、reason code；缺失为 unknown |
| `result` | scope、唯一 host completion ID、`check=true|false|unavailable`、已定稿证据引用；只有 host 批准的同一子任务重试才附显式 predecessor attempt ID |
| `profile` | 冻结契约／版本／摘要、必需来源、时效／间隔／窗口／样本上限、比较器契约、失败次数条件、有界历史限制；无自动默认阈值 |
| `observation` | `schema=levi.aeri.no_progress.v1`、scope、profile 摘要、状态、reason codes、证据 ID、有效独立样本／attempt 数、观测区间、`authority=advisory`, `safe_to_approach=null`；不含停止命令或任务结果 |

`state` 只保留有界测量摘要和已完成 attempt 摘要，不保存无界图像或任意模型文本。
溢出时拒绝新窗口并置 unknown，不能悄悄丢掉不利证据。将来的 journal 使用独立提示
note/sidecar，和 judgement ID、人标、episode result 分离。未经精确 round/member
可见性检查，不得向已有 UI 投影暴露原始证据。

## 观测状态机

以下是检测器状态，不是 runner 状态：

| 状态／事件 | 后继及输出 |
| --- | --- |
| 无已审查并启用的 profile | `disabled`，无建议 |
| 新有效 scope | `collecting`，清空片段内历史 |
| 缺失、过时、只有重复、非法、矛盾或不可比证据 | `unknown`，附原因；作废候选窗口，无建议 |
| 证据充足有效，比较器报告变化 | `collecting`，清空无变化连续计数；不宣告成功 |
| 完整有效无变化窗口，且满足已完成 false attempt 条件 | `suspected`，对此 scope／证据窗口只发一次提示 |
| 同一证据／result 的重复副本 | 不增加计数或持续时间，不再发提示 |
| 人确认看到／忽略提示 | 只记录提示回执，不改变执行；忽略仅抑制这条提示，不影响守卫 |
| guard、stop、scope 关闭或执行终态 | `closed`，提示失效；守卫仍有最高权限 |

活动信号与“场景无变化”不必矛盾：手臂运动未必移动目标。不同场景比较器互相冲突，
或来源冲突，则比较无效。缺少必需信号打断连续窗口；unknown 不转成 false 或 unchanged。
单靠计时不能进入 `suspected`。unknown 后的新鲜有效证据须重新建窗口，不能续接旧时长。

严格隔离片段：新 episode／epoch／process generation 清空原始历史、计时、待处理提示及
样本。跨重试检测另用已完成 attempt 摘要 reducer；只有 host 提供 predecessor 边，且
run／round／plan／subtask 和场景比较契约一致，才能跨两个片段比较。不能按编号相邻、
时间戳或 provider 身份猜关联。人工处理场景、新 plan、缺失 attempt、成功或 unavailable
都打断失败连续计数。进程重启从 unknown 开始，不从旧文件恢复运行时计时器。

窗口要求同一 clock domain/generation，并满足 `observed_ns <= received_ns <= now_ns`；
执行 profile 时效和样本间隔限制。回拨、未来观测、epoch 改变、时间戳或 serial 倒退
使窗口无效。重复 poll 缓存不能增加时长。字节相同但独立证明是新 capture 的图像可以
作为输入；重复 frame ID 不是新样本。不跨时钟域相减。异步比较在 scope 关闭后才返回，
必须丢弃。

## 守卫优先级与人工确认

物理守卫和既有故障处理独立执行。red health/latch、quiesce/seal 失败及既有安全条件
无法证明时仍走原 fault 路径；检测器异常不能吞掉或降级这些错误。普通 stop／人工请求
或 scope 关闭使提示无效；迟到成功 check 不能推翻已接受 stop。检测器不拥有 motion
 token、准入、home、resume、重试预算或 VLM 电源权限。

将来的只读文案：“疑似复位无进展；依据：…；不确定：…。请复核场景。
这不表示机械臂已停止运动。”不写“不可达”“复位失败”“可接近”或“可以挪动物体”。
若人选择现有人工复位操作，终端必须重新读取 `reverse_human_scope()`，经
`reverse_human_request` 提交人的明确请求，而非重放提示携带的 scope。
自动检测不得伪造 `source=terminal`／`by=operator`。既有 quiesce/seal、可选已审查 home、
安全守卫、`WAIT_HUMAN`、人工确认和独立轮次放行全部保留。请求被接受后，已准入 step
仍可能执行完毕。静默时间或检测器状态均不能证明物理保持或已回位。
自动进入 `WAIT_HUMAN` 属于另一个安全变更，需要明确授权、接口审查及真机人工验收；
本文不授权该行为，不扩大当前 reverse-only 请求窗口。

## 后续实现的测试契约

运行验证获授权后才能执行。新夹具全部使用合成数据、内存或临时文件，不连接网络、GPU、
机器人，也不读取真实数据。

| 拟新增节点／复用夹具 | 必须满足的可证伪断言 |
| --- | --- |
| `test_no_progress.py::test_missing_and_static_are_distinct` | 相机／状态／比较器缺失为 unknown；只有连续 false 或静止保持不能提示 |
| `test_no_progress.py::test_window_requires_distinct_fresh_samples` | 重复 serial、frame ID、completion ID 不增加窗口／计数；区分字节相同的新 capture |
| `test_no_progress.py::test_clock_and_gap_boundaries` | 注入精确边界、过时／未来、回拨、跨域／generation、间隙；不 sleep；无效窗口不能恢复旧时长 |
| `test_no_progress.py::test_retry_lineage` | 同子任务显式 predecessor 才能聚合摘要；新片段样本仍隔离；缺边、改 plan、人工处理、成功／unavailable 打断连续计数 |
| `test_no_progress.py::test_comparator_is_not_progress_truth` | 比较契约标记 unknown 的光照／遮挡／腕部运动／稳定等待案例不能提示；changed 清连续计数而不授权成功 |
| `test_no_progress.py::test_bounded_and_late` | 超大／非有限／错误类型拒绝，历史有界；关闭后迟到结果丢弃；新进程不能复用旧提示 |
| `test_reverse_human_entry.py` 与 `subtask_rig.build` | 在 chunk/step/verify/check 注入 suspected/unknown 提示，相比禁用提示，robot calls、home、requests、stop generation、results、labels 完全不变 |
| 既有 reverse scope 用例加显式人工动作 | 只有人的命令才接受，重复幂等，旧提示不能停止下个片段；guard/fault 优先，已接受 stop 优于迟到 check |

复用 `tests/automatic/test_signal_events.py` 的 `Clock`、`Robot` 及现有
age／identity／freshness 测试；不在纯 reducer 再复制整个 adapter 矩阵。
控制边界复用 `test_current_reverse_request_stops_without_a_verdict`、
`test_payload_identity_duplicates_and_stale_requests_do_not_register`、
`test_reverse_stop_safety_wins_even_when_no_home_is_due` 和
`test_resume_requires_an_independent_round_release`。
现有测试覆盖带作用域人工停止和来源新鲜度，不覆盖语义场景可比性、失败序列关联及
新提示对执行的无干扰。

实现前须确定真实只读消费者、冻结比较器和信号质量契约、取得已审参数依据，并建立
独立于 reducer 的合成测试判据。启用前在获授权的已标注数据上分别统计误报与漏报，
列分母及不确定性，保持人标／模型标签分离；本设计不提供质量结论或选定阈值。
即使提示测试通过，未来自动动作仍须单独审查和经人批准的物理验证。
