"""Tray / exe icon generator — 米游社自动签到.

Default source of truth is procedural (approved style):
cyan rounded tile + blue day-cells + gold checkmark,
palette sampled from E:\\抢码工具\\repo\\docs\\images\\logo.png.

Optional override: assets/custom_icon.png (square app icon only).
Preview sheets / AI source dumps are never used as the tray icon.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

try:
    from app_config import HOME
    OUT = HOME / "assets"
except Exception:
    OUT = Path(__file__).resolve().parent / "assets"
OUT.mkdir(parents=True, exist_ok=True)

# Palette from 抢码工具 final logo
CYAN = (110, 226, 255, 255)        # #6EE2FF
GLOSS = (160, 239, 255, 255)       # #A0EFFF
BLUE = (25, 163, 255, 255)         # #19A3FF
GOLD = (249, 204, 20, 255)         # #F9CC14
GOLD_SHADOW = (30, 140, 180, 80)

# Only a dedicated square override is accepted — never preview/collage files.
CUSTOM_ICON = OUT / "custom_icon.png"


def _rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return m


def _draw_cells(d: ImageDraw.ImageDraw, size: int, detail: bool) -> None:
    if detail:
        pts = [
            (0.22, 0.24), (0.40, 0.24), (0.58, 0.22),
            (0.22, 0.42), (0.40, 0.40), (0.58, 0.38),
            (0.22, 0.60), (0.40, 0.58),
        ]
        cell = size * 0.115
        radius = cell * 0.32
    else:
        pts = [
            (0.24, 0.28), (0.44, 0.26),
            (0.24, 0.48), (0.44, 0.46),
            (0.24, 0.68),
        ]
        cell = size * 0.14
        radius = cell * 0.3
    for cx, cy in pts:
        x, y = cx * size, cy * size
        half = cell / 2
        d.rounded_rectangle(
            (x - half, y - half, x + half, y + half),
            radius=radius,
            fill=BLUE,
        )


def _check_path(size: int) -> list[tuple[float, float]]:
    s = size
    return [
        (s * 0.36, s * 0.54),
        (s * 0.48, s * 0.68),
        (s * 0.76, s * 0.34),
    ]


def _draw_check(
    layer: Image.Image,
    size: int,
    color: tuple[int, int, int, int],
    stroke: int,
    offset: tuple[int, int] = (0, 0),
) -> None:
    d = ImageDraw.Draw(layer)
    pts = [(x + offset[0], y + offset[1]) for x, y in _check_path(size)]
    d.line(pts, fill=color, width=stroke, joint="curve")
    r = stroke // 2
    for x, y in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)


def make_icon(size: int = 256) -> Image.Image:
    """Procedural approved icon at the given pixel size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    radius = max(2, int(size * 0.22))
    detail = size >= 48

    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=CYAN)
    mask = _rounded_mask(size, radius)

    if size >= 32:
        gloss = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gloss)
        gd.polygon(
            [(size * 0.48, 0), (size, 0), (size, size * 0.48)],
            fill=(*GLOSS[:3], 90),
        )
        gloss = gloss.filter(ImageFilter.GaussianBlur(radius=max(3, size * 0.06)))
        tile = Image.alpha_composite(
            tile,
            Image.composite(gloss, Image.new("RGBA", (size, size), (0, 0, 0, 0)), mask),
        )

    cell_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_cells(ImageDraw.Draw(cell_layer), size, detail)

    stroke = max(3, int(size * (0.105 if detail else 0.13)))
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_check(
        shadow,
        size,
        GOLD_SHADOW,
        stroke + max(1, stroke // 6),
        offset=(max(1, size // 64), max(1, size // 48)),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(0.5, size * 0.012)))
    check = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_check(check, size, GOLD, stroke)

    composed = Image.alpha_composite(tile, cell_layer)
    composed = Image.alpha_composite(composed, shadow)
    composed = Image.alpha_composite(composed, check)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(composed, (0, 0), mask)
    return out


def _load_custom(size: int = 256) -> Image.Image | None:
    """Load optional custom_icon.png if it looks like a real square icon."""
    if not CUSTOM_ICON.exists():
        return None
    try:
        img = Image.open(CUSTOM_ICON)
    except Exception:
        return None
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size
    # Reject preview collages / non-square dumps
    if w < 16 or h < 16 or w > h * 1.2 or h > w * 1.2:
        return None
    return img.resize((size, size), Image.Resampling.LANCZOS)


def master_icon(size: int = 256) -> Image.Image:
    custom = _load_custom(size)
    return custom if custom is not None else make_icon(size)


def ensure_icon() -> Path:
    """Write assets/icon.png + multi-size assets/icon.ico. Returns ico path."""
    ico = OUT / "icon.ico"
    png = OUT / "icon.png"
    master = master_icon(256)
    master.save(png)

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    frames = []
    for w, h in sizes:
        side = max(w, h)
        base = master_icon(side)
        if base.size != (w, h):
            base = base.resize((w, h), Image.Resampling.LANCZOS)
        frames.append(base)
    frames[-1].save(
        ico,
        format="ICO",
        sizes=sizes,
        append_images=frames[:-1],
    )
    return ico


def write_preview_sheet(path: Path | None = None) -> Path:
    path = path or (OUT / "icon_preview.png")
    sizes = [256, 128, 64, 48, 32, 24, 16]
    pad = 24
    width = pad + 256 + pad + 128 + pad + 256 + pad
    height = pad + 256 + pad + 72 + pad + 72 + pad
    sheet = Image.new("RGB", (width, height), (28, 30, 36))
    icons = {s: master_icon(s) for s in sizes}

    sheet.paste(icons[256], (pad, pad), icons[256])
    x128 = pad + 256 + pad
    sheet.paste(icons[128], (x128, pad), icons[128])
    sheet.paste(icons[64], (x128, pad + 128 + pad), icons[64])
    sheet.paste(icons[48], (x128 + 64 + 8, pad + 128 + pad + 8), icons[48])
    sheet.paste(icons[32], (x128 + 64 + 8 + 48 + 8, pad + 128 + pad + 16), icons[32])
    sheet.paste(icons[24], (x128 + 128 + pad, pad + 200), icons[24])
    sheet.paste(icons[16], (x128 + 128 + pad + 32, pad + 204), icons[16])

    tray_sizes = [64, 48, 32, 24, 16]
    y = pad + 256 + pad

    def draw_strip(bg: tuple[int, int, int]) -> None:
        nonlocal y
        d = ImageDraw.Draw(sheet)
        d.rectangle((0, y, width, y + 72), fill=bg)
        x = pad
        for s in tray_sizes:
            ic = icons[s]
            sheet.paste(ic, (x, y + (72 - s) // 2), ic)
            x += s + pad
        y += 72 + pad

    draw_strip((243, 244, 246))
    draw_strip((15, 18, 24))
    sheet.save(path)
    return path


def write_compare_ref(path: Path | None = None) -> Path | None:
    """Side-by-side with 抢码工具 logo (if present) + tray-size crop."""
    path = path or (OUT / "icon_compare_ref.png")
    ref_path = Path(r"E:\抢码工具\repo\docs\images\logo.png")
    if not ref_path.exists():
        return None
    ref = Image.open(ref_path).convert("RGBA").resize((256, 256), Image.LANCZOS)
    new = master_icon(256)
    pad = 24
    sheet = Image.new("RGB", (pad + 256 + pad + 256 + pad, pad + 256 + pad + 80 + pad), (22, 24, 30))
    sheet.paste(ref, (pad, pad), ref)
    sheet.paste(new, (pad + 256 + pad, pad), new)
    y = pad + 256 + pad + 8

    def up(im: Image.Image, f: int) -> Image.Image:
        return im.resize((im.width * f, im.height * f), Image.NEAREST)

    ref32, new32 = ref.resize((32, 32), Image.LANCZOS), new.resize((32, 32), Image.LANCZOS)
    ref16, new16 = ref.resize((16, 16), Image.LANCZOS), new.resize((16, 16), Image.LANCZOS)
    sheet.paste(up(ref32, 2), (pad + 80, y), up(ref32, 2))
    sheet.paste(up(new32, 2), (pad + 256 + pad + 80, y), up(new32, 2))
    sheet.paste(up(ref16, 3), (pad + 160, y + 10), up(ref16, 3))
    sheet.paste(up(new16, 3), (pad + 256 + pad + 160, y + 10), up(new16, 3))
    sheet.save(path)
    return path


if __name__ == "__main__":
    src = CUSTOM_ICON if _load_custom(256) is not None else "(procedural approved design)"
    print("source", src)
    print("built", ensure_icon())
    print("preview", write_preview_sheet())
    cmp = write_compare_ref()
    if cmp:
        print("compare", cmp)
