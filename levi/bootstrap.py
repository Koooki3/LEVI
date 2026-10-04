"""Install a pinned project-local Bun, verified against release checksums."""

import hashlib
import json
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
    mark_frontend_deps()
    print("LEVI installed. Run: uv run levi build && uv run levi serve")


# Written into node_modules after a successful `bun install`: the lockfile and
# package.json are compared against it (the folder's own time changes only
# when an entry is added or removed).
DEPS_STAMP = "node_modules/.levi-deps-installed"
LOCK_FILES = ("bun.lock", "package.json")


def mark_frontend_deps(project=None) -> None:
    project = project or PROJECT
    stamp = project / DEPS_STAMP
    if stamp.parent.is_dir():
        stamp.write_text("bun install --frozen-lockfile\n")


def frontend_deps_problem(project=None) -> str:
    """Why ``node_modules`` does not match ``bun.lock``/``package.json`` ("" =
    it does): it is missing, one of them is newer than the last install, or
    a dependency ``package.json`` names is not installed."""
    project = project or PROJECT
    modules = project / "node_modules"
    if not modules.is_dir():
        return "node_modules is missing"
    stamp = project / DEPS_STAMP
    try:
        installed = (stamp if stamp.is_file() else modules).stat().st_mtime
    except OSError:
        return "node_modules cannot be read"
    for name in LOCK_FILES:
        path = project / name
        if path.is_file() and path.stat().st_mtime > installed:
            return f"{name} is newer than node_modules"
    try:
        manifest = json.loads((project / "package.json").read_text())
    except (OSError, ValueError):
        return ""
    missing = [
        name
        for group in ("dependencies", "devDependencies")
        for name in (manifest.get(group) or {})
        if not (modules / name / "package.json").is_file()
    ]
    if missing:
        shown = ", ".join(sorted(missing)[:5]) + (", ..." if len(missing) > 5 else "")
        return f"node_modules lacks {shown}"
    return ""
