"""Compress the vendored robot meshes in place and re-record them in SOURCE.json.

Run from the repository root after vendor-fr3.py or vendor-robotiq.py has written
plain GLBs: python3 scripts/robot-studio/compress-glb.py [public/robots/<robot> ...]

gltfpack (pinned below, run through bunx) applies meshopt compression with 16-bit
quantized positions. It does not simplify: triangles, normals, node transforms and
diffuse colours are kept (a few degenerate triangles are dropped). On 2026-10-30
the 17 meshes went from 12.3 MB to 1.47 MB, and the largest distance between the
original and the decoded vertices was 0.004 mm. The browser needs the meshopt
decoder (src/lib/robot-studio/glb-loader.ts). A compressed file is never
compressed again: one that already declares EXT_meshopt_compression is skipped.
Run `bun run format` afterwards: SOURCE.json is written with json.dumps.
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

GLTFPACK = "gltfpack@0.22.0"
FLAGS = ["-cc", "-kn", "-km", "-vp", "16"]
DEFAULT = [Path("public/robots/fr3"), Path("public/robots/robotiq-2f85")]


def extensions(path: Path) -> list[str]:
    data = path.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    return json.loads(data[20 : 20 + length]).get("extensionsUsed", [])


def bunx() -> list[str]:
    for name in ("bunx", "bun"):
        found = shutil.which(name)
        if found:
            return [found, GLTFPACK] if name == "bunx" else [found, "x", GLTFPACK]
    sys.exit("bun is not on PATH (LEVI/.runtime/bun-linux-x64)")


def main(dirs: list[Path]) -> None:
    command = bunx()
    for directory in dirs:
        source = directory / "SOURCE.json"
        record = json.loads(source.read_text())
        for glb in sorted(directory.glob("*.glb")):
            if "EXT_meshopt_compression" in extensions(glb):
                continue
            with tempfile.TemporaryDirectory() as scratch:
                out = Path(scratch) / glb.name
                subprocess.run([*command, "-i", str(glb), "-o", str(out), *FLAGS], check=True,
                               stdout=subprocess.DEVNULL)
                os.replace(out, glb)
        for name in record["outputs"]:
            data = (directory / name).read_bytes()
            record["outputs"][name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        record["mesh_compression"] = {
            "tool": GLTFPACK,
            "flags": FLAGS,
            "script": "scripts/robot-studio/compress-glb.py",
            "note": "meshopt compression, 16-bit positions, no simplification; decode with the meshopt decoder",
        }
        source.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"dir": str(directory), "bytes": sum(p.stat().st_size for p in directory.glob("*.glb"))}))


if __name__ == "__main__":
    main([Path(arg) for arg in sys.argv[1:]] or DEFAULT)
