"""Upstream assets and GRUB class mappings must stay authentic and complete."""

import hashlib
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGOS = ROOT / "assets/logos"
MANIFEST = json.loads((LOGOS / "manifest.json").read_text())


class LogoTest(unittest.TestCase):
    def test_pinned_sources_match_original_svg_checksums(self):
        self.assertGreaterEqual(len(MANIFEST["icons"]), 39)
        for name, entry in MANIFEST["icons"].items():
            data = (LOGOS / entry["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], name)
            self.assertIn(entry["revision"], entry["url"])
            self.assertTrue(entry["source"].startswith("https://"))
            self.assertIn("viewBox", ET.fromstring(data).attrib)
            self.assertTrue((ROOT / "theme/icons" / f"{name}.png").is_file(), name)

    def test_aliases_resolve_without_impersonating_other_distros(self):
        for alias, target in MANIFEST["aliases"].items():
            self.assertIn(target, MANIFEST["icons"])
            self.assertEqual(
                (ROOT / "theme/icons" / f"{alias}.png").read_bytes(),
                (ROOT / "theme/icons" / f"{target}.png").read_bytes(),
            )
        self.assertEqual(MANIFEST["aliases"]["os"], "unknown")
        for name in ("cachyos", "manjaro", "endeavouros", "unknown", "linux"):
            self.assertNotEqual(
                (ROOT / "theme/icons" / f"{name}.png").read_bytes(),
                (ROOT / "theme/icons/arch.png").read_bytes(),
                name,
            )

    def test_distributed_provenance_matches_sources(self):
        self.assertEqual(
            json.loads((ROOT / "theme/icons/SOURCES.json").read_text()), MANIFEST
        )
        for licence in (LOGOS / "licenses").iterdir():
            self.assertEqual(
                licence.read_bytes(),
                (ROOT / "theme/licenses" / licence.name).read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
