"""Validate the artifact contracts that GRUB actually relies on."""

import re
import struct
import unittest
from pathlib import Path

from PIL import Image

THEME = Path(__file__).resolve().parents[1] / "theme"


class ThemeTest(unittest.TestCase):
    def test_font_names_match_pff2_metadata(self):
        available = set()
        for p in THEME.glob("*.pf2"):
            data = p.read_bytes()
            pos = 0
            while pos + 8 <= len(data):
                section = data[pos : pos + 4]
                size = struct.unpack(">I", data[pos + 4 : pos + 8])[0]
                pos += 8
                if section == b"NAME":
                    available.add(data[pos : pos + size].rstrip(b"\0").decode())
                if size == 0xFFFFFFFF:
                    break
                pos += size
        self.assertTrue(available)
        for theme in THEME.glob("*.txt"):
            if theme.name == "FONT-LICENSE.txt":
                continue
            for name in re.findall(
                r'(?:font|item_font)\s*[:=]\s*"([^"]+)"', theme.read_text()
            ):
                self.assertIn(
                    name, available, f"{theme.name} references an unloaded font"
                )

    def test_pngs_use_grub_supported_pixel_formats(self):
        for p in THEME.rglob("*.png"):
            with Image.open(p) as im:
                self.assertIn(im.mode, ("RGB", "RGBA"), str(p))
                self.assertFalse(im.info.get("interlace"), str(p))
                self.assertGreater(im.width, 0)
                self.assertGreater(im.height, 0)
        with Image.open(THEME / "background.png") as im:
            self.assertGreaterEqual(im.width, 1920)
            self.assertGreaterEqual(im.height, 1080)

    def test_referenced_images_and_nine_slices_exist(self):
        for name in ("carousel.txt",):
            source = (THEME / name).read_text()
            refs = re.findall(
                r'(?:file|desktop-image|terminal-box|\w*pixmap_style|bar_style|highlight_style|scrollbar_frame|scrollbar_thumb)\s*[:=]\s*"([^"]+)"',
                source,
            )
            for ref in refs:
                if "*" in ref:
                    for piece in ("nw", "n", "ne", "w", "c", "e", "sw", "s", "se"):
                        self.assertTrue(
                            (THEME / ref.replace("*", piece)).is_file(), ref
                        )
                else:
                    self.assertTrue((THEME / ref).is_file(), ref)


if __name__ == "__main__":
    unittest.main()
