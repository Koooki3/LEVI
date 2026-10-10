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
| `change_points` | 对每个执行器的速度、夹爪电平、角速度做带惩罚的变点检测，输出 `EventCandidate`。 |
| `calibrate` | 在开发集金标准上选定变点惩罚系数的脚本。 |
| `third_party.json`、`third_party` | 本包所用外部来源的结构化登记，以及它必须遵守的规则。 |
| `boundary_metrics` | 多个容差下的边界召回率、每分钟误报候选数、边界 MAE/P90、多个 IoU 阈值下的时间片段 F1。 |

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

## 边界与时间片段指标

`boundary_metrics` 用参考边界（按片段，单位秒）评判候选：

- `boundary_scores(reference, candidates, durations, tolerances=(0.1, 0.2))`：对每个容差给出边界召回率（容差内有候选的参考边界所占比例；一个候选只算一个边界，按最大一对一匹配）、精确率、F1、误报候选（没匹配上任何边界的候选）数和每分钟误报数，以及匹配误差的 MAE/P90。不分容差的部分：每分钟候选数，以及每个参考边界到最近候选的距离（`nearest_mae`、`nearest_p90`；漏掉的边界按这个距离计入；整个片段没有候选时计入 `no_candidate`）。
- `segment_f1(reference, candidates, ious=(0.3, 0.5, 0.7))`：每个 IoU 阈值下的时间片段 F1，匹配规则与 `levi.harness.grading` 相同（同一子任务、IoU 不低于阈值时取最佳、每个候选只用一次）；`mean` 像 `grading.grade` 一样对每个片段的 F1 求平均（IoU 0.3 时等于 grading 的 `segment_f1`），`pooled` 把所有匹配合在一起算。
- `boundaries(segments)` 把首尾相接的时间片段转成内部边界。

`levi.agent.evaluation.temporal`（单一容差、按身份匹配的行）和 `levi.harness.grading`（单一 IoU）保持不变。

## 变点

`change_points.candidates(table, info, profile)` 为每个执行器返回 `change_point` 类型的 `EventCandidate`。对每个执行器，它按信号语义声明拼出一个多维序列：位置的速度（按记录的时间戳算，丢帧不会变成跳变）、夹爪电平（有实测通道就不用指令通道）、姿态的角速度（先展开；声明单位为 `deg` 时按角度换算）。每个特征按稳健尺度缩放（取 1–99% 分位范围的四分之一与一阶差分估计的噪声中较大者；没有起伏的特征不用；缺失值由相邻值补齐），然后用分段常数均值和平方误差代价求精确的带惩罚分段（带剪枝的最优划分，即 PELT 一类方法；Truong、Oudre、Vayatis 2020，arXiv:1801.00718 描述了它并在 `ruptures` 中实现，BSD-2-Clause）。LEVI 不依赖 `ruptures`。有最短段长度时，剪枝推迟这段长度再执行（在第 t 行没通过剪枝检验的起点，对 t + min_size 之前的终点仍可能是最优起点），所以结果是精确最优解；测试在 1000 多个随机序列（噪声、阶跃、随机游走；最短段长度 1 到 8）上与不剪枝的最优划分对照。

变点只说明信号的水平变了，不说明发生了什么：它是取证的候选，本身绝不是子任务边界。`salience` 取 `gain / (gain + beta)`（该变点相对惩罚节省的代价），只表示先后，不是概率。

防止过切的措施（每条都有测试）：

| 措施 | 默认值 |
| --- | --- |
| 每个变点的惩罚：`penalty * (d + 1) * log(n)`，`d` 为特征数，`n` 为行数 | `penalty = 0.5`（下面校准所得） |
| 最短时间片段 | `min_seconds = 0.5` |
| 每分钟最多变点数；超过时把惩罚乘 1.5 直到满足（结果里 `capped` 标明） | `max_per_minute = 30`（事先设定，没有调） |

在同样缩放的纯噪声上，默认惩罚每分钟产生的变点远少于一个；惩罚加倍时为零（有测试）。

### 校准

`python -m levi.events.calibrate --root <目录> --gold <目录> ... --report-gold <目录> ...` 在一组惩罚系数上，用原始机器人采集数据产生候选，与参考时间片段（时间片段的内部边界，按采集时间戳）比较，选 0.5 s 容差下 F1 与最佳值相差不超过 0.01 的最大惩罚（几个设置差不多时，选切得最少的）。路径里含 `frozen`、`heldout`、`held-out` 的目录一律拒绝；只读位姿和夹爪 CSV（不读视频）；除 `--out` 外不写任何文件。报告记下每个片段的来源和所读文件的 SHA-256。

2026-10-10 的运行（惩罚 0.1 到 8）：

- 校准用：screws 开发集金标准（12 个片段，已锁定）和 plates 诊断集（15 个片段，从帧数与金标准一致的副本读取），共 27 个片段、10.1 分钟、每分钟 36.2 个参考边界。没有读取任何测试集（冻结测试族和留出族）。
- 报告用（不参与选择）：通用 v2 试点金标准 9 个片段中的 7 个；来自 robotiq 示范采集的 2 个被排除（扩大规模阶段之前不用这批数据）。

| 数据 | 惩罚 | 候选/分钟 | 召回@0.2 s | 召回@0.5 s | F1@0.5 s | 误报/分钟@0.5 s | 最近距离 MAE / P90（秒） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 校准 | 0.2（F1 最佳） | 24.2 | 0.25 | 0.48 | 0.58 | 6.6 | 0.58 / 1.53 |
| 校准 | **0.5（选定）** | 24.1 | 0.23 | 0.48 | 0.57 | 6.8 | 0.59 / 1.52 |
| 校准 | 2.0 | 15.3 | 0.19 | 0.37 | 0.52 | 2.0 | 0.95 / 2.38 |
| 报告 | 0.5 | 23.3 | 0.20 | 0.46 | 0.44 | 13.3 | 0.72 / 1.87 |

结论：惩罚从 0.1 到 1 之间 F1 基本不变，因为每分钟上限在这一段起作用（参考标注比上限更密）；约一半参考边界在 0.5 s 内有变点，约四分之一在 0.2 s 内。报告集上误报翻倍，所以单靠变点切分边界很弱：它的用途是和其他来源一起排定取证先后，不是用来分段。样本小，0.01–0.02 的差异在噪声之内。

## 第三方来源

`levi/events/third_party.json`（`levi.third_party_registry.v1`）登记本包依赖或借鉴的每个外部来源：来源 `source`、版本 `version`、许可 `licenses`（代码、权重、数据）、采用方式 `use`（`dependency`、`adopt-idea`、`adapt-code`、`vendored`）、是否随 LEVI 分发（`shipped`）或拷贝了代码（`code_copied`）、能否再分发（`redistributable`）、带核实过的 `reference_key` 的引用 `citation`，以及用在哪里（`where`）。测试用 `levi.events.third_party.problems` 强制这些规则：代码许可为非商业、缺失或未核实的来源只能借鉴思路（不拷贝、不分发）；有引用就必须对应已核实的参考条目；依赖必须在 `pyproject.toml` 里声明；`levi/events` 模块里出现的每个 arXiv 编号都必须属于某个已登记来源。目前登记了 `ruptures`（只借鉴思路，BSD-2-Clause，未安装）和现有核心依赖 NumPy、pydantic：本包没有新增依赖。发布用的清单仍是 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
