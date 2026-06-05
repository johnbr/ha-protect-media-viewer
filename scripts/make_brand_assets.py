"""Generate HACS/HA brand assets for Protect Media Viewer.

Produces a polished app-style icon and a horizontal logo lockup into
custom_components/protect_media_viewer/brand/ (the HACS local-brand fallback
path). Rendered at high resolution and downscaled for crisp anti-aliasing.

Run: . .venv/bin/activate && pip install Pillow && python scripts/make_brand_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BRAND_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "protect_media_viewer" / "brand"
FONT_PATH = "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"

TOP = (3, 169, 244)     # #03A9F4 HA light blue
BOTTOM = (1, 87, 155)   # #01579B deep blue
WHITE = (255, 255, 255)
SS = 4                  # supersample factor


def _gradient(size: int) -> Image.Image:
    """Vertical blue gradient square."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = round(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
        g = round(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
        b = round(TOP[2] + (BOTTOM[2] - TOP[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return img


def _icon_mark(size: int) -> Image.Image:
    """The square icon mark: rounded blue tile, 2x2 thumbnail grid, play button."""
    S = size * SS
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Rounded-square mask + gradient fill.
    radius = int(S * 0.22)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=255)
    base.paste(_gradient(S), (0, 0), mask)

    d = ImageDraw.Draw(base)

    # 2x2 grid of translucent "thumbnail" tiles, well separated.
    pad = int(S * 0.17)
    gap = int(S * 0.085)
    cell = (S - 2 * pad - gap) // 2
    tile_r = int(cell * 0.22)
    for i in range(2):
        for j in range(2):
            x0 = pad + i * (cell + gap)
            y0 = pad + j * (cell + gap)
            d.rounded_rectangle(
                [x0, y0, x0 + cell, y0 + cell],
                radius=tile_r,
                fill=(255, 255, 255, 90),
            )

    # Central play button: a deep-blue ring separates the white disc from the
    # tiles behind it, then a blue triangle on the white disc.
    cx = cy = S // 2
    ring = int(S * 0.245)
    disc = int(S * 0.205)
    d.ellipse([cx - ring, cy - ring, cx + ring, cy + ring], fill=BOTTOM + (255,))
    d.ellipse([cx - disc, cy - disc, cx + disc, cy + disc], fill=(255, 255, 255, 255))

    # Right-pointing play triangle, optically centered.
    half_h = int(disc * 0.50)
    left = cx - int(disc * 0.34)
    right = cx + int(disc * 0.52)
    d.polygon(
        [(left, cy - half_h), (left, cy + half_h), (right, cy)],
        fill=BOTTOM,
    )

    return base.resize((size, size), Image.LANCZOS)


def _logo(width: int, height: int) -> Image.Image:
    """Horizontal lockup: icon mark + wordmark on transparent background."""
    S = SS
    img = Image.new("RGBA", (width * S, height * S), (0, 0, 0, 0))
    mark_size = height * S
    mark = _icon_mark(height).resize((mark_size, mark_size), Image.LANCZOS)
    img.alpha_composite(mark, (0, 0))

    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, int(height * S * 0.30))
    sub_font = ImageFont.truetype(FONT_PATH, int(height * S * 0.18))
    tx = mark_size + int(height * S * 0.18)
    d.text((tx, height * S * 0.22), "Protect", font=font, fill=BOTTOM)
    d.text((tx, height * S * 0.52), "Media Viewer", font=sub_font, fill=TOP)

    return img.resize((width, height), Image.LANCZOS)


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)

    _icon_mark(256).save(BRAND_DIR / "icon.png")
    _icon_mark(512).save(BRAND_DIR / "icon@2x.png")
    _logo(512, 160).save(BRAND_DIR / "logo.png")
    _logo(1024, 320).save(BRAND_DIR / "logo@2x.png")

    for f in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        p = BRAND_DIR / f
        print(f"  wrote {p.relative_to(BRAND_DIR.parents[2])}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
