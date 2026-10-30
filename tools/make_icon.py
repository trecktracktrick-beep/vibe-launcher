"""Draw Vibe's cube logo (same shape as in ui.html) into assets/vibe.ico + vibe.png.
Run:  py tools/make_icon.py   (needs Pillow; only used when building the .exe)"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "assets"
S = 1024  # draw big, scale down for each icon size


def cube(draw, scale, offset, width, color):
    # The logo's paths, in its 100x100 viewBox.
    hexagon = [(50, 8), (88, 29), (88, 71), (50, 92), (12, 71), (12, 29), (50, 8)]
    inner = [[(12, 29), (50, 50), (88, 29)], [(50, 50), (50, 92)]]
    pt = lambda p: (offset + p[0] * scale, offset + p[1] * scale)  # noqa: E731
    for line in [hexagon] + inner:
        draw.line([pt(p) for p in line], fill=color, width=width, joint="curve")
    for p in hexagon + [(50, 50)]:  # round the corners like stroke-linejoin=round
        x, y = pt(p)
        r = width / 2
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def make():
    OUT.mkdir(exist_ok=True)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # dark rounded tile with the launcher's background gradient feel
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle([24, 24, S - 24, S - 24], radius=210, fill=(22, 23, 28, 255))
    glow_bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow_bg).ellipse([180, 160, S - 180, S - 200], fill=(60, 64, 74, 255))
    glow_bg = glow_bg.filter(ImageFilter.GaussianBlur(120))
    mask = tile.split()[3]
    tile = Image.alpha_composite(tile, Image.composite(glow_bg, Image.new("RGBA", (S, S)), mask))
    img = Image.alpha_composite(img, tile)

    scale, offset, width = 6.4, S / 2 - 50 * 6.4, 46
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cube(ImageDraw.Draw(glow), scale, offset, width + 30, (200, 228, 255, 200))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(34)))
    lines = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    cube(ld, scale, offset, width, (255, 255, 255, 255))
    # the small blue accent stroke under the top face
    acc = [(31, 36), (50, 46), (69, 36)]
    ld.line([(offset + x * scale, offset + y * scale) for x, y in acc], fill=(191, 227, 255, 210), width=20)
    img = Image.alpha_composite(img, lines)

    img.resize((512, 512), Image.LANCZOS).save(OUT / "vibe.png")
    img.save(OUT / "vibe.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Wrote {OUT / 'vibe.ico'} and vibe.png")


if __name__ == "__main__":
    make()
