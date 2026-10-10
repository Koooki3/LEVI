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

一张图就是一个 `FigureSpec`：一份带版本的纯数据描述（schema 为 `levi.aeri.figure_spec.v1`），里面没有绘图代码。分析代码每张图生成一份 spec；网页把同一份 JSON 映射到 Recharts，这里的两个写出器把它变成 SVG 和 PDF：

| 函数 | 输出 |
| --- | --- |
| `svgplot.render_svg(spec, lang=None, embed_spec=False, width=640)` | SVG 文本（UTF-8） |
| `pdfplot.render_pdf(spec, lang=None, width=640)` | 单页矢量 PDF（字节） |
| `levi.automatic.figure_files.write_figure(spec, path_stem, formats=("svg", "pdf"), lang=None, embed_spec=False, strict=False)` | 在磁盘上写出 `<path_stem>.svg` 和/或 `.pdf`；每种格式返回一条记录，供报告 manifest 使用 |
| `figspec.table(spec)`、`table_csv`、`table_html` | 可访问表格：图里的每个数字 |

分析库本身从不碰文件（只返回文本和字节），`write_figure` 放在库外。它先把所有格式都渲染出来，所以画不出的图一个文件也不写；然后每个文件都经过同目录临时文件、`fsync`、改名、对目录 `fsync`：崩溃只会留下旧文件或新文件，不会留下半个文件。每条记录有 `path`、`bytes` 和 `sha256`；PDF 的记录另带“语言限制”一节说的替换报告。目标目录必须已存在。

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
| `confusion_matrix` | 判定一致性：每个子图一张矩阵，每格按其占行比例着色，并写出计数和占比 | x、y 都是类别轴 |

`Point` 有 `x`、`y`，可选的区间 `lo`/`hi`（落在数值轴上：一般是 `y`，森林图是 `x`），以及可选的短标注 `label`，例如 `8/20`。标题、摘要、轴名、系列名和注释都可以写中英两种（`{"en": ..., "zh-CN": ...}`），缺哪种语言就退回英文。`validate()` 拒绝不能如实画出的输入：未知类型、非有限数、`lo` 大于 `hi`、越界的类别序号、同一位置给了两个点、x 倒退的系列、负的堆叠值、文字里的控制字符、同一子图里重名的系列，以及堆叠柱上的区间（堆叠柱不支持区间）。绘图时拒绝**落在固定坐标轴范围之外的数据**：数据不会被悄悄裁掉。矩阵格子的计数放在 `Point.value` 里，`value` 只用于 `confusion_matrix`。

### 区间不会被悄悄丢掉

* 阶梯曲线上，一个点的区间和它的值一样，从这个点一直保持到下一个点；连续带区间的点共用一条区间带，即使下一个点没有区间，带子也延伸到它为止。带区间、但右边没有可延伸位置的点（曲线的最后一个点、只有一个点的曲线、下一个点 x 相同）画误差棒，所以曲线终点的区间也看得见。没有区间的点把区间带断开。所有区间带先画、所有曲线后画，区间带不会盖住别的组的曲线。
* 在别处有区间的系列里，缺区间的点在表格里标 `区间不可用`，图下也会写有多少个这样的点。
* **阶梯曲线在 x = 0 处没有区间，就表示没有不确定性**：Kaplan-Meier 结果里没有 t = 0 这一行，适配层自己补上不带区间的起点 (0, 0)（见“与分析库的接口”）。该点的值就是它自己的区间，表格写 `起点无不确定性`，也不算缺失。
* 画完之后，`validate_render(spec, scene)` 在页面上量 spec 里的每个区间：误差棒必须跨满区间；区间带在该点处必须有宽度，并且在那里跨满区间（误差 0.01 pt 以内）。没有标记、区间带宽度为 0、误差棒缩成一个点，都抛 `ValueError`；区间本身只是一个值时除外（例如所有片段都成功后 Kaplan-Meier 区间为 [0, 0]）。`layout()` 会调用它，所以丢了区间的绘图函数产不出图。
* 估计值落在区间之外（bootstrap 百分位或 BCa 区间、中位数都可能合法地这样）会被保留，表格标 `估计值在区间之外`，图下计数。`warnings(spec)` 以 `estimate_outside_interval:panel0/series1/point0` 这样的代码列出这些情况和不可用项。
* 没有数据的组是 `unavailable=True`、没有点的系列：它仍有图例项（`Arm D（无数据）`）、表格行和图下计数，但不画任何东西。

### 多个子图

所有子图共用一个图例，由各子图系列的并集生成；系列的颜色、标记、线型和斜线按**名称**决定，与它在子图里的位置无关。

### 不靠颜色也能读

* 调色板有八种颜色，在红色盲、绿色盲、蓝色盲模拟下两两至少相差 15 个 CIELAB 单位，排列顺序还让相邻两色的灰度也不同（有测试）。
* 每个系列另有各自的标记形状、线型，柱状图还有各自的斜线填充，所以黑白打印也读得出。参照组（`emphasis=True`）画得更粗。
* 组名始终用文字印出（图例、刻度），不只靠颜色。
* 坐标刻度取 1、2、5 乘以 10 的幂；常数序列会得到一个看得清的窗口。
* 颜色保证两两可分，灰度只保证调色板里相邻的两色可分；其余靠斜线、标记和线型。
* `drift_lines` 里各组左右错开最多 3 pt，免得区间互相重叠；数值型 x 轴上这只是绘图偏移，不改数据。
* SVG 带 `<title>` 和 `<desc>`（标题和一句话摘要）；`embed_spec=True` 时把 spec 本身放进 `<metadata>`。网页里请把 `table_html(spec)` 放在图旁边，读屏用户才能拿到数字。

### 语言限制

SVG 文字是真文字，用通用字体族（文字需要时加上 CJK 字体族）。PDF 用标准的 Helvetica 和 Helvetica-Bold（不嵌入字体，任何阅读器都有），所以**只支持拉丁字符**，而且不会悄悄替换。对 Windows-1252 显示不了的文字，PDF 依次采用：其他语言里能显示的形式（英文）；统计符号的转写（`Δ` 写作 `Delta`、`−` 写作 `-`、`α` 写作 `alpha`、`≥` 写作 `>=` 等）；`?`，什么都不剩时写 `[n/a]`。系列名什么可读的都不剩时，改写成 `Series 1`、`Series 2`……（按图例顺序），各组仍然分得开。`pdfplot.render_pdf_report(spec, lang, strict=False)` 返回字节和 `substitutions`（一组 `{"text", "to", "reason"}`，原因为 `transliterated`、`fallback_en`、`unencodable`、`numbered`），以及标题实际所用的语言（同时写入 PDF 的 `/Lang`）；它的 `manifest()` 给出写进报告 manifest 的 `{"pdf": "ok" | "lossy(n)", ...}`。`strict=True` 时改为抛 `LossyTextError`。`render_pdf` 只返回字节，所以报告生成器要用 `render_pdf_report` 或 `write_figure`（它的 PDF 记录带同样的报告），并把替换清单写进 manifest。中文图请出 SVG，或者给每段文字都写上英文形式。

标题、摘要和注释分别截到 300、600、400 个字符（最多 3、4、6 行），图例名截到 60、子图标题 80、参考线标注 40、点标注 40、轴名 100 个字符；`Scene.truncated` 列出被截断的部分。表格保留全文。排版时间与文字长度成线性关系。本模块不写 PNG；PNG 导出留在网页里（浏览器画 SVG），命令行只在 PATH 上有 `rsvg-convert` 之类的转换器时才生成 PNG。

### 检查 PDF

`pdfplot.verify_pdf(data)` 用自带的小解析器重新读一遍文件：文件头、交叉引用偏移、trailer、页面树、流长度、字体和结束标记，并返回页面大小和所有显示出来的字符串。机器上装了 poppler 时，测试还会跑 `pdftotext` 和 `pdfinfo`。读取器还会对页面内容分词：只允许本写出器会用到的操作符、操作数个数要对、`q`/`Q` 和 `BT`/`ET` 要配对、文字只能在 `BT`/`ET` 里并且只用已声明的字体；悬空的对象引用是 `PdfError`。每个版本请人用普通阅读器打开一份生成的 PDF 看一次观感：测试证明的是结构和文字，不是视觉排版。

SVG 黄金文件在 `tests/automatic/analysis/test_fig_golden/`；有意修改之后，用 `LEVI_UPDATE_GOLDEN=1` 重新生成，并检查差异。

### 与分析库的接口

图表这一侧只需要普通数字。从分析结果到 `FigureSpec` 的适配层由报告生成器（T-CP-06）来写；`tests/automatic/analysis/test_figadapter.py` 是可运行的示例，直接调用分析库并映射它的真实输出。各函数的键名不同：

| 分析结果 | 键 | 图 |
| --- | --- | --- |
| `proportion(k, n)` | `rate`、`wilson.low`、`wilson.high`、`k`、`n`；`n = 0` 时 `available: false`，`rate` 和 `wilson` 为 `None` | `grouped_bar`：`Point(0, rate, wilson.low, wilson.high, "k/n")` |
| `paired_bootstrap`、`unpaired_bootstrap` | `estimate`、`low`、`high`；没有数据时为 `None`，`available: false` | `forest`：`Point(estimate, 行号, low, high)` |
| `newcombe_paired` | `difference`、`low`、`high` | `forest`：`Point(difference, 行号, low, high)` |
| `kaplan_meier` | `steps[]`，每个事件时刻一行：`time`、`survival`、`incidence`（= 1 - S）、`low`、`high`（在 S(t) 上）；没有 t = 0 这一行；没有片段时 `available: false`、没有 steps | `step_curve`：先补不带区间的 `Point(0, 0)`，然后每行 `Point(time, incidence, 1 - high, 1 - low)` |

* 没有数据的组（`available: false`）变成 `unavailable=True` 的系列，绝不丢掉。森林图里不可用的比较保留它的行、不画点，并加一条注释。
* Kaplan-Meier 的区间在 S(t) 上；到成功所需时间的图画的是 1 - S(t)，所以区间换成 `[1 - high, 1 - low]`。0 < S < 1 时总有区间；S 降到 0 之后区间是 [0, 0]，图上显示为单个值 [1, 1]。分析库没有 t = 0 这一行：适配层补上不带区间的 (0, 0)（见上）。
* bootstrap 区间原样传入。区间若不含点估计，图会告警并保留，不要裁剪或重新居中。
* 比率用 0..1 的小数（`fmt="percent"`）；计数放进点的 `label`（`8/20`），矩阵则放进 `value`。

## 计划与时间表

**状态：只有库**（`levi/automatic/campaign/spec.py`、`schedule.py`）。还没有命令、API 或页面调用它，命令在 T-CP-08 做。

多模型评测计划（campaign）的作业文件就是一份普通的 `levi.aeri.job.v1` 作业文件（共享设置：任务、终止、复位、记录），再加一个 `campaign` 块。它和作业文件用同一个严格的 YAML 子集读取，这个子集不支持“映射组成的列表”，所以各组写成以组号（`A` 到 `H`，最多 8 组）为键的映射：

```yaml
campaign:
  id: c20261010-eggplant        # 字母、数字、_ 和 -；用于文件名和运行编号
  robot: fr3                    # 一台机器人同一时刻只跑一个 campaign
  trials_per_arm: 30
  schedule:
    kind: counterbalanced_segments
    segment_trials: 5
    seed: 7
  layouts:
    source: card_set            # 或 none（复位策略模式）
    file: layouts.yaml          # 相对作业文件
  pairing:                      # 检查点目录名模式 -> 配置名
    recap_cfg_*: pi05_fr3_all_state_cfg
    pi05_fr3_all_step*: pi05_fr3_all_state
  stop_rules:
    consecutive_faults: 2
    unplanned_interventions_per_arm: 5
  arms:
    A:
      role: reference           # 至多一个
      policy_forward:
        checkpoint_dir: /abs/path/checkpoints/pi05_fr3_all_step49999
        config: pi05_fr3_all_state
        port: 8000
      checkpoint:
        sha256_status: recorded # verified | recorded | none
        manifest_sha256: <64 位十六进制>
    B:
      policy_forward:
        checkpoint_dir: /abs/path/checkpoints/recap_cfg_r2_best_step14300_jax
        config: pi05_fr3_all_state_cfg
        cfg_scale: 1.0
        port: 8000
```

其他可选键：`primary`（`comparison: [B, A]`、`alpha`、`label_basis`、`preregistered`；`sequential: step` 是预留项，会被拒绝）、`control`、`blinding`（`operator: arm_codes` 时操作员看到 X1、X2… 而不是组号）、`treatment_includes_reset`；每组还可以写 `policy_reset`、`versions` 和 `group`（rollout 分组，默认取检查点目录名）。

**会被拒绝的计划**（各带错误码）：

| 错误码 | 情形 |
| --- | --- |
| `E_CAMPAIGN_SCHEMA` | 未知键、类型不对、不足两组、组号不在 A–H |
| `E_CAMPAIGN_JOB` | 共享设置不是合法的作业文件 |
| `E_CAMPAIGN_ARMS` | 两个参照组；两组写进同一个 rollout 分组；`primary.comparison` 不是本 campaign 的两个组；某组单独写了 `policy_reset` 却没写 `treatment_includes_reset: true`（或者是 `human_assisted`） |
| `E_CAMPAIGN_PAIRING` | 没有 `pairing` 表；检查点目录名一个模式都不匹配，或匹配到配置不同的几个模式；配置名不是它的模式要求的那个（CFG 检查点不配 CFG 配置会悄悄退化成普通采样） |
| `E_CAMPAIGN_CHECKPOINT` | `sha256_status` 为 `verified` 或 `recorded` 却没有 `manifest_sha256`，或为 `none` 却写了 |
| `E_CAMPAIGN_SCHEDULE` | `randomized_blocks` 的段长于 1 次试验；`sequential: step` |
| `E_CAMPAIGN_LAYOUTS` | 复位策略模式用了卡片（初始场景由复位策略决定，应写 `source: none`）；`human_assisted` 没有卡片；卡片数少于一轮需要的数量；卡片文件有误；`per_round` 不等于段长 |
| `E_CAMPAIGN_SETTINGS_DIFFER` | 两份子作业的共享设置不同（见下） |
| `E_CAMPAIGN_EXISTS` | 本 campaign 的某个文件已经存在且内容不同：改过的 campaign 请换一个 id 再规划 |

配对表放在配置里，不写在代码里：它把检查点目录名（shell 通配模式）对应到配置名，新的策略系列只需加一行，不用改代码。

**布局卡**（`layouts.yaml`，以卡号为键的映射）：

```yaml
schema_version: levi.aeri.layouts.v1
cards:
  c01:
    description: 盘子在左，杯子在右
    reference_image: refs/c01.jpg   # 作为叠图显示；后端从不打开
    predicates:
      object_at_source: true
    params:
      x_cm: 10
```

**展开。** `spec.plan_campaign(job, job_root=...)` 为每段写一份普通作业文件 `<job_root>/campaigns/<id>/<id>__<arm>__s<NN>.yaml`，最后写 `campaign.plan.json`。每份子作业就是共享设置，加上它自己的运行编号、试验次数、本组的 rollout 分组、各组共用的 forward（和 reset）任务目录，路径都改成绝对路径。文件只写一次（临时文件、fsync、`link`、目录 fsync）：同一份文件再规划一次不改变任何东西；同一 id 下内容变了就拒绝。然后用启动核心的规划函数（`launch.plan` 完成之前用 `levi.automatic.cli.load_job`）算出每份子作业的 `plan_sha256`。

- `settings_sha256`：把子作业里允许随组变化的键去掉以后算的 sha256。去掉的是 `experiment.name`（运行编号）、`experiment.episodes`（由时间表推出）、`policies` 和 `recording.group`。初始状态契约文件用它字节的 sha256 表示，每份子作业各读一次。所有子作业的摘要必须相同，否则以 `E_CAMPAIGN_SETTINGS_DIFFER` 拒绝，并指出第一个不同的键。
- `campaign_sha256` 覆盖规范化的 campaign 块、卡片和卡片文件的 sha256、整张时间表、每份子作业文件的 sha256 和 `plan_sha256`，以及 `settings_sha256`。`spec.read_plan` 按它核对计划文件；`spec.verify_children` 核对子作业文件（给了规划函数时，连同它读的文件）在规划之后没有变。

**时间表**（`schedule.build`）。每轮摆出一组卡位（卡片；没有卡片时是 `slot01`、`slot02`…），每个组在一段里按同样的顺序跑完它们：

| `kind` | 每轮的组顺序 | 默认段长 | 结论等级 |
| --- | --- | --- | --- |
| `counterbalanced_segments`（默认） | Williams 设计的一行：在一个完整的行周期里，每个组紧跟在其他每个组之后的次数相同（两组时为 AB、BA） | 5 | 可作确证性 |
| `randomized_blocks` | 按种子随机；一个区组就是一张卡 | 1（固定） | 可作确证性 |
| `latin_square` | 循环拉丁方的一行：每个周期里每个组在每个位置各出现一次 | 5 | 可作确证性 |
| `interleaved` | 总是 A、B、… | 1 | 只能探索性 |
| `blocked` | 先跑完 A 的所有段，再跑 B，… | 5 | 只能探索性 |

“可作确证性”是必要条件，不是充分条件：报告生成器还要核对其他条件（预注册、漂移检查）。卡片从按种子洗过的牌堆里发，同一轮内不重复，各卡使用次数大致相同。相邻两段是同一组时，正在运行的策略继续使用，所以时间表的 `switches` 是真正需要启动策略的次数。所有随机选择都是按 `sha256(种子, 用途, 项)` 排序：同一个种子在任何进程、任何机器上得到同一张时间表（用不同的哈希种子测过）。

## 状态机与恢复

**状态：只有库**（`levi/automatic/campaign/journal.py`、`conductor.py`、`switch.py`）。启动核心（`launch.py`）、命令通道和会话写入器通过下面的接口接入。

```
DRAFT -> PLANNED -> { SEGMENT_PREPARE -> POLICY_STOP -> POLICY_START -> POLICY_READY
      -> ENV_CONFIRM（人） -> ARM_RUNNING（子运行） -> SEGMENT_SEALED } x 段数
      -> ANALYZING -> REPORTED
旁路：PAUSED（只在段边界）、WAIT_HUMAN、FAULT_LOCKED、ABORTED（只能由操作员）
```

campaign 的文件在 `$LEVI_AERI_HOME/campaigns/<id>/`（`LEVI_AERI_HOME` 默认 `~/.levi-aeri`）：`journal.jsonl`、`plan.json`（计划，每次打开都按日志头的 `campaign_sha256` 核对）、`state.json`（派生，从不读回）和 `torn/`。

**日志。** 每行是一条 `levi.aeri.campaign_event.v1` 消息，沿用运行日志的事务协议（prepared，在任何副作用之前落盘 -> acknowledged -> committed 或 aborted），哈希链，只有一个写入者（对目录加 `flock`）。这个契约放在单独的登记表里（`aeri.CAMPAIGN_SCHEMAS`，minor 0），所以运行日志头和五种消息都不变；快照是 `docs/architecture/aeri/v1/campaign_event.schema.json`，`aeri.check_campaign_against_base` 把它和基线分支比较（`levi dev check-contracts` 暂时还没调用它）。事务的幂等键是 `<id>:s<NN>:<进入的状态>`（不属于某段时是 `<id>:<状态>`）。回放会拒绝：表里不允许的转移；没有操作员命令就离开 `WAIT_HUMAN`/`FAULT_LOCKED`/`PAUSED` 或进入 `ABORTED`；恢复做等待以外的事；还有段没封存就进入 `ANALYZING`；在段边界以外暂停。

| 步骤 | 做什么 | 断电后 |
| --- | --- | --- |
| `SEGMENT_PREPARE` | 等机器人空闲（没有运行持有它，也没有别的活会话） | 等人 |
| `POLICY_STOP` | 给 C2 会话写 `waiting_reset`，停掉本 campaign 的所有策略单元，等到策略端口没有监听者；同一组继续服务时跳过 | `FAULT_LOCKED`（停没停不确定） |
| `POLICY_START` | 按启动配方 `systemd-run --user --unit=levi-policy-<id>-<arm>` | `FAULT_LOCKED` |
| `POLICY_READY` | 只做被动检查（见下）；600 秒内没就绪就 `WAIT_HUMAN` | 等人 |
| `ENV_CONFIRM` | 问操作员：机械臂静止、卡片已摆好 | 重新问（新的问题） |
| `ARM_RUNNING` | 启动子运行（唯一的非幂等动作），然后观察它 | 启动事务悬空则 `FAULT_LOCKED`，否则 `WAIT_HUMAN`；绝不自动再启动 |
| `SEGMENT_SEALED` | 记下子运行报告的计数；检查停止规则和待生效的暂停 | 等人 |

**人的答复**（`Confirmations`）：每道题带一个随机 nonce，只有针对这道题、nonce 一致、命令号没用过的答复才算数（其余丢弃并记一条备注）。确认场景需要 `arm_still` 和 `layout_ready` 两项。在等待状态下，`resume` 走到下一个安全的位置：下一个要准备的段；子运行已经启动（或可能已经启动）时观察它；所有段都封存后进入 `ANALYZING`。`relaunch` 只在启动从未得到确认、而且这个运行确实不存在时，才重新准备这一段；已确认启动过的运行绝不再次启动。`abort` 结束 campaign。由停止规则引起的等待，恢复时必须带 `override_stop_rule: true`。

**停止规则**（`campaign.stop_rules`）：连续 `consecutive_faults` 段的子运行故障或崩溃，或某组的非计划介入超过 `unplanned_interventions_per_arm`，campaign 就停下等人，从不跳过一个组。子运行自己的计划内等待（比如等人复位场景）不算故障。

**一台机器人只跑一个 campaign。** 指挥进程在存活期间持有 `$LEVI_AERI_HOME/campaign-<robot>.lock`（`flock`）；同一台机器人上的第二个 campaign 会以 `E_BUSY` 被拒，并给出持有者。

**策略切换**（`switch.SystemdPolicyHost`）。就绪靠读，不靠问：单元处于 active，而且策略端口上的每个监听进程都属于这个单元的 cgroup，命令行里有本组的配置名（整词匹配：普通配置名是 CFG 配置名的前缀）和检查点，这些都从 `/proc/net/tcp{,6}`、`/proc/<pid>/fd`、`cgroup` 和 `cmdline` 读取。后端从不连接策略端口，真正的握手在子运行的 PREFLIGHT 里做。监听者不属于本 campaign（例如在终端里手动起的策略服务）、读不到属主、或者跑的是别的配置，campaign 都会锁定（`FAULT_LOCKED`）。没有启动配方时什么都不启动。

**恢复。** `Conductor.attach(id, job_dir)` 先拿机器人锁，再打开日志（损坏的日志以 `E_CORRUPT` 拒绝，什么也不写），核对子作业文件，中止悬空的事务，然后进入 `FAULT_LOCKED`（悬空事务有副作用时）或 `WAIT_HUMAN`。原本就在等人、在问人（`ENV_CONFIRM`）或在分析的 campaign 留在原处。人答复之前什么都不动。测试在一个完整 campaign 的每一行日志写入之前和之后各杀一次指挥进程，核对每次恢复都停在等人的位置，而且每个子运行恰好启动一次。

**集成用的接口：** `PolicyHost`（`stop`、`start`、`passive_ready`）、`RunLauncher`（`launch`、`status`、`robot_busy`）、`Confirmations`（`ask`、`take`、`withdraw`、`pause_requested`）、`SessionWriter`（`waiting_reset`、`release`）和 `ChildPlanner`（`plan`）。

**还没有做：** 命令和页面（T-CP-08、T-CP-09），试验台账和补跑 `<id>__<arm>__s<NN>r2`（T-CP-05），真实的启动配方（T-SU），报告（T-CP-06）。

## 试验台账

`levi.automatic.campaign.ledger` 把评测计划跑过的每个前向片段列成一行，写在 `ledger.jsonl`（schema `levi.aeri.campaign_trial.v1`）。评测计划本身不另存真值：台账每次都从子运行重新推导，同样的输入得到逐字节相同的文件（文件内容已经相同时 `write_ledger` 什么也不写；否则经临时文件、`fsync`、改名写入）。

**输入。** 一个 `CampaignLayout`（计划 ID；按计划顺序排列的各段，每段有组、轮次、按卡位顺序的布局卡、跑这一段的运行 ID：先是第一次运行，再是补跑），以及各次运行的片段：

| 来源 | 函数 | 读取 |
| --- | --- | --- |
| AERI 子运行 | `read_aeri_run(run_dir, max_steps=None)` | `manifest.json`、运行日志（各片段已提交的结果）和 `labels/`；不加锁、不写任何文件；日志损坏时拒绝 |
| 旧评测客户端 | `guided.legacy_fact(...)`（见[引导式旧客户端](#引导式旧客户端)） | rollout 的 `metadata.json` |

**行。** `trial_id = <计划>:<轮次>:<卡>:<组>`，`slot`（卡在本段中的位置）、`segment`、`run_id`、`episode_id`、`order_index`、`started_at`/`ended_at`、`layout_fidelity`（`attested`、`verified`，或带原因的 `deviated`）、`preceded_by_arm`（上一段的组）、`rerun_of`（补跑的行记本段第一次运行），以及步数、停止原因、片段怎样结束（`budget`、`early_stop`、`operator_stop`）和早停对照字段。

**片段用的是哪张卡。**

| 情形 | 卡 | 是否成对 |
| --- | --- | --- |
| 运行 manifest 写明（`episodes[*].campaign.card`），或有人确认过 | 照用 | 是 |
| AERI 片段没写明 | 本段下一张没完成的卡（运行就是让操作员摆这张） | 是 |
| 旧客户端片段还没人确认 | 没有卡；`candidate_card` 是下一张没完成的卡 | **否** |
| 卡不属于本段、卡已被占用、片段超出最后一张卡 | 记在 `card_problem` | 否 |

作废（操作员按 `d`，或标签为 `discarded`）和不完整的片段留在台账里计数，但不占卡（操作员会重新摆同一张卡），也没有结果。`counts(ledger, layout)` 按组给出：有效、作废、不完整、偏离、补跑行、未确认、卡号问题、可成对、计划卡位和缺失卡位。

**标签口径。** 每个片段的四种标签互不覆盖（自动判定、后台复核、操作员标签、裁定标签）。`label_value(entry, basis)` 按五种口径之一读取：`autonomous_verdict`、`posthoc_verdict`、`operator_label`、`adjudicated_ground_truth`、`adjudicated_then_operator`。未定或缺失的判定、`discarded` 或 `unclear` 的操作员标签都不算值。后两种口径就是 `metrics.LabelStore.truth` 读取的口径，它的默认行为不变。

## 报告产物

`levi.automatic.campaign.report` 把台账和一种标签口径做成报告。`analyse(ledger, info, basis, layout=None, seed=None)` 把所有数字放进一个 JSON 文档（schema `levi.aeri.campaign_report.v1`）；`write_report(report_root, ledger, info, basis, ...)` 把它写出来：

```
<report_root>/<口径>/
  summary.en.md  summary.zh-CN.md
  tables/   success  pairwise  continuous  failure_modes  agreement  power（.csv 和 .tex）；drift.csv
  figures/  f1-success  f2-differences  f3-time-to-success  f4-failure-modes
            f5-early-stop  f6-drift  f7-agreement（.svg、.zh-CN.svg、.pdf、.json FigureSpec）
  data/     trials.parquet  trials.csv  labels.csv  analysis.json
  manifest.json
```

每种口径一个目录，写一个从不改动另一个。目录在按口径加锁（`.<口径>.lock`）后先在旧目录旁边建好，再整体换入：读者看到的不是旧报告就是新报告，写入失败时旧报告保持原样。写到一半被杀掉会留下 `.<口径>.tmp-*` 目录，下次写同一口径时删掉。同样的台账、标签、计划信息和种子得到逐字节相同的文件（manifest 的 `generated_at` 除外；传 `now` 可固定）。`.tex` 表只用 `tabular` 和 `\hline`；所有打印的数字都经 `fmt` 格式化。CSV 里会被电子表格当公式执行的文本单元格前加一个撇号。

**分析内容。** 每组：成功率及 Wilson、Clopper–Pearson 区间，标签覆盖率。每对组（预注册的比较排第一，B 减 A）：在同一轮、同一张卡上的试验对上做 McNemar 检验、Newcombe 成对区间和成对 bootstrap；由复位策略摆场景时（`layout_source: none`）改用 Fisher 检验和 Newcombe 独立样本区间。Holm 校正整个比较族；三组及以上另做 Cochran Q。两组都成功的对上比较步数（Wilcoxon、Hodges–Lehmann）；时间到成功（Kaplan–Meier、log-rank、到步数上限的 RMST）；失败模式；提前终止（检测器以人工标签为准：有裁定用裁定，否则用操作员标签）；漂移（参照组趋势、组×时间、残留效应）；每组、按片段结束方式分层的自动判定与操作员标签一致性，以及判定器误判率是否因组而异的检验；功效表。有偏离试验时另做一次剔除它们的敏感性分析。

**标签口径与命名。** 摘要开头是口径块：口径、每组标签覆盖率（组间相差超过 10 个百分点时警告）、操作员盲法、布局控制与偏离试验、复位方式和场景检查。比率按口径命名，生成器拒绝其他写法（`check_naming`）：

| 口径 | 比率名称 |
| --- | --- |
| `autonomous_verdict`、`posthoc_verdict` | 自动判定成功率（未经人工核实） |
| `operator_label` | 成功率（操作员标签） |
| `adjudicated_ground_truth` | 真值成功率（唯一可以写“真值”的口径） |
| `adjudicated_then_operator` | 成功率（裁定优先，否则操作员），并写明两种标签各占几条 |

**结论等级。** 全部条件都满足才算确证性：主分析已预注册；每组带标签的试验数达到计划值；没有中途查看；口径经过人工（操作员或裁定）；没有漂移警告；顺序策略不是 `blocked` 或 `interleaved`；分析库按预设功效判定（预设差值在 80% 功效下可检出）。否则每个结论句都标“探索性”，摘要列出未满足的条件。区间含 0 时写“本次数据不足以区分”，并给出本设计的最小可检出差，从不写两组相当。夸大的措辞（“显著优于”“证明”“state-of-the-art”等）在确证性句子之外一律拒绝（`check_wording`；有测试扫描所有模板分支）。文字来自 `templates/`（`sentences.json`、`summary.<语言>.md`），不调用语言模型，其中每个数字都由 `analysis.json` 格式化而来（有测试）。从不报告事后功效。`blinded=True` 写出不含任何分组数值的摘要（用于仍在进行的计划）；其他文件照常写出，由总览页决定显示什么。

**隐私。** `manifest.json` 记录计划和共享设置的摘要、各子运行的计划摘要、状态、LEVI 提交号和模式、各组的检查点名称（从不写路径）、配置、哈希状态和版本、种子与时间表、中途查看次数、偏离试验数、方法及其文献、每个文件的大小和 SHA-256、PDF 文字替换记录，以及 `png: skipped(no converter)`。从不写姓名和邮箱。相机序列号、IP 地址、主机名、URL 和本机路径一律去掉（按键名，也查字符串内容），除非 `include_site_details=True`。
