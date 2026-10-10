# 多模型评测计划（AERI campaign）

[English](AUTOMATIC_CAMPAIGN.md)

**状态：只有库。** 本页将说明自动测评流水线（AERI）怎样在同一个任务上比较多个策略（组）。目前只有统计方法：`levi/automatic/analysis/`，一组纯函数，还没有任何命令、页面或 API 调用它。评测计划、时间表、报告和页面是后续工作，届时在本页另写章节。

## 统计方法

`levi.automatic.analysis` 把试验结果算成数字：点估计、区间、p 值和诊断。它不生成任何句子，措辞由报告生成器决定。

**保证。**

- 只用标准库和 numpy；不读任何文件，不导入其他 LEVI 模块，唯一的例外是 `levi.live.stats.wilson`，这样实时页面和报告给出的 Wilson 区间完全一致。
- 所有随机抽样都显式传入 `seed`，各自建生成器；没有缓存，也没有模块级状态。同样的输入在任何进程、任何线程数下都得到逐位相同的输出（有测试）。
- 每个函数返回可直接序列化为 JSON 的字典，包含 `schema_version`（`levi.aeri.analysis.v1`）、`method`、`implementation`（`levi.automatic.analysis.<模块>@<版本>`）、`references`（引用表 `levi/automatic/analysis/references.py` 的键）、`exploratory` 和 `caveats`（每条带固定的 `code`）。
- 缺失值（`None`、NaN）被剔除，剔除数写进结果。无穷大、不可能的计数和错误选项抛出 `AnalysisInputError`。某组没有试验时给出 `available: false`，不给数字。
- 计算量有上限：bootstrap 重抽样和置换最多 10^5 次；大样本分块重抽样，取值很少的数据按类别计数重抽样（10 万对用时远少于 1 秒）。

**是否探索性。** 成功率比较只有同时满足两个条件才算确证性（`exploratory: false`）：调用方给出研究设计时要检出的差值（`design_difference`，在第一个试验之前定下），并且在观测到的试验数下，对这个差值的精确功效不低于 80%。每个这类结果都附带本样本量能检出的最小差值（`detectable_difference` 说明）。没有功效模型的结果（连续指标、生存分析、总体检验、诊断）一律是探索性的。报告结论还取决于预注册、标签口径和漂移检查，这些由报告生成器处理。

### 方法

默认 95% 区间、双侧 α=0.05。“精确”指用完整的离散分布计算，不用正态近似。

| 问题 | 函数 | 方法 | 适用条件 | 局限 |
| --- | --- | --- | --- | --- |
| 单组成功率 | `proportion` | Wilson 得分区间（主区间）和 Clopper–Pearson 精确区间 | 总是 | Clopper–Pearson 偏保守（覆盖率至少 95%，往往更高） |
| 同一组布局卡上的两组 | `mcnemar` | McNemar 精确条件检验、mid-p 版本和渐近统计量 | 成对试验（同一张卡、同一轮） | 只用不一致对；不一致对少于 10 时渐近值不可靠 |
| | `newcombe_paired` | Newcombe 成对得分区间（方法 10），估计 p_B − p_A | 成对试验 | 由 Wilson 区间组合而成，不是精确区间 |
| | `paired_bootstrap` | 按对重抽样，对均值（或中位数）差做 bootstrap；默认百分位法，可选 BCa | 成对的二元或连续结果 | 少于约 30 对时偏宽松；BCa 需要刀切法 |
| 两个独立组 | `fisher_exact` | Fisher 精确检验（双侧：累加不比观测表更可能的所有表） | 不成对试验（复位策略模式、偏离的卡） | 以两个边际为条件，偏保守 |
| | `boschloo_exact` | Boschloo 无条件检验（以 Fisher p 值为统计量，在公共成功率的网格上取最大并局部细化） | 不成对试验，功效高于 Fisher | 最大值是数值求得的 |
| | `newcombe_independent`、`agresti_caffo` | Newcombe 混合得分区间（方法 10）；Agresti–Caffo 加二法区间作对照 | 不成对试验 | 近似区间 |
| | `posterior_prob_greater` | 均匀先验 Beta(1, 1) 下的 P(p_B > p_A)，精确计算 | 只作描述 | 不是检验 |
| 两组以上 | `cochran_q`、`friedman` | 按区组计算的 Cochran Q（二元）和 Friedman 秩检验（连续）；p 值由区组内置换组标签求得 | 每个区组内每组各跑一次 | 只是总体检验：说明组间有差异，不说明哪两组 |
| 多个比较 | `holm` | Holm 逐步校正 | 预注册的主要两两比较族 | 在任意依赖结构下控制族错误率 |
| | `bonferroni` | Bonferroni 校正 | 需要单一阈值时（字母标记图） | 功效从不高于 Holm |
| | `benjamini_hochberg` | Benjamini–Hochberg 错误发现率 | 探索性的次要指标 | 原文的证明假设检验相互独立；同一批试验上的指标相关，所以只用于筛选 |
| 连续指标 | `wilcoxon_signed_rank` | Wilcoxon 符号秩检验；非零差值不超过 50 个时精确计算（含并列），更多时用正态近似 | 成对的完成时间、步数 | 差值为 0 的对被剔除 |
| | `mann_whitney` | Mann–Whitney 秩和检验；总数不超过 60 时精确计算 | 不成对的指标 | 检验的是随机大小顺序，不是均值 |
| | `hodges_lehmann` | Hodges–Lehmann 位移（Walsh 平均或交叉差值的中位数），附百分位 bootstrap 区间 | 位移的效应量 | 超过 400 个值不算区间；Walsh 平均超过约 450 万个时不给估计 |
| | `cliffs_delta`、`improvement_share` | Cliff's δ（不成对）；B 更好的对数占比（成对） | 有序的效应量 | 不反映差值大小 |
| | `smoothness` | SPARC（速度谱弧长）和基于速度的对数无量纲 jerk | 末端位置轨迹的平滑度 | 受采样率和截断方式影响：只比较条件相同的轨迹 |
| 时间到成功 | `kaplan_meier`、`logrank`、`rmst` | Kaplan–Meier 曲线（Greenwood 方差、log(−log) 区间）；k 组 log-rank 检验；到共同上限为止的限制平均生存时间，差值附 bootstrap 区间 | 失败或中止的片段右删失 | 故障和操作员中止是竞争事件，这里按删失近似处理 |
| 失败模式 | `failure_modes` | 按 `stop_reason` 和后台复核的失败类别计数，各附 Wilson 区间 | 每份报告 | 只作描述，不做检验 |
| 提前终止 | `early_stop` | 误终止率只来自判定为失败且跑完全程的对照片段（Wilson）；组间按卡位配对用 McNemar 比较 | 开启提前终止的组 | 没有这类对照片段时为 `unavailable`；非对照片段只能给出下界 |
| 判定器与人工标签 | `agreement`、`cohen_kappa`、`misjudgement_by_arm`、`rogan_gladen` | 混淆矩阵、一致率（Wilson）、误报成功率与漏报成功率（与实时页面定义相同）、Cohen κ；组间误判率差异的置换检验；Rogan–Gladen 校正 | 有人工标签的报告 | κ 受成功率影响（与一致率并列看）；Rogan–Gladen 只是敏感性分析 |
| 样本量 | `power_table`、`min_detectable_difference` | 枚举求精确功效：Fisher（不成对）和含组内相关的 McNemar（成对）；在 0.01 网格上求 n=10…100 时的最小可检出差 | 规划阶段，以及每个比较附带的说明 | 每组超过 200 次时改用正态近似（成对用 Connor 公式） |
| 漂移与顺序 | `reference_drift`、`arm_time_interaction`、`carryover`、`drift_warning` | 参照组按轮成功率的 Mann 趋势检验，加前后半程差值；B − A 差值在前后半程是否不同的置换检验；按前一组分层的成功率；任一 p < 0.05 时亮起一个标志 | 有参照组且多轮的 campaign | 诊断用：用来发现问题，从不修正结果 |

**规划用数字。** 80% 功效、双侧 α=0.05、基线成功率 0.5 时，每组 20 次能检出的最小差值是 0.42（不成对）和 0.44（成对，组内无相关）；30 次时 0.36 和 0.37；50 次时 0.29 和 0.29（`power_table`；测试用暴力枚举和模拟逐个复算）。每组 20–30 次只能分辨很大的差异。

### 参考文献

各条都按 DOI 或 arXiv 记录核对过（2026-10-10）。书目信息与英文版相同，见 [English](AUTOMATIC_CAMPAIGN.md#references)：Agarwal 等 2021；Agresti 与 Caffo 2000；Balasubramanian 等 2015；Benjamini 与 Hochberg 1995；Boschloo 1970；Brown 等 2001；Cliff 1993；Clopper 与 Pearson 1934；Cochran 1950；Cohen 1960；Connor 1987；Dunn 1961；Efron 1979、1987；Fagerland 等 2013；Feinstein 与 Cicchetti 1990；Fisher 1922；Friedman 1937；Hodges 与 Lehmann 1963；Holm 1979；Kaplan 与 Meier 1958；Kress-Gazit 等 2024；Mann 1945；Mann 与 Whitney 1947；Mantel 1966；McNemar 1947；Newcombe 1998a、1998b；Rogan 与 Gladen 1978；Royston 与 Parmar 2013；TRI LBM Team 等 2025；Wilcoxon 1945；Williams 1949；Wilson 1927。
