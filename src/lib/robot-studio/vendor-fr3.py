"""Reproduce the pinned, uncalibrated FR3 visual-only preview assets (stdlib only).
Run from the repository root. Network access is used only during this explicit vendoring step.
DAE matrices, normals, triangle topology and diffuse colors are retained in GLB;
there is no decimation, gripper, inertial model, collision model or hardware connection.
"""
import hashlib
import json
import re
import struct
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

COMMIT = "7aeeddc449edf8d62b594f9e36a81da53e7796f9"
BASE = f"https://raw.githubusercontent.com/frankarobotics/franka_description/{COMMIT}/"
OUT = Path("public/robots/fr3")
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
SOURCES = {}


def fetch(path):
    data = urllib.request.urlopen(BASE + path, timeout=40).read()
    SOURCES[path] = {"url": BASE + path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return data


def convert(data):
    root = ET.fromstring(data)
    assert root.find("c:asset/c:up_axis", NS).text == "Z_UP"
    assert float(root.find("c:asset/c:unit", NS).get("meter")) == 1
    assert not list(root.find("c:library_images", NS)), "Textures need an explicit conversion"
    gltf = {"asset": {"version": "2.0", "generator": "LEVI pinned FR3 DAE conversion; original Z-up link coordinates"}, "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "materials": [], "accessors": [], "bufferViews": []}
    binary = bytearray()

    def accessor(values, width, component, kind, target, bounds=False):
        while len(binary) % 4:
            binary.append(0)
        start = len(binary)
        binary.extend(struct.pack("<" + ("f" if component == 5126 else "I") * len(values), *values))
        view = len(gltf["bufferViews"])
        gltf["bufferViews"].append({"buffer": 0, "byteOffset": start, "byteLength": len(binary) - start, "target": target})
        item = {"bufferView": view, "componentType": component, "count": len(values) // width, "type": kind}
        if bounds:
            item.update(min=[min(values[k::width]) for k in range(width)], max=[max(values[k::width]) for k in range(width)])
        gltf["accessors"].append(item)
        return len(gltf["accessors"]) - 1

    effects = {}
    for effect in root.findall("c:library_effects/c:effect", NS):
        color = effect.find(".//c:diffuse/c:color", NS)
        assert color is not None, "Unsupported material"
        effects[effect.get("id")] = list(map(float, color.text.split()))
    materials = {}
    for mat in root.findall("c:library_materials/c:material", NS):
        materials[mat.get("id")] = len(gltf["materials"])
        color = effects[mat.find("c:instance_effect", NS).get("url")[1:]]
        gltf["materials"].append({"name": mat.get("name"), "pbrMetallicRoughness": {"baseColorFactor": color, "metallicFactor": 0, "roughnessFactor": 0.55}})
    geometries = {x.get("id"): x for x in root.findall("c:library_geometries/c:geometry", NS)}
    for node in root.findall("c:library_visual_scenes/c:visual_scene/c:node", NS):
        assert not node.findall("c:node", NS), "Nested nodes require explicit support"
        matrix = list(map(float, node.find("c:matrix", NS).text.split()))
        primitives = []
        for inst in node.findall("c:instance_geometry", NS):
            mesh = geometries[inst.get("url")[1:]].find("c:mesh", NS)
            sources = {}
            for src in mesh.findall("c:source", NS):
                array = list(map(float, src.find("c:float_array", NS).text.split()))
                stride = int(src.find("c:technique_common/c:accessor", NS).get("stride"))
                assert stride == 3
                sources[src.get("id")] = [array[i:i+3] for i in range(0, len(array), 3)]
            vertices = {v.get("id"): v.find("c:input", NS).get("source")[1:] for v in mesh.findall("c:vertices", NS)}
            bindings = {m.get("symbol"): materials[m.get("target")[1:]] for m in inst.findall(".//c:instance_material", NS)}
            assert len(mesh.findall("c:triangles", NS)) > 0
            assert not mesh.findall("c:polylist", NS)
            for tri in mesh.findall("c:triangles", NS):
                inputs = {i.get("semantic"): (i.get("source")[1:], int(i.get("offset"))) for i in tri.findall("c:input", NS)}
                stride = max(i[1] for i in inputs.values()) + 1
                vsrc, voff = inputs["VERTEX"]; nsrc, noff = inputs["NORMAL"]
                positions = sources[vertices[vsrc]]; normals = sources[nsrc]
                raw = list(map(int, tri.find("c:p", NS).text.split()))
                assert len(raw) == int(tri.get("count")) * 3 * stride
                pairs = {}; pos = []; normal = []; indices = []
                for k in range(0, len(raw), stride):
                    key = (raw[k+voff], raw[k+noff])
                    if key not in pairs:
                        pairs[key] = len(pairs); pos.extend(positions[key[0]]); normal.extend(normals[key[1]])
                    indices.append(pairs[key])
                primitives.append({"attributes": {"POSITION": accessor(pos, 3, 5126, "VEC3", 34962, True), "NORMAL": accessor(normal, 3, 5126, "VEC3", 34962)}, "indices": accessor(indices, 1, 5125, "SCALAR", 34963), "material": bindings[tri.get("material")]})
        gltf["scenes"][0]["nodes"].append(len(gltf["nodes"]))
        gltf["nodes"].append({"name": node.get("name"), "mesh": len(gltf["meshes"]), "matrix": [matrix[r*4+c] for c in range(4) for r in range(4)]})
        gltf["meshes"].append({"primitives": primitives})
    gltf["buffers"] = [{"byteLength": len(binary)}]
    encoded = json.dumps(gltf, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 4); binary.extend(b"\0" * (-len(binary) % 4))
    return struct.pack("<III", 0x46546C67, 2, 28+len(encoded)+len(binary)) + struct.pack("<II",len(encoded),0x4E4F534A) + encoded + struct.pack("<II",len(binary),0x004E4942) + binary


def sections(text):
    result = {}
    for name, body in re.findall(r"(?m)^(joint[0-9]+):\n((?:[ \t].*\n|\n)*)", text + "\n"):
        result[name] = {key: float(value) for key, value in re.findall(r"(?m)^ +([a-z_]+): +([-+0-9.eE]+)", body)}
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for p in ["LICENSE", "NOTICE"]:
        OUT.joinpath(p).write_bytes(fetch(p))
    kinematics = sections(fetch("robots/fr3/kinematics.yaml").decode())
    limits = sections(fetch("robots/fr3/joint_limits.yaml").decode())
    for p in ["robots/fr3/fr3.urdf.xacro", "robots/common/franka_arm.xacro", "robots/common/utils.xacro"]:
        OUT.joinpath(p.rsplit("/",1)[1]).write_bytes(fetch(p))
    OUT.joinpath("kinematics.yaml").write_bytes(fetch("robots/fr3/kinematics.yaml"))
    OUT.joinpath("joint_limits.yaml").write_bytes(fetch("robots/fr3/joint_limits.yaml"))
    chain = []
    robot = ET.Element("robot", name="fr3_preview")
    for i in range(9):
        link = ET.SubElement(robot, "link", name=f"fr3_link{i}")
        if i < 8:
            path = f"meshes/robots/fr3/visual/link{i}.dae"
            OUT.joinpath(f"link{i}.glb").write_bytes(convert(fetch(path)))
            visual = ET.SubElement(link,"visual")
            geometry = ET.SubElement(visual,"geometry")
            ET.SubElement(geometry,"mesh",filename=f"link{i}.glb")
        if i:
            name = f"joint{i}"; k = kinematics[name]
            xyz = [k[a] for a in ["x", "y", "z"]]; rpy = [k[a] for a in ["roll", "pitch", "yaw"]]
            j = ET.SubElement(robot,"joint",name=f"fr3_joint{i}",type="revolute" if i<8 else "fixed")
            ET.SubElement(j,"parent",link=f"fr3_link{i-1}");ET.SubElement(j,"child",link=f"fr3_link{i}")
            ET.SubElement(j,"origin",xyz=" ".join(map(str,xyz)),rpy=" ".join(map(str,rpy)))
            entry = {"name": f"fr3_joint{i}", "xyz": xyz, "rpy": rpy}
            if i<8:
                ET.SubElement(j,"axis",xyz="0 0 1")
                lim = {a: limits[name][a] for a in ["lower","upper","effort","velocity"]}
                ET.SubElement(j,"limit",**{a:str(v) for a,v in lim.items()});entry["limit"] = lim
            chain.append(entry)
    ET.indent(robot)
    OUT.joinpath("fr3.urdf").write_text('<?xml version="1.0"?>\n<!-- Derived visual-only FR3 model; see SOURCE.json and NOTICE. No end effector. -->\n'+ET.tostring(robot,encoding="unicode")+"\n")
    Path("src/lib/robot-studio/fr3-chain.json").write_text(json.dumps(chain,indent=2)+"\n")
    outputs = {p.name: {"bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(OUT.iterdir()) if p.is_file() and p.name != "SOURCE.json"}
    OUT.joinpath("SOURCE.json").write_text(json.dumps({"repository":"https://github.com/frankarobotics/franka_description", "commit":COMMIT,"copyright":"Copyright 2023, 2024 Franka Robotics GmbH", "license":"Apache-2.0; upstream DAE headers explicitly apply Apache-2.0 to model files", "derivation":"DAE triangles, normals, diffuse materials and scene matrices converted to indexed GLB without simplification. Original Z-up link coordinates retained. URDF visual-only chain derived from official kinematics/joint_limits and franka_arm.xacro; accelerometers, dynamics, collision and grippers omitted. No calibrated TCP: endpoint fr3_link8 is the flange.","sources":SOURCES,"outputs":outputs},indent=2)+"\n")
    print(f"Vendored {len(outputs)} files, {sum(v['bytes'] for v in outputs.values())} bytes; commit {COMMIT}")


if __name__ == "__main__":
    main()
