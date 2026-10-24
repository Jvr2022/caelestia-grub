#!/usr/bin/env python3
"""QEMU fixture and QMP client. Boots the real install.sh payload."""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class QMP:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(20)
        self.socket.connect(str(path))
        self.file = self.socket.makefile("rwb", buffering=0)
        self.file.readline()
        self.execute("qmp_capabilities")

    def execute(self, command, **arguments):
        self.file.write(
            (json.dumps({"execute": command, "arguments": arguments}) + "\n").encode()
        )
        while True:
            response = json.loads(self.file.readline())
            if "error" in response:
                raise RuntimeError(response["error"])
            if "return" in response:
                return response["return"]

    def key(self, key):
        return self.execute(
            "human-monitor-command", **{"command-line": f"sendkey {key} 10"}
        )

    def screenshot(self, path):
        self.execute("screendump", filename=str(path))

    def type(self, text):
        import time

        special = {
            " ": "spc",
            "\n": "ret",
            "/": "slash",
            "-": "minus",
            "_": "shift-minus",
            ".": "dot",
            "=": "equal",
            "(": "shift-9",
            ")": "shift-0",
        }
        for c in text:
            key = special.get(c, c.lower())
            if c.isupper():
                key = "shift-" + key
            self.key(key)
            time.sleep(0.025)

    def close(self):
        self.file.close()
        self.socket.close()


def fixture(args):
    """Exercise the actual installer payload with disposable GRUB menu entries."""
    out = ROOT / "build/vm"
    command = ["bash", str(ROOT / "install.sh"), "--stage", str(out)]
    if args.secure_boot:
        certificate, key = test_key()
        command += ["--sign-key", str(key), "--sign-cert", str(certificate)]
    subprocess.run(command, check=True)
    (out / "EFI/BOOT").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out / "EFI/caelestia/grubx64.efi", out / "EFI/BOOT/BOOTX64.EFI")
    config = f"""set default=1
set timeout={args.timeout}
set caelestia_gfxmode={args.resolution},auto
set caelestia_motion={0 if args.reduced_motion else 1}
serial --unit=0 --speed=115200
# Verify that the installer overrides a distribution's previous theme.
set theme=/nonexistent/old-theme.txt
# Simulate a distro that has already activated a low-resolution gfxterm.
set gfxmode=800x600
terminal_output gfxterm
function fixture_boot {{
 sleep 1
 terminal_output console serial
 echo "CAELESTIA_BOOT:$1"
 sleep --interruptible 2
 halt
}}
menuentry 'Windows' --class windows {{ fixture_boot windows; }}
menuentry 'Arch Linux' --class arch {{ fixture_boot arch; }}
menuentry 'Ubuntu' --class ubuntu {{ fixture_boot ubuntu; }}
"""
    if args.many:
        config += """menuentry 'CachyOS' --class cachyos { fixture_boot cachyos; }
menuentry 'Fedora' --class fedora { fixture_boot fedora; }
menuentry 'NixOS' --class nixos { fixture_boot nixos; }
menuentry 'Unknown operating system with a very long kernel title' --class missing { fixture_boot unknown; }
submenu 'Recovery' --class recovery {
 menuentry 'Linux LTS' --class arch { fixture_boot lts; }
 menuentry 'Memory diagnostic' --class recovery { fixture_boot diagnostic; }
}
menuentry 'UEFI settings' --class firmware { fixture_boot firmware; }
"""
    if args.secure_boot:
        config += secure_entries(out)
    (out / "boot/grub/grub.cfg").write_text(config)
    # Expose a real EDID preferred mode to OVMF, including nonstandard ratios.
    width, height = map(int, args.native_resolution.split("x"))
    (out / "display.json").write_text(json.dumps({"width": width, "height": height}))
    print(f"VM fixture: {out}")


def test_key(name="db"):
    """Disposable test keys. Never reads or enrols the host's signing keys."""
    directory = ROOT / "build/test-keys"
    directory.mkdir(exist_ok=True)
    certificate, key = directory / f"{name}.pem", directory / f"{name}.key"
    if not certificate.exists():
        subprocess.run(
            [
                "openssl",
                "req",
                "-new",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "30",
                "-subj",
                f"/CN=Caelestia VM {name}/",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        key.chmod(0o600)
    return certificate, key


def secure_entries(out):
    """Positive and negative executable-trust probes under actual OVMF Secure Boot."""
    build = ROOT / ".cache/grub-build"
    child = ROOT / "build/child.cfg"
    child.write_text(
        "serial --unit=0 --speed=115200\nterminal_output serial\necho CAELESTIA_TRUSTED_CHILD\nhalt\n"
    )
    unsigned = out / "unsigned-child.efi"
    subprocess.run(
        [
            str(build / "grub-mkstandalone"),
            "-O",
            "x86_64-efi",
            "-d",
            str(build / "grub-core"),
            "--locales=",
            "--fonts=",
            "--modules=normal serial terminal echo halt",
            "-o",
            str(unsigned),
            f"boot/grub/grub.cfg={child}",
        ],
        check=True,
    )
    for name, destination in (
        ("db", "signed-child.efi"),
        ("untrusted", "untrusted-child.efi"),
    ):
        cert, key = test_key(name)
        subprocess.run(
            [
                "sbsign",
                "--key",
                str(key),
                "--cert",
                str(cert),
                "--output",
                str(out / destination),
                str(unsigned),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    entries = """
terminal_output serial
if [ "$lockdown" = y ]; then echo CAELESTIA_SECUREBOOT:enforced; else echo CAELESTIA_SECUREBOOT:missing; fi
terminal_output console
menuentry 'Trusted EFI image' --class firmware {
 terminal_output console serial
 chainloader /signed-child.efi
 boot
}
menuentry 'Unsigned EFI image' --class firmware {
 terminal_output console serial
 if chainloader /unsigned-child.efi; then echo CAELESTIA_SECURITY_FAILURE; else echo CAELESTIA_UNSIGNED_REJECTED; fi
 halt
}
menuentry 'Untrusted signing key' --class firmware {
 terminal_output console serial
 if chainloader /untrusted-child.efi; then echo CAELESTIA_SECURITY_FAILURE; else echo CAELESTIA_UNTRUSTED_REJECTED; fi
 halt
}
menuentry 'External executable module' --class recovery {
 terminal_output console serial
 insmod hello
 hello
 echo CAELESTIA_MODULE_PROBE_DONE
 halt
}
"""
    kernel = os.environ.get("CAELESTIA_TEST_KERNEL")
    if kernel:
        shutil.copyfile(kernel, out / "unsigned-linux.efi")
        cert, key = test_key()
        subprocess.run(
            [
                "sbsign",
                "--key",
                str(key),
                "--cert",
                str(cert),
                "--output",
                str(out / "signed-linux.efi"),
                str(out / "unsigned-linux.efi"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        entries += """
menuentry 'Signed Linux kernel' --class linux {
 terminal_output console serial
 if linux /signed-linux.efi console=ttyS0 panic=1; then boot; else echo CAELESTIA_SECURITY_FAILURE; fi
}
menuentry 'Unsigned Linux kernel' --class linux {
 terminal_output console serial
 if linux /unsigned-linux.efi console=ttyS0; then echo CAELESTIA_SECURITY_FAILURE; else echo CAELESTIA_UNSIGNED_KERNEL_REJECTED; fi
 halt
}
"""
    return entries


def run(args):
    out = ROOT / "build/vm"
    if not (out / "EFI/BOOT/BOOTX64.EFI").exists():
        raise SystemExit("Run make run or tests/vm.py fixture first")
    local = ROOT / ".cache/tools"
    binary = shutil.which("qemu-system-x86_64") or str(
        local / "usr/bin/qemu-system-x86_64"
    )
    candidates = [
        (Path("/usr/share/edk2/x64"), "OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
        (Path("/usr/share/edk2-ovmf/x64"), "OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
        (Path("/usr/share/OVMF"), "OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
        (local / "usr/share/edk2/x64", "OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
    ]
    found = next(((p, c, v) for p, c, v in candidates if (p / c).exists()), None)
    if not found:
        raise SystemExit("Install OVMF / edk2-ovmf")
    directory, code, variables = found
    var_copy = ROOT / "build/OVMF_VARS.fd"
    if args.secure_boot:
        secure_code = (
            code.replace(".fd", ".secboot.fd")
            if "_4M" in code
            else "OVMF_CODE.secboot.4m.fd"
        )
        if not (directory / secure_code).exists():
            raise SystemExit("Install Secure Boot capable OVMF firmware")
        code = secure_code
        cert, _ = test_key()
        subprocess.run(
            [
                str(Path(sys.executable).parent / "virt-fw-vars"),
                "-i",
                str(directory / variables),
                "--enroll-cert",
                str(cert),
                "--add-db",
                "1a4cd9d0-aebd-4ca9-bb18-9104b91e0140",
                str(cert),
                "--no-microsoft",
                "--microsoft-kek",
                "none",
                "--sb",
                "-o",
                str(var_copy),
            ],
            check=True,
        )
    else:
        shutil.copyfile(directory / variables, var_copy)
    Path(args.qmp).unlink(missing_ok=True)
    env = os.environ.copy()
    if str(local) in binary:
        env["LD_LIBRARY_PATH"] = str(local / "usr/lib")
    display_file = out / "display.json"
    display = (
        json.loads(display_file.read_text())
        if display_file.exists()
        else {"width": 1920, "height": 1080}
    )
    command = [
        binary,
        "-machine",
        "q35,smm=on" if args.secure_boot else "q35",
        "-accel",
        "tcg",
        "-m",
        "384",
        "-nic",
        "none",
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={directory / code}",
        "-drive",
        f"if=pflash,format=raw,file={var_copy}",
        "-drive",
        f"format=raw,file=fat:rw:{out}",
        "-vga",
        "none",
        "-device",
        f"VGA,vgamem_mb=64,xres={display['width']},yres={display['height']}",
        "-display",
        "none",
        "-qmp",
        f"unix:{args.qmp},server=on,wait=off",
        "-serial",
        f"file:{ROOT}/build/serial.log",
        "-no-reboot",
    ]
    if args.secure_boot:
        command += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
    if (local / "usr/share/qemu").exists():
        command += ["-L", str(local / "usr/share/qemu")]
    if not args.headless:
        command += ["-vnc", "127.0.0.1:1"]
        print("QEMU: connect VNC to 127.0.0.1:5901", flush=True)
    raise SystemExit(subprocess.call(command, env=env))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Real GRUB integration-test harness")
    subs = p.add_subparsers(dest="command", required=True)
    f = subs.add_parser("fixture")
    f.add_argument("--resolution", default="1920x1080")
    f.add_argument("--native-resolution", default="1920x1080")
    f.add_argument("--timeout", type=int, default=-1)
    f.add_argument("--many", action="store_true")
    f.add_argument("--reduced-motion", action="store_true")
    f.add_argument("--secure-boot", action="store_true")
    r = subs.add_parser("run")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--qmp", default=str(ROOT / "build/qmp.sock"))
    r.add_argument("--secure-boot", action="store_true")
    args = p.parse_args()
    if args.command == "fixture":
        if args.resolution != "auto" and not re.fullmatch(
            r"\d{3,4}x\d{3,4}", args.resolution
        ):
            p.error("Use WIDTHxHEIGHT or auto")
        if not re.fullmatch(r"\d{3,4}x\d{3,4}", args.native_resolution):
            p.error("Use WIDTHxHEIGHT for the native display resolution")
        fixture(args)
    else:
        run(args)
