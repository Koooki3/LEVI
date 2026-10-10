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
    subject: str = "this_dir"  # "this_dir" or "source:<path>; norm_stats_from:<path>"
    weights_entries: int = (
        0  # manifest lines that name weight files (params/, *.pt, *.distcp)
    )

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
    # "recorded": a hash list for files of this directory exists; "indirect": only hashes of
    # other files (CONVERSION.json: the PyTorch source, the norm_stats donor); "missing".
    # Never show "indirect" as "verified"; only params_hashed says the weights have a hash.
    sha256_state: str
    params_hashed: bool
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


_HINTS = ("reset", "recovery", "recover", "return", "home")
_ROLE_WORDS = ("forward", "reset")


def _norm(value: Any) -> str | None:
    return value.strip().casefold() if isinstance(value, str) else None


def _hint(text: Any) -> str | None:
    t = _norm(text)
    return next((h for h in _HINTS if t and h in t), None)


def _find_hints(
    name: str,
    version: Mapping[str, Any],
    readme_title: str | None,
    cfgs: Iterable[str | None],
) -> list[str]:
    """Where a reset-like word shows up: directory name, README title, config names,
    and every short string or key of VERSION.json."""
    found: list[str] = []
    if _hint(name):
        found.append("name")
    if _hint(readme_title):
        found.append("readme_title")
    if any(_hint(c) for c in cfgs):
        found.append("config")
    for k, v in version.items():
        if _hint(k) or (isinstance(v, str) and len(v) <= 200 and _hint(v)):
            found.append(f"VERSION.json:{k}")
    return found


def _step(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# --- one directory -------------------------------------------------------------------


def _tree_size(path: Path, cap: int) -> tuple[int, bool]:
    """Sum file sizes by ``lstat`` without following links; stop after ``cap`` directory
    entries of any kind (files, directories, links), so floods of either are bounded."""
    total = 0
    seen = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for de in it:
                    seen += 1
                    if seen > cap:
                        return total, True
                    try:
                        if de.is_symlink():
                            continue
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                        elif de.is_file(follow_symlinks=False):
                            total += de.stat(follow_symlinks=False).st_size
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


def _norm_stats_ok(d: Path, problems: list[str]) -> bool:
    """``norm_stats.json`` at the top, or under ``assets/`` (directly or one level down,
    where openpi's loader looks). Reads one byte; the reason of a refusal is kept."""
    data, why = _read_small(d / "norm_stats.json", 1)
    if data is not None:
        return True
    if (d / "assets" / "norm_stats.json").exists() and (
        _read_small(d / "assets" / "norm_stats.json", 1)[0] is not None
    ):
        return True
    try:
        subs = sorted(os.listdir(d / "assets"))[:50]
    except OSError:
        subs = []
    for sub in subs:
        sd = d / "assets" / sub
        if _is_real_dir(sd) and _read_small(sd / "norm_stats.json", 1)[0] is not None:
            return True
    problems.append(f"norm_stats.json:{why}")
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


def _safe_rel(rel: str) -> str | None:
    """A relative path inside the listed tree, or None: absolute paths, ``..`` and
    hidden first components never name a directory."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or rel.replace("\\", "/").startswith("/") or ".." in parts:
        return None
    return "/".join(parts)


def _is_weights(rel: str) -> bool:
    parts = rel.split("/")
    return "params" in parts[:-1] or rel.endswith((".pt", ".distcp", ".safetensors"))


def _count_weights(entries: Mapping[str, str], strip: int) -> int:
    n = 0
    for rel in entries:
        safe = _safe_rel(rel)
        if safe is None:
            continue
        parts = safe.split("/")[strip:]
        if parts and _is_weights("/".join(parts)):
            n += 1
    return n


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


def _safe_listdir(d: Path) -> list[str]:
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _decide_role(
    name: str,
    version: Mapping[str, Any],
    recipe: Mapping[str, Any],
    cfg: str | None,
    readme_title: str | None,
    serve_cfgs: list[str],
    problems: list[str],
    warnings: list[str],
) -> tuple[str, str]:
    """Role is ``forward``/``reset`` only on an explicit statement (VERSION.json ``policy_role``
    or ``role`` equal to the word after trimming and case folding, or the caller's recipe) or,
    for ``forward`` only, because the config belongs to the known forward family. Any
    reset-like word (name, README title, config, VERSION.json key or short string), an
    unrecognised ``policy_role`` or contradictory statements give ``unknown``."""
    declared: list[str] = []
    for key in ("policy_role", "role"):
        if key not in version:
            continue
        v = _norm(version[key])
        if v in _ROLE_WORDS:
            declared.append(v)  # type: ignore[arg-type]
        elif key == "policy_role":
            problems.append("VERSION.json:unrecognised_policy_role")
            return "unknown", "none"
        # "role" other than forward/reset is a variant such as "best": not a role statement
    hints = _find_hints(name, version, readme_title, [cfg, *serve_cfgs])
    if len(set(declared)) > 1:
        problems.append("role_declarations_conflict")
        return "unknown", "none"
    if declared == ["reset"] or (declared and declared[0] == "reset"):
        return "reset", "VERSION.json"
    if declared:  # forward
        if hints:
            problems.append("role_hint_conflicts_with_declaration")
            return "unknown", "none"
        return "forward", "VERSION.json"
    rr = recipe.get("roles", {}).get(name)
    if rr in _ROLE_WORDS:
        return rr, "recipe"
    if hints:
        warnings.append("role_withheld:reset_hint:" + ",".join(hints[:4]))
        return "unknown", "none"
    if cfg and cfg in recipe.get("forward_configs", ()):
        return "forward", "convention:config"
    return "unknown", "none"


def _inspect(
    root: Path,
    name: str,
    recipe: Mapping[str, Any],
    root_lists: Mapping[str, tuple[int, int]],
    max_files: int,
) -> PolicyEntry:
    d = root / name
    problems: list[str] = []
    warnings: list[str] = []

    has_params = _is_real_dir(d / "params")
    has_actor = _is_real_dir(d / "actor")
    kind = "jax" if has_params else ("pytorch" if has_actor else "unknown")
    is_jax = kind == "jax"
    deployable = False
    if is_jax:
        norm_ok = _norm_stats_ok(d, problems)
        assets_ok = _is_real_dir(d / "assets")
        if not assets_ok:
            problems.append("assets:missing")
        deployable = norm_ok and assets_ok
    if kind == "unknown":
        problems.append("layout_not_recognised")

    version = _load_json(d / "VERSION.json", "VERSION.json", problems) or {}
    conversion = _load_json(d / "CONVERSION.json", "CONVERSION.json", problems)

    readme_title = None
    training_name = None
    serve_cfgs: list[str] = []
    rdata, why = _read_small(d / "README_DEPLOY.md", MAX_README_BYTES)
    if rdata is not None:
        text = rdata[:MAX_README_BYTES].decode("utf-8", "replace")
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        readme_title = first.lstrip("# ").strip()[:200] or None
        serve_cfgs = _SERVE_CONFIG.findall(text)
        m = _TRAIN_CONFIG_ROW.search(text)
        training_name = m.group(1) if m else None
    elif why not in (None, "missing"):
        problems.append(f"README_DEPLOY.md:{why}")

    # configuration name, with the source that gave it
    cfg = _str(version.get("config"))
    cfg_src = "VERSION.json" if cfg else "none"
    if (
        serve_cfgs
        and (cfg is not None or len(set(serve_cfgs)) > 1)
        and set(serve_cfgs) != {cfg}
    ):
        warnings.append("config_sources_disagree")
    if cfg is None and serve_cfgs:
        cfg, cfg_src = serve_cfgs[0], "README_DEPLOY.md"
    if cfg is None:
        rc = recipe.get("configs", {}).get(name)
        if isinstance(rc, str) and rc:
            cfg, cfg_src = rc, "recipe"
    if cfg and "cfg" in name.lower() and not cfg.endswith("_cfg"):
        warnings.append("cfg_checkpoint_without_cfg_config")
    if training_name and cfg and training_name != cfg:
        warnings.append("training_config_name_differs_from_serving_config")

    role, role_src = _decide_role(
        name, version, recipe, cfg, readme_title, serve_cfgs, problems, warnings
    )

    # hashes already on disk
    records: list[Sha256Record] = []
    sha_files = _list_sha_files(d)
    if len([n for n in _safe_listdir(d) if n.endswith(".sha256")]) > len(sha_files):
        problems.append("sha256_lists_truncated")
    for p in sha_files:
        ents = _sha_manifest(p, f"{p.name}", problems)
        if ents:
            records.append(
                Sha256Record(
                    f"{name}/{p.name}",
                    "manifest",
                    "files",
                    len(ents),
                    "this_dir",
                    _count_weights(ents, 0),
                )
            )
    for src, (count, wcount) in sorted(root_lists.items()):
        records.append(
            Sha256Record(src, "manifest", "files", count, "this_dir", wcount)
        )
    if conversion:
        n = sum(
            1
            for k in ("source_sha256", "norm_stats_sha256")
            if _HEX64.match(str(conversion.get(k, "")))
        )
        if n:
            subject = "source:{}; norm_stats_from:{}".format(
                str(conversion.get("source"))[:120],
                str(conversion.get("norm_stats_from"))[:120],
            )
            records.append(
                Sha256Record(
                    f"{name}/CONVERSION.json",
                    "conversion",
                    "source_weights+norm_stats",
                    n,
                    subject,
                )
            )
    direct = [r for r in records if r.kind == "manifest"]
    sha_state = "recorded" if direct else ("indirect" if records else "missing")
    params_hashed = any(r.weights_entries > 0 for r in direct)
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
        if _norm(version.get("role")) not in _ROLE_WORDS
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
        sha256_state=sha_state,
        params_hashed=params_hashed,
        sha256_records=tuple(records),
        size_bytes=size,
        size_truncated=trunc,
        warnings=tuple(warnings),
        problems=tuple(problems),
    )


def _list_sha_files(d: Path) -> list[Path]:
    names = [n for n in _safe_listdir(d) if n.endswith(".sha256")]
    return [d / n for n in names[:8]]


# --- the root ------------------------------------------------------------------------


def _root_manifests(
    root: Path, names: Iterable[str], problems: list[str]
) -> dict[str, dict[str, tuple[int, int]]]:
    """``*.sha256`` lists at the root, attributed to a directory by the first component of
    their (safe, relative) paths. ``{dir: {source file: (entries, weight entries)}}``."""
    wanted = set(names)
    out: dict[str, dict[str, tuple[int, int]]] = {}
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
        per_dir: dict[str, list[int]] = {}
        for rel in ents:
            safe = _safe_rel(rel)
            head = safe.split("/", 1) if safe else []
            if len(head) == 2 and head[0] in wanted:
                c = per_dir.setdefault(head[0], [0, 0])
                c[0] += 1
                c[1] += 1 if _is_weights(head[1]) else 0
        for dname, (n, w) in per_dir.items():
            out.setdefault(dname, {})[fn] = (n, w)
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
        if os.fspath(policy_root) in ("", b""):
            raise ValueError("empty root")
        root = Path(os.path.realpath(policy_root))
        st = os.stat(root)
    except (ValueError, TypeError):
        return DiscoveryReport("", (), False, (), ("root_invalid",))
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
    entries: Iterable[PolicyEntry],
    role: str | None = None,
    *,
    include_unknown: bool = False,
) -> list[PolicyEntry]:
    """Deployable entries, optionally only one role. An entry of role ``unknown`` is returned
    for ``role="forward"`` or ``"reset"`` only with ``include_unknown=True``.
    ``select(e, "reset") == []`` means the caller can offer human reset only."""
    keep = {role, "unknown"} if include_unknown else {role}
    return [e for e in entries if e.deployable and (role is None or e.role in keep)]
