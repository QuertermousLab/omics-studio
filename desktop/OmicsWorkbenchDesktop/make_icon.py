#!/usr/bin/env python3
"""Draw the Omics Workbench icon: Resources/OmicsWorkbench.svg and the 1024 px master Resources/OmicsWorkbench-1024.png.

The same tile as the other apps (hub/web/pages/favicon.svg for Omics Canvas, genome-canvas/public/favicon.svg): a
128-unit teal tile with Nord colors. Sequencing reads (left) run into a pipeline step (the ring) that gives results
(bars, right); the line below is a job's progress. build.sh turns the PNG into the .icns, as the other apps do.

The PNG is drawn with Pillow from the same shapes as the SVG (no SVG renderer needed): python3 make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
LIGHT = "#eceff4"

# (kind, ...) in the 128-unit tile; drawn in this order
SHAPES = [
    # reads into the step, the step into the results
    ("curve", [(46, 36), (58, 36), (54, 56), (64, 56)], LIGHT, 4, 1.0),
    ("curve", [(48, 56), (52, 56), (58, 56), (64, 56)], LIGHT, 4, 1.0),
    ("curve", [(44, 76), (58, 76), (54, 56), (64, 56)], LIGHT, 4, 1.0),
    ("curve", [(66, 56), (74, 56), (80, 56), (86, 56)], LIGHT, 4, 1.0),
    ("rect", 14, 31, 32, 10, 5, "#ebcb8b", 1.0),
    ("rect", 22, 51, 26, 10, 5, "#d08770", 1.0),
    ("rect", 12, 71, 32, 10, 5, "#bf616a", 1.0),
    ("circle", 66, 56, 15, LIGHT, 1.0),
    ("circle", 66, 56, 6.5, "#4c7a86", 1.0),
    ("rect", 88, 48, 7, 20, 2.5, "#a3be8c", 1.0),
    ("rect", 98, 36, 7, 32, 2.5, "#a3be8c", 1.0),
    ("rect", 108, 43, 7, 25, 2.5, "#8fbcbb", 1.0),
    # a job's progress
    ("curve", [(20, 97), (50, 97), (80, 97), (108, 97)], LIGHT, 6, 0.32),
    ("curve", [(20, 97), (40, 97), (60, 97), (78, 97)], LIGHT, 6, 1.0),
    ("circle", 78, 97, 6.5, "#a3be8c", 1.0),
]


def svg() -> str:
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><defs>'
           '<linearGradient id="tile" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#628f9b"/>'
           '<stop offset="1" stop-color="#3a6370"/></linearGradient></defs>',
           '<rect width="128" height="128" rx="30" fill="url(#tile)"/>']
    for shape in SHAPES:
        kind = shape[0]
        if kind == "curve":
            (a, b, c, d), color, width, opacity = shape[1:]
            out.append(f'<path d="M{a[0]} {a[1]}C{b[0]} {b[1]} {c[0]} {c[1]} {d[0]} {d[1]}" fill="none" stroke="{color}" '
                       f'stroke-width="{width}" stroke-linecap="round"' + (f' stroke-opacity="{opacity}"' if opacity < 1 else "") + "/>")
        elif kind == "rect":
            x, y, w, h, r, color, opacity = shape[1:]
            out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{color}"/>')
        elif kind == "circle":
            x, y, r, color, opacity = shape[1:]
            out.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def rgb(color: str):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def png(size: int = 1024, supersample: int = 4) -> Image.Image:
    big = size * supersample
    # the tile covers 870 of 1024 px, centered, like Resources/OmicsCanvas-1024.png
    tile = 870 / 1024 * big
    offset = (big - tile) / 2
    unit = tile / 128

    def p(x, y):
        return offset + x * unit, offset + y * unit

    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    # diagonal gradient #628f9b -> #3a6370 inside the rounded tile
    top, bottom = rgb("#628f9b"), rgb("#3a6370")
    n = 128  # computed small (t = (x + y) / 2 over the tile), then scaled up smoothly
    small = Image.new("RGBA", (n, n))
    small.putdata([tuple(round(top[k] + (bottom[k] - top[k]) * (x + y) / (2 * (n - 1))) for k in range(3)) + (255,)
                   for y in range(n) for x in range(n)])
    gradient = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    gradient.paste(small.resize((round(tile), round(tile)), Image.BILINEAR), (round(offset), round(offset)))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([*p(0, 0), *p(128, 128)], radius=30 * unit, fill=255)
    image.paste(gradient, (0, 0), mask)

    for shape in SHAPES:
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        kind = shape[0]
        if kind == "curve":
            (a, b, c, d), color, width, opacity = shape[1:]
            points = []
            for i in range(65):
                t = i / 64
                x = (1 - t) ** 3 * a[0] + 3 * (1 - t) ** 2 * t * b[0] + 3 * (1 - t) * t ** 2 * c[0] + t ** 3 * d[0]
                y = (1 - t) ** 3 * a[1] + 3 * (1 - t) ** 2 * t * b[1] + 3 * (1 - t) * t ** 2 * c[1] + t ** 3 * d[1]
                points.append(p(x, y))
            fill = rgb(color) + (255,)
            draw.line(points, fill=fill, width=round(width * unit), joint="curve")
            for x, y in (points[0], points[-1]):  # round caps
                r = width * unit / 2
                draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)
        elif kind == "rect":
            x, y, w, h, r, color, opacity = shape[1:]
            draw.rounded_rectangle([*p(x, y), *p(x + w, y + h)], radius=r * unit, fill=rgb(color) + (255,))
        elif kind == "circle":
            x, y, r, color, opacity = shape[1:]
            draw.ellipse([*p(x - r, y - r), *p(x + r, y + r)], fill=rgb(color) + (255,))
        opacity = shape[-1]
        if opacity < 1:
            alpha = layer.getchannel("A").point(lambda v: round(v * opacity))
            layer.putalpha(alpha)
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
        image = Image.alpha_composite(image, layer)
    return image.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    resources = HERE / "Resources"
    (resources / "OmicsWorkbench.svg").write_text(svg())
    png().save(resources / "OmicsWorkbench-1024.png", optimize=True)
    print("wrote", resources / "OmicsWorkbench.svg", "and", resources / "OmicsWorkbench-1024.png")
