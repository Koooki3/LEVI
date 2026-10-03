# 反事实数据：动作契约

这是 CAST 类“事后重标和反事实数据”工作的数据契约基础（计划见维护者工作区的 `levi-hub/PLAN-cast.md`）。**目前只有契约和校验。** LEVI 还不生成、导入、存储反事实动作，界面里也没有，没有 API，也没有命令行。

代码包是 `levi.counterfactual`：

| 模块 | 内容 |
| --- | --- |
| `schema.py` | `ActionContract`（不可变）和注册表。契约有 `id` 和整数 `version`：`get("fr3-robotiq@1")` 固定版本，`get("fr3-robotiq")` 取最新版本。没注册的 id 抛 `UnknownContract`；同一个 `(id, version)` 不会被替换 |
| `validation.py` | `validate_action_block(actions, contract_id, metadata)`：对一个候选动作块做确定性检查，返回 `ValidationResult` |

## FR3-Robotiq 契约（`fr3-robotiq@1`）

常量取自生成训练数据和训练的代码；每一条的文件和行号写在契约的 `source` 字段里，`tests/test_counterfactual_contract.py` 会把常量和这些文件对照。

| 事实 | 取值 |
| --- | --- |
| 本体 | Franka Research 3 加 Robotiq 2F-85 |
| 维度 | 7 维，顺序 `x, y, z, rx, ry, rz, gripper`（状态和动作相同） |
| 动作块 | `H = 10` 步（1 秒）；客户端执行 8 步后重新查询 |
| 控制 | 10 Hz，每步 0.1 秒 |
| 每一步是什么 | 下一帧的**绝对**末端位姿：`action[t] = state[t+1]`（最后一帧重复自己的状态） |
| 单位 | 米；欧拉角用弧度 |
| 坐标系 | `franka_hand_tcp` |
| 夹爪 | 二值指令，`1.0` 张开，`0.0` 闭合（是指令，不是测得的开度） |
| 训练时做什么 | rx 加 π 并回绕到 [-π, π)，`x, y, z, rx, ry, rz` 换成相对当前状态的增量，三个角度增量回绕，夹爪保持绝对值 |

候选动作块在存储空间（绝对量）里检查，早于上面的训练变换。契约还注明：该本体没有相机外参标定；各数据转换器对 `rx` 的截断方式不同（采集脚本把 roll 放在 [0, 2π)，LEVI 的转换沿时间展开），所以校验器不对旋转范围做严格限制。

## 校验器检查什么

`validate_action_block(actions, contract_id, metadata=None)` 接收一个 numpy 数组和已注册的契约 id。`metadata` 可含 `representation`（`"absolute"` 或 `"delta"`）、`time_scale`、`action_mask`。它不读文件、不打开 pickle、不执行外部代码。

| 检查 | 什么时候失败 |
| --- | --- |
| `contract` | id 或版本没注册 |
| `array` | 不是数值型 numpy 数组（列表、字符串、布尔、对象） |
| `shape` | 形状不是契约的 `(10, 7)` |
| `finite` | 有 NaN 或无穷大 |
| `gripper_values` | 夹爪出现契约允许的 `0.0` / `1.0` 以外的值 |
| `representation` | 没声明，或与契约的（`absolute`）不同 |
| `time_scale` | 不是正的有限数。缺失、或与 1.0 偏差超过 5%，只是**警告** |
| `action_mask` | 缺失、不是每步一个标志的一维列表，或没有任何有效步 |
| `value_ranges` | 不会失败：绝对量块里位置超过 2 米或角度超过 4π 时警告（可能是毫米或角度制） |

`time_scale` 是该块的控制频率除以契约的：1.0 恰好 10 Hz，0.95 是 9.5 Hz（rollout 约以 9.5 到 9.96 fps 记录）。前面的检查失败导致无法进行的检查记为 `skipped`。任何检查失败则 `passed` 为 false，警告不算失败。

**通过意味着什么。** 只表示表里的这些性质成立。它**不**说明动作在物理上可行、在真机上安全，也不说明它对应任何语言指令。每个结果的 `ValidationResult.limits` 都写明了这一点。
