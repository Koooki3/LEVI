# FR3 片段的三维回放

[English](REPLAY_3D.md)

数据集查看器的 Episodes（片段）标签页可以在视频旁边显示该片段的三维回放：FR3 机械臂随视频运动；数据集里没有关节角时，改为显示末端的轨迹和它的坐标轴。它跟随视频、播放条和曲线上的游标，所以暂停、拖动、点击曲线时三者一起动。不联网：机械臂模型在 `public/robots/fr3/`（来源见 [Robot Studio](ROBOT_STUDIO.zh-CN.md)）。机械臂工作台和这个回放从同一个模块 `src/components/robot-3d/fr3-model.tsx` 画机械臂。

SO-100/101、OpenArm 和 Unitree G1 的数据集保留原来的 **3D Replay**（三维回放）标签页（模型来自上游的存储桶）；本面板不替代它。

## 什么时候出现

查看器读取 `meta/info.json` 里的 `robot_type`，以及该片段的列名（`<feature> | <name>`，来自 `features[...].names`）：

| `robot_type` | 列 | 面板 |
| --- | --- | --- |
| 含 `fr3`、`franka` 或 `panda` | 7 个关节，名为 `joint_1..7`、`joint_0..6`、`fr3_joint1..7`、`panda_joint1..7`，或以上名称加 `_pos`（先找 `observation.joint_positions`，再找 `observation.state`，再找其他 `observation.*`） | **关节角**：画出机械臂。 |
| 含 `fr3`、`franka` 或 `panda`，或为空、`generic`、`generic_arm` | `x, y, z` 加 `rx, ry, rz`（或 `qx, qy, qz, qw`） | **仅末端位姿**：轨迹和坐标轴，不画机械臂。 |
| 空、`generic`、`generic_arm` | 七个关节列 | 有位姿列就按位姿显示，否则什么都不显示：这些关节可能属于任何 7 关节机械臂（xArm7、Kinova Gen3 也叫 `joint_1..7`），不能画成 FR3 |
| 其他 | | 没有面板 |

先判断 FR3 家族，再判断 Unitree G1 的名字，所以 `franka_g1x` 是 Franka，不会进 G1 查看器。（`hasURDFSupport` 本身仍把它当 G1；这一点记在 `replay-kind.ts` 里，这次没有改。）

单位是弧度和米。关节值会被截断到 FR3 的限位；超出限位 0.1 弧度以上的会在面板上提示（“数值单位是弧度吗？”）。`rx, ry, rz` 是 `levi/conversion/raw.py` 写出的 roll、pitch、yaw，已展开，依次绕固定的 X、Y、Z 轴转（外旋 XYZ）。这对应 three 的 `"ZYX"` 顺序，**不是** three 的 `Euler "XYZ"`；`src/lib/replay/pose.ts` 负责换算，`pose.test.ts` 用已知旋转钉住。四元数是 `[x, y, z, w]`，行与行之间保持不变，不做插值。

## 面板里有什么

- 视口（高度至少 280 px；窄屏在视频下方，1200 px 及以上在视频旁边）。拖动旋转、滚轮缩放；**视角**按钮（透视、正面、俯视）不用鼠标也能切换。
- **显示动作（目标）**：数据集里有同类型的 `action` 时出现，画半透明的第二台机械臂（关节）或淡色坐标轴（位姿）。实色是 `observation.state`；半透明是被指令的 `action`，**不是**实测。若数据集的 action 是下一帧状态（LEVI 转换的默认做法），它只比状态领先一帧。查看器读不到转换时记录的 `action_semantics`（在 `meta/levi_conversion.json` 里），所以这句话是按条件写的。
- **尾迹**：最近最多 300 个末端位置（关节模式下是法兰）。
- 一行文字说明当前行、时间和数值（关节角用度，或位置和姿态），夹爪的数值和条形，以及下面的备注。视口是一个有名字的分组，由这段文字描述。数据集里有名为 `gripper_width` 的列（例如 `observation.gripper_width`）时，夹爪显示**实测开度，单位米**（条形以 Robotiq 2F-85 的行程为满量程，行与行之间线性混合）；否则显示 `observation.state | gripper` 里的夹爪数值，保持到下一行。只有 `robot_type` 是 `generic_arm`（LEVI 转换默认写的值；查看器不读 `meta/levi_conversion.json`）时，这个数值才标为**指令**（张开为 1，闭合为 0，绝不是实测开度）；其他数据集只写“夹爪：{数值}”，并注明该列的含义和张开方向没有说明：例如 DROID 视图的 `gripper` 是实测位置，方向相反（0 为张开，1 为闭合，为推断），写成指令就是错的。两种列同时存在时，实测开度优先。
- 两种模式下面板都默认展开，可以用标题栏的按钮收起；收起状态、两个开关和视角在本次会话里保持。
- 模型或 WebGL 加载失败时，视口换成带**重试**按钮的错误状态；视频和曲线不受影响。

空格（播放/暂停）和 ↑/↓（上一个/下一个片段）与 Episodes 标签页的其他地方一样；焦点在面板自己的开关和视角按钮上时，这些键只作用于这些控件。面板没有动画，视角切换是直接跳转，所以 `prefers-reduced-motion` 没有要关闭的东西。

## 怎样和视频对齐时间

每一行按它自己的 `timestamp` 查找，不用帧号除以 fps，所以被查看器抽稀过的片段也能在正确的时间回放。分段（v3）视频的时间是视频时间减去片段起点，与播放器和曲线游标的算法一致。

第一个可见摄像头播放时，机械臂在每个显示出来的视频帧更新（`requestVideoFrameCallback`，约每秒 30 次）。暂停和每次拖动时，用页面的时间（播放条、点击曲线）。没有 `requestVideoFrameCallback` 的浏览器，在播放器两次 `timeupdate`（约每秒 4 次）之间用时钟和 `playbackRate` 估算，并且不超过上一次之后 0.5 秒，所以视频卡在缓冲时机械臂不会跑到前面再跳回来。更新路径不经过 React 渲染。

## 限制

- **位姿模式不画机械臂。** 记录下来的位姿在哪个坐标系（法兰 `fr3_link8` 还是夹爪尖端、相对哪个基座）还没有核实，而且七关节机械臂对同一个位姿有很多组关节解，所以不求解。面板上写着“位姿坐标系未核实”。
- **只存了末端位姿的数据集按位姿模式打开**，LEVI 在能导出关节角之前转换出的数据集也都是这样。有七个关节列、且 `robot_type` 属于 FR3 家族的数据集才画机械臂（DROID 视图，或写出 `observation.joint_positions` 并把 `robot_type` 设为 `franka_fr3` 的转换）。
- **最多 4000 行。** 查看器每个片段最多保留 4000 行，等间隔抽取；正好这么多行的片段可能被抽稀，行与行之间线性插值（角度不会跨过 ±π 的跳变去混合）。
- **没有夹爪模型。** 不论数值来自哪一列，夹爪都只显示数值和条形。机械臂工作台的 Robotiq 模型这里没有用。
- `observation.state` 按记录的值绘制；面板不声称它是标定过的实测。
- 尾迹、完整轨迹和坐标轴按数据自己的坐标系绘制，单位是米。

## 代码在哪里

`src/lib/replay/`（纯代码：`fr3-columns.ts` 判断哪些列、`replay-kind.ts` 判断用哪个视图、`frames.ts` 打包、`clock.ts` 和 `playback-clock.ts` 处理时间、`sampler.ts` 取某一时刻的帧、`pose.ts` 处理旋转）和 `src/components/robot-3d/`（`fr3-model.tsx` 共用的机械臂、`replay-scene.tsx` 画布、`replay-rig.ts` 尾迹与坐标轴、`replay-panel.tsx` 面板）。测试在各自旁边的 `__tests__` 目录。面板和场景按需加载，所以 three.js 不在查看器的主包里。
