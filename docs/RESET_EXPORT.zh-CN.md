# 复位数据导出 / Reset export

复位片段就是把一条正向示范倒着跑：机器人从示范结束的地方出发，回到示范开始的状态，用来训练策略“把东西放回去”。训练池（[训练池](TRAINING_POOL.md)）可以把复位片段写在正向片段旁边，或者只写复位片段，任务指令为 `Reset: <正向任务>`。

倒放视频不等于复位数据。每条反转片段必须满足四件事，本文说明每件事怎么做到，以及哪里做不到。

1. 每路相机、状态和动作是同一份新顺序，逐帧对应。
2. 动作按反转后的时间重建（存储的动作是下一状态；夹爪指令必须领先手指的实际运动）。
3. 反转后的每一步都是机器人能做的。反转的松爪就是抓取，只有物体仍在张开的手指够得着的位置时才成立。
4. 无法反转的部分要排除、标记，或用真实录制补全，不拿别的东西遮盖。

[English](RESET_EXPORT.md)

## 用法

```bash
# 预估哪些能反转、为什么不能（不写文件；每个片段几秒）
uv run levi pool reset-analyze pi05-mix --limit 12

# 正向 + 复位写进同一个 LeRobot v2.1 数据集（不过滤静止帧）
uv run levi pool export pi05-mix --format lerobot_v21 --name pi05-mix-reset --reset forward_and_reset
uv run levi pool export pi05-mix --format lerobot_v21 --name pi05-reset-only --reset reset_only \
    [--reset-template "Undo: {task}"] [--reset-max-release in_place|in_reach] [--reset-partial] \
    [--reset-contract fr3-robotiq@1] [--reset-review-model <连接名>] \
    [--reset-min-settled-rows 2] [--reset-allow-no-grasp] \
    [--reset-bridge <正向片段键>=<补录片段键>] [--reset-any-outcome]
```

训练池页面的导出面板在“复位数据”下有同样的设置和“检查可逆性”按钮。接口：`ExportOptions.reset`（`direction`、`task_template`、`action_contract`、`max_release`、`on_ineligible`、`min_settled_rows`、`allow_no_grasp`、`require_forward_success`、`release_camera`、`gripper_lead_rows`、`review_model`、`bridges`）和 `POST /api/levi/pool/reset/analyze`。不带 `reset`，或 `direction` 为 `forward_only` 时，导出和以前完全一样。

复位片段只写进 `lerobot_v21` 导出（`raw_capture` 是原始录制的拷贝；`recap_value` 要给没人做过的任务编造奖励）。你按任务要的数量指**正向来源片段**数；每个最多派生一条复位片段，所以 `forward_and_reset` 最多是两倍。

## 写了什么

| | |
| --- | --- |
| 行 | 保留的正向行倒序。时间戳仍是 `frame_index / fps`；`index`、`episode_index`、`task_index` 接着导出自己的计数。 |
| 相机 | 每路相机的帧解码一次，按新顺序排列，再编码一次（H.264，导出的 fps）。不做流拷贝：拷贝出来的视频仍然是正放。 |
| 状态 | 正向状态行倒序。 |
| 动作 | 重建，不是倒序：契约规定动作含义为**下一状态**（`action[t] = state[t+1]`，LEVI 转换就这样写，`fr3-robotiq@1` 也这样声明），反转后第 k 行的动作是反转后第 k+1 行的状态，最后一行保持。把动作列倒序会让每个动作错一步。数据会对照契约检查（维度、夹爪二值、动作确实是下一状态）；含义未知的动作拒绝处理。 |
| 夹爪 | 指令必须**先于**手指运动翻转。正向时指令在第 r 行变为张开，手指在第 m 行完成；反转后手指在这几行里闭合，所以反转后的指令在第 m 行就要已经是“闭合”。直接倒序会把翻转放在运动之后。手指运动结束的行优先取自测得的夹爪宽度，没有宽度时用 `gripper_lead_rows`。 |
| 任务指令 | `Reset: <导出写入的正向任务指令>`（已套用 `task_text` 和批准的订正）。它是 `meta/tasks.jsonl` 里独立的一个任务；两个正向任务不会合并成一个复位任务，复位指令与某个正向指令相同会被拒绝。 |
| 标签 | 不继承结局。正向成功不代表复位成功，`levi_outcome` 不存在；需要结局的训练端要另找来源。 |
| 记录 | `meta/levi_reset.json`：选项、阈值版本、契约；每条复位片段的分析、**时间映射**（输出的每一帧是哪个源帧，以步长 ±1 的区间记录）和生成方式。`meta/episodes.jsonl` 的行带 `levi_reset`。`pool_export.json` 分别统计正向和复位片段，并列出每个排除及原因。`meta/levi_reset_capture_requests.json` 列出补不全的片段需要录什么（见下）。 |

复位导出不会被当作来源再次读入：带 `levi_reset` 的片段被拒绝（`reset_already_reset`）。

## 为什么松爪是难点

伸臂、搬运和**抓取**反转都是安全的：反转的抓取是把物体放回它原来所在的表面再松开，正是当时发生的事倒着放。**松爪**反转就是抓取，只有物体仍在张开的手指够得着的位置才成立。多数松爪之后是这样的：物体掉几厘米，停在两指之间。有的不是：它滚走、躺倒或翻出够不着的地方。反转后夹爪夹了个空，物体却“飞”进夹爪；而录像里从来没有机械臂去到物体躺着的地方、拿起它、带回来，因为没人做过。

所以导出会逐次松爪测量，按测量结果处理。

| 类别 | 含义 | 导出怎么做 |
| --- | --- | --- |
| `in_place` | 物体没动。 | 原样反转，闭合手指的帧也保留。 |
| `in_reach` | 物体落稳在张开的手指之间（掉过，但合拢夹爪仍能拿到）。 | 把从抓持帧到落稳帧（掉落过程）之间的帧从反转序列里剪掉。剩下的每一帧都真实一致，夹爪夹的是真正在那里的物体。 |
| `escaped` | 物体离开了手指够得着的范围。 | 不生成复位（`reset_release_escaped`）：排除、从最后一次安全抓持处起反转，或用录制补全。 |
| `unknown` | 什么也测不出：机械臂马上离开、物体仍在动或模糊、匹配有歧义、没有相机。 | 同 `escaped`（`reset_release_unknown`）。绝不凭猜测反转。 |

`max_release: "in_place"` 只接受第一类。

### 度量怎么做

松爪后机械臂常常静止几行，物体在这段时间下落并停稳，腕部相机随手一起动。机械臂不动的时候，抓持帧（物体在闭合的手指间）和落稳帧（手指张开、物体静止）是同一个场景、同一个位置，差异就是物体和手指。分析从手指张开的那一行起，取机械臂每行移动小于 3 mm 且离抓持位姿 2.5 cm 以内的行，比较两张图的中心（物体夹在两指之间的位置）：同一位置的相关系数（`same`）、中心附近多种尺寸下的最佳匹配（`best`、`dx`、`dy`、`scale`：掉下去的物体更低更小，不会更大或更高）、抓持帧中心有多平（`texture`：一块平的托盘总能匹配自己）、落稳帧有多清晰（`sharp`：模糊说明还在动）。

物体只有在至少 `min_settled_rows`（默认 2）个连续的清晰落稳帧与最后一帧一致时，才算**已落稳**。一帧什么也证明不了：10 Hz 下落体常常并不模糊，机械臂也可能在物体还在路上时就离开。类别按最后一个落稳帧判定（物体最终的位置）。对 `in_reach`，缝放在落稳段里最早的一帧，从这一帧到结尾每一帧都已显示物体在够得着的范围内：掉落的帧一帧都不留，机械臂也动得最少（缝处状态跳变只有几毫米：下面这批数据上中位数 4 mm，90 分位 16 mm）。

这是测量，不是识别：它不知道物体是什么。阈值（`levi/pool/reset/profile.py`，版本号记入每次导出）在真实的 FR3 + Robotiq 10 Hz 采集上标定，并且刻意从严：只凭颜色相似从不放行，因为盘子或桌面有没有物体看起来都一样。换机器人或相机要重新看一遍数字：`levi pool reset-analyze` 会打印它们。已知弱点：杂乱背景上的小物体（螺丝）物体走了仍可能继续匹配上，平整的背景则会匹配自己；建议用 `reset-analyze` 抽查导出。可选的本地视觉模型（`review_model`）只能**否决**已放行的松爪（`vlm_veto`），不能放行被拒的（模型没能回答时，该松爪保持 `unknown`）：一个公开的视觉语言模型操作失败判断评测（FailBench，2026）里，整体平衡准确率最好 0.77，物体运动可见的类别约 0.8，接触类不超过 0.6，并偏向判“成功”：拿来抓错误够用，单独做决定不够。

### 在本工作区数据上的实测

仅分析，`data_collection_robotiq` 随机 300 条遥操作采集（10 Hz、640×480、不过滤静止帧），共 507 次松爪，按出厂阈值：

| | `min_settled_rows` 2（默认） | 1（只信一帧） |
| --- | --- | --- |
| 能整条反转的片段 | 46（15%） | 66（22%） |
| 松爪 `in_place` / `in_reach` | 29 / 59 | 53 / 103 |
| 松爪 `escaped` | 6 | 7 |
| 松爪 `unknown` | 413 | 344 |

`unknown` 的松爪主要是 `not_settled`（机械臂一两帧内就离开：184）、`ambiguous_match`（158）、`object_still_moving` 和 `arm_left_before_settle`（各 34）。损失多半出在采集而不是方法：这批示范在打开夹爪后约 0.3 秒内就抬臂。**松爪后停约 0.5 秒再抬臂**，能证明的片段会多很多。带静止帧过滤转换的 LeRobot 数据集（`lerobot_fr3_filtered_robotiq_screws_plates`，89 个片段）恰好丢掉了这里需要的行：没有一个能反转。更早的、更宽松的阈值在同一批采集上放行了 56%；审查发现它会放行不该放行的掉落（机械臂离开时物体还在下落、缝放在掉落之前），所以现在的规则用产量换证据。

## 无法反转的部分

某次松爪是 `escaped` 或 `unknown` 的片段，有三种处理：

- **排除**（`on_ineligible: "exclude"`，默认）。正向片段不受影响，但仍会以 `reset_…` 原因出现在 `excluded` 里（看原因的前缀：`forward_and_reset` 会把复位的排除和已正向导出的片段列在一起）。度量在 `meta/levi_reset.json` 的 `analysis_of_excluded`。
- **从最后一次安全抓持处起反转**（`"partial"`）。复位从物体已经在夹爪里开始，位姿是正向片段在出问题的松爪之前抓持的位姿。它带标记（`levi_reset.scope: "partial"`，生成方式 `partial`）：教的是搬运并放回，不是把物体从落点拿起来，起点也不是正向任务真实的终态。
- **用录制补全**（`bridges`）。问题只出在一次松爪、且它之后不再有抓取时（否则录制还得把后面的操作也撤销），可以把一段真实录制接在反转序列前面：池里的另一个片段，从正向片段最后的位姿出发，物体在它躺的地方，接近、抓住，把它带到反转序列接手的位姿。衔接是检查出来的，不是假设：录制起点离正向终点在 3 cm 内；夹爪闭合并保持闭合；测得的宽度（如有）与正向抓持宽度一致，没有宽度时夹爪至少要先张开再闭合；衔接处的位姿和旋转与正向的抓持吻合；衔接处两张腕部相机帧是同一个场景。任何一项不过，该片段连同原因（`reset_bridge_*`）被排除。录制必须是池里单独的一个片段（它和其他来源一样要过留出名单和移除检查），不会作为正向片段导出。

只出在那一次松爪、且还没有录制的片段，会在 `meta/levi_reset_capture_requests.json` 里有一条：起始位姿、抓着物体结束的位姿、抓持宽度、相机和 fps，以及一句话说明。录这些（或者整条复位，作为任务指令为 `Reset: …` 的普通片段）是补上缺口的办法，训练池像其他片段一样收回录制。

### 纯信号处理或本地模型能补全吗

**能补一部分，这一部分已经做了。** 所有能靠信号判断的都做了：由夹爪指令和宽度检测事件、手指运动在哪一行结束、机械臂是否静止、接缝处的位姿跳变，以及上面的图像度量。物体仍在够得着的范围时，把掉落过程剪掉，剩下的就是真实一致的帧，不需要合成任何东西。

**其余部分无法从这条录制里合成。** 物体离开手指够得着的范围时，缺失的是新的行为（去到它躺的地方、拿起、带回来）和对应的新相机画面。规划器可以画出机械臂的路径（`levi.pool.reset.bridge.reference` 是用于补录请求的最小加加速度路径），但没有对应画面的路径不是训练数据，动作也无法对照从未拍摄过的像素验证。本地视觉语言模型也做不到：它能看一帧并说出看到什么，不能产生物理一致、多相机、与动作一致的视频。生成式视频和世界模型也核查过：公布了多视角、带动作条件结果的那几个，动作要么是从视频反推的伪动作，要么只改外观，没有一个能作为 VLA 的动作真值。反转示范的已发表工作（Reverse to Advance、TR-DRL、Green-VLA）都是把不可逆的部分排除；本导出同样排除，并在此之上对常见情形做了有测量依据的“剪掉再衔接”，其余用真实录制补全。

## 保证与限制

- 不写源数据。复位视频是新文件（不是源的硬链接），和其他输出一样写进导出的暂存目录；中断的导出从日志续做（`reset|<片段>|<相机>` 单元按大小和哈希校验）。
- 留出和已移除的片段，对正向和复位一视同仁地拒绝，补录片段也一样；复位导出不能作为另一个导出的来源。
- 动作契约是对照数据检查的声明。目前只登记了 `fr3-robotiq@1`；换机器人要先在 `levi.counterfactual` 登记它的契约，才能反转它的片段。
- 复位导出默认不过滤静止帧（物体落稳的那几行就是判断依据）。开了过滤，或 fps 低于采集，分析读的是保留下来的行，可能少到看不出物体落稳：更多片段变成 `unknown`。
- 没有抓取的片段（推、倒、擦）不反转（`reset_no_grasp`；`allow_no_grasp` 可覆盖）：反转后物体会被拉回、水会倒流，而信号分不清这些和无害的伸臂。
- LeRobot v3 来源不反转（训练池本来就把它们列为不可导出）。
- 复位片段记录的是示范倒着做了什么，**不是**机器人复位成功的证据，也没有成功标签。要求每条都有标签的训练端需要另找来源。
- 成本：每条复位片段每路相机一次解码、一次编码，外加一个解码视频的临时拷贝（640×480 约每帧 0.9 MB），放在暂存目录，该阶段结束就删。

## 排除原因

`reset_forward_failed`、`reset_forward_unlabeled`（只反转完成了的任务；没有结局的人工录制算完成）、`reset_already_reset`、`reset_action_contract`（数据不符合契约）、`reset_release_escaped`、`reset_release_unknown`、`reset_release_in_reach`（`max_release: "in_place"` 时）、`reset_no_grasp`、`reset_video_rows`（视频帧数与行数不一致）、`reset_write_error`（这一条写入失败，其他不受影响）、`reset_unreadable`；补录相关：`reset_bridge_missing`、`reset_bridge_contract`、`reset_bridge_cameras`、`reset_bridge_start_mismatch`、`reset_bridge_no_grasp`、`reset_bridge_never_reaches_anchor`、`reset_bridge_nothing_held`、`reset_bridge_visual_mismatch`、`reset_bridge_visual_unchecked`。单次松爪自己的原因是：`no_hold_frame`、`arm_left_before_settle`、`not_settled`、`review_failed`、`no_release_camera`、`object_still_moving`、`ambiguous_match`、`low_texture`、`object_left_the_fingers`、`vlm_veto`。

## 相关工作

已对照论文和仓库本身核查（2026-10）：Reverse to Advance（arXiv 2607.13455）把简单任务倒序并用相邻位姿重算动作；TR-DRL（arXiv 2505.13925）用前向动力学模型过滤反转的转移，仅在仿真中；Green-VLA（arXiv 2602.00919）只反转可逆技能，并排除可能掉落的放置；ReWiND 只用倒放视频训练进度模型；HALTER 和 FLARE 的复位技能是录制出来的，不是反转的。LeRobot 自带的数据集工具没有反转。它们都没有用合成数据补上本文说的缺口。
