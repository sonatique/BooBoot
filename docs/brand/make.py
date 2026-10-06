"""Makes the BooBoot icon, logo and banner, and the icons of BooBoot Console and of the web page.

Needs fontTools and Pillow (pip install fonttools pillow), the DejaVu fonts,
and Chrome or Chromium to draw the PNG files (CHROME, or chromium in PATH).
Run from the repository root: python3 docs/brand/make.py
"""

import base64
import io
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image

BRAND = os.path.dirname(os.path.abspath(__file__))
ICO = os.path.join(BRAND, "..", "..", "client", "csharp", "BooBootConsole", "Assets", "booboot.ico")
WEB = os.path.join(BRAND, "..", "..", "server", "booboot_server", "web")
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

GREEN = "#23D18B"
DARK_GREEN = "#0E8A50"  # on light backgrounds
LIGHT = "#E6EDF3"
INK = "#1F2328"
TAGLINE = "Scarily good DUT control"
SUBLINE = "Power, SD card and serial console, over the network"

GRADIENT = ('<linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#323A44"/><stop offset="1" stop-color="#1D2128"/></linearGradient>')


def ghost(x, y, w, h, r, cut, lobes):
    """Path of a ghost made of an SD card: round top left corner, cut top right corner, scalloped hem."""
    k = min(r, cut) * 0.3
    d = k / math.sqrt(2)
    right, bottom = x + w, y + h
    lobe = w / lobes / 2
    path = (f"M{x + r} {y}H{right - cut - k:.2f}Q{right - cut} {y} {right - cut + d:.2f} {y + d:.2f}"
            f"L{right - d:.2f} {y + cut - d:.2f}Q{right} {y + cut} {right} {y + cut + k:.2f}V{bottom - lobe:.2f}")
    for i in range(1, lobes + 1):
        path += f"A{lobe:.2f} {lobe:.2f} 0 0 1 {right - 2 * lobe * i:.2f} {bottom - lobe:.2f}"
    return path + f"V{y + r}A{r} {r} 0 0 1 {x + r} {y}Z"


def icon_body():
    """The ghost saying "Boo" on a 256 grid, with GRADIENT in the defs.

    One eye is a block cursor, the other winks as an underline cursor.
    """
    shape = ghost(16, 12, 224, 232, 72, 56, 4)
    return (f'<path d="{shape}" fill="url(#tile)"/>'
            f'<path d="{shape}" fill="none" stroke="#FFFFFF" stroke-opacity="0.22" stroke-width="2.5"/>'
            f'<rect x="74" y="82" width="32" height="50" rx="4" fill="{GREEN}"/>'
            f'<rect x="140" y="118" width="32" height="14" rx="4" fill="{GREEN}"/>'
            f'<ellipse cx="123" cy="174" rx="17" ry="21" fill="none" stroke="{LIGHT}" stroke-width="10"/>')


def icon_svg():
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><defs>{GRADIENT}</defs>{icon_body()}</svg>\n'


def small_icon_svg():
    """The same ghost for 32 px and less: thicker eyes and mouth, and an edge for dark backgrounds."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            f'<path d="{ghost(1, 0.5, 30, 31, 9, 8, 3)}" fill="#2B3139" stroke="#5A6470"/>'
            f'<rect x="7.5" y="8.5" width="5" height="8" rx="0.8" fill="{GREEN}"/>'
            f'<rect x="17.5" y="14" width="5" height="2.5" rx="0.6" fill="{GREEN}"/>'
            f'<ellipse cx="15.3" cy="21.6" rx="2.6" ry="3.2" fill="none" stroke="{LIGHT}" stroke-width="2"/></svg>\n')


fonts = {}


def text(s, font, size, x, baseline):
    """Text as path data, so that it does not depend on installed fonts. Returns (path, width)."""
    f = fonts.setdefault(font, TTFont(font))
    glyphs, cmap = f.getGlyphSet(), f.getBestCmap()
    scale = size / f["head"].unitsPerEm
    paths, pos = [], x
    for ch in s:
        pen = SVGPathPen(glyphs)
        glyph = glyphs[cmap[ord(ch)]]
        glyph.draw(TransformPen(pen, (scale, 0, 0, -scale, pos, baseline)))
        paths.append(pen.getCommands())
        pos += glyph.width * scale
    path = re.sub(r"-?\d+\.\d+", lambda m: "%.1f" % float(m.group()), " ".join(paths))
    return path, pos - x


def ink(s, font, size):
    """Left and right edges of the ink of text set at x 0."""
    f = fonts.setdefault(font, TTFont(font))
    glyphs, cmap = f.getGlyphSet(), f.getBestCmap()
    scale = size / f["head"].unitsPerEm
    left = right = None
    pos = 0
    for ch in s:
        glyph = glyphs[cmap[ord(ch)]]
        pen = BoundsPen(glyphs)
        glyph.draw(TransformPen(pen, (scale, 0, 0, -scale, pos, 0)))
        if pen.bounds:
            left = pen.bounds[0] if left is None else min(left, pen.bounds[0])
            right = pen.bounds[2] if right is None else max(right, pen.bounds[2])
        pos += glyph.width * scale
    return left, right


def wordmark(x, baseline, size, boo, boot):
    d1, w1 = text("Boo", MONO, size, x, baseline)
    d2, w2 = text("Boot", MONO, size, x + w1, baseline)
    return f'<path d="{d1}" fill="{boo}"/><path d="{d2}" fill="{boot}"/>', w1 + w2


def logo_svg(boo, boot):
    words, width = wordmark(188, 115, 96, boo, boot)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {188 + width + 4:.0f} 160">'
            f'<defs>{GRADIENT}</defs><g transform="scale(0.625)">{icon_body()}</g>{words}</svg>\n')


def banner_svg():
    words, _ = wordmark(332, 150, 124, LIGHT, GREEN)
    # The tagline spans the ink of the name, from the B to the t; the second line starts at the B.
    left, right = (332 + edge for edge in ink("BooBoot", MONO, 124))
    a, b = ink(TAGLINE, SANS, 38)
    size = 38 * (right - left) / (b - a)
    baseline = 172 + size * 0.95
    tagline, _ = text(TAGLINE, SANS, size, left - a * size / 38, baseline)
    subline, _ = text(SUBLINE, SANS, 26, left - ink(SUBLINE, SANS, 26)[0], baseline + 44)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 320">'
            f'<defs>{GRADIENT}<linearGradient id="panel" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#262C34"/><stop offset="1" stop-color="#121418"/></linearGradient></defs>'
            '<rect x="1" y="1" width="1278" height="318" rx="32" fill="url(#panel)" '
            'stroke="#FFFFFF" stroke-opacity="0.08" stroke-width="2"/>'
            f'<g transform="translate(56 48) scale(0.875)">{icon_body()}</g>'
            f'{words}<path d="{tagline}" fill="#C9D1D9"/><path d="{subline}" fill="#9DA7B3"/></svg>\n')


def chrome():
    for name in (os.environ.get("CHROME"), "chromium", "chromium-browser", "google-chrome"):
        if name and shutil.which(name):
            return shutil.which(name)
    raise SystemExit("Chrome or Chromium not found: set CHROME")


def png(svg, width, height):
    """Draws svg at width x height with Chrome, transparent around. Returns PNG bytes."""
    data = base64.b64encode(svg.encode()).decode()
    page = ("<html><body style='margin:0'>"
            f"<img style='display:block' width={width} height={height} src='data:image/svg+xml;base64,{data}'>")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "page.html"), "w") as f:
            f.write(page)
        shot = os.path.join(d, "shot.png")
        # Headless Chrome has a minimum window size: draw in a large window, then crop.
        subprocess.run([chrome(), "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--default-background-color=00000000", "--force-device-scale-factor=1",
                        f"--window-size={max(width, 600) + 100},{max(height, 600) + 300}",
                        "--screenshot=" + shot, "file://" + os.path.join(d, "page.html")],
                       check=True, capture_output=True)
        out = io.BytesIO()
        Image.open(shot).crop((0, 0, width, height)).save(out, "PNG", optimize=True)
        return out.getvalue()


def ico(images):
    """ICO file with PNG images, as Windows Vista and later read them."""
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = len(head) + 16 * len(images)
    entries, data = b"", b""
    for size, image in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    return head + entries + data


def save(name, content):
    path = os.path.join(BRAND, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)


def main():
    icon, small = icon_svg(), small_icon_svg()
    logo, logo_dark, banner = logo_svg(INK, DARK_GREEN), logo_svg(LIGHT, GREEN), banner_svg()
    for name, svg in [("booboot-icon.svg", icon), ("booboot-icon-small.svg", small), ("booboot-logo.svg", logo),
                      ("booboot-logo-dark.svg", logo_dark), ("booboot-banner.svg", banner)]:
        save(name, svg)

    def icon_png(size):
        return png(small if size <= 32 else icon, size, size)

    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        save(f"png/booboot-icon-{size}.png", icon_png(size))
    save("png/booboot-banner.png", png(banner, 1280, 320))
    save("png/booboot-banner@2x.png", png(banner, 2560, 640))
    for name, svg in [("booboot-logo", logo), ("booboot-logo-dark", logo_dark)]:
        width = int(re.search(r'viewBox="0 0 (\d+) 160"', svg).group(1))
        save(f"png/{name}.png", png(svg, width * 2, 320))
    os.makedirs(os.path.dirname(ICO), exist_ok=True)
    with open(ICO, "wb") as f:
        f.write(ico([(size, icon_png(size)) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)]))
    with open(os.path.join(WEB, "favicon.svg"), "w") as f:
        f.write(small)
    with open(os.path.join(WEB, "favicon.ico"), "wb") as f:
        f.write(ico([(size, icon_png(size)) for size in (16, 32, 48)]))


if __name__ == "__main__":
    main()
