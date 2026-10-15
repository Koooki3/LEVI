# 事件智能：信号事实、信号语义声明与事件候选

[English](EVENTS.md)

`levi.events` 读取机器人记录的信号说明了什么时候发生了什么。**只有开启了它的时间片段标注计划才用它**（[见下文](#计划中的事件智能)）：事件候选在那里为边界精修挑选额外的帧。下文的在线观测流是独立的显式适配器，运行服务默认不挂载。在离线标注路径中，没开启的计划、界面和命令行行为不变，agent 读到的信号行（`levi.agent.signals`）和锚定复核（`levi.agent.anchored`）也不变。离线读取器和因果内核是作用在数组与记录上的纯计算：不调模型，不导入 Torch，不写文件。可选适配器只通过显式证据存储回调保存编码帧。

| 模块 | 内容 |
| --- | --- |
| `gripper` | 单个夹爪通道带滞回的张开/闭合穿越；信号行和锚定复核共用的内核。 |
| `motion` | 按记录的时间戳计算速度和静止。 |
| `contracts` | 读取器产出的记录：`SignalObservation` 和 `EventCandidate`。 |
| `stream` | 有界已观测前缀的因果候选内核；显式声明与固定阈值。 |
| `signal_profiles` | 每个记录通道的含义：角色、执行器、单位、坐标系、夹爪哪一端是张开。 |
| `facts` | 把一个片段的信号事实按通道和执行器整理成 `SignalObservation`。 |
| `change_points` | 对每个执行器的速度、夹爪电平、角速度做带惩罚的变点检测，输出 `EventCandidate`。 |
| `calibrate` | 在开发集金标准上选定变点惩罚系数的脚本。 |
| `third_party.json`、`third_party` | 本包所用外部来源的结构化登记，以及它必须遵守的规则。 |
| `boundary_metrics` | 多个容差下的边界召回率、每分钟误报候选数、边界 MAE/P90、多个 IoU 阈值下的时间片段 F1。 |
| `candidates` | 一个片段各来源的事件候选，合并后按取证优先级排序。 |
| `sampling` | 证据规划器：按层级贪心地把一次精修的帧预算分给各个窗口。 |
| `ablation` | 同预算下有无事件候选的对照（脚手架，不调模型）。 |

## 记录

两种记录都能从片段的数据表重建，不进标注版本包。每条记录同时保留表的行号（`source_frame_index`）和以秒计的时间（`time_s` / `center_time_s` 和 `time_window_s`），下游不必用 `frame / fps` 换算：表里丢帧时这个换算是错的。JSON 字段 `schema_version` 写 schema 名（`levi.signal_observation.v1`、`levi.event_candidate.v1`），与 LEVI 其他合同一致。

- `SignalObservation`：一条事实（`gripper_open`、`gripper_close`、`height_low`、`height_high`、`still`、`change_point` 等），带执行器、来源（feature、维度名、维度序号、`measured` / `commanded` / `derived`、单位、坐标系）、可选的数值和可选的源表 `source_sha256`。点事实的时间窗口从前一个有限时间戳到它自己的时间戳：变化发生在这两个采样之间。
- `EventCandidate`：某处可能发生了变化。`salience`（0 到 1）只用来排定取证的先后，**不是概率**，不得当作概率展示。在有校准过的估计之前，`boundary_probability` 保持 `null`。`status` 取 `proposed`、`merged` 或 `rejected`。

## 信号语义声明（signal profile）

一份声明（`levi.signal_profile.v1`）列出若干通道，每个通道有 `role`（`gripper`、`position`、`rotation`，声明里还可以写 `ignore`）、`feature` 和 `index`（以及维度名 `dimension`）、`actor_id`、`kind`（`action` 为 `commanded`，其余为 `measured`）、`units`、`frame`、`axis`、夹爪的 `open_level`（`high` 或 `low`）和可选的 `valid_range`。

- `infer(info)` 按信号行一直用的规则读 `meta/info.json`：名字匹配 `grip|finger|claw|jaw` 的浮点向量维度是夹爪；名为 `x`/`y`/`z` 的是位置（每列每个轴取第一个）；`rx`/`ry`/`rz` 或 `roll`/`pitch`/`yaw` 是姿态。名字里带 `left`、`right`、`arm_N`、`robot_N` 的维度归该执行器，其余归 `arm_0`。**名字说明不了夹爪哪一端是张开**，所以推断出的 `open_level` 总是空的，读取器退回“片段开头的电平就是张开”的规则；片段开头夹爪已闭合时，这条规则会把整段读反。
- 声明可以纠正这一点，而且**声明总是优先**。`resolve(info, declared)` 把声明叠加到推断结果上：声明的通道替换同一 (feature, index) 上推断出的通道，或者新增一个通道；`role: "ignore"` 删除名字误导出的通道。同一 feature 里另一个序号上、含义相同的推断通道（位置和姿态看执行器和轴，夹爪看执行器和 measured/commanded）会被删掉，记入声明结果的 `overridden`，并发出 `SignalProfileWarning`（是告警而不是报错：替换推断本来就是声明的用途）。声明的通道排在最前，按角色只取一个通道的读取器取到的是声明的那个。声明里写了数据集没有的列、超出范围的序号、对不上的维度名，或者把同一执行器的同一个轴写了两次，一律拒绝；维度名个数与 shape 不符的列也拒绝。
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

`change_points.candidates(table, info, profile)` 为每个执行器返回 `change_point` 类型的 `EventCandidate`。对每个执行器，它按信号语义声明拼出一个多维序列：位置的速度（按记录的时间戳算，丢帧不会变成跳变）、夹爪电平（有实测通道就不用指令通道）、姿态的角速度（先展开；声明单位为 `deg` 时按角度换算）。每个特征按稳健尺度缩放（取 1–99% 分位范围的四分之一与一阶差分估计的噪声中较大者；没有起伏的特征不用；缺失值由相邻值补齐），然后用分段常数均值和平方误差代价求精确的带惩罚分段（带剪枝的最优划分，即 PELT 一类方法；Truong、Oudre、Vayatis 2020，arXiv:1801.00718 描述了它并在 `ruptures` 中实现，BSD-2-Clause）。LEVI 不依赖 `ruptures`。有最短段长度时，剪枝推迟这段长度再执行（在第 t 行没通过剪枝检验的起点，对 t + min_size 之前的终点仍可能是最优起点），所以结果是精确最优解；测试在 1000 多个随机序列（噪声、阶跃、随机游走；最短段长度 1 到 8）上与不剪枝的最优划分对照。超过 `MAX_ROWS` 的序列在块上搜索，每块保存其中各行的充分统计量（行数 `W`、和 `S`、平方范数之和 `Q`）；连续若干块的代价为 `sum(Q) - |sum(S)|^2 / sum(W)`，正好是这些行自身的平方误差，惩罚也按行数计，所以结果是“变点只能落在块起点”时不分块问题的精确最优解（有测试：随机序列，块长不等、多维、含恒定区间，与在块起点上不剪枝的最优划分对照）。

变点只说明信号的水平变了，不说明发生了什么：它是取证的候选，本身绝不是子任务边界。`salience` 取 `gain / (gain + beta)`（该变点相对惩罚节省的代价），只表示先后，不是概率。

防止过切的措施（每条都有测试）：

| 措施 | 默认值 |
| --- | --- |
| 每个变点的惩罚：`penalty * (d + 1) * log(n)`，`d` 为特征数，`n` 为行数（片段的原始行数，分块搜索时也是） | `penalty = 0.75`（下面校准所得） |
| 最短时间片段（按中位采样间隔折算成行数） | `min_seconds = 0.5` |
| 逐行搜索的最长序列；更长的序列按每块 `ceil(n / 3000)` 行分块搜索（剩余的行并入最后一块），每块保存其中各行的行数、和与平方和，代价和惩罚都与原始行一致；变点落在块的起点，结果里 `bin_rows` 标明，增益仍按行计，最短长度按行数成立（`min_size` 向上取整到整块）。2026-10-15 之前（`change_points.VERSION` v1）块值是块均值乘以块长的平方根、惩罚按块数计：最后一块较短时，恒定水平会被当成变化。惩罚 0.75 是在这次修正之前校准的；超过 3000 行的序列尚未重新校准。搜索最坏是平方复杂度；18000 行噪声（30 Hz、10 分钟）远少于 2 秒（有测试） | `MAX_ROWS = 3000` |
| 每分钟最多变点数；超过时把惩罚乘 1.5 直到满足；等强度的变化会一起消失时，恰好保留能容纳的最强那几个，强度相同按时间先后（结果里 `capped` 标明） | `max_per_minute = 30`（事先设定，没有调） |

在同样缩放的纯噪声上，默认惩罚每分钟产生的变点远少于一个；惩罚加倍时为零（有测试）。

### 校准

`python -m levi.events.calibrate --root <目录> --gold <目录> ... --report-gold <目录> ... [--exclusion-list <清单.json> ...]` 在一组惩罚系数上，用原始机器人采集数据产生候选，与参考时间片段（时间片段的内部边界，按采集时间戳）比较。它在“纯噪声上每分钟不超过一个变点”（合成数据，`noise_rate`）的惩罚中，选 0.5 s 容差下 F1 与最佳值相差不超过 0.01 的最大惩罚（几个设置差不多时，选切得最少的）：参考标注很密，没有这条限制时惩罚会被拉低到连噪声都切的程度（0.3 在噪声上每分钟切 5.8 个）。

它从不读取测试集：gold 目录路径、片段的采集源路径（`--map` 改写之后）或它解析到的目录含 `frozen`、`heldout`、`held-out`，或者采集源按路径、按某个视频的 sha256 命中排除清单（`--exclusion-list` 指定的文件和 `LEVI_POOL_HELDOUT` 的清单，格式同训练池的留出清单）时，整次运行拒绝，而不是跳过该片段（视频只算哈希，不解码）。它只读位姿和夹爪 CSV，跳过作废片段（`episode_success: "void"`），除 `--out` 外不写任何文件；报告记下每个片段的来源和所读文件的 SHA-256、拒绝模式、每个排除清单的 SHA-256，以及每个惩罚在噪声上的变点率。

2026-10-10 的运行（惩罚 0.1 到 8），排除清单用了 screws 与 plates 冻结测试集清单和通用 v2 冻结测试族清单：

```
python -m levi.events.calibrate --root <工作区> \
  --gold <gold>/screws-devgold --gold <gold>/diag15 --report-gold <gold>/generic-pilot \
  --map <plates 源>=<plates 副本> --exclude-source data_collection_robotiq \
  --exclusion-list <gold>/frozen/screws-frozen-v1.json \
  --exclusion-list <gold>/frozen/plates-frozen-v1.json \
  --exclusion-list <gold>/frozen/generic-frozen-v1.heldout.json \
  --penalties 0.1,0.2,0.3,0.5,0.75,1,1.5,2,3,4,6,8
```

- 校准用：screws 开发集金标准（12 个片段，已锁定）和 plates 诊断集（15 个片段，从帧数与金标准一致的副本读取），共 27 个片段、10.1 分钟、每分钟 36.2 个参考边界。没有读取任何测试集（冻结测试族和留出族）。
- 报告用（不参与选择）：通用 v2 试点金标准 9 个片段中的 6 个：剔除了 1 个作废片段（人手入镜）和 2 个来自 robotiq 示范采集的片段（扩大规模阶段之前不用这批数据）。6 个里有 4 个来自冻结和留出切分后剩下的池子（不在任何盲集里）：只能用来报告，不能用来调参。

| 数据 | 惩罚 | 候选/分钟 | 召回@0.2 s | 召回@0.5 s | F1@0.5 s | 误报/分钟@0.5 s | 最近距离 MAE / P90（秒） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 校准 | 0.2（F1 最佳；噪声上 18.7 个/分钟） | 28.3 | 0.29 | 0.56 | 0.62 | 8.1 | 0.48 / 1.12 |
| 校准 | **0.75（选定）** | 26.6 | 0.27 | 0.52 | 0.60 | 7.7 | 0.52 / 1.33 |
| 校准 | 2.0 | 15.3 | 0.19 | 0.37 | 0.52 | 2.0 | 0.95 / 2.38 |
| 报告 | 0.75 | 20.1 | 0.17 | 0.42 | 0.44 | 11.1 | 0.78 / 1.91 |

结论：约一半参考边界在 0.5 s 内有变点，五分之一到四分之一在 0.2 s 内。报告集上误报多了一半，所以单靠变点切分边界很弱：它的用途是和其他来源一起排定取证先后，不是用来分段。样本小，0.01–0.02 的差异在噪声之内。

## 计划中的事件智能

时间片段标注计划可以设置 `workflow.event_intelligence`，其他类型的计划会拒绝它。省略、`null` 和 `{"mode": "off"}` 都表示关闭，存储时都是没有这个键，所以没开启的计划冻结的内容、缓存键和估计与这个配置块出现之前完全一样（有测试把六个有代表性的计划与在 main 上取的快照逐字节比较）。`{"mode": "candidates"}` 表示开启，计划会把每个设置连同取值一起冻结：

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| `sources` | `gripper`、`height`、`still`、`change_point` | 哪些读取器提供候选。 |
| `max_windows` | 4（1–16） | 一个片段的精修最多加多少个候选窗口。未校准的启发式取值。 |
| `merge_seconds` | 0.5 | 与更强候选相距不超过这个值的候选并入它。未校准的启发式取值。 |
| `change_point_penalty` | 0.75 | 变点惩罚系数（上文在开发集金标准上校准所得）。 |
| `planner` | `greedy` | 唯一的规划方式：按优先级取整个窗口，放不下的窗口跳过。 |
| `active_evidence` | `false` | 模型自行要求更多证据：不提供，`true` 会被拒绝。 |

改动其中任何一项，或者开启、关闭这个配置块，都会改变计划摘要，计划必须重新批准。候选算法也在批准范围内：开启的计划存一份 `event_algorithm`——规划器版本（`levi.events.sampling.PLANNER_VERSION`）和各读取器的常数（来源优先级、合并距离、变点惩罚、最短长度和上限、夹爪滞回、静止判定、高度转折），以及它们规范化 JSON 的 SHA-256（`levi.events.candidates.algorithm`）。它计入计划摘要；启动或续跑时，如果计划里的算法与当前代码不一致，运行会拒绝（“The event candidate algorithm changed”）。凡是常数体现不出的改动（候选怎么找、怎么合并、怎么排序，窗口怎么选），都要提高 `PLANNER_VERSION`；变点搜索或其代价的改动提高 `levi.events.change_points.VERSION`，它也在常数之列（2026-10-15 分块修正后为 v2，此前批准的开启计划须重新批准）。关闭的计划没有这份记录。模型缓存的指纹包含计划的上下文，所以一次运行不会复用在其他设置下得到的回答。计划估计的请求数不变，另外加上 `estimate.event_intelligence`（`extra_requests: 0`、一个窗口增加的帧数、每个片段最多增加的帧数、帧上限），并在估计依据里加一句（精修请求的图像变多，token 也会增加，仍以 `max_tokens` 为上限）；`plan.event_intelligence` 把设置再列一遍。审批面板只读显示这些设置（模式、来源、窗口数、合并距离、惩罚系数、规划方式、模型自行要求证据固定关闭、每个片段最多增加的帧数与帧上限、不加请求）；没有这个配置块的计划，界面与以前相同。

**LEVI 自己执行的运行。** 粗看请求之后，LEVI 从运行快照读取该片段的候选（每个片段读一次，运行事件 `event_candidates` 记下数量），再规划精修要读的帧（`levi.events.sampling`），预算是计划的帧上限减去粗看已用的帧，同时受模型的图像上限约束：

1. 草稿自己的边界（及其边界候选）：从不丢弃；只放这些都放不下时，精修照旧放宽采样间隔或分批；
2. 候选窗口，显著度高的在前，最多 `max_windows` 个；
3. 已发布的画面变化窗口（`evidence.refine_top_k`），用同一条规则：放不下就跳过，再试下一个。没开启事件智能时，它们是从变化最小的开始删，直到剩下的放得下；所以预算紧时两者选法不同：开启但片段没有候选，并不等于关闭。

窗口要么整个放进来，要么跳过（从不抽稀）；帧已经全部选过的窗口不占预算，记为已覆盖。窗口的帧按 `observations.frame_scope` 的同一套算法计算（有测试），所以规划时算出的帧数就是运行时受约束的帧数。每次精修的规划都有记录：运行事件 `event_evidence_plan`（每一层加了多少帧、哪些被跳过以及原因）、片段 shard 里的 `event_plan`（候选读取情况和每批的完整规划）；变更集的来源记录（`event_intelligence`）写明设置、算法、每个片段的候选读取情况和每批接受的窗口，以及汇总。草稿太长、分批精修时不加候选窗口，和不加画面变化窗口的规则一样。

**续跑。** 精修帧已经写入、但请求没完成（provider 出错、GPU 闸门、暂停）的片段，续跑时会按当时的图像上限重新规划；本地模型的图像上限是拟合值，每次请求后都会变。贪心规划在两个上限下得到的窗口不嵌套，所以每次规划都把该片段证据账本里已有的帧当作不占预算，新帧计入整个片段的帧上限：账本永远不会超过 `max_evidence_frames`（5000 组合成续跑场景，越限 0 次；不加这条时，运行时测试能复现越限），第一次精修的规划与不看账本时完全一样（有测试）。读哪些窗口取决于图像上限：重复或续跑的运行只有在图像上限相同时才读同一批帧（`event_evidence_plan` 记下了 `budget_images`，对照时按它分组）。

不变的部分：粗看、请求数（候选只给精修请求加帧，不加请求；`Budget.max_calls` 照样会让运行停下）、提示词（模型请求里的 workflow 去掉了 `event_intelligence`，候选也从不作为结论给模型看）、校验以及之后的所有环节。

**信号读不出东西时。** 候选读取失败或什么都没找到，运行只靠画面继续，并用原因码写明：`unreadable`（快照里的文件读不了）、`invalid_signals`（数据表、`meta` 或信号声明不满足读取器的要求）、`internal_error`（其他情况，包括 LEVI 自身的缺陷）、`no_candidates`（读取成功但没有候选）。原因码写进 `event_candidates` 事件、shard 和来源记录；来源记录的 `summary` 统计有候选的片段数和按原因分的无候选片段数，所以“开启了但实际没有候选”的结果能看出来。错误信息不含本机路径；来源记录只写原因码和异常类型。

**外部 agent。** `runs.prepare` 返回 `events_first`（每个片段中，按 `evidence.refine` 的采样间隔、窗口放得进剩余帧上限的候选时刻）和 `events_policy`。`events.candidates`（参数 `run_id`、`episode`；该片段须已准备证据）列出保留的候选（`id`、`event_type`、`actor_id`、`at`、`window`、`salience`）、被合并的数量、同样的 `suggested_around_seconds`、剩余帧数，读取失败或没有候选时还有 `error_code`（以及异常类型）。它是只读能力：只返回文字，没有帧、路径或哈希（有测试固定其字段）。候选来自记录的状态和动作列，不来自相机，所以不需要媒体外传授权；而所有帧仍然需要（没有授权时 `evidence.refine` 和 `evidence.read` 会被拒绝）。没开启事件智能的计划会拒绝这个调用。

### 候选

`candidates.read(table, info, profile, stats, episode_index, sources, merge_seconds, change_point_penalty)` 把各读取器的输出变成 `EventCandidate`：夹爪穿越（`gripper_open`、`gripper_close`）、高度转折（`height_low`、`height_high`）、每段静止的起点和终点（`still_start`、`still_end`），以及变点。每个来源有固定的优先级——夹爪 0.9，变点 0.7 乘以它自己的显著度，高度 0.5，静止 0.3。**这些优先级、`max_windows` 和 `merge_seconds` 都是未校准的启发式取值**，人为设定，没有在任何金标准上调过；只有变点惩罚系数经过校准。校准计划：只用开发集金标准（screws 开发集金标准、plates 诊断集；不用任何冻结集或留出集），先用 `ablation plan` 做不调模型的覆盖对照，再做下文的标注对照；新取值就是新的 `event_algorithm`，需要重新批准。与更强候选相距不超过 `merge_seconds` 的候选并入它：更强的保留自己的时间，加上它们的来源，窗口扩大到覆盖它们；被并入的候选仍留在列表里，状态为 `merged`。优先级相同时，来源种类多的在前，再按时间先后，再按编号。结果先是保留的候选（按优先级），后是被合并的候选；顺序与输入顺序无关（有测试）。

### 待 GPU 窗口

同样的帧预算下事件候选能否让标注更好，要用本地模型才能测，所以还没有测。GPU 空闲时（持 GPU 锁）只在开发集片段上做（不用任何冻结集或留出集，不在测试集上调任何参数）：

1. 对同一批片段做两次时间片段标注运行，模型、解码、预算和帧上限都相同，一次带 `event_intelligence: {"mode": "candidates"}`，一次不带（可再加一次 `max_windows` 为 2 的），另配常规的不经 LEVI 的对照组。
2. `python -m levi.events.ablation plan --spec <spec.json>`：不调模型，看每组的精修会看哪里、覆盖多少参考边界（spec 格式见该模块的文档字符串）。
3. `python -m levi.events.ablation score --reference <ref.json> --durations <durations.json> --arm off=<a.json> --arm candidates=<b.json>`：每组在 0.1/0.2/0.5 s 下的边界得分和时间片段 F1，以及每组相对 `off` 的差值。

命令行拒绝任何路径里带冻结集或留出集字样的输入，只写到标准输出或 `--out`。同样待测的还有每组请求的 token 成本（每次精修请求的图像更多）和墙钟时间。

## 第三方来源

`levi/events/third_party.json`（`levi.third_party_registry.v1`）登记本包依赖或借鉴的每个外部来源：来源 `source`、版本 `version`、许可 `licenses`（代码、权重、数据）、采用方式 `use`（`dependency`、`adopt-idea`、`adapt-code`、`vendored`）、是否随 LEVI 分发（`shipped`）或拷贝了代码（`code_copied`）、能否再分发（`redistributable`）、带核实过的 `reference_key` 的引用 `citation`，以及用在哪里（`where`）。测试用 `levi.events.third_party.problems` 强制这些规则：代码许可为非商业、缺失或未核实的来源只能借鉴思路（不拷贝、不分发）；有引用就必须对应已核实的参考条目；依赖必须在 `pyproject.toml` 里声明；`levi/events` 模块里出现的每个 arXiv 编号都必须属于某个已登记来源。目前登记了 `ruptures`（只借鉴思路，BSD-2-Clause，未安装）、VideoSeek（只借鉴思路：证据规划器的分层名称来自它的 overview、skim、focus 三个工具；Lin 等 2026，arXiv:2603.20185，代码 MIT，未拷贝任何代码）和现有核心依赖 NumPy、pydantic：本包没有新增依赖。发布用的清单仍是 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 显式启用的在线观测流

`levi.events.stream.CausalSignals` 逐次读取已观测前缀。
`feed(Sample(step, observed_ns, values), now_ns=...)` 使用冻结的声明式
`SignalProfile`：夹爪单位、有效范围、开口极性必须明确；位置必须是同一声明
坐标系中的实测 xyz，单位为米。`StreamConfig` 固定阈值，不从未来样本拟合。
缺失、非法或越界信号、观测间断都会打断连续状态，报告 `unknown`，不会凭空
补出事件。在线旋转、变点和物体语义检测尚未实现。

`levi.automatic.adapters.signal_events.CachedObserver` 包装既有机器人。
每个既有 `observe()` 只调用一次，原观测原样返回，缓存只有一个有界快照。
`SignalEvents.poll` 消费该快照，不请求机器人或相机。调用者须提供当前已完成
控制步的显式 `step_of`、同一个时钟对象、机器人 actor、冻结的 `Binding`、
本人证据目录及有字节预算的 `keep_frame(ref, encoded_bytes)` 回调。
`EvidenceStore.keep_frame` 可直接使用；`open` / `poll` / `close` 与
`FileEvents` 使用相同的接口，输出符合 `levi.aeri.event.v1`。

SDK `pose[6]` 是**指令**，对应事件名为 `gripper_command_open` /
`gripper_command_close`。实测夹爪必须绑定 `robot_state.gripper_position`，
并提供同次 `getstate` 读取的 `gripper_measurement`：`route=getstate`、
`field=gripper_pos`、严格递增的 `generation`、位于本次观测调用内的
`read_from_ns` / `read_through_ns`、相同 `clock_domain`，以及与实测字段相等的
归一化 `value`。缺测量时不能用指令代替。实测位置绑定 `pose[0:3]`；
全部信号必须属于显式指定的同一机器人 actor。

候选包含观测流代次内的事件 ID、序号、actor、准确观测步数窗口、时钟、
信号类型及单位、检测器版本中的声明 profile 哈希，以及当前帧引用。
适配器把缓存图像编码成 JPEG，通过回调保存这些确切字节，再以不跟随符号
链接的方式重读有界普通文件，核对编码字节与落盘文件的 SHA-256，待取候选投递前再次核对。
ndarray 像素哈希不能冒充已保留 JPEG 的哈希。缺失、跳过、损坏或过期证据
会报告降级，不输出候选消息。未来、过期、错步数或异时钟观测被拒绝；
关闭或重新开启片段会清空观测缓存，旧观测流 ID 不能继续使用。

存储有上限：缓存最多两幅图像、合计 16 MiB 像素；profile 最多 64 个通道；
标量历史可配置 2–4096 个样本，默认 128；待取消息和可撤销候选各 128 个；
一个适配器实例最多登记 1024 个片段身份。证据目录字节预算由调用者负责，
事件模块不删除保留的证据。`status` 显示未知原因、历史淘汰和消息丢弃计数；
`retract(stream_id, event_id)` 可以在有界历史内显式撤销当前代次的候选。

**验证范围**：CPU/mock 测试覆盖既有观测消费、前缀因果性、单位和极性、
缺信号回退、真实 JPEG 哈希、`EvidenceStore` / `FileEvents` 兼容、有界存储及
步数、时钟、代次攻击。FR3 入口仅在下述已审核声明存在时挂载；实时成败判定和后台标注入口默认不挂载；
GPU 准入、服务接线与真机验证仍是独立门禁。候选只请求新证据和既有稳定窗口
判定，不代表任务成功，也不是运动指令。


### 显式 FR3 事件绑定

人工入口 `python -m levi.automatic.fr3_run` 的既有绑定文件可选填内联
`signal_events`；不填时仍使用 `FileEvents`。对象必须完整包含 `schema`
（`levi.aeri.signal_events.binding.v1`）、`actor_id`（`arm_0`）、`profile`、
`bindings` 和 `config`，空值或缺项被拒绝。`profile` 必须有 `schema_version`、
`origin: declared`、非空 `channels` 和空 `overridden`；每个通道明确写角色、
feature、index、actor、kind 和单位。夹爪还须声明有效范围和张开端；实测 xyz
须声明轴、米和 FR3 的 `base` 坐标系，不从任务名称或数据集推断语义。

每个 binding 明确写 `feature`、整数 `index`、`kind` 和 `path`：实测 xyz
绑定 `["pose", 0]` 至 `["pose", 2]`；实测夹爪绑定
`["robot_state", "gripper_position"]`；夹爪指令绑定 `["pose", 6]`。
`config` 必须显式给出全部 `StreamConfig` 字段：`max_history`、`max_age_ns`、
`still_ns`、`motion_speed_m_s` 和 `max_gap_ns`。数值与物理阈值由人工提供，
入口不补默认阈值；共享夹爪检测器继续使用既有归一化滞回规则。

`--check` 显示声明和事件来源。全部内联字节进入 `binding_sha256`；
`--execute --expect-binding` 在终端确认前后核对审批摘要。声明、阈值或
binding 改变后，即使任务计划 SHA 相同，也必须重新审核绑定。
同一个 `CachedObserver` 包装既有 Robot，供策略、记录器、场景／复位提供者
和判定器使用；只读步号来自编排器当前控制步，不使用回家／复位动作计数。
事件轮询不新增观测或 HTTP 调用。实际运行的 EvidenceStore 在编排器创建后、
事件片段打开前挂载。

拒绝候选后，同一控制步的新鲜观测只消费一次，不重复送入因果检测器；
只有控制步前进才产生新候选。观测时间仍须前进，并通过年龄、控制步、
时钟和片段隔离检查；排队证据交付前仍重新核验。

完整验证的带帧信号来源可供模型 worker 使用任务原有的提前停止设置；旧
FileEvents 来源仍拒绝这项模型 worker 组合，场景／复位 READY 心跳无需伪称
外部 events 能力。信号候选保留真实当前 JPEG，再由既有正向 Media／C5 或
复位初始状态合约判定是否停止。候选不绕过 GPU 准入、看门狗、停止信号、
运动隔离、终局判定和人工批准。CPU 测试走真实入口、假 SDK 与注入模型；
实际 GPU 时限和物理执行仍未验证。


### 事件核验使用暂存观测来源

显式 FR3 信号入口在发送动作前暂存当前缓存观测。内部台账把流代次、来源序号、运行、
片段、角色、主机时钟、控制步和观测时间绑定到同一录制路径及原始观测对象；图像转换成
带哈希的不可变 JPEG。`phase=pre_action` 与独立的 `recording_row` 区分观测和已确认动作：
策略块失败时控制步仍会推进，但不会增加录制行。

台账最多保留绑定中已冻结的 `max_history` 个来源，另保留开头来源；全部保留的 JPEG
合计不超过 **16 MiB**，复用已有信号帧字节上限。超预算会淘汰旧来源；候选已被淘汰、
来源缺失、时间倒序或时钟／片段／代次不符都会让事件核验不可用。只逐张解码实际选中的
JPEG。编码后的事件请求也有 16 MiB 总预算，沿用服务的图像数量与尺寸上限。接近服务
1280 像素长边上限的高熵图像，可能只保留少量观测就耗尽预算，提前淘汰所需事件窗口并
返回不可用。真实观测不足时不能补造帧或放宽证据要求。共享主机上的合成 CPU 计时只
测暂存成本，不能证明真实相机窗口容量或完整控制循环的截止时间。

事件请求不会额外读取机器人观测，也不向旧录制缓冲区补帧。它使用实际 C5 客户端的固定
偏移采样与冻结规格；每个必需末端视角都必须实际选中至少两个独立来源序号，且最早与
最晚选帧覆盖候选到稳定末端。开头图只供规格的开头检查，不能扩大这些返回范围。
实际选帧不足时返回不可用，不改变偏移或模型判定。HTTP 仍是原四个顶层字段并复用编码
与传输；省略可选图像 `step`、把片段 `steps` 留空，因为动作前观测不是已执行的 CSV 行。
控制步及哈希留在内部证明中，不增加 HTTP 字段。

票据在传输前冻结实际采样范围和事件编号；后续新帧、缓冲区淘汰和请求修改不会改变
证明。取消或已交付的票据不能再次返回结果。C5 模型工作线程沿用原有请求所有权，直到
实际退出才释放。没有事件的旧终局请求和 reset 初始状态契约路径保留兼容行为；没有
对应缓存来源台账的文件事件返回不可用。这不开放策略运行期间的 GPU 门，也不构成
机械安全保证；使用真机仍需操作员批准。
