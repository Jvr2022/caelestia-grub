"""Read-only installer preflight against real disposable LUKS headers."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("cryptsetup", "jq", "od")),
    "Storage preflight tests require cryptsetup, jq and od",
)
class StorageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="caelestia-storage-")
        self.addCleanup(self.directory.cleanup)
        self.image = Path(self.directory.name) / "disk.img"
        with self.image.open("wb") as image:
            image.truncate(64 * 1024 * 1024)

    def format(self, version="luks2", detached=False):
        command = [
            "cryptsetup",
            "luksFormat",
            "--batch-mode",
            "--type",
            version,
            "--key-file",
            "-",
            "--pbkdf-force-iterations",
            "1000" if version == "luks1" else "4",
        ]
        if version == "luks2":
            command += [
                "--pbkdf",
                "argon2id",
                "--pbkdf-memory",
                "32768",
                "--pbkdf-parallel",
                "1",
            ]
        if detached:
            command += ["--header", str(Path(self.directory.name) / "header.img")]
        subprocess.run(
            [*command, str(self.image)],
            input="test-only-not-a-host-key",
            text=True,
            check=True,
            capture_output=True,
        )

    def validate(self):
        # Execute the production helper, replacing sudo with direct calls on a
        # regular test file. No installer main routine or host device is used.
        script = (ROOT / "install.sh").read_text()
        helper = re.search(
            r"^validate_luks_header\(\) \{\n.*?^\}",
            script,
            flags=re.MULTILINE | re.DOTALL,
        ).group()
        driver = (
            'set -euo pipefail\nroot() { "$@"; }\ndie() { echo "$*" >&2; exit 1; }\n'
        )
        return subprocess.run(
            [
                "bash",
                "-c",
                driver + helper + '\nvalidate_luks_header "$1"',
                "storage-test",
                str(self.image),
            ],
            text=True,
            check=False,
            capture_output=True,
        )

    def test_accepts_luks1_and_luks2_argon2id(self):
        for version in ("luks1", "luks2"):
            with self.subTest(version=version):
                self.format(version)
                result = self.validate()
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_detached_header_without_touching_data(self):
        self.format(detached=True)
        before = self.image.read_bytes()
        result = self.validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached headers", result.stderr)
        self.assertEqual(before, self.image.read_bytes())

    def test_refuses_non_luks_data_without_touching_it(self):
        before = self.image.read_bytes()
        result = self.validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.image.read_bytes())


class BootDetectionTest(unittest.TestCase):
    def detect(self, uuids="", abstractions="", manual=""):
        import os

        script = (ROOT / "install.sh").read_text()
        add_unlock = re.search(
            r"^add_unlock\(\) \{\n.*?^\}", script, flags=re.MULTILINE | re.DOTALL
        ).group()
        probes = re.search(
            r"^  boot_uuid=\$\(root grub-probe.*?(?=^  config_path=)",
            script,
            flags=re.MULTILINE | re.DOTALL,
        ).group()
        driver = """set -euo pipefail
config=/test/grub.cfg
unlock_uuids=()
boot_fs=
abstractions=
die() { echo "$*" >&2; exit 1; }
root() {
 case "$2" in
  --target=fs_uuid) echo 11111111-1111-1111-1111-111111111111 ;;
  --target=fs) echo ext2 ;;
  --target=abstraction) echo "$TEST_ABSTRACTIONS" ;;
  --target=cryptodisk_uuid) echo "$TEST_UUIDS" ;;
  *) exit 99 ;;
 esac
}
validate_boot_mappings() { echo CHECKED_BOOT_MAPPING; }
"""
        driver += (
            add_unlock
            + '\nif [[ -n $TEST_MANUAL ]]; then add_unlock "$TEST_MANUAL"; fi\n'
        )
        driver += probes + '\nprintf "UUID:%s\\n" "${unlock_uuids[@]}"\n'
        return subprocess.run(
            ["bash", "-c", driver],
            check=False,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "TEST_UUIDS": uuids,
                "TEST_ABSTRACTIONS": abstractions,
                "TEST_MANUAL": manual,
            },
        )

    def test_clear_boot_does_not_unlock_encrypted_root(self):
        result = self.detect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("CHECKED_BOOT_MAPPING", result.stdout)
        self.assertEqual(result.stdout.strip(), "UUID:")

    def test_detected_nested_volumes_precede_manual_unlocks(self):
        outer, inner, extra = "1" * 32, "2" * 32, "3" * 32
        result = self.detect(f"{outer}\n{inner}\n{outer}", "cryptodisk lvm", extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["CHECKED_BOOT_MAPPING", f"UUID:{outer}", f"UUID:{inner}", f"UUID:{extra}"],
        )

    def test_missing_encrypted_boot_uuid_stops_installation(self):
        result = self.detect(abstractions="cryptodisk")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not determine its LUKS UUID", result.stderr)
