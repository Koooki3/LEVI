"""Shared base-model folders for RECAP value checkpoints.

``<checkpoint store>/_base/<name>/`` holds the files RLinf's ValueCriticModel
reads before a checkpoint's ``full_weights.pt`` replaces every weight: the
SigLIP2 so400m-patch14-224 and Gemma3 270M ``config.json`` (and, optionally,
their weights), the Gemma3 tokenizer and SigLIP2's preprocessor files. Several
checkpoints share one folder through a relative path in their manifests
(``../_base/<name>``). ``levi_base.json`` records where the files came from
and whether each one is byte-identical to the official Hub release (a git blob
id for small files, the LFS sha256 for large ones, both published by the Hub
even for gated repositories) or to a ``pretrained_models.sha256`` list shipped
with a checkpoint package. A folder whose files are not all verified is
labelled ``dev_only``; results computed with it say so.

Importing copies (hard-links on the same filesystem) and never modifies or
deletes the source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

BASE = "_base"
RECORD = "levi_base.json"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
CONFIG_FILES = (
    "config.json",
    "preprocessor_config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
)
WEIGHT_FILES = (
    "model.safetensors",
    "model.safetensors.index.json",
    "pytorch_model.bin",
    "pytorch_model.bin.index.json",
)

# The official Hub releases RLinf's value model is built from. ("git", id) is
# the file's git blob sha1, ("sha256", h) the LFS content hash.
OFFICIAL: dict[str, dict[str, Any]] = {
    "google/gemma-3-270m": {
        "revision": "9b0cfec892e2bc2afd938c98eabe4e4a7b1e0ca1",
        "files": {
            "config.json": ("git", "48bf8ed00bfb682ff0d4713914e9934c85caae4f"),
            "generation_config.json": (
                "git",
                "2a395e46010392fc17bc69095c79bf3ec0febcb3",
            ),
            "tokenizer.json": (
                "sha256",
                "7d4046bf0505a327dd5a0abbb427ecd4fc82f99c2ceaa170bc61ecde12809b0c",
            ),
            "tokenizer.model": (
                "sha256",
                "1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c",
            ),
            "tokenizer_config.json": (
                "git",
                "550423832ceb5344d2910f2a099d79da8e850686",
            ),
            "special_tokens_map.json": (
                "git",
                "1a6193244714d3d78be48666cb02cdbfac62ad86",
            ),
            "added_tokens.json": ("git", "e17bde03d42feda32d1abfca6d3b598b9a020df7"),
            "model.safetensors": (
                "sha256",
                "abb58eb73aece5163624090d5383c59cbcdb9e9539ec84cb864f74386c4b21d5",
            ),
        },
    },
    "google/siglip2-so400m-patch14-224": {
        "revision": "78e403963a4f6a3640d07803284752326fdf4edf",
        "files": {
            "config.json": ("git", "e36b2dd31c4a71f0c04df8fdf837ee1b6c88aa54"),
            "preprocessor_config.json": (
                "git",
                "2e52d8e8492b5c496ae04c37bfa09760469fb18b",
            ),
            "special_tokens_map.json": (
                "git",
                "8d6368f7e735fbe4781bf6e956b7c6ad0586df80",
            ),
            "tokenizer.json": (
                "sha256",
                "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322",
            ),
            "tokenizer.model": (
                "sha256",
                "61a7b147390c64585d6c3543dd6fc636906c9af3865a5548f27f31aee1d4c8e2",
            ),
            "tokenizer_config.json": (
                "git",
                "d97c5412159422c3b56fbc99076b1dcc25dd7856",
            ),
            "model.safetensors": (
                "sha256",
                "2041e5330b3db96299b26bc3a6506c7fa8d5e61d5c94f267dd8aa4cfb12c1050",
            ),
        },
    },
}
# The folder names RLinf's configs use for these models.
OFFICIAL_BY_FOLDER = {
    "gemma-3-270m": "google/gemma-3-270m",
    "siglip2-so400m-patch14-224": "google/siglip2-so400m-patch14-224",
}


def root(store: Path) -> Path:
    return store / BASE


def folder(store: Path, name: str) -> Path:
    if not re.fullmatch(NAME_PATTERN, name or ""):
        raise ValueError(
            "A base-model name is 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return root(store) / name


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(16 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_blob(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def read_sha256_file(path: Path) -> dict[str, str]:
    """``sha256sum`` output: ``<hex>  <relative path>`` per line."""
    out = {}
    for line in Path(path).read_text().splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match:
            out[match.group(2).strip().removeprefix("./")] = match.group(1).lower()
    return out


def _listed(sums: dict[str, str], source: Path, name: str) -> str | None:
    """The listed hash of ``name`` in a package list whose paths may carry
    the model folder (``gemma-3-270m/config.json``)."""
    for key in (name, f"{source.name}/{name}"):
        if key in sums:
            return sums[key]
    hits = [v for k, v in sums.items() if k.endswith(f"/{source.name}/{name}")]
    return hits[0] if len(hits) == 1 else None


def _place(source: Path, target: Path) -> str:
    temp = target.with_name(f".{target.name}.{time.time_ns()}.partial")
    try:
        os.link(source, temp)
        how = "hardlink"
    except OSError:
        shutil.copy2(source, temp)
        how = "copy"
    os.replace(temp, target)
    return how


def import_base(
    store: Path,
    source: Path,
    name: str | None = None,
    *,
    official: str | None = None,
    sha256_file: Path | None = None,
    weights: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    """Copy a model folder's config/tokenizer files (and with ``weights`` its
    weight files) into ``_base/<name>`` and record their verification.

    ``official`` names the Hub release to check against (default: guessed
    from the folder name); ``sha256_file`` is a package's hash list. A file
    that disagrees with either refuses the import; a folder where some
    imported file could not be verified is ``dev_only``. ``label`` marks a
    folder as development-only whatever the checks say (e.g. a mirror)."""
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"{source} is not a folder")
    name = name or source.name
    target = folder(store, name)
    if target.exists():
        raise FileExistsError(f"Base model {name!r} already exists at {target}")
    official = official or OFFICIAL_BY_FOLDER.get(source.name)
    if official is not None and official not in OFFICIAL:
        raise ValueError(f"No official hashes known for {official!r}")
    reference = OFFICIAL.get(official, {}).get("files", {}) if official else {}
    sums = read_sha256_file(sha256_file) if sha256_file else {}
    wanted = CONFIG_FILES + (WEIGHT_FILES if weights else ())
    files = [source / f for f in wanted if (source / f).is_file()]
    if not (source / "config.json").is_file():
        raise ValueError(f"{source} has no config.json")
    records: dict[str, dict[str, Any]] = {}
    mismatches = []
    for path in files:
        digest = sha256(path)
        entry: dict[str, Any] = {"sha256": digest, "size": path.stat().st_size}
        checks = []
        if path.name in reference:
            kind, expected = reference[path.name]
            actual = digest if kind == "sha256" else git_blob(path)
            ok = actual == expected
            checks.append(
                {
                    "against": f"{official}@{OFFICIAL[official]['revision'][:7]}",
                    "ok": ok,
                }
            )
            if not ok:
                mismatches.append(f"{path.name} differs from {official}")
        listed = _listed(sums, source, path.name) if sums else None
        if listed is not None:
            ok = listed == digest
            checks.append({"against": str(sha256_file), "ok": ok})
            if not ok:
                mismatches.append(f"{path.name} differs from {sha256_file}")
        entry["checks"] = checks
        entry["verified"] = bool(checks) and all(c["ok"] for c in checks)
        records[path.name] = entry
    if mismatches and label is None:
        raise ValueError(
            "Refusing a base model that differs from its reference: "
            + "; ".join(mismatches)
            + " (import it with --label to keep it as development-only)"
        )
    verified = all(r["verified"] for r in records.values())
    record = {
        "schema": "levi.recap_value.base_model.v1",
        "name": name,
        "source": str(source),
        "official": official,
        "official_revision": OFFICIAL[official]["revision"] if official else None,
        "sha256_file": str(sha256_file) if sha256_file else None,
        "weights_included": any(p.name in WEIGHT_FILES for p in files),
        "files": records,
        "verified": verified and not mismatches,
        "dev_only": label is not None or not verified or bool(mismatches),
        "label": label,
        "imported_at": time.time(),
    }
    staging = target.with_name(f".{name}.{time.time_ns()}.partial")
    staging.mkdir(parents=True)
    try:
        for path in files:
            _place(path, staging / path.name)
        (staging / RECORD).write_text(json.dumps(record, indent=2) + "\n")
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**record, "path": str(target)}


def describe(path: Path | None) -> dict[str, Any] | None:
    """What a manifest's base-model folder is: the import record's
    verification and label, or ``unrecorded`` for a folder placed by hand."""
    if path is None:
        return None
    record_path = Path(path) / RECORD
    if not record_path.is_file():
        return {"path": str(path), "recorded": False, "dev_only": None}
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        return {"path": str(path), "recorded": False, "dev_only": None}
    return {
        "path": str(path),
        "recorded": True,
        "name": record.get("name"),
        "official": record.get("official"),
        "verified": bool(record.get("verified")),
        "dev_only": bool(record.get("dev_only")),
        "label": record.get("label"),
        "weights_included": bool(record.get("weights_included")),
    }


def listing(store: Path) -> list[dict[str, Any]]:
    base = root(store)
    if not base.is_dir():
        return []
    return [
        describe(p)
        for p in sorted(base.iterdir())
        if p.is_dir() and not p.name.startswith(".")
    ]
