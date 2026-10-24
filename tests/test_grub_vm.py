"""UEFI GRUB integration tests; opt in with CAELESTIA_VM_TESTS=1.

Uses only project demo disks and serial markers, never host boot partitions.
"""

import hashlib
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from PIL import Image
from vm import QMP

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("CAELESTIA_VM_TESTS") == "1",
    "Set CAELESTIA_VM_TESTS=1 for QEMU integration",
)
class GrubVMTest(unittest.TestCase):
    def start(self, *options):
        (ROOT / "build/captures").mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, str(ROOT / "tests/vm.py"), "fixture", *options],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.qmp_path = ROOT / "build/test-qmp.sock"
        self.qmp_path.unlink(missing_ok=True)
        with open(ROOT / "build/qemu-test.log", "w") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tests/vm.py"),
                    "run",
                    "--headless",
                    "--qmp",
                    str(self.qmp_path),
                    *(["--secure-boot"] if "--secure-boot" in options else []),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 15
        while not self.qmp_path.exists():
            if time.monotonic() > deadline:
                self.fail("QMP did not start")
            time.sleep(0.1)
        self.q = QMP(self.qmp_path)
        # Wait for real theme pixels, rather than assuming a firmware boot time.
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("VM exited before theme loaded")
            im = self.capture()
            counts = im.resize((320, 180)).getcolors(320 * 180) or []
            if sum(n for n, c in counts if c[:3] == (208, 188, 255)) > 80:
                time.sleep(0.4)
                return
            time.sleep(0.5)
        self.fail("Theme did not render its primary badge/highlight")

    def stop(self):
        if getattr(self, "process", None) and self.process.poll() is None:
            try:
                self.q.execute("quit")
            except (OSError, RuntimeError, ValueError):
                self.process.terminate()
            self.process.wait(timeout=10)
        if getattr(self, "q", None):
            self.q.close()

    def capture(self):
        path = ROOT / "build/vm-frame.ppm"
        self.q.screenshot(path)
        with Image.open(path) as im:
            return im.copy()

    def key(self, value, delay=0.4):
        self.q.key(value)
        time.sleep(delay)

    def serial(self):
        return (ROOT / "build/serial.log").read_text(errors="replace")

    def boot_marker(self, expected):
        self.key("ret", 0.1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if f"CAELESTIA_BOOT:{expected}" in self.serial():
                return
            time.sleep(0.1)
        self.fail(f"Missing boot marker {expected!r}: {self.serial()[-1200:]}")

    def assert_center(self, color):
        # Verify the actual selection badge occupies the centre of the screen.
        im = self.capture()
        w, h = im.size
        counts = (
            im.crop(
                (int(w * 0.42), int(h * 0.40), int(w * 0.58), int(h * 0.67))
            ).getcolors(w * h)
            or []
        )
        self.assertGreater(sum(n for n, c in counts if c[:3] == color), 1500)

    def test_carousel_keys_animation_edit_console_unknown_and_submenu(self):
        self.start("--many")
        # The distro fixture starts gfxterm at 800x600 before our loader runs.
        self.assertEqual(self.capture().size, (1920, 1080))
        self.capture().save(ROOT / "build/captures/grub-carousel-1080p.png")
        self.assert_center((208, 188, 255))
        before = self.capture()
        self.q.key("right")
        frames = []
        for _ in range(14):
            frames.append(self.capture())
            time.sleep(0.015)
        time.sleep(0.4)
        self.assert_center((255, 207, 181))
        after = self.capture()
        w, h = before.size
        for left, right in ((0, int(w * 0.07)), (int(w * 0.93), w)):
            box = (left, int(h * 0.31), right, int(h * 0.79))
            self.assertEqual(
                before.crop(box).tobytes(),
                after.crop(box).tobytes(),
                "Offscreen carousel titles must never paint into screen edges",
            )
        self.assertNotIn(
            "CAELESTIA_BOOT:", self.serial(), "Right must navigate, never activate"
        )
        unique = {hashlib.sha256(f.tobytes()).hexdigest() for f in frames}
        self.assertGreater(len(unique), 2, "Animation must produce intermediate frames")
        # Record actual firmware-rendered GRUB frames.
        before.resize((960, 540)).save(
            ROOT / "build/captures/grub-motion.gif",
            save_all=True,
            append_images=[f.resize((960, 540)) for f in frames],
            duration=35,
            loop=0,
        )
        self.key("home")
        self.key("left")
        self.assert_center((188, 235, 220))
        for _ in range(6):
            self.key("right")
        self.assert_center((208, 188, 255))  # Generic fallback for unrecognised class.
        self.capture().save(ROOT / "build/captures/grub-long-title.png")
        menu = self.capture()
        self.key("e")
        self.assertNotEqual(menu.tobytes(), self.capture().tobytes())
        self.key("esc")
        self.key("c")
        self.assertNotEqual(menu.tobytes(), self.capture().tobytes())
        self.q.type("set caelestia_motion=0\n")
        time.sleep(0.1)
        self.key("esc")
        self.key("end")
        self.assert_center((212, 208, 229))
        self.key("right")
        self.assert_center((212, 208, 229))
        self.key("left")
        self.key("ret")  # Recovery submenu.
        self.assert_center((208, 188, 255))
        self.key("down")
        self.assert_center((235, 214, 168))
        # GRUB restarts the parent menu at its configured default after return.
        self.key("esc")
        self.assert_center((208, 188, 255))
        self.key("end")
        self.key("left")
        self.key("ret")
        self.key("down")
        self.boot_marker("diagnostic")

    def test_right_then_enter_executes_selected_os(self):
        self.start("--reduced-motion")
        self.key("right")
        self.assert_center((255, 207, 181))
        self.boot_marker("ubuntu")

    def test_timeout_is_cancelled_by_navigation(self):
        self.start("--timeout", "4")
        self.key("home")
        time.sleep(4.5)
        self.assertNotIn("CAELESTIA_BOOT:", self.serial())
        self.boot_marker("windows")

    def test_timeout_activates_default(self):
        self.start("--timeout", "3")
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if "CAELESTIA_BOOT:arch" in self.serial():
                return
            time.sleep(0.1)
        self.fail("Default entry did not autoboot")

    def test_low_resolution_carousel(self):
        self.start("--resolution", "1024x768", "--reduced-motion")
        self.assertEqual(self.capture().size, (1024, 768))
        self.assert_center((208, 188, 255))
        self.capture().save(ROOT / "build/captures/grub-carousel-1024.png")
        self.key("home")
        self.boot_marker("windows")

    def test_widescreen_badge_console_and_boot_transition(self):
        self.start("--resolution", "1280x720", "--many", "--reduced-motion")
        im = self.capture()
        self.assertEqual(im.size, (1280, 720))
        badge = im.crop((500, 225, 780, 525))
        mask = Image.eval(
            badge.convert("RGB").getchannel("R"),
            lambda value: 255 if value == 208 else 0,
        )
        box = mask.getbbox()
        self.assertIsNotNone(box)
        self.assertLessEqual(
            abs((box[2] - box[0]) - (box[3] - box[1])),
            4,
            "The selected badge must retain its aspect ratio",
        )
        im.save(ROOT / "build/captures/grub-carousel-720p.png")
        self.key("c")
        console = self.capture()
        console.save(ROOT / "build/captures/grub-console.png")
        self.assertNotEqual(console.tobytes(), im.tobytes())
        self.q.type("echo CAELESTIA_CONSOLE_WORKS\n")
        time.sleep(0.1)
        self.assertNotEqual(console.tobytes(), self.capture().tobytes())
        self.key("esc")
        self.q.key("ret")
        time.sleep(0.25)
        boot = self.capture()
        boot.save(ROOT / "build/captures/grub-booting.png")
        self.assertNotEqual(boot.tobytes(), im.tobytes())
        # Console and boot share the same wallpaper and panel geometry.
        self.assertEqual(
            console.crop((0, 0, 40, 720)).tobytes(),
            boot.crop((0, 0, 40, 720)).tobytes(),
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if "CAELESTIA_BOOT:arch" in self.serial():
                return
            time.sleep(0.1)
        self.fail("Styled boot transition must still execute the selected system")

    def test_resolution_and_aspect_ratio_matrix(self):
        for resolution in (
            "640x480",
            "800x600",
            "1280x1024",
            "2560x1080",
            "3840x2160",
            "768x1024",
        ):
            with self.subTest(resolution=resolution):
                try:
                    self.start(
                        "--resolution",
                        resolution,
                        "--native-resolution",
                        resolution,
                        "--many",
                        "--reduced-motion",
                    )
                    im = self.capture()
                    w, h = map(int, resolution.split("x"))
                    self.assertEqual(
                        im.size, (w, h), "The requested firmware mode must activate"
                    )
                    self.assert_center((208, 188, 255))
                    # Exact badge pixels must retain a square at every aspect ratio.
                    from PIL import ImageChops

                    region = im.crop(
                        (int(w * 0.35), int(h * 0.31), int(w * 0.65), int(h * 0.77))
                    )
                    diff = ImageChops.difference(
                        region.convert("RGB"),
                        Image.new("RGB", region.size, (208, 188, 255)),
                    )
                    channels = diff.split()
                    mask = ImageChops.lighter(
                        ImageChops.lighter(channels[0], channels[1]), channels[2]
                    ).point(lambda value: 0 if value else 255)
                    box = mask.getbbox()
                    self.assertIsNotNone(box)
                    self.assertLessEqual(abs((box[2] - box[0]) - (box[3] - box[1])), 4)
                    im.save(ROOT / f"build/captures/grub-{resolution}.png")
                    self.key("c")
                    console = self.capture()
                    self.assertEqual(
                        console.getpixel((w // 2, h // 2))[:3], (23, 19, 34)
                    )
                    self.key("esc")
                    self.boot_marker("arch")
                finally:
                    self.stop()

    def test_text_fallback_keeps_upstream_right_arrow_activation(self):
        self.start("--reduced-motion")
        self.key("c")
        self.q.type("terminal_output console serial\n")
        time.sleep(0.2)
        self.key("esc")
        self.assertNotIn("CAELESTIA_BOOT:", self.serial())
        self.key("right", 0.1)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if "CAELESTIA_BOOT:arch" in self.serial():
                return
            time.sleep(0.1)
        self.fail("Text fallback should keep upstream Right Arrow semantics")

    def secure_boot_probe(self, index, marker):
        self.start("--secure-boot", "--reduced-motion")
        self.assertIn("CAELESTIA_SECUREBOOT:enforced", self.serial())
        self.capture().save(ROOT / "build/captures/grub-secure-boot.png")
        self.key("home")
        for _ in range(index):
            self.key("right", 0.05)
        self.key("ret", 0.1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            log = self.serial()
            self.assertNotIn("CAELESTIA_SECURITY_FAILURE", log)
            if marker in log:
                return
            time.sleep(0.1)
        self.fail(f"Missing Secure Boot marker {marker}: {self.serial()[-1500:]}")

    def test_secure_boot_accepts_enrolled_signer(self):
        self.secure_boot_probe(3, "CAELESTIA_TRUSTED_CHILD")

    def test_secure_boot_rejects_unsigned_image(self):
        self.secure_boot_probe(4, "CAELESTIA_UNSIGNED_REJECTED")

    def test_secure_boot_rejects_untrusted_signer(self):
        self.secure_boot_probe(5, "CAELESTIA_UNTRUSTED_REJECTED")

    def test_secure_boot_rejects_external_module(self):
        self.secure_boot_probe(6, "CAELESTIA_MODULE_PROBE_DONE")
        self.assertIn("prohibited by secure boot policy", self.serial())
        self.assertNotIn("Hello World", self.serial())

    @unittest.skipUnless(
        os.environ.get("CAELESTIA_TEST_KERNEL"),
        "Set CAELESTIA_TEST_KERNEL to an unsigned EFI-stub Linux kernel",
    )
    def test_secure_boot_starts_signed_linux_kernel(self):
        self.secure_boot_probe(7, "Linux version ")

    @unittest.skipUnless(
        os.environ.get("CAELESTIA_TEST_KERNEL"),
        "Set CAELESTIA_TEST_KERNEL to an unsigned EFI-stub Linux kernel",
    )
    def test_secure_boot_rejects_unsigned_linux_kernel(self):
        self.secure_boot_probe(8, "CAELESTIA_UNSIGNED_KERNEL_REJECTED")


if __name__ == "__main__":
    unittest.main()
