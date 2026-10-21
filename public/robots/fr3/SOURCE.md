# FR3 offline preview model / FR3 离线预览模型

Official source: [Franka Robotics franka_description](https://github.com/frankarobotics/franka_description/tree/7aeeddc449edf8d62b594f9e36a81da53e7796f9).
Pinned commit: `7aeeddc449edf8d62b594f9e36a81da53e7796f9`.

Copyright 2023, 2024 Franka Robotics GmbH. Apache License 2.0 applies to both
the source description and the original DAE model files (their headers explicitly
state this). See the retained [LICENSE](LICENSE) and [NOTICE](NOTICE).

The local URDF is a visual-only derivative of the official FR3 description:
`franka_arm.xacro`, `utils.xacro`, `kinematics.yaml`, `joint_limits.yaml`.
Joint origins, axes, limits, and the fixed flange transform are retained.
The eight detailed DAE visual meshes were converted to indexed GLB while
retaining triangle topology, normals, diffuse colors and scene node transforms.
There is no mesh simplification. The link coordinates remain in meters, Z-up;
DAE row-major scene matrices become equivalent GLB column-major matrices.
No collision or inertial model, accelerometers, gripper, or measured TCP is included.
The base frame is `fr3_link0`; the preview endpoint is the flange `fr3_link8`,
0.107 m along link7's Z axis. The initial pose is illustrative, not robot feedback.

Detailed source URLs, original SHA-256 hashes and generated asset hashes are in
[SOURCE.json](SOURCE.json). Reproduction tool: `scripts/robot-studio/vendor-fr3.py` (run by a person; it downloads from GitHub, it is not part of the app)
(Python standard library only; run from repository root). Asset generation is an
explicit development step; the browser fetches only these local assets.

This model and numerical IK are for offline visualization only. They do not
certify collision clearance, calibration, dynamics, velocity limits, stopping
behavior, or hardware safety, and do not send any physical motion commands.

中文：本目录来自上述固定版本的 Franka 官方 FR3 模型；代码和原始 DAE 模型文件
均明确适用 Apache-2.0，已保留许可与版权声明。URDF 保留官方关节变换、转轴、
位置限位及法兰偏移；GLB 保留原始三角面、法线、材质颜色与节点变换，无减面。
只提供离线机械臂与法兰预览，没有碰撞认证、夹爪或实测 TCP 标定。初始姿态为
示意状态，不是真机反馈。求解不收敛时保留最后有效关节姿态，不输出部分解。
此模型不会发送真机动作，也不能作为安全运动规划或采集授权依据。
