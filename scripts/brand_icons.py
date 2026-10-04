"""Draw LEVI's browser icons from the mark in src/components/shell/brand.tsx.

    uv run --with pillow python scripts/brand_icons.py

Writes src/app/icon.svg (the tab icon; black tile on light browser chrome,
white tile on dark), src/app/apple-icon.png (180 px, full bleed: the system
rounds the corners) and src/app/favicon.ico (16, 32, 48 px). The shape is
read from brand.tsx, so changing the mark there and running this again keeps
every copy the same; src/components/shell/__tests__/brand.test.ts checks
icon.svg.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "src/components/shell/brand.tsx"
APP = ROOT / "src/app"

# The graphite pair of the design tokens (--ds-gray-l-11 / --ds-gray-d-12 and
# the page backgrounds); an image file cannot read CSS variables.
INK = "#1d1d1f"
PAPER = "#ffffff"
INK_DARK = "#f5f5f7"
PAPER_DARK = "#0b0b0c"


def mark():
    text = BRAND.read_text()
    size = int(re.search(r"size: (\d+),", text).group(1))
    radius = int(re.search(r"radius: (\d+),", text).group(1))
    rects = [
        tuple(int(v) for v in m)
        for m in re.findall(r"\[(\d+), (\d+), (\d+), (\d+)\]", text)
    ]
    return size, radius, rects


def path(rects):
    return "".join(f"M{x} {y}h{w}v{h}h-{w}z" for x, y, w, h in rects)


def svg(size, radius, rects):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
        f"<style>.t{{fill:{INK}}}.g{{fill:{PAPER}}}"
        f"@media (prefers-color-scheme:dark){{.t{{fill:{INK_DARK}}}.g{{fill:{PAPER_DARK}}}}}"
        "</style>"
        f'<rect class="t" width="{size}" height="{size}" rx="{radius}"/>'
        f'<path class="g" d="{path(rects)}"/></svg>\n'
    )


def raster(size, radius, rects, pixels, rounded):
    from PIL import Image, ImageDraw

    scale = 8  # draw large, then shrink: smooth edges
    big = pixels * scale
    unit = big / size
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if rounded:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius * unit, fill=INK)
    else:
        draw.rectangle((0, 0, big, big), fill=INK)
    for x, y, w, h in rects:
        draw.rectangle(
            (x * unit, y * unit, (x + w) * unit - 1, (y + h) * unit - 1), fill=PAPER
        )
    return image.resize((pixels, pixels), Image.LANCZOS)


def main():
    size, radius, rects = mark()
    (APP / "icon.svg").write_text(svg(size, radius, rects))
    raster(size, radius, rects, 180, rounded=False).convert("RGB").save(
        APP / "apple-icon.png", optimize=True
    )
    icons = [raster(size, radius, rects, px, rounded=True) for px in (48, 32, 16)]
    icons[0].save(
        APP / "favicon.ico", sizes=[(48, 48), (32, 32), (16, 16)], append_images=icons[1:]
    )
    print("wrote icon.svg, apple-icon.png, favicon.ico")


if __name__ == "__main__":
    main()
