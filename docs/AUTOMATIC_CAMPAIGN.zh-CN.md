# 多模型评测计划（AERI campaign）

[English](AUTOMATIC_CAMPAIGN.md)

**状态：只有库。** 本页将说明自动测评流水线（AERI）怎样在同一个任务上比较多个策略（组）。目前有两部分，都还没有任何命令、页面或 API 调用：统计方法（`levi/automatic/analysis/`，一组纯函数），以及把统计结果画给网页和论文的图表写出器（见[图表](#图表)）。评测计划、时间表、报告和页面是后续工作，届时在本页另写章节。

## 统计方法

`levi.automatic.analysis` 把试验结果算成数字：点估计、区间、p 值和诊断。它不生成任何句子，措辞由报告生成器决定。

**保证。**

- 只用标准库和 numpy；不读任何文件，不导入其他 LEVI 模块，唯一的例外是 `levi.live.stats.wilson`，这样实时页面和报告给出的 Wilson 区间完全一致。
- 所有随机抽样都显式传入 `seed`，各自建生成器（PCG64）；没有缓存，也没有模块级状态。**在同一 numpy 版本下**，同样的输入在任何进程、任何线程数下都得到逐位相同的输出（有测试）。不同 numpy 版本之间，`Generator` 各方法的随机流可能改变（numpy 不保证它们稳定；本项目的 `uv.lock` 在 Python 3.12 以下解析出 numpy 2.4.6，3.12 及以上解析出 2.5.3），这时 bootstrap 和置换结果只在蒙特卡洛误差内一致（用不同随机流做了测试）。每个结果都记录 `numpy_version`、`bit_generator` 和 `algorithm_version`。
- 每个结果函数返回可直接序列化为 JSON 的字典，包含 `schema_version`（`levi.aeri.analysis.v1`）、`method`、`implementation`（`levi.automatic.analysis.<模块>@<版本>`）、`references`（引用表 `levi/automatic/analysis/references.py` 的键）、`exploratory` 和 `caveats`（每条带固定的 `code`）。例外是数值辅助函数 `power_paired`、`power_unpaired` 和 `min_detectable_difference`，它们给规划代码直接返回一个数。
- 缺失值（`None`、NaN）被剔除，剔除数写进结果。无穷大、不可能的计数和错误选项抛出 `AnalysisInputError`。某组没有试验时给出 `available: false`，不给数字（也不给 p 值）。
- 计算量有上限：bootstrap 重抽样和置换最多 10^5 次；大样本分块重抽样，取值很少的数据按类别计数重抽样（10 万对用时远少于 1 秒）。

**是否探索性。** 判定只用预先设定的值，从不用试验结果。成功率比较只有满足以下条件才算确证性（`exploratory: false`）：调用方给出研究设计时要检出的差值（`design_difference`），可选地给出计划的基线成功率（`design_baseline`），两者都在第一个试验之前定下；并且在所用的试验数下，对这个差值的功效达到 80%：每组不超过 200 次时用枚举求精确功效，超过 200 次时用正态近似（略偏乐观，见下面“样本量”一行）。没有给计划基线时，取 0.05 网格上最不利的基线来判定。每个结果都返回 `power_basis`（`basis: "planned"`、预设值、找到的最小功效和给出这个功效的基线 `least_favourable_baseline`），以及本样本量在计划基线（没给时取设计表的基线 0.5）下能检出的最小差值。

为什么不用观测到的成功率：用观测数据算出的功效（事后功效）只是观测 p 值的函数，没有提供新信息；而且会让同一份预注册设计仅仅因为结果偏极端就变成“确证性”。所以预设值相同、试验数相同时，`exploratory` 总是相同，与结果无关（有测试）。

没有功效模型的结果（连续指标、生存分析、总体检验、诊断）一律是探索性的。多重校正（`holm`、`bonferroni`）只有在族内每个检验都是确证性时才是确证性的（用 `exploratory=[...]` 传入各检验的标记）；Benjamini–Hochberg 是筛选工具，一律是探索性的。单个检验的 `exploratory: false` 没有考虑多重性。报告结论还取决于预注册、标签口径和漂移检查，这些由报告生成器处理。

### 方法

默认 95% 区间、双侧 α=0.05。“精确”指用完整的离散分布计算，不用正态近似。

| 问题 | 函数 | 方法 | 适用条件 | 局限 |
| --- | --- | --- | --- | --- |
| 单组成功率 | `proportion` | Wilson 得分区间（主区间）和 Clopper–Pearson 精确区间 | 总是 | Clopper–Pearson 偏保守（覆盖率至少 95%，往往更高） |
| 同一组布局卡上的两组 | `mcnemar` | McNemar 精确条件检验、mid-p 版本和渐近统计量 | 成对试验（同一张卡、同一轮） | 只用不一致对；不一致对少于 10 时渐近值不可靠；超过 2000 个不一致对时在对数空间求和 |
| | `newcombe_paired` | Newcombe 成对得分区间（方法 10），估计 p_B − p_A | 成对试验 | 由 Wilson 区间组合而成，不是精确区间 |
| | `paired_bootstrap` | 按对重抽样，对均值（或中位数）差做 bootstrap；默认百分位法，可选 BCa | 成对的二元或连续结果 | 小样本时偏宽松（见下）；差值全是同一个值时区间宽度为 0，标为 `degenerate_bootstrap`，应改用 McNemar 和 Newcombe；BCa 需要刀切法 |
| 两个独立组 | `fisher_exact` | Fisher 精确检验（双侧：累加不比观测表更可能的所有表） | 不成对试验（复位策略模式、偏离的卡） | 以两个边际为条件，偏保守；总数超过 4000 时在对数空间求和 |
| | `boschloo_exact` | Boschloo 无条件检验（以 Fisher p 值为统计量，在公共成功率的网格上取最大并局部细化） | 不成对试验，功效高于 Fisher | 最大值是数值求得的；每组超过 300 次时拒绝计算 |
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
| 时间到成功 | `kaplan_meier`、`logrank`、`rmst` | Kaplan–Meier 曲线（Greenwood 方差、log(−log) 区间）；k 组 log-rank 检验；到共同上限为止的限制平均生存时间，差值附 bootstrap 区间 | 失败或中止的片段右删失 | 故障和操作员中止是竞争事件，这里按删失近似处理；上限超过某组最后观测时间时，该组曲线按最后的值延长，并标为 `tau_beyond_follow_up`。Greenwood 方差和带并列校正的 Mann–Kendall 方差是标准公式，没有单列文献 |
| 失败模式 | `failure_modes` | 按 `stop_reason` 和后台复核的失败类别计数，各附 Wilson 区间 | 每份报告 | 只作描述，不做检验 |
| 提前终止 | `early_stop` | 误终止率只来自判定为失败且跑完全程的对照片段（Wilson）；组间按卡位配对用 McNemar 比较 | 开启提前终止的组 | 按（卡位，轮次）配对；配不上的对照片段计入 `unpaired`，同一组里同一个键出现两次时拒绝计算。没有这类对照片段时为 `unavailable`；非对照片段只能给出下界 |
| 判定器与人工标签 | `agreement`、`cohen_kappa`、`misjudgement_by_arm`、`rogan_gladen` | 混淆矩阵、一致率（Wilson）、误报成功率与漏报成功率（与实时页面定义相同）、Cohen κ；组间误判率差异的置换检验；Rogan–Gladen 校正 | 有人工标签的报告 | κ 受成功率影响（与一致率并列看）；Rogan–Gladen 只是敏感性分析 |
| 样本量 | `power_table`、`min_detectable_difference` | 枚举求精确功效：Fisher（不成对）和含组内相关的 McNemar（成对）；在 0.01 网格上求 n=10…100 时的最小可检出差 | 规划阶段，以及每个比较附带的说明 | 每组超过 200 次时改用正态近似（成对用 Connor 公式）；近似值可能把最小可检出差低估约 0.01（偏乐观），`power_approximate` 说明里会写明 |
| 漂移与顺序 | `reference_drift`、`arm_time_interaction`、`carryover`、`drift_warning` | 参照组按轮成功率的 Mann 趋势检验，加前后半程差值；B − A 差值在前后半程是否不同的置换检验；按前一组分层的成功率；任一 p < 0.05 时亮起一个标志 | 有参照组且多轮的 campaign | 诊断用：用来发现问题，从不修正结果 |

**bootstrap 覆盖率。** 用本库实测：模拟 1000 组数据、每组 30 对，每组数据重抽样 2000 次，种子 20261010（蒙特卡洛标准误约 0.007）。名义 95% 的百分位区间覆盖真实均值差的比例：正态差值 N(0.4, 1) 为 0.93，指数差值 Exp(1) 为 0.91，二元配对（A ~ Bernoulli(0.5)、B ~ Bernoulli(0.65)，组内独立）为 0.95；BCa 分别为 0.94、0.92、0.94。二元配对的覆盖率随两组成功率变化：独立复核在其他成功率（0.5/0.5、0.2/0.4、0.7/0.9、0.5/0.6）下测得 0.93 到 0.95。每个 bootstrap 结果都在 `coverage_note` 里给出这段说明；少于 30 对时另加 `small_sample` 说明。

**规划用数字。** 80% 功效、双侧 α=0.05、基线成功率 0.5 时，每组 20 次能检出的最小差值是 0.42（不成对）和 0.44（成对，组内无相关）；30 次时 0.36 和 0.37；50 次时 0.29 和 0.29（`power_table`；测试用暴力枚举和模拟逐个复算）。每组 20–30 次只能分辨很大的差异。

### 参考文献

各条都按 DOI 或 arXiv 记录核对过（2026-10-10）。书目信息与英文版相同，见 [English](AUTOMATIC_CAMPAIGN.md#references)：Agarwal 等 2021；Agresti 与 Caffo 2000；Balasubramanian 等 2015；Benjamini 与 Hochberg 1995；Boschloo 1970；Brown 等 2001；Cliff 1993；Clopper 与 Pearson 1934；Cochran 1950；Cohen 1960；Connor 1987；Dunn 1961；Efron 1979、1987；Fagerland 等 2013；Feinstein 与 Cicchetti 1990；Fisher 1922；Friedman 1937；Hodges 与 Lehmann 1963；Holm 1979；Kaplan 与 Meier 1958；Kress-Gazit 等 2024；Mann 1945；Mann 与 Whitney 1947；Mantel 1966；McNemar 1947；Newcombe 1998a、1998b；Rogan 与 Gladen 1978；Royston 与 Parmar 2013；Wilcoxon 1945；Williams 1949；Wilson 1927。

## 图表

一张图就是一个 `FigureSpec`：一份带版本的纯数据描述（schema 为 `levi.aeri.figure_spec.v1`），里面没有绘图代码。分析代码每张图生成一份 spec；网页把同一份 JSON 映射到 Recharts，这里的两个写出器把它变成文件：

| 函数 | 输出 |
| --- | --- |
| `svgplot.render_svg(spec, lang=None, embed_spec=False, width=640)` | SVG 文本（UTF-8） |
| `pdfplot.render_pdf(spec, lang=None, width=640)` | 单页矢量 PDF（字节） |
| `svgplot.write_svg`、`pdfplot.write_pdf` | 同上，原子写入（先写 `.partial` 再改名） |
| `figspec.table(spec)`、`table_csv`、`table_html` | 可访问表格：图里的每个数字 |

纯标准库，没有 matplotlib、Pillow，也不用安装任何东西。输出是确定的：没有时钟、没有随机 id，PDF 里没有 `/ID` 和日期，所以同一份 spec 得到同样的字节，两份 campaign 报告可以直接 `diff`。

### 图的类型

| `kind` | 含义 | 坐标轴 |
| --- | --- | --- |
| `grouped_bar` | 各组成功率，带区间 | x 为类别轴，y 为数值轴 |
| `forest` | 成对差值，一行一个比较，0 处画参考线 | x 为数值轴，y 为类别轴 |
| `step_curve` | 到成功所需时间：每组一条阶梯曲线，可带区间带 | x、y 都是数值轴 |
| `stacked_bar` | 失败模式按组堆叠 | x 为类别轴，y 为数值轴 |
| `early_stop` | 早停节省和误终止率；1 到 4 个子图 | x 为类别轴，y 为数值轴 |
| `drift_lines` | 各组按轮的成功率，带区间；参照组画得更粗 | x 为数值轴或类别轴，y 为数值轴 |

`Point` 有 `x`、`y`，可选的区间 `lo`/`hi`（落在数值轴上：一般是 `y`，森林图是 `x`），以及可选的短标注 `label`，例如 `8/20`。标题、摘要、轴名、系列名和注释都可以写中英两种（`{"en": ..., "zh-CN": ...}`），缺哪种语言就退回英文。`validate()` 拒绝不能如实画出的输入：未知类型、非有限数、不包含其值的区间、越界的类别序号、x 倒退的系列、负的堆叠值，以及**落在固定坐标轴范围之外的数据**（数据不会被悄悄裁掉）。

### 不靠颜色也能读

* 调色板有八种颜色，在红色盲、绿色盲、蓝色盲模拟下两两至少相差 15 个 CIELAB 单位，排列顺序还让相邻两色的灰度也不同（有测试）。
* 每个系列另有各自的标记形状、线型，柱状图还有各自的斜线填充，所以黑白打印也读得出。参照组（`emphasis=True`）画得更粗。
* 组名始终用文字印出（图例、刻度），不只靠颜色。
* 坐标刻度取 1、2、5 乘以 10 的幂；常数序列会得到一个看得清的窗口；区间一定画出。
* SVG 带 `<title>` 和 `<desc>`（标题和一句话摘要）；`embed_spec=True` 时把 spec 本身放进 `<metadata>`。网页里请把 `table_html(spec)` 放在图旁边，读屏用户才能拿到数字。

### 语言限制

SVG 文字是真文字，用通用字体族（文字需要时加上 CJK 字体族）。PDF 用标准的 Helvetica 和 Helvetica-Bold（不嵌入字体，任何阅读器都有），所以**只支持拉丁字符**：Windows-1252 编不出的字符串会换成英文形式，没有英文形式就显示 `?`。中文图请出 SVG，或者给每段文字都写上英文形式。本模块不写 PNG；PNG 导出留在网页里（浏览器画 SVG），命令行只在 PATH 上有 `rsvg-convert` 之类的转换器时才生成 PNG。

### 检查 PDF

`pdfplot.verify_pdf(data)` 用自带的小解析器重新读一遍文件：文件头、交叉引用偏移、trailer、页面树、流长度、字体和结束标记，并返回页面大小和所有显示出来的字符串。机器上装了 poppler 时，测试还会跑 `pdftotext` 和 `pdfinfo`。每个版本请人用普通阅读器打开一份生成的 PDF 看一次观感：测试证明的是结构和文字，不是视觉排版。

SVG 黄金文件在 `tests/automatic/analysis/test_fig_golden/`；有意修改之后，用 `LEVI_UPDATE_GOLDEN=1` 重新生成，并检查差异。
