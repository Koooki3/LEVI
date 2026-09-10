"""Install a pinned project-local Bun, verified against release checksums."""

import hashlib
import os
import platform
import subprocess
import urllib.request
import zipfile
from .paths import PROJECT, ROOT, inside

BUN_VERSION = "1.3.10"


def bun_path():
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    if not system:
        raise RuntimeError("Use Linux, macOS, or WSL2 for LEVI")
    arch = {
        "x86_64": "x64",
        "AMD64": "x64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(platform.machine())
    if not arch:
        raise RuntimeError("Unsupported CPU architecture")
    return PROJECT / ".runtime" / f"bun-{system}-{arch}" / "bun"


def setup():
    executable = bun_path()
    asset = executable.parent.name + ".zip"
    release = f"https://github.com/oven-sh/bun/releases/download/bun-v{BUN_VERSION}"
    if not executable.exists():
        archive = inside(ROOT / "tmp/downloads" / asset)
        archive.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(
            release + "/SHASUMS256.txt", timeout=60
        ) as response:
            checksums = {
                name.strip("*"): digest
                for digest, name in (
                    line.split() for line in response.read().decode().splitlines()
                )
            }
        urllib.request.urlretrieve(release + "/" + asset, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != checksums[asset]:
            raise RuntimeError("Bun checksum verification failed")
        with zipfile.ZipFile(archive) as bundle:
            # Extract only the executable named by the pinned archive layout.
            content = bundle.read(executable.parent.name + "/bun")
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(content)
        executable.chmod(0o755)
    version = subprocess.check_output([str(executable), "--version"], text=True).strip()
    if version != BUN_VERSION:
        raise RuntimeError(f"Expected Bun {BUN_VERSION}, found {version}")
    os.environ["PATH"] = str(executable.parent) + os.pathsep + os.environ["PATH"]
    subprocess.run(
        [str(executable), "install", "--frozen-lockfile"], cwd=PROJECT, check=True
    )
    print("LEVI installed. Run: uv run levi build && uv run levi serve")
