# 自动评测模型提供者

[English](AERI_PROVIDERS.md) · [自动评测流水线](AUTOMATIC_PIPELINE.zh-CN.md)

此提供者通过现有实时服务的在线 Judge 真正执行谓词模型调用，不生成机器人动作。文件响应必须通过完整 AERI 合同、当前保留证据、run/plan 身份和期限检查才可使用。传输测试不能证明模型或真机已准备好。

## 先走无复位策略路径

现有 `human_assisted` / `operator_attested` 路径保留人工场景门和前向 C5 终局判定，不需要场景模型 worker。不要为了绕过缺失的提供者加复位策略。复位模型只读已确认的初始状态合同（ISC）；`generic-final` 只用于前向终局。

使用原 ISC 规格的 worker 只有 `scene` 和 `reset_judgement` 能力，**没有 events 能力**。现有 FileEvents 请求没有观测帧，不能据此实现视觉事件。使用模型 worker 的新计划必须明确冻结 `termination.allow_early_stop: false`。保留旧 true 默认值的作业会阻断，代码不暗改旧作业。以后添加观测事件或延长期限，需新计划／规格和独立测试。

## 冻结完整模型规格

`levi.automatic.provider_model.FrozenSpec` 读取 `levi.aeri.model_spec.v1`，以下字段全部必需：

| 字段 | 来源与核验 |
| --- | --- |
| `id`、`version`、`run_id`、`plan_sha256` | 明确的规格版本和完全匹配的冻结作业身份。 |
| `contract` | 完整 confirmed ISC，含 required/optional 谓词及证据／视角规则。 |
| `definitions` | 每个谓词均有操作员给出的物理定义；不根据名字猜。物体、位置、容差和可见性规则均由该来源确定。 |
| `source.ref`、`source.sha256` | 操作员冻结来源入口，以及 `{"contract": ..., "definitions": ...}` 规范 JSON 的 SHA256。哈希绑定内容，不证明物理安全，也不代替人工审阅。 |
| `provider` | 明确声明的 ProviderConfig 字段：本地回环模型地址、名称／摘要、视觉／结构化能力、图像／上下文上限。实际服务逐项匹配，不猜 checkpoint 或摘要。 |
| `deadlines_ns` | 与作业完全一致的 scene、reset_final、reset_active 期限；当前桥为 5／5／3 秒。 |
| `image_budget`、`max_output_tokens` | 明确且不超模型容量的预算。 |
| `observation_max_age_ns`、`validity_ns` | 明确有界的证据年龄和结果有效期。 |
| `prompt_sha256`、`answer_schema_sha256` | 固定生成的提示模板及输出 schema 哈希。 |

纯函数 `freeze(mapping)` 计算最后两个哈希并核验显式字段，不生成物理定义、人工批准、模型或作业身份。规范 JSON 使用 UTF-8、排序键和紧凑分隔符，`FrozenSpec.sha256` 绑定全部字段。来源及规格字节随新计划保留。

先做**纯文件检查**：

```bash
python -m levi.automatic.provider_worker check --spec /absolute/path/frozen-model-spec.json
```

`check` 不访问网络／模型 SDK，不写文件。缺定义、ISC 未确认、来源／提示／schema 哈希错误、无模型摘要或字段不全均阻断。桥绑定必须同时配置 `providers.model_spec_file` 和 `providers.model_spec_sha256`，并核对作业 run、plan、ISC 和三种期限。新规格路径缺失不能降级。旧外部文件协议可兼容读取，但其心跳不能证明这个模型 worker 存在。

模型 profile 必须写齐 `ProviderConfig` 的全部字段，不能用省略字段的默认值替代冻结配置。每个字段均与实际共享 Judge profile 核对。

## 显式前向目标规格

前向任务成功使用单独显式启用的 schema `levi.aeri.forward_goal_model_spec.v1`，类型仍是 `FrozenSpec`。`freeze_forward(job, settings)` 要求真实 `cli.load_job` 返回值：已解析任务、运行配置、冻结计划和 `plan_sha256` 必须一致。任务 instruction 与 `task.success_condition` 都必须存在且非空白。完整计划保留为 `frozen_plan`，摘要沿用 `cli.load_job` 的准确 JSON 序列化方式。helper 将操作员成功条件原文冻结为唯一必需谓词 `task_success_condition`，不自动拆条件或发明物理参数。ISC 谓词继续定义初始／复位就绪，不定义前向任务成功。

`settings` 必须显式给出 `id`、`version`、`run_id`、完整 `provider` profile、`deadlines_ns`、`observations`、`image_budget`、`max_output_tokens`、`observation_max_age_ns` 和 `validity_ns`。`deadlines_ns` 仅含 `forward_final` 与 `forward_active`，必须同时等于实际 `RunConfig` 和冻结计划的 `final_judge_timeout_ns` 与 `judge_request_timeout_ns`。只替换配置期限，或重新哈希计划而未同步实际配置，均拒绝；计划预算必须是整数。`observations` 显式给出 `preferred` 视角、`require_visible_evidence: true`、`min_evidence_refs` 和 `require_all_views`，证据要求必须在图像预算内。helper 从这些输入生成目标描述、定义、来源绑定及提示／schema 哈希；沿用模型、请求、保留帧与新鲜度检查。

此规格仅声明 `forward_judgement`。文件请求位于 `forward_judgement/requests`，响应和取消使用同能力目录，沿用请求 ID 哈希文件名和幂等规则。原 ISC 规格仍准确声明 `scene` 与 `reset_judgement`，仍拒绝 forward 目标。前向请求必须绑定 forward 片段与 `forward_goal`，保留观测步数窗口和事件 ID，并使用对应 active／final 期限。对完整条件的当前观测给出 `confirmed`、`rejected` 或 `unknown`；读不清或可见证据不足仍为 `unknown`。操作员标签不进入提示。

共享在线 Judge 注册与文件 worker 共用同一冻结规格和模型仲裁；worker 不增加 events 能力，也不创建额外模型服务。本段只实现软件编解码／provider 通路：真实 `event_pause_judge` 仍拒绝，生产组装、角色调度及物理／运行时证明仍需各自经过审查的绑定。
## 运行与证据

worker 启动前，现有在线 Judge 必须显式注册同一冻结规格。新 `/v1/aeri/provider` 接受注册摘要、请求、当前 JPEG 和取消身份，不接受调用者提示或替换规格；`/v1/aeri/cancel` 只取消匹配身份。两者沿用回环 Host／Origin／请求体上限检查。

操作者通过已有实时配置登记本地冻结文件：

```toml
[online]
aeri_specs = ["/absolute/path/frozen-model-spec.json"]
```

列表默认空，默认配置渲染时省略该键，兼容原 C5 配置。条目必须是互不重复的绝对路径，指向当前身份拥有的完整冻结规格。配置检查只读文件，构造在线端点时加载登记内容，不执行推理；未知摘要不可用，HTTP 不能注册规格。更改列表或作业绑定须保留新规格／计划身份，本身不启动服务，也不证明实际模型 profile 已匹配。

经单独运行授权后：

```bash
python -m levi.automatic.provider_worker run \
  --spec /absolute/path/frozen-model-spec.json \
  --root /absolute/path/provider-files \
  --evidence-root /absolute/path/current-run/evidence \
  --online-url http://127.0.0.1:7882
```

worker 文件根需预先存在并属于当前执行身份。新运行的证据目录可以尚未出现：bootstrap 只打开已有父目录并核验 NOFOLLOW／身份，不创建 run 或证据目录。READY 只表示模型通道准备，不能代表物理场景准备。execute 创建新 run 后，首个票据再次核验完整授权目录链，目录更换／链接／身份变化均阻断。证据根必须显式授权并与请求一致。worker 逐级 NOFOLLOW 打开目录，有界读取当前身份的普通请求／JPEG 文件，在模型前后核对哈希，原子替换并 fsync 响应。在线 Judge 的图片只是临时副本，保留的原图仍在本次运行证据目录。

原 ISC 请求位于 `scene/requests` 或 `reset_judgement/requests`；文件名为请求 ID 的 SHA256，responses／cancelled 子目录沿用同名。桥发送 run、plan/spec 摘要、phase、单调时钟 submitted_ns 和绝对 deadline_due_ns，期限不得超冻结 phase 上限。缺绑定、身份／时钟错、观测过旧／来自未来、哈希变化／引用不属于本次证据、漏谓词或降级 required，均不能产生场景准备好或复位确认。

模型只返回完整冻结谓词的 true／false／null、本次真实 ref/hash、规格／模型身份和采集时间。代码算出 ready／reset_required／unknown 或 confirmed／rejected／unknown；optional 不决定结果。非 null 判读必须引用当前证据，视角和最少证据规则也适用于复位确认，不凭空给校准置信度。

READY 只有活通道及实际模型 profile 校验成功才维护，绑定 PID、启动 ticks、boot ID、run/plan/spec/model 及该显式规格的能力。模型路径拒绝死亡／复用 PID 和过期心跳；通道或 GPU 门失败即取消准备状态，正常退出删除 READY，同目录锁拒绝第二个 worker。自身合法在途请求不会被 heartbeat 当成外部 busy 而删除已有 READY；原心跳仍受 10 秒时限，不伪造新的准备证明。文件 poll 游标每轮推进，不因前 1000 个历史票据饿死新票据，也不删除历史证据。

## 取消与恢复

C5 与 AERI 无论谁先运行，caller 取消／超时回复都不会提前释放实际 worker 的归属、GPU 准入和并发锁。AERI 使用一个串行 GPU 检查线程，监督路径只读已完成结果；慢检查也受原期限和取消约束，线程未退出时保留所有权。

AERI 与 C5 共用 Judge 并发锁、请求归属、GPU online_admit/check/done 与 transport abort，不另起 vLLM、不绕 GPU 门，其他训练持有者仍可阻断准入。

实际期限在模型 Budget 外严格执行（Budget 最小为 10 秒）。超时、文件取消、GPU 门关闭或 supervisor abort 切断本次传输并返回不可用，晚到结果不能成功。调用者已收到不可用时，Judge 仍持并发锁与 GPU 准入，直到实际模型线程退出；不听取消的模型不能与下一次 C5／AERI 重叠。若线程不退出，通道保持 busy。

持久 intent 防止重启后重复推理。已完成请求仅在 request/spec/response 哈希一致时复用原响应；旧 intent 未完成则重启产出不可用，不猜成功、不自动重放。相同 ID 的内容变化被拒绝；请求／响应／processed 文件随运行证据保留。

使用 `reset_horizon_completion: wait_human` 时，复位达到步数上限仍需人工处理，`reset_active` 候选不等于 `reset_goal` 终局验证。独立的 `verify_final` 有界复位路径继续要求既有冻结稳定性和终局初始状态合约。选择信号事件不改变完成策略和这些验证门。


FR3 绑定现在可显式选择[事件文档](EVENTS.zh-CN.md#显式-fr3-事件绑定)中的带帧
信号来源，完整 profile、bindings 和阈值都进入人工审批的绑定 SHA。只有合法
信号路径解除模型 worker 的提前停止拒绝；旧 FileEvents 来源配提前停止模型
worker 仍被拒绝。worker 继续提供场景／复位判定，并保留共享 GPU 和取消门。

## 已验证边界

测试执行真正的文件 worker、JPEG 保留、AERI／ISC 合同、桥 submit/collect 和共享 Judge；模型注入、HTTP 为 fake。覆盖坏 JSON／规格／模型／ref／hash／时间／身份、漏或降级谓词、GPU 门关闭、取消、晚回、重启歧义、纯文件零网络零写检查和冻结计划／期限绑定。

本轮未运行真实模型推理、GPU 延迟测量、机器人 SDK／网络或现场动作，也未验证物理参数。模型判读质量、是否能在 5／3 秒完成仍未测。物理定义、checkpoint／模型摘要、相机视角、安全和现场批准均保留人工门，不把这些 mock 验证称为真机可用。

## 子任务循环的响应计量

子任务循环的聊天适配器仅保留 `usage.prompt_tokens` 或
`usage.completion_tokens` 中的非负整数，布尔值不算整数计量。真实报告的零仍为零；
缺失、null 或非法字段分别保留为 null，不用总数或模型名称推算补齐。
每题证据在至少一个字段有效时记录 `usage_source: provider_reported`，否则为
`unavailable`。这些字段只是响应元数据，不参与模型答案或状态转移。没有实际计量的
外部桥应省略 `usage`。不带 `usage_source` 的旧证据仍可使用；缺失来源表示未知，
不能据此认定用量为零。

provider 可用 `wake_need_mib() == 0` 表示不需要额外的本地 GPU 预算。显式配置的
正数覆盖值仍优先；GPU 空闲显存不可读或不足配置的 headroom 时仍拒绝唤醒。
provider 预算缺失或非法时使用原有回退值。正向与反向规划提示词均说明事实字段：
`in_gripper` 的 subject 必须是物体，target 为 `"-"`；`gripper_empty` 的
subject 为 `"-"` 或 `"gripper"`，target 为 `"-"`。解析器与结构化输出 schema 不变。

## 子任务物体清单预算与截断响应

物体清单的默认输出上限仍为 400 token。如需更大预算，在审核并冻结计划前写入任务文件：

```yaml
subtask:
  vlm:
    inventory_max_tokens: 1024
```

`inventory_max_tokens` 只接受 1 至 4096 的整数，且必须小于 `min_model_len`；
拒绝布尔值或字符串转换。上限约束输出成本，不保证识别质量或请求耗时。显式值进入冻结
计划和配置校验摘要，并改变计划 SHA。省略或 null 保持原计划表示与 SHA。绑定从计划
取得预算，不接受另一份绑定覆盖值。`extra_body.max_tokens` 与
`extra_body.max_completion_tokens` 均被拒绝，直接构造适配器时也一样。规划与核对的
上限仍为 500 和 200 token，模型配置不变。任务 schema 在 minor 1 增加此可选字段，
标识仍为 `levi.aeri.job.v1`。

`choices[0].finish_reason: length` 表示回答不完整，即使内容碰巧是合法 JSON
也不接受。适配器在每题证据中保留原文、finish reason、usage 及
`response truncated: finish_reason=length` 错误；协调器把无回答交给原有校验器，
沿既有拒绝或未知路径进入 `WAIT_HUMAN`。物体清单、规划与核对均适用。不改变其他
finish reason 的解释，也不自动提高预算重试。原有原文证据长度上限保持不变。

每次规划请求只包含当前方向的说明。正向使用当前侧视与腕部图，要求覆盖任务中仍需
移动的物体，并保留已就位物体；反向使用当前侧视图与本轮起点侧视图，把移动过的物体
放回本轮起点位置。两者复用相同事实契约。on/in 语义、核对提示词与 true/false/null
规则均不变。预算覆盖和方向说明只是软件控制，不能据此宣称物体识别改善或真机可用；
这些结果仍需单独评测。
