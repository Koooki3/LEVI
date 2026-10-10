# 事件智能：信号事实、信号语义声明与事件候选

[English](EVENTS.md)

`levi.events` 读取机器人记录的信号说明了什么时候发生了什么。**目前它只是一个库**：计划、运行、界面、API 和命令行都还没有调用它；agent 读到的信号行（`levi.agent.signals`）和锚定复核（`levi.agent.anchored`）的行为不变。各模块都是作用在 numpy 数组和 pydantic 记录上的纯函数：不调模型，不导入 Torch，不写文件。

| 模块 | 内容 |
| --- | --- |
| `gripper` | 单个夹爪通道带滞回的张开/闭合穿越；信号行和锚定复核共用的内核。 |
| `motion` | 按记录的时间戳计算速度和静止。 |
| `contracts` | 读取器产出的记录：`SignalObservation` 和 `EventCandidate`。 |
| `signal_profiles` | 每个记录通道的含义：角色、执行器、单位、坐标系、夹爪哪一端是张开。 |
| `facts` | 把一个片段的信号事实按通道和执行器整理成 `SignalObservation`。 |

## 记录

两种记录都能从片段的数据表重建，不进标注版本包。每条记录同时保留表的行号（`source_frame_index`）和以秒计的时间（`time_s` / `center_time_s` 和 `time_window_s`），下游不必用 `frame / fps` 换算：表里丢帧时这个换算是错的。JSON 字段 `schema_version` 写 schema 名（`levi.signal_observation.v1`、`levi.event_candidate.v1`），与 LEVI 其他合同一致。

- `SignalObservation`：一条事实（`gripper_open`、`gripper_close`、`height_low`、`height_high`、`still`、`change_point` 等），带执行器、来源（feature、维度名、维度序号、`measured` / `commanded` / `derived`、单位、坐标系）、可选的数值和可选的源表 `source_sha256`。点事实的时间窗口从前一个有限时间戳到它自己的时间戳：变化发生在这两个采样之间。
- `EventCandidate`：某处可能发生了变化。`salience`（0 到 1）只用来排定取证的先后，**不是概率**，不得当作概率展示。在有校准过的估计之前，`boundary_probability` 保持 `null`。`status` 取 `proposed`、`merged` 或 `rejected`。

## 信号语义声明（signal profile）

一份声明（`levi.signal_profile.v1`）列出若干通道，每个通道有 `role`（`gripper`、`position`、`rotation`，声明里还可以写 `ignore`）、`feature` 和 `index`（以及维度名 `dimension`）、`actor_id`、`kind`（`action` 为 `commanded`，其余为 `measured`）、`units`、`frame`、`axis`、夹爪的 `open_level`（`high` 或 `low`）和可选的 `valid_range`。

- `infer(info)` 按信号行一直用的规则读 `meta/info.json`：名字匹配 `grip|finger|claw|jaw` 的浮点向量维度是夹爪；名为 `x`/`y`/`z` 的是位置（每列每个轴取第一个）；`rx`/`ry`/`rz` 或 `roll`/`pitch`/`yaw` 是姿态。名字里带 `left`、`right`、`arm_N`、`robot_N` 的维度归该执行器，其余归 `arm_0`。**名字说明不了夹爪哪一端是张开**，所以推断出的 `open_level` 总是空的，读取器退回“片段开头的电平就是张开”的规则；片段开头夹爪已闭合时，这条规则会把整段读反。
- 声明可以纠正这一点。`resolve(info, declared)` 把声明逐通道叠加到推断结果上：声明的通道替换同一 (feature, index) 上推断出的通道，或者新增一个通道；`role: "ignore"` 删除名字误导出的通道。声明里写了数据集没有的列、超出范围的序号或对不上的维度名，一律拒绝。
- `from_action_contract(contract, feature)` 把已登记的动作契约（`levi.counterfactual`，见 [COUNTERFACTUAL.zh-CN.md](COUNTERFACTUAL.zh-CN.md)）转成声明：维度顺序、单位、坐标系，以及夹爪哪个值是张开（`fr3-robotiq`：1.0 为张开，即 `high`）。
- 数据集可以在 `meta/levi_signal_profile.json` 里自带声明，`for_dataset(root)` 会读取它。LEVI 从不写这个文件。
- `open_levels(profile)` 把声明的张开端整理成 `levi.agent.signals.summarize(open_levels=...)` 接受的形式。

`facts.read(table, info, profile)` 读取每个执行器的每个夹爪（不像信号行那样只取第一个动过的），以及高度转折和静止区间，每条事实都带来源通道；声明了 `open_level` 时，无论片段开头夹爪处于什么状态，夹爪事实都不会读反。
