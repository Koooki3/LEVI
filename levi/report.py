"""Technical report: a read-only view of one configured report folder.

The folder (``LEVI_REPORT_DIR``) is maintained outside LEVI -- usually by the
project's own tooling -- and holds ``LEVI.md`` (English), ``LEVI.zh-CN.md``
(Chinese), ``status.json`` (schema ``levi.report.status.v1``: progress,
metrics, charts, tables, milestones, resources) and ``assets/`` (images the
Markdown references as ``assets/<file>``). See docs/API.md.

LEVI only reads it. It may lie outside ``LEVI_WORKSPACE``; the workspace
guard (``paths.inside``) does not apply to it, so this module confines every
read to that one folder itself: fixed file names for the document and the
status, and for assets a resolved path (symlinks followed) that must stay
under ``assets/``, with no hidden component and an image suffix.
"""

import hashlib
import json
import os
from pathlib import Path

DOCUMENTS = {"en": "LEVI.md", "zh": "LEVI.zh-CN.md"}
STATUS = "status.json"
ASSETS = "assets"
SCHEMA = "levi.report.status.v1"
# Only images: nothing a browser would run as a page from LEVI's origin
# except SVG, which is served with a sandboxing CSP (service.report_asset).
ASSET_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
# A report is prose plus a status file; anything far larger is a mistake,
# not a document to ship to the browser every few seconds.
MAX_BYTES = 8 * 1024 * 1024


def report_dir() -> Path | None:
    """The configured folder, or None when ``LEVI_REPORT_DIR`` is unset."""
    configured = os.getenv("LEVI_REPORT_DIR", "").strip()
    if not configured:
        return None
    return Path(configured).expanduser().resolve()


def _language(lang: str | None) -> str:
    return "zh" if (lang or "").lower().startswith("zh") else "en"


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size) if path.is_file() else None


def _sources(root: Path, lang: str) -> tuple[Path | None, Path]:
    """The document to show (the language's own, else English) and status."""
    wanted = root / DOCUMENTS[lang]
    document = wanted if _stat(wanted) else root / DOCUMENTS["en"]
    return (document if _stat(document) else None), root / STATUS


def version(lang: str | None = None) -> dict:
    """A cheap change marker: stats only, no file is read."""
    lang = _language(lang)
    root = report_dir()
    if root is None:
        return {"configured": False, "etag": _etag("unconfigured"), "mtime": None}
    # Both languages: a translation appearing replaces the English fallback.
    names = (*DOCUMENTS.values(), STATUS)
    stats = [(name, _stat(root / name)) for name in names]
    mtimes = [s[0] for _, s in stats if s]
    return {
        "configured": True,
        "etag": _etag(repr((str(root), lang, stats))),
        "mtime": max(mtimes) / 1e9 if mtimes else None,
    }


def _etag(text: str) -> str:
    return '"' + hashlib.sha256(text.encode()).hexdigest()[:24] + '"'


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_BYTES // 2**20} MiB")
    return path.read_text(encoding="utf-8", errors="replace")


def load(lang: str | None = None) -> dict:
    """The document in ``lang`` (English fallback), status and change marker.

    Never raises for a missing or broken folder: the page explains what is
    wrong instead (``configured``, ``exists``, ``errors``).
    """
    lang = _language(lang)
    marker = version(lang)
    result = {
        "configured": marker["configured"],
        "dir": None,
        "exists": False,
        "lang": lang,
        "document_lang": None,
        "markdown": None,
        "status": None,
        "errors": [],
        "mtime": marker["mtime"],
        "etag": marker["etag"],
    }
    root = report_dir()
    if root is None:
        return result
    result["dir"] = str(root)
    result["exists"] = root.is_dir()
    if not result["exists"]:
        return result
    document, status = _sources(root, lang)
    if document is not None:
        try:
            result["markdown"] = _read_text(document)
            result["document_lang"] = "zh" if document.name == DOCUMENTS["zh"] else "en"
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{document.name}: {exc}")
    if _stat(status):
        try:
            data = json.loads(_read_text(status))
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{STATUS}: {exc}")
        else:
            if not isinstance(data, dict):
                result["errors"].append(f"{STATUS}: expected a JSON object")
            else:
                if data.get("schema") not in (None, SCHEMA):
                    result["errors"].append(
                        f"{STATUS}: schema {data.get('schema')!r}, expected {SCHEMA!r}"
                    )
                result["status"] = data
    return result


def asset(path: str) -> tuple[Path, str]:
    """Resolve an asset under ``<report>/assets``; raises LookupError (404)
    or PermissionError (403)."""
    root = report_dir()
    if root is None:
        raise LookupError("LEVI_REPORT_DIR is not set")
    base = (root / ASSETS).resolve()
    parts = Path(path).parts
    if (
        not path
        or Path(path).is_absolute()
        or "\\" in path
        or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
    ):
        raise PermissionError("Not a report asset")
    full = (base / path).resolve()
    if not full.is_relative_to(base):
        raise PermissionError("Not a report asset")
    kind = ASSET_TYPES.get(full.suffix.lower())
    if kind is None:
        raise PermissionError("Not a report asset")
    if not full.is_file():
        raise LookupError("Asset not found")
    return full, kind
