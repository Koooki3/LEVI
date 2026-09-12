"""Small, dependency-free COCO-compatible uncompressed RLE codec.

COCO's mask API accepts an uncompressed RLE object with ``size`` and a list of
run lengths.  Keeping the counts as an Arrow list avoids a compiled image
dependency in LEVI's core environment while retaining lossless mask storage.
The isolated SAM3 worker may use pycocotools for compressed transport later;
the sidecar contract remains compatible with both representations.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def _shape(size: Sequence[int]) -> tuple[int, int]:
    if len(size) != 2:
        raise ValueError("RLE size must contain [height, width]")
    height, width = (int(size[0]), int(size[1]))
    if height <= 0 or width <= 0:
        raise ValueError("RLE dimensions must be positive")
    return height, width


def encode_rle(mask: Sequence[Sequence[object]]) -> dict[str, object]:
    """Encode a rectangular row-major Python mask using COCO column order.

    COCO vectorizes pixels in Fortran order (top-to-bottom within each column)
    and starts with a run of background pixels.  The returned object is JSON
    serializable and can be written directly to a Parquet list/struct column.
    """

    rows = [list(row) for row in mask]
    if not rows or not rows[0]:
        raise ValueError("mask must be a non-empty rectangle")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValueError("mask rows must have equal width")
    height = len(rows)
    flat = [bool(rows[y][x]) for x in range(width) for y in range(height)]
    counts: list[int] = []
    current = False
    run = 0
    for value in flat:
        if value == current:
            run += 1
        else:
            counts.append(run)
            current = value
            run = 1
    counts.append(run)
    result = {"size": [height, width], "counts": counts}
    validate_rle(result)
    return result


def decode_rle(rle: dict[str, object]) -> list[list[bool]]:
    """Decode an uncompressed COCO RLE object into a row-major mask."""

    height, width = _shape(rle.get("size", []))
    counts = rle.get("counts")
    if not isinstance(counts, Iterable) or isinstance(counts, (str, bytes)):
        raise TypeError("uncompressed RLE counts must be a list of integers")
    values: list[bool] = []
    current = False
    for count in counts:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("RLE counts must be non-negative integers")
        values.extend([current] * count)
        current = not current
    expected = height * width
    if len(values) != expected:
        raise ValueError(f"RLE covers {len(values)} pixels; expected {expected}")
    return [
        [values[x * height + y] for x in range(width)] for y in range(height)
    ]


def validate_rle(rle: dict[str, object]) -> None:
    """Validate an RLE without allocating a decoded image."""

    height, width = _shape(rle.get("size", []))
    counts = rle.get("counts")
    if not isinstance(counts, list):
        raise TypeError("uncompressed RLE counts must be a list")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in counts
    ):
        raise ValueError("RLE counts must be non-negative integers")
    if sum(counts) != height * width:
        raise ValueError("RLE counts do not cover the declared image size")
