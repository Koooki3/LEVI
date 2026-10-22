"""Vendor the local BSD-3-Clause Robotiq 2F-85 model without network or ROS.

Run from the repository root with --source pointing to the shared, read-only
ros2_robotiq_gripper checkout. No installation, robot connection or test is run.
Reuse the existing standard-library DAE converter without invoking its main.
"""
import argparse
import copy
import hashlib
import json
import runpy
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "public/robots/robotiq-2f85"
PINNED_COMMIT = "a74d007d8f2f06dc6a503ad21038ba869d4999a6"
REPOSITORY = "https://github.com/PickNikRobotics/ros2_robotiq_gripper"
MACRO = "robotiq_description/urdf/robotiq_2f_85_macro.urdf.xacro"
PREFIX = "robotiq_description/meshes/visual/2f_85/"
XACRO = "{http://wiki.ros.org/xacro}"
CONVERTER = ROOT / "scripts/robot-studio/vendor-fr3.py"


def fingerprint(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def relabel_glb(data):
    length = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + length])
    gltf["asset"]["generator"] = "LEVI local Robotiq DAE conversion; original Z-up link coordinates"
    encoded = json.dumps(gltf, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 4)
    tail = data[20 + length:]
    return struct.pack("<III", 0x46546C67, 2, 20 + len(encoded) + len(tail)) + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded + tail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--server-source", type=Path, required=True, help="Read-only robotiq_gripper_server_ros2.py used for conversion provenance")
    args = parser.parse_args()
    source = args.source
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != PINNED_COMMIT:
        raise ValueError("Source commit differs from the reviewed local model")
    converter = runpy.run_path(str(CONVERTER))["convert"]
    inputs = {}

    def read(path):
        data = source.joinpath(path).read_bytes()
        expected = subprocess.check_output(["git", "-C", str(source), "show", f"{PINNED_COMMIT}:{path}"])
        if data != expected:
            raise ValueError(f"Source differs from pinned commit: {path}")
        inputs[path] = {"url": f"{REPOSITORY}/blob/{PINNED_COMMIT}/{path}", **fingerprint(data)}
        return data

    license_data = read("LICENSE")
    package = read("robotiq_description/package.xml")
    if ET.fromstring(package).findtext("license") != "BSD-3-Clause":
        raise ValueError("Unexpected model package license")
    macro_data = read(MACRO)
    macro = ET.fromstring(macro_data).find(XACRO + "macro")
    if macro is None:
        raise ValueError("Expected the local gripper macro")
    OUT.mkdir(parents=True, exist_ok=True)
    OUT.joinpath("LICENSE").write_bytes(license_data)
    robot = ET.Element("robot", name="robotiq_2f85_preview")
    ET.SubElement(robot, "link", name="gripper_mount")
    mesh_files = set()
    for child in macro:
        if child.tag not in ["link", "joint"]:
            continue
        node = copy.deepcopy(child)
        for element in node.iter():
            for key, value in element.attrib.items():
                value = value.replace("${prefix}", "").replace("${parent}", "gripper_mount")
                if "${" in value:
                    raise ValueError("Unexpected unresolved macro expression")
                element.set(key, value)
        if node.tag == "link":
            for child_node in list(node):
                if child_node.tag != "visual":
                    node.remove(child_node)
            for mesh in node.findall("visual/geometry/mesh"):
                original = mesh.get("filename")
                if not original.startswith("package://robotiq_description/meshes/visual/2f_85/"):
                    raise ValueError("Unexpected mesh location")
                stem = Path(original).stem
                if stem not in mesh_files:
                    OUT.joinpath(stem + ".glb").write_bytes(relabel_glb(converter(read(PREFIX + stem + ".dae"))))
                    mesh_files.add(stem)
                mesh.set("filename", stem + ".glb")
        else:
            for block in list(node):
                if block.tag == XACRO + "insert_block":
                    node.remove(block)
                    # Standalone model coordinates only: NOT a flange installation.
                    ET.SubElement(node, "origin", xyz="0 0 0", rpy="0 0 0")
        robot.append(node)
    ET.indent(robot)
    OUT.joinpath("robotiq-2f85.urdf").write_text('<?xml version="1.0"?>\n<!-- BSD-3-Clause derivative; see LICENSE and SOURCE.md. Standalone identity mount is not flange calibration. -->\n' + ET.tostring(robot, encoding="unicode") + "\n")
    server = args.server_source.read_bytes()
    # Record only the relevant conversion derivation, not unrelated robot code.
    text = server.decode()
    derivation = text[text.index("_X0 ="):text.index("\n\nclass RobotiqGripperServerROS2")]
    OUT.joinpath("SOURCE.md").write_text("""# Robotiq 2F-85 offline model / 离线夹爪模型

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
""")
    outputs = {p.name: fingerprint(p.read_bytes()) for p in sorted(OUT.iterdir()) if p.is_file() and p.name != "SOURCE.json"}
    manifest = {"repository": REPOSITORY, "commit": commit, "license": "BSD-3-Clause", "copyright": "Copyright (c) 2022, PickNik Robotics", "sources": inputs, "conversion_tool": {"path": "scripts/robot-studio/vendor-fr3.py", **fingerprint(CONVERTER.read_bytes())}, "server_derivation": {"source_name": "robotiq_gripper_server_ros2.py", **fingerprint(server), "conversion_excerpt": derivation}, "standalone_frame": "gripper_mount", "installation_calibrated": False, "outputs": outputs}
    OUT.joinpath("SOURCE.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"files": len(outputs) + 1, "glb_files": len(mesh_files), "output_bytes": sum(p.stat().st_size for p in OUT.iterdir() if p.is_file()), "commit": commit}))


if __name__ == "__main__":
    main()
