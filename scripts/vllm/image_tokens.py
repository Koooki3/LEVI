#!/usr/bin/env python3
"""Offline image-token table for a Qwen3-VL-family vision processor.

How many vision tokens one frame costs, for the native size and for each way a
profile can shrink it: the profile's ``image_max_side`` (LEVI scales the copy
it sends) and ``max_pixels`` (the server's processor caps the pixel count).
Nothing is loaded but the processor's size settings: no weights, no torch, no
CUDA, no server, no network. Standard library only.

    python scripts/vllm/image_tokens.py MODEL_DIR [--image 640x480]
        [--sides 448,384,320,256] [--markdown OUT.md] [--json OUT.json]

``MODEL_DIR`` is a model snapshot folder (``processor_config.json``, or the
older flat ``preprocessor_config.json``) or one of those files. Without
``--markdown`` and ``--json`` the Markdown table goes to the terminal.

The arithmetic is the processor's own: ``smart_resize`` rounds both sides to
a multiple of ``patch_size * merge_size`` (Python's ``round``, which sends an
exact half to the even neighbour), shrinks past ``max_pixels``, enlarges below
the pixel floor (``shortest_edge``), and one token covers a
``merge_size x merge_size`` block of patches. This is arithmetic on the
processor's settings; what a running server does with ``max_pixels`` is
measured on that server.
"""

import argparse
import json
import math
import sys
from pathlib import Path

DEFAULT_IMAGE = (640, 480)
DEFAULT_SIDES = (448, 384, 320, 256)
CONFIG_FILES = ("processor_config.json", "preprocessor_config.json")


def load_processor(location) -> dict:
    """The size settings of a model's image processor.

    Reads ``processor_config.json`` (settings under ``image_processor``) or
    the older flat ``preprocessor_config.json`` from a folder, or the given
    file. Raises ``ValueError`` naming what is missing."""
    path = Path(location)
    candidates = [path] if path.is_file() else [path / name for name in CONFIG_FILES]
    for candidate in candidates:
        if candidate.is_file():
            break
    else:
        raise ValueError(f"no {' or '.join(CONFIG_FILES)} in {location}")
    data = json.loads(candidate.read_text(encoding="utf8"))
    image = data.get("image_processor", data)
    size = image.get("size") or {}
    found = {
        "patch_size": image.get("patch_size"),
        "merge_size": image.get("merge_size"),
        "min_pixels": size.get("shortest_edge", image.get("min_pixels")),
        "max_pixels": size.get("longest_edge", image.get("max_pixels")),
    }
    for name, value in found.items():
        if type(value) is not int or value <= 0:
            raise ValueError(
                f"{candidate.name} has no positive integer `{name}` for the image "
                f"processor (got {value!r}); this table covers Qwen2-VL-style "
                "processors (patch_size, merge_size, size.shortest_edge/longest_edge)"
            )
    if found["min_pixels"] > found["max_pixels"]:
        raise ValueError(f"{candidate.name}: min_pixels exceeds max_pixels")
    return {
        "source": candidate.name,
        **found,
        "factor": found["patch_size"] * found["merge_size"],
    }


def smart_resize(height, width, factor, min_pixels, max_pixels):
    """The size the processor resizes to, as (height, width): the same rules as
    transformers' Qwen2-VL ``smart_resize``."""
    if max(height, width) / min(height, width) > 200:
        raise ValueError("absolute aspect ratio must be smaller than 200")
    h_bar = round(height / factor) * factor
    w_bar = round(width / factor) * factor
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def scaled_size(width, height, side):
    """The size ``levi.inference.provider.encode_image`` sends for a profile's
    ``image_max_side``: unchanged when the longer side already fits, else both
    sides scaled by ``side / longer side`` and rounded."""
    longer = max(width, height)
    if not side or longer <= side:
        return width, height
    scale = side / longer
    return max(1, round(width * scale)), max(1, round(height * scale))


def image_tokens(width, height, processor, max_pixels=None) -> dict:
    """The processor's view of one frame: the size it resizes to and the number
    of vision tokens. ``max_pixels`` overrides the processor's own ceiling, as
    a request's ``mm_processor_kwargs`` does."""
    ceiling = processor["max_pixels"] if max_pixels is None else max_pixels
    h, w = smart_resize(
        height, width, processor["factor"], processor["min_pixels"], ceiling
    )
    patch, merge = processor["patch_size"], processor["merge_size"]
    return {
        "processed": [w, h],
        "tokens": (h // patch) * (w // patch) // (merge * merge),
    }


def _row(route, value, sent, processor, native_tokens, max_pixels=None):
    result = image_tokens(*sent, processor, max_pixels)
    notes = []
    if sent[0] * sent[1] < processor["min_pixels"]:
        notes.append(
            "smaller than the processor's pixel floor: the server enlarges it "
            f"to {result['processed'][0]}x{result['processed'][1]}"
        )
    if max_pixels is not None and max_pixels < processor["min_pixels"]:
        notes.append("below the pixel floor: the profile field refuses this value")
    return {
        "route": route,
        "value": value,
        "sent": list(sent),
        "max_pixels": max_pixels,
        **result,
        "relative": round(result["tokens"] / native_tokens, 3),
        "note": "; ".join(notes),
    }


def build_table(processor, width=DEFAULT_IMAGE[0], height=DEFAULT_IMAGE[1], sides=()):
    """The table as data: the native frame, then for each ``image_max_side``
    the scaled copy, then for each the ``max_pixels`` that equals the scaled
    copy's pixel count on the native frame."""
    native = image_tokens(width, height, processor)["tokens"]
    rows = [_row("native", None, (width, height), processor, native)]
    scaled = [(side, scaled_size(width, height, side)) for side in sides]
    rows += [
        _row("image_max_side", side, size, processor, native) for side, size in scaled
    ]
    rows += [
        _row(
            "max_pixels",
            size[0] * size[1],
            (width, height),
            processor,
            native,
            max_pixels=size[0] * size[1],
        )
        for _, size in scaled
    ]
    return {
        "processor": processor,
        "image": {"width": width, "height": height},
        "rows": rows,
    }


def render_markdown(table) -> str:
    processor, image = table["processor"], table["image"]
    heading = (
        f"Vision tokens for a {image['width']}x{image['height']} frame "
        f"(patch {processor['patch_size']} x merge {processor['merge_size']} = "
        f"factor {processor['factor']}; pixel floor {processor['min_pixels']:,}, "
        f"ceiling {processor['max_pixels']:,}; from {processor['source']})"
    )
    lines = [
        heading,
        "",
        "| route | setting | sent (WxH) | processed (WxH) | tokens | vs native | note |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in table["rows"]:
        setting = "" if row["value"] is None else f"{row['value']:,}"
        lines.append(
            f"| {row['route']} | {setting} | {row['sent'][0]}x{row['sent'][1]} "
            f"| {row['processed'][0]}x{row['processed'][1]} | {row['tokens']} "
            f"| {row['relative']:.2f} | {row['note']} |"
        )
    return "\n".join(lines) + "\n"


def _size(text):
    try:
        width, height = (int(part) for part in text.lower().split("x"))
        if width > 0 and height > 0:
            return width, height
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"{text!r} is not WIDTHxHEIGHT (e.g. 640x480)")


def _sides(text):
    try:
        sides = tuple(int(part) for part in text.split(",") if part.strip())
        if sides and all(side > 0 for side in sides):
            return sides
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"{text!r} is not a list like 448,384,320,256")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline vision-token table for a Qwen3-VL-family processor "
        "(reads only the processor's size settings)"
    )
    parser.add_argument(
        "model_dir", help="model snapshot folder, or its processor_config.json"
    )
    parser.add_argument("--image", type=_size, default=DEFAULT_IMAGE, metavar="WxH")
    parser.add_argument("--sides", type=_sides, default=DEFAULT_SIDES, metavar="LIST")
    parser.add_argument("--markdown", metavar="FILE", help="write the Markdown table")
    parser.add_argument("--json", metavar="FILE", help="write the table as JSON")
    args = parser.parse_args(argv)
    try:
        table = build_table(load_processor(args.model_dir), *args.image, args.sides)
    except (OSError, ValueError) as exc:
        print(f"image_tokens: {exc}", file=sys.stderr)
        return 2
    text = render_markdown(table)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf8")
    if args.json:
        Path(args.json).write_text(
            json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf8"
        )
    if not (args.markdown or args.json):
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
