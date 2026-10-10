"""Write campaign figures to disk (the analysis library itself never touches
files: ``levi.automatic.analysis`` only returns bytes and text).

``write_figure(spec, path_stem, formats)`` renders every requested format in
memory first, then writes each file durably: a temporary file in the same
directory, ``fsync``, ``os.replace`` onto the target, ``fsync`` of the
directory. A crash or an exception at any point leaves either the old file or
the new one under the target name, never a half-written one, and the
temporary file is removed when Python is still running to remove it. A render
error (for example ``strict`` PDF text, or a spec that cannot be drawn) is
raised before any file is touched.

See docs/AUTOMATIC_CAMPAIGN.md ("Figures").
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path

from .analysis.figspec import FigureSpec
from .analysis.pdfplot import render_pdf_report
from .analysis.svgplot import render_svg

FORMATS = ("svg", "pdf")
FILE_MODE = 0o644  # a figure is a readable report artefact, not a secret


def _fsync_dir(folder: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(folder, flags)
    except OSError:  # a platform that cannot open a directory (Windows)
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_durable(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so that a crash leaves the old or the
    new bytes. The temporary name is unique, so two writers (threads or
    processes) never share one; the last ``replace`` wins."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as f:
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), FILE_MODE)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def write_figure(
    spec: FigureSpec,
    path_stem: str | os.PathLike,
    formats: tuple[str, ...] | list[str] = FORMATS,
    *,
    lang: str | None = None,
    embed_spec: bool = False,
    strict: bool = False,
    width: float = 640.0,
) -> dict[str, dict]:
    """Write ``<path_stem>.svg`` and/or ``<path_stem>.pdf``.

    Returns one record per format for a report manifest: ``path``, ``bytes``
    and ``sha256``; the PDF record adds ``pdf`` (``ok`` or ``lossy(n)``),
    ``substitutions``, ``truncated`` and ``lang`` from
    ``PdfRender.manifest()``. ``strict`` makes any PDF text substitution an
    error (``LossyTextError``) before anything is written. The folder must
    exist."""
    wanted = list(dict.fromkeys(formats))
    unknown = [f for f in wanted if f not in FORMATS]
    if unknown or not wanted:
        raise ValueError(
            f"formats must be a non-empty selection of {FORMATS}, got {formats!r}"
        )
    stem = Path(path_stem)
    rendered: dict[str, tuple[bytes, dict]] = {}
    for fmt in wanted:  # render everything before writing anything
        if fmt == "svg":
            data = render_svg(spec, lang=lang, embed_spec=embed_spec, width=width)
            rendered[fmt] = (data.encode("utf-8"), {})
        else:
            res = render_pdf_report(spec, lang=lang, width=width, strict=strict)
            rendered[fmt] = (res.data, res.manifest())
    out: dict[str, dict] = {}
    for fmt, (data, extra) in rendered.items():
        target = stem.with_name(f"{stem.name}.{fmt}")
        write_durable(target, data)
        out[fmt] = {
            "path": str(target),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            **extra,
        }
    return out
