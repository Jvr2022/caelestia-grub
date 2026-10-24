#!/usr/bin/env python3
"""Generate GRUB-safe RGB/RGBA PNGs, nine-slices and PFF2 fonts."""

import hashlib
import io
import json
import math
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import cairosvg
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "theme"
FONT = ROOT / "assets/fonts/GoogleSansFlex-Regular.ttf"
INK = "#171322"
MANIFEST = json.loads((ROOT / "assets/logos/manifest.json").read_text())
PALETTE = {
    "arch": "#d0bcff",
    "windows": "#bcebdc",
    "ubuntu": "#ffcfb5",
    "fedora": "#c1d9ff",
    "debian": "#f5b8cf",
    "nixos": "#b9dfff",
    "macos": "#e1d9ef",
    "linux": "#d0bcff",
    "recovery": "#ebd6a8",
    "firmware": "#d4d0e5",
    "cachyos": "#b8eed4",
}


def prepare_font():
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    source = next((ROOT / "assets/fonts").glob("GoogleSansFlex-VariableFont*.ttf"))
    variable = TTFont(source)
    axes = {axis.axisTag: axis.defaultValue for axis in variable["fvar"].axes}
    axes.update({"wght": 450, "wdth": 100, "ROND": 50})
    instantiateVariableFont(variable, axes, inplace=True)
    variable.save(FONT)


def badge(name, size=512):
    """Decorative Material-inspired container with an unmodified upstream logo path."""
    entry = MANIFEST["icons"][name]
    source = ROOT / "assets/logos" / entry["file"]
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError(f"Logo checksum mismatch: {source}")
    svg = ET.fromstring(data)
    # Only the ink colour changes; the upstream vector geometry stays intact.
    svg.set("fill", INK if entry["collection"] == "Simple Icons" else "none")
    svg.set("color", INK)
    logo_size = 480
    logo = Image.open(
        io.BytesIO(
            cairosvg.svg2png(
                bytestring=ET.tostring(svg),
                output_width=logo_size,
                output_height=logo_size,
            )
        )
    ).convert("RGBA")
    n = 1024
    im = Image.new("RGBA", (n, n))
    d = ImageDraw.Draw(im)
    color = PALETTE.get(name, "#d0bcff")
    petals = {"arch": 12, "windows": 4, "ubuntu": 6, "macos": 8, "cachyos": 9}.get(
        name, 7
    )
    points = []
    for i in range(1440):
        a = i * math.tau / 1440
        r = n * 0.46 * (1 + 0.065 * math.cos(petals * a))
        points.append((n / 2 + r * math.cos(a), n / 2 + r * math.sin(a)))
    d.polygon(points, fill=color)
    im.alpha_composite(logo, ((n - logo.width) // 2, (n - logo.height) // 2))
    return im.resize((size, size), Image.Resampling.LANCZOS)


def slices(name, color, radius=24):
    im = Image.new("RGBA", (radius * 2 + 4, radius * 2 + 4))
    ImageDraw.Draw(im).rounded_rectangle(
        (0, 0, im.width - 1, im.height - 1), radius=radius, fill=color
    )
    xs = [0, radius, radius + 4, im.width]
    ys = xs
    for row, ns in enumerate(["n", "", "s"]):
        for col, ew in enumerate(["w", "", "e"]):
            key = (ns + ew) or "c"
            im.crop((xs[col], ys[row], xs[col + 1], ys[row + 1])).save(
                OUT / f"{name}_{key}.png"
            )


def background(width, height):
    im = ImageOps.fit(
        Image.open(ROOT / "assets/wallpaper.webp").convert("RGB"),
        (width, height),
        centering=(0.5, 0.44),
    )
    overlay = Image.new("RGBA", im.size, (17, 13, 27, 80))
    im = Image.alpha_composite(im.convert("RGBA"), overlay)
    # Soft vignette is baked once. No wallpaper animation in firmware.
    shade = Image.new("RGBA", (1, height))
    for y in range(height):
        t = y / height
        alpha = int(40 + 105 * max(0, (t - 0.48) / 0.52))
        shade.putpixel((0, y), (15, 12, 24, alpha))
    im = Image.alpha_composite(im, shade.resize(im.size))
    im.convert("RGB").save(OUT / "background.png", optimize=True)


def control_hints():
    """Antialiased, transparent keycap strip at its native GRUB display size."""
    scale = 3
    im = Image.new("RGBA", (640 * scale, 40 * scale))
    draw = ImageDraw.Draw(im)
    label_font = ImageFont.truetype(str(FONT), 16 * scale)
    key_font = ImageFont.truetype(str(FONT), 14 * scale)
    groups = [
        (["left", "right"], "Choose"),
        (["Enter"], "Boot"),
        (["E"], "Edit"),
        (["C"], "Console"),
    ]
    widths = {"left": 32, "right": 32, "Enter": 60, "E": 32, "C": 32}
    text_width = lambda value: draw.textlength(value, font=label_font) / scale
    group_widths = [
        sum(widths[k] for k in keys) + 6 * (len(keys) - 1) + 10 + text_width(label)
        for keys, label in groups
    ]
    x = (640 - sum(group_widths) - 28 * (len(groups) - 1)) / 2
    for keys, label in groups:
        for i, key in enumerate(keys):
            width = widths[key]
            draw.rounded_rectangle(
                (round(x * scale), 4 * scale, round((x + width) * scale), 36 * scale),
                radius=7 * scale,
                fill="#211a30dc",
                outline="#a898c46e",
                width=scale,
            )
            cx = x + width / 2
            if key in ("left", "right"):
                direction = -1 if key == "left" else 1
                points = [(cx - direction * 6, 20), (cx + direction * 6, 20)]
                draw.line(
                    [(round(px * scale), py * scale) for px, py in points],
                    fill="#f2eafa",
                    width=scale,
                )
                points = [
                    (cx + direction * 2, 16),
                    (cx + direction * 6, 20),
                    (cx + direction * 2, 24),
                ]
                draw.line(
                    [(round(px * scale), py * scale) for px, py in points],
                    fill="#f2eafa",
                    width=scale,
                    joint="curve",
                )
            else:
                draw.text(
                    (round(cx * scale), 20 * scale),
                    key,
                    font=key_font,
                    fill="#f2eafa",
                    anchor="mm",
                )
            x += width + (6 if i < len(keys) - 1 else 0)
        x += 10
        draw.text(
            (round(x * scale), 20 * scale),
            label,
            font=label_font,
            fill="#c7bdd4",
            anchor="lm",
        )
        x += text_width(label) + 28
    im.resize((640, 40), Image.Resampling.LANCZOS).save(
        OUT / "controls.png", optimize=True
    )


def main():
    prepare_font()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icons").mkdir(exist_ok=True)
    background(2560, 1440)
    control_hints()
    for name in MANIFEST["icons"]:
        badge(name).save(OUT / "icons" / f"{name}.png", optimize=True)
    for alias, src in MANIFEST["aliases"].items():
        shutil.copyfile(OUT / "icons" / f"{src}.png", OUT / "icons" / f"{alias}.png")
    slices("track", "#393143", 3)
    slices("progress", "#d0bcff", 3)
    for size in (12, 14, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128):
        subprocess.run(
            [
                "grub-mkfont",
                "-s",
                str(size),
                "-n",
                "Caelestia",
                "-o",
                str(OUT / f"caelestia-{size}.pf2"),
                str(FONT),
            ],
            check=True,
        )
    for size in (12, 14, 16, 20, 24, 32, 40, 48, 64, 80, 96):
        subprocess.run(
            [
                "grub-mkfont",
                "-s",
                str(size),
                "-n",
                "Caelestia Mono",
                "-r",
                "0x20-0x7e,0xa0-0x24f,0x370-0x52f,0x2000-0x206f,0x2190-0x21ff,0x2500-0x257f,0xfffd-0xfffd",
                "-o",
                str(OUT / f"caelestia-mono-{size}.pf2"),
                str(ROOT / "assets/fonts/CaskaydiaCoveNerdFontMono-Regular.ttf"),
            ],
            check=True,
        )
    shutil.copyfile(ROOT / "assets/fonts/OFL.txt", OUT / "FONT-LICENSE.txt")
    shutil.copyfile(
        ROOT / "assets/fonts/CASKAYDIA-LICENSE.txt", OUT / "MONO-LICENSE.txt"
    )
    shutil.copyfile(ROOT / "LICENSE", OUT / "LICENSE.txt")
    shutil.copyfile(ROOT / "NOTICE.md", OUT / "ATTRIBUTION.md")
    shutil.copytree(
        ROOT / "assets/logos/licenses", OUT / "licenses", dirs_exist_ok=True
    )
    shutil.copyfile(ROOT / "assets/logos/manifest.json", OUT / "icons/SOURCES.json")
    print(f"Built theme assets in {OUT}")


if __name__ == "__main__":
    main()
