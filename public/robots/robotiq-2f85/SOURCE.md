# Robotiq 2F-85 offline model / 离线夹爪模型

Source: [PickNik Robotics ros2_robotiq_gripper](https://github.com/PickNikRobotics/ros2_robotiq_gripper),
commit `a74d007d8f2f06dc6a503ad21038ba869d4999a6`, read from the local shared checkout.
The source package declares BSD-3-Clause. The original LICENSE is retained verbatim:
Copyright (c) 2022, PickNik Robotics. No external download or installation was used.
Original source and generated output SHA-256 hashes are in SOURCE.json.

This standalone URDF retains all nine source gripper links, nine joints (including
three fixed joints), and all five mimic relations. It substitutes an identity
`gripper_mount` parent for the source macro's caller-provided origin. This is
only the gripper's own coordinate frame, never an FR3 flange installation or TCP
calibration. Only visual geometry is retained; ROS control, collision, inertia,
hardware plugins and motion interfaces are absent. Nine detailed DAE meshes are
converted to GLB with the existing standard-library FR3 converter; triangle
indices, normals, diffuse colors and node transforms are retained without
simplification. Coordinates stay Z-up, in meters; GLB does not trigger any
implicit coordinate rotation in Three.js.

Opening width follows the source URDF geometry and the existing shared Robotiq
server derivation. With `x0=0.03060114`, `dx=0.03152616+0.00563134`,
`dz=-0.00376347+0.04718515`, define
`padX(theta)=x0+dx*cos(theta)-dz*sin(theta)` and
`width(theta)=2*(padX(theta)-padX(0.7929))`.
The usable open angle is 0 rad and the server's closed angle is 0.7929 rad.
The URDF joint bound 0.8 rad is retained, but is not used as the closed target.
The opening ratio is physical width divided by the geometric stroke, not a
linear angle fraction. The inverse is a monotonic bisection. These are nominal
geometry values, not measured pad width, force, contact, or installation data.

Required future installation parameters remain unknown: FR3 flange-to-mount
translation and rotation, adapter geometry, tool orientation and calibrated TCP,
and the physical gripper/pad configuration. The UI must label any installation
transform as a draft until checked by a person. This asset never issues commands.

中文：来源为上述固定版本、本机共享仓库中的 Robotiq 2F-85 描述，已保留原样 BSD
许可及来源哈希。仅提取视觉 link、joint 与 mimic，DAE 转 GLB 不减面。根节点
`gripper_mount` 的单位变换只是夹爪自身原点，不表示 FR3 法兰安装标定。夹爪
开度采用官方 URDF 几何与现有共享 server 一致的三角函数关系，0.7929 rad
为 server 的闭合角；URDF 的 0.8 rad 上限不被当作闭合目标。开度比例是物理
宽度比例，不是角度线性比例。安装平移/旋转、转接板、工具朝向、TCP、实物
指垫配置仍待核对；本文件不含真机运动接口，也不是碰撞或采集安全认证。

Mesh compression: the GLBs are stored meshopt-compressed with 16-bit quantized
positions (`scripts/robot-studio/compress-glb.py`, gltfpack 0.22.0). There is
no simplification; gltfpack drops a few degenerate (zero-area) triangles. On
2026-10-30 the meshes of both robots went from 12.3 MB to 1.47 MB, and decoded
vertices lie within 0.004 mm of the uncompressed conversion. Loaders need the
meshopt decoder (`src/lib/robot-studio/glb-loader.ts`).

网格压缩：GLB 以 meshopt 压缩、坐标 16 位量化保存（`scripts/robot-studio/compress-glb.py`，
gltfpack 0.22.0）。不减面，只去掉少量面积为零的退化三角形。2026-10-30 两台设备的网格
合计从 12.3 MB 降到 1.47 MB，解码后的顶点与未压缩版本相差不超过 0.004 mm。加载时需要
meshopt 解码器（`src/lib/robot-studio/glb-loader.ts`）。
