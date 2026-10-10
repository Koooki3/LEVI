"""Evaluation-policy checkpoint discovery (read only).

Lists the checkpoint directories under a policy root (on the maintainer's
machine ``openpi/checkpoints``) so a launch wizard can offer "which policy to
deploy" and "which reset policy, if any". It reads directory listings and a
few small metadata files and nothing else: it never imports openpi, never
loads weights, never recomputes a hash, never opens a port and never follows a
symbolic link. A directory it cannot make sense of is reported as ``unknown``
with a reason, not as an error.

What is read, per directory, each capped in size: ``VERSION.json``,
``CONVERSION.json``, the head of ``README_DEPLOY.md``, ``*.sha256`` lists in
the directory, and ``*.sha256`` lists in the root that name the directory.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROLES = ("forward", "reset", "unknown")
KINDS = ("jax", "pytorch", "unknown")

MAX_ENTRIES = 200  # directories examined under one root
MAX_FILES_PER_ENTRY = 20_000  # files counted when summing the size of one directory
MAX_ROOT_SHA_FILES = 64  # *.sha256 lists read at the root
MAX_META_BYTES = 256 * 1024  # VERSION.json, CONVERSION.json, *.sha256
MAX_README_BYTES = 64 * 1024
MAX_SHA_ENTRIES = 5_000  # lines read from one hash list

# Machine-local conventions, used only when a directory states nothing itself.
# They live here as plain data so a caller can pass its own recipe instead.
DEFAULT_RECIPE: dict[str, Any] = {
    # directory name -> serving config name (setup.md section 5.1)
    "configs": {"pi05_fr3_all_step49999": "pi05_fr3_all_state"},
    # config names that belong to the forward (task) policy family
    "forward_configs": ["pi05_fr3_all_state", "pi05_fr3_all_state_cfg"],
    # directory name -> "forward" | "reset"
    "roles": {},
}

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SERVE_CONFIG = re.compile(r"--policy\.config[ \t=]+([A-Za-z0-9_.\-]+)")
_TRAIN_CONFIG_ROW = re.compile(
    r"^\|\s*配置名\s*\|\s*`([A-Za-z0-9_.\-]+)`", re.MULTILINE
)


@dataclass(frozen=True)
class Sha256Record:
    """One place where a hash is already written down. Nothing is recomputed."""

    source: str  # file name relative to the root
    kind: str  # "manifest" (file hashes) | "conversion" (source and norm_stats hashes)
    covers: str  # "files" | "source_weights+norm_stats"
    entries: int  # number of hashes the record holds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyEntry:
    name: str
    path: str
    kind: str  # "jax" | "pytorch" | "unknown"
    deployable: bool  # JAX directory with params/ and norm_stats.json
    is_jax: bool
    converted: bool  # CONVERSION.json present and readable (converted from PyTorch)
    config: str | None
    config_source: str  # "VERSION.json" | "README_DEPLOY.md" | "recipe" | "none"
    training_config_name: (
        str | None
    )  # README's training-machine name, never used to serve
    role: str  # "forward" | "reset" | "unknown"
    role_source: str  # "VERSION.json" | "recipe" | "convention:config" | "none"
    version: str | None
    model: str | None
    variant: str | None  # VERSION.json "role", e.g. "best", "step15000"
    step: int | None
    version_note: str | None  # VERSION.json "parallel_to", verbatim
    siblings: tuple[str, ...]
    verified: tuple[str, ...]
    not_verified: tuple[str, ...]
    readme_title: str | None
    sha256_state: str  # "recorded" | "missing"
    sha256_records: tuple[Sha256Record, ...]
    size_bytes: int
    size_truncated: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
    problems: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("siblings", "verified", "not_verified", "warnings", "problems"):
            d[key] = list(d[key])
        d["sha256_records"] = [r.to_dict() for r in self.sha256_records]
        return d


@dataclass(frozen=True)
class DiscoveryReport:
    root: str
    entries: tuple[PolicyEntry, ...]
    truncated: bool  # more than max_entries directories; the rest were not examined
    skipped: tuple[
        str, ...
    ]  # names not examined: plain files, symlinks, hidden entries
    problems: tuple[str, ...]  # root-level reasons, e.g. "root_missing"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "entries": [e.to_dict() for e in self.entries],
            "truncated": self.truncated,
            "skipped": list(self.skipped),
            "problems": list(self.problems),
        }


# --- safe reading --------------------------------------------------------------------


def _read_small(path: Path, limit: int) -> tuple[bytes | None, str | None]:
    """Read at most ``limit`` bytes of a regular file; never follow a symlink.

    Returns ``(data, None)`` or ``(None, reason)`` when missing or refused. At most
    ``limit + 1`` bytes are read, so a caller tells "too large" by ``len(data) > limit``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:  # ELOOP for a symlink, EACCES, ...
        return None, f"unreadable:{exc.errno}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not_a_regular_file"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        return None, f"unreadable:{exc.errno}"
    finally:
        os.close(fd)
    return data, None


def _load_json(path: Path, label: str, problems: list[str]) -> dict[str, Any] | None:
    data, why = _read_small(path, MAX_META_BYTES)
    if data is None:
        if why != "missing":
            problems.append(f"{label}:{why}")
        return None
    if len(data) > MAX_META_BYTES:
        problems.append(f"{label}:too_large")
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        problems.append(f"{label}:bad_json:{type(exc).__name__}")
        return None
    if not isinstance(obj, dict):
        problems.append(f"{label}:not_an_object")
        return None
    return obj


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def _step(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# --- one directory -------------------------------------------------------------------


def _tree_size(path: Path, cap: int) -> tuple[int, bool]:
    """Sum file sizes by ``lstat`` without following links; stop after ``cap`` files."""
    total = 0
    seen = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for de in it:
                    try:
                        if de.is_symlink():
                            continue
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                        elif de.is_file(follow_symlinks=False):
                            total += de.stat(follow_symlinks=False).st_size
                            seen += 1
                            if seen >= cap:
                                return total, True
                    except OSError:
                        continue
        except OSError:
            continue
    return total, False


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _parse_sha_list(data: bytes) -> tuple[dict[str, str], int]:
    """``sha256sum`` format: ``<hex>  <path>``. Returns ({path: hex}, bad_lines)."""
    out: dict[str, str] = {}
    bad = 0
    text = data[:MAX_META_BYTES].decode("utf-8", "replace")
    for n, line in enumerate(text.splitlines()):
        if n >= MAX_SHA_ENTRIES:
            break
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not _HEX64.match(parts[0]):
            bad += 1
            continue
        out[parts[1].lstrip("*")] = parts[0].lower()
    return out, bad


def _sha_manifest(path: Path, label: str, problems: list[str]) -> dict[str, str] | None:
    data, why = _read_small(path, MAX_META_BYTES)
    if data is None:
        if why != "missing":
            problems.append(f"{label}:{why}")
        return None
    if len(data) > MAX_META_BYTES:
        problems.append(f"{label}:too_large")
        return None
    entries, bad = _parse_sha_list(data)
    if bad:
        problems.append(f"{label}:{bad}_bad_lines")
    return entries


def _inspect(
    root: Path,
    name: str,
    recipe: Mapping[str, Any],
    root_lists: Mapping[str, int],
    max_files: int,
) -> PolicyEntry:
    d = root / name
    problems: list[str] = []
    warnings: list[str] = []

    has_params = _is_real_dir(d / "params")
    has_actor = _is_real_dir(d / "actor")
    norm_ok = _read_small(d / "norm_stats.json", 1)[0] is not None
    kind = "jax" if has_params else ("pytorch" if has_actor else "unknown")
    is_jax = kind == "jax"
    deployable = is_jax and norm_ok
    if is_jax and not norm_ok:
        problems.append("norm_stats.json:missing")
    if kind == "unknown":
        problems.append("layout_not_recognised")

    version = _load_json(d / "VERSION.json", "VERSION.json", problems) or {}
    conversion = _load_json(d / "CONVERSION.json", "CONVERSION.json", problems)

    readme_title = None
    training_name = None
    serve_cfg = None
    rdata, why = _read_small(d / "README_DEPLOY.md", MAX_README_BYTES)
    if rdata is not None:
        text = rdata[:MAX_README_BYTES].decode("utf-8", "replace")
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        readme_title = first.lstrip("# ").strip()[:200] or None
        m = _SERVE_CONFIG.search(text)
        serve_cfg = m.group(1) if m else None
        m = _TRAIN_CONFIG_ROW.search(text)
        training_name = m.group(1) if m else None
    elif why not in (None, "missing"):
        problems.append(f"README_DEPLOY.md:{why}")

    # configuration name, with the source that gave it
    cfg = _str(version.get("config"))
    cfg_src = "VERSION.json" if cfg else "none"
    if cfg is None and serve_cfg:
        cfg, cfg_src = serve_cfg, "README_DEPLOY.md"
    if cfg is None:
        rc = recipe.get("configs", {}).get(name)
        if isinstance(rc, str) and rc:
            cfg, cfg_src = rc, "recipe"
    if cfg and "cfg" in name.lower() and not cfg.endswith("_cfg"):
        warnings.append("cfg_checkpoint_without_cfg_config")
    if training_name and cfg and training_name != cfg:
        warnings.append("training_config_name_differs_from_serving_config")

    # role: explicit statement first, then the recipe, then the config family
    role, role_src = "unknown", "none"
    for key in ("policy_role", "role"):
        v = version.get(key)
        if v in ("forward", "reset"):
            role, role_src = v, "VERSION.json"
            break
    if role == "unknown":
        rr = recipe.get("roles", {}).get(name)
        if rr in ("forward", "reset"):
            role, role_src = rr, "recipe"
    if role == "unknown" and cfg and cfg in recipe.get("forward_configs", ()):
        role, role_src = "forward", "convention:config"

    # hashes already on disk
    records: list[Sha256Record] = []
    for p in _list_sha_files(d):
        ents = _sha_manifest(p, f"{p.name}", problems)
        if ents:
            records.append(
                Sha256Record(f"{name}/{p.name}", "manifest", "files", len(ents))
            )
    for src, count in sorted(root_lists.items()):
        records.append(Sha256Record(src, "manifest", "files", count))
    if conversion:
        n = sum(
            1
            for k in ("source_sha256", "norm_stats_sha256")
            if _HEX64.match(str(conversion.get(k, "")))
        )
        if n:
            records.append(
                Sha256Record(
                    f"{name}/CONVERSION.json",
                    "conversion",
                    "source_weights+norm_stats",
                    n,
                )
            )
    size, trunc = _tree_size(d, max_files)
    if trunc:
        warnings.append("size_is_a_lower_bound")

    return PolicyEntry(
        name=name,
        path=str(d),
        kind=kind,
        deployable=deployable,
        is_jax=is_jax,
        converted=conversion is not None,
        config=cfg,
        config_source=cfg_src,
        training_config_name=training_name,
        role=role,
        role_source=role_src,
        version=_str(version.get("version")),
        model=_str(version.get("model")),
        variant=_str(version.get("role"))
        if version.get("role") not in ("forward", "reset")
        else None,
        step=_step(version.get("step")),
        version_note=_str(version.get("parallel_to")),
        siblings=tuple(
            v
            for k, v in sorted(version.items())
            if k.startswith("sibling") and isinstance(v, str)
        ),
        verified=tuple(
            s
            for k, v in sorted(version.items())
            if k.startswith("verified")
            for s in _str_list(v)
        ),
        not_verified=_str_list(version.get("not_verified")),
        readme_title=readme_title,
        sha256_state="recorded" if records else "missing",
        sha256_records=tuple(records),
        size_bytes=size,
        size_truncated=trunc,
        warnings=tuple(warnings),
        problems=tuple(problems),
    )


def _list_sha_files(d: Path) -> list[Path]:
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".sha256"))
    except OSError:
        return []
    return [d / n for n in names[:8]]


# --- the root ------------------------------------------------------------------------


def _root_manifests(
    root: Path, names: Iterable[str], problems: list[str]
) -> dict[str, dict[str, int]]:
    """``*.sha256`` lists at the root, attributed to a directory by the path prefix of
    their lines (``<dir>/...``). ``{dir: {source file: entry count}}``."""
    wanted = set(names)
    out: dict[str, dict[str, int]] = {}
    try:
        files = sorted(n for n in os.listdir(root) if n.endswith(".sha256"))
    except OSError:
        return out
    if len(files) > MAX_ROOT_SHA_FILES:
        problems.append("root_sha256_lists_truncated")
        files = files[:MAX_ROOT_SHA_FILES]
    for fn in files:
        ents = _sha_manifest(root / fn, fn, problems)
        if not ents:
            continue
        per_dir: dict[str, int] = {}
        for rel in ents:
            head = rel.replace("\\", "/").lstrip("./").split("/", 1)
            if len(head) == 2 and head[0] in wanted:
                per_dir[head[0]] = per_dir.get(head[0], 0) + 1
        for dname, n in per_dir.items():
            out.setdefault(dname, {})[fn] = n
    return out


def _mark_conflicts(entries: list[PolicyEntry]) -> list[PolicyEntry]:
    """Flag directories that cannot be told apart: names equal after case/Unicode
    folding, or the same (model, version, variant, step) stated twice."""
    groups: dict[tuple, list[int]] = {}
    for i, e in enumerate(entries):
        groups.setdefault(
            ("name", unicodedata.normalize("NFKC", e.name).casefold()), []
        ).append(i)
        if e.model and e.version and e.step is not None:
            groups.setdefault(("id", e.model, e.version, e.variant, e.step), []).append(
                i
            )
    extra: dict[int, list[str]] = {}
    for key, idx in groups.items():
        if len(idx) > 1:
            tag = "name_conflict" if key[0] == "name" else "duplicate_identity"
            for i in idx:
                others = ",".join(entries[j].name for j in idx if j != i)
                extra.setdefault(i, []).append(f"{tag}:{others}")
    out = []
    for i, e in enumerate(entries):
        if i in extra:
            e = PolicyEntry(**{**e.__dict__, "problems": e.problems + tuple(extra[i])})
        out.append(e)
    return out


def discover_report(
    policy_root: str | os.PathLike[str],
    *,
    recipe: Mapping[str, Any] | None = None,
    max_entries: int = MAX_ENTRIES,
    max_files_per_entry: int = MAX_FILES_PER_ENTRY,
) -> DiscoveryReport:
    """Examine every directory directly under ``policy_root``. Read only; see the
    module docstring. Never raises for a bad directory or a bad root."""
    rcp = {**DEFAULT_RECIPE, **(recipe or {})}
    root_problems: list[str] = []
    try:
        root = Path(os.path.realpath(policy_root))
        st = os.stat(root)
    except OSError:
        return DiscoveryReport(str(policy_root), (), False, (), ("root_missing",))
    if not stat.S_ISDIR(st.st_mode):
        return DiscoveryReport(str(root), (), False, (), ("root_not_a_directory",))

    skipped: list[str] = []
    dirs: list[str] = []
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        return DiscoveryReport(
            str(root), (), False, (), (f"root_unreadable:{exc.errno}",)
        )
    for n in names:
        if n.startswith(".") or n in ("", ".", "..") or "/" in n:
            skipped.append(n)
            continue
        p = root / n
        try:
            lst = os.lstat(p)
        except OSError:
            skipped.append(n)
            continue
        if stat.S_ISLNK(lst.st_mode):
            skipped.append(n)  # never followed: could point outside the root
            continue
        if stat.S_ISDIR(lst.st_mode):
            dirs.append(n)
        else:
            skipped.append(n)
    truncated = len(dirs) > max_entries
    dirs = dirs[:max_entries]

    manifests = _root_manifests(root, dirs, root_problems)
    entries = [
        _inspect(root, n, rcp, manifests.get(n, {}), max_files_per_entry) for n in dirs
    ]
    entries = _mark_conflicts(entries)
    return DiscoveryReport(
        str(root), tuple(entries), truncated, tuple(skipped), tuple(root_problems)
    )


def discover(policy_root: str | os.PathLike[str], **kwargs: Any) -> list[PolicyEntry]:
    """List the checkpoint directories under ``policy_root`` (see ``discover_report``)."""
    return list(discover_report(policy_root, **kwargs).entries)


def select(
    entries: Iterable[PolicyEntry], role: str | None = None
) -> list[PolicyEntry]:
    """Deployable entries, optionally only one role. ``select(e, "reset") == []`` means
    the caller can offer human reset only."""
    return [e for e in entries if e.deployable and (role is None or e.role == role)]
