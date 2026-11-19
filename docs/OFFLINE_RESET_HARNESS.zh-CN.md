# 离线复位与重新抓取 harness

[English](OFFLINE_RESET_HARNESS.md)

入口位于现有 `/marvel_workstation` 的 Autonomy 步骤，名称为**离线复位演练**。
卡片在运行卡片的下面，开始按钮直接可见，前面没有折叠开关；加载时只读取离线目录（一次 GET），点击之前不写入任何东西。它使用独立 API 和固定 fake 执行器；现有真机操作
和停止区继续保留。修改或构建此分支不会自动部署到运行中的工作站。

## 实际执行的流程

演练从上一任务的合成终态出发：

1. 观察并绑定场景、对象与观测序号。
2. 按 seed 在合成场景区域内随机选择放置目标。
3. 确认空爪证据后请求张开，用新测量确认张开；接近经过验证的抓取位，确认到位后才闭合。
   抬起后同时检查测量夹爪状态和物体随动证据。
4. 清除旧策略动作块，更新 epoch，取得新观测。
5. 持物证据与任务匹配后，交给对应复位策略。
6. 放到随机目标、释放，再用新观测确认落点、开爪和物夹分离。

不能先把物体移到目标再开始抓取，也不能把命令返回成功等同于已经放好。
`holding` 保存交接前的持物证据，`final_observation` 保存释放后的终态证据。
缺失、过期、串 run 或串 epoch 的证据会阻断流程。最多抓取两次，持物未知立即停止。
停止在技能之间生效；它不是物理急停，也不会让真机回 Home。交接不调用 `env.reset()`。

观测和技能提议准备完成后、派发每个技能前，还会再次检查停止请求。在准备开爪、接近、
闭爪、抬升或释放期间收到停止请求，该技能不会启动。这种协作式检查不能中断已经开始的技能。

张开和闭合分别需要测量证据，未充分张开不等于已经闭合。合成适配器以归一化开度
`>=0.9` 表示张开、`<=0.3` 表示闭合；中间开度不能请求抬升。这些只是离线场景阈值，
不是真机审核过的限值。张开与闭合证据同时为真会被拒绝。持物证明保存前后两次采集时间；
交接绑定最后一次证明的序号和采集时间，要求证明序号、时间递增，且两次采集均处于现有
100 ms 观测时效窗口。即使交接的新观测新鲜，缺失时间或已经过期的持物证明也不能通过。

## 模型接口和当前能力边界

`Planner.propose(skill, target)` 返回类型明确的 `SkillProposal`，限定已知技能和目标。
当前提供方是 **scripted**，尚未调用大模型。后续可让 LLM 通过此接口提出技能计划，
但不能直接执行代码、裸关节动作或未校验坐标。

网页场景卡的观测、动作与策略都是合成的；没有接入相机、几何可达性求解、
接触物理或真机适配器。“不可达”是故障注入。打开历史记录是在网页重看证据，不会重放
硬件命令。外部模型调用次数确为 0；token 数未知，记录为 `null`，不虚构为 0。

所有结果始终标明 `execution_mode="offline"`、`adapter="fake"`、
`physical_execution=false`、`handoff.physical_allowed=false`。
离线交接通过不会解锁真机，也没有往原 AERI dispatcher 增加真实复位模式。

### 真实 reset 策略的录制观测对照

新增 `scripts/offline_reset_policy.py` 作为独立离线入口。它用 checkpoint 配套源码快照和
原始转换加载本地 `reset_r1_step20000`，固定 `(10,32)` 噪声推理。输入是事先提取的
双 RGB 和状态 NPZ，不连接实时相机或策略服务。推理需提供 `--samples`、`--checkpoint`、
`--openpi-root`、`--tokenizer` 与新 `--output` 目录。不改权重、不下载、不下发动作。
模型的夹爪输入保持原训练所用命令状态，实测开度单列为诊断数据。

`levi.automatic.policy_replay` 每条原始回合选首次闭爪前、首次闭爪、闭爪后八行三个观测，
使用 CSV 时间而不是按 FPS 推算墙钟。两条历史 reset 指令不在 checkpoint 的训练指令表，
因此明确标为未见指令。历史动作块和采样噪声未保存，新推理不能称为精确重放当时输出。

`regrasp_gate.advance_regrasp` 与合成流程使用同一套类型化阶段检查，只请求独立技能，
不会顺带放行未经验证的模型位姿。录制数据缺少独立空爪、抓取几何或持物证据时返回
unknown 并拒绝执行。这不代表抓取成功率提高；更改动作后继续读原录像也不构成闭环。
`GateObservation` 的可选 `closed_measured` 放在 `source` 后，保留原位置参数兼容，
缺省为未知。闭爪命令或 `open_measured=false` 都不能提供闭合或持物证明。
harness 的阈值是合成值（张开 ≥ 0.9、闭合 ≤ 0.3），只适用于 `synthetic_measurement`；真机夹爪用
[实测持物事实](AUTOMATIC_PIPELINE.zh-CN.md#实测持物事实leviautomaticholdingpy)里按物体登记的持物带，
其来源为 `verified_measurement`。

## 不启动网页即可运行

在此 checkout 中使用已有 Python 环境：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -B -m levi.automatic.offline_report \
  --workspace ./lab/offline-reset-check --seed 42
```

七个场景包括正常随机复位、第一次空抓后重试、持物未知、旧观测、策略不匹配、不可达与停止。
报告同时检查预期成功和预期拒绝，写入指定 workspace 的 `reset-harness-report.json`，
并保留每条运行记录。同一 workspace 重复相同请求会返回旧结果；验证新代码版本应换新目录。
不需要服务器、GPU 或权重；共享机器人主机仍遵守其资源和 CPU 使用规则。

## API 与运行恢复

前缀为 `/api/levi/marvel0/reset-harness`。

| 方法 | 路径后缀              | 输入与结果                                      |
| ---- | --------------------- | ----------------------------------------------- |
| GET  | 空                    | 场景、最近 50 条运行、损坏记录提示              |
| POST | `/runs`               | `{request_id, scenario_id, seed}`，返回排队记录 |
| GET  | `/runs/{run_id}`      | 查询阶段和证据，包括终态                        |
| POST | `/runs/{run_id}/stop` | `{request_id}`，持久保存停止请求                |

写接口沿用现有网页身份检查，拒绝 Bearer agent 凭据和真机参数。seed 必须为
`[0, 2^32)` 内的整数。同一 request_id 绑定场景和 seed，重复提交不会再次执行，
内容冲突返回 409。

网页运行数据位于 `$LEVI_WORKSPACE/offline-reset-harness`，未设置时采用现有 workspace
默认目录。同一时刻最多一个固定子进程，最多保留 1000 条，不自动删记录。worker 活动时
新请求返回 409。子进程只收到最小环境、禁用 GPU，并在自己进程内禁止 socket；网页进程
网络不受影响。状态原子写入，启动期间持锁。不会根据磁盘上的 PID 杀进程；缺少完成证据
的退出标为失败，不自动重跑。损坏记录保留并单列，不阻塞其余记录查询。

历史读取也会核对成功持物和释放证明与 run、物体、epoch、目标及已保存阶段事件的一致性。
证明为空、互相矛盾或串到其他 run 时，会单列为损坏记录，不能继续显示成功。
旧版 v1 历史可以同时缺少两项持物采集时间；只缺一项则无效，不补造缺失时间。
历史证明按记录内部的一致性检查，不按当前墙钟重新判定过期。这不是对整份伪造记录的
真实性认证，也不授予执行权限。

## 代码与验证

- [状态机](../levi/automatic/offline_harness.py)、[场景](../levi/automatic/offline_scenarios.py)、
  [独立 worker](../levi/automatic/offline_worker.py)。
- [运行管理](../levi/marvel0/reset_harness.py)、[API](../levi/marvel0/reset_harness_api.py)、
  [Autonomy 卡片](../src/components/marvel0/reset-harness-card.tsx)。
- [状态测试](../tests/automatic/test_offline_harness.py)、
  [交接测试](../tests/automatic/test_offline_handoff.py)、
  [真实子进程与 API 测试](../tests/marvel0/test_reset_harness.py)。

接入真实提供方前，感知、标定、抓取几何、碰撞、控制器限值和人工审核的执行许可
需要单独验证。这些软件测试不构成真机抓取或复位成功的证据。
