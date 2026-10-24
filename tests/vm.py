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
import uuid
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
    luks_uuid = (
        str(uuid.uuid4()) if args.encrypted_boot or args.encrypted_root else None
    )
    inner_uuid = (
        str(uuid.uuid4())
        if luks_uuid and args.storage_layout == "nested-luks"
        else None
    )
    if args.encrypted_boot:
        command += ["--unlock", luks_uuid]
        if inner_uuid:
            command += ["--unlock", inner_uuid]
    if args.secure_boot:
        certificate, key = test_key()
        command += ["--sign-key", str(key), "--sign-cert", str(certificate)]
    subprocess.run(command, check=True)
    (out / "luks.json").unlink(missing_ok=True)
    (out / "efi-path-probe").unlink(missing_ok=True)
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
    if args.efi_path_probe:
        efi_path_probe(args.secure_boot)
        (out / "efi-path-probe").touch()
        config += """menuentry 'Windows Boot Manager path probe' --class windows {
 search --file --set=windows_esp /EFI/Microsoft/Boot/bootmgfw.efi
"""
        if args.explicit_efi_device:
            config += " chainloader ($windows_esp)/EFI/Microsoft/Boot/bootmgfw.efi\n}\n"
        else:
            config += " set root=$windows_esp\n chainloader /EFI/Microsoft/Boot/bootmgfw.efi\n}\n"
    (out / "boot/grub/grub.cfg").write_text(config)
    if luks_uuid:
        encrypted_boot(
            out,
            args.encrypted_boot or args.encrypted_root,
            luks_uuid,
            layout=args.storage_layout,
            sector_size=args.sector_size,
            boot_encrypted=bool(args.encrypted_boot),
            inner_uuid=inner_uuid,
        )
    # Expose a real EDID preferred mode to OVMF, including nonstandard ratios.
    width, height = map(int, args.native_resolution.split("x"))
    (out / "display.json").write_text(json.dumps({"width": width, "height": height}))
    print(f"VM fixture: {out}")


LUKS_PASSWORD = "caelestia-test-only"


def efi_path_probe(secure_boot):
    """A separate ESP and a native UEFI app that opens a sibling file."""
    directory = ROOT / "build/windows-esp/EFI/Microsoft/Boot"
    directory.mkdir(parents=True, exist_ok=True)
    obj = ROOT / "build/efi-path-probe.obj"
    image = directory / "bootmgfw.efi"
    subprocess.run(
        [
            "clang",
            "--target=x86_64-pc-windows-msvc",
            "-ffreestanding",
            "-fno-stack-protector",
            "-mno-red-zone",
            "-fshort-wchar",
            "-O1",
            "-c",
            str(ROOT / "tests/efi_path_probe.c"),
            "-o",
            str(obj),
        ],
        check=True,
    )
    subprocess.run(
        [
            "lld-link",
            "/subsystem:efi_application",
            "/entry:efi_main",
            "/nodefaultlib",
            "/dynamicbase",
            "/fixed:no",
            f"/out:{image}",
            str(obj),
        ],
        check=True,
    )
    if secure_boot:
        certificate, key = test_key()
        signed = directory / "signed.efi"
        subprocess.run(
            [
                "sbsign",
                "--key",
                str(key),
                "--cert",
                str(certificate),
                "--output",
                str(signed),
                str(image),
            ],
            check=True,
        )
        signed.replace(image)
    (directory / "BCD").write_text("CAELESTIA_EFI_PATH_OK")


def lvm_volume(payload):
    """Wrap a disposable image in a single-PV, linear-LV LVM2 container."""
    import struct

    def lvm_id(value):
        return "-".join(
            value[a:b]
            for a, b in (
                (0, 6),
                (6, 10),
                (10, 14),
                (14, 18),
                (18, 22),
                (22, 26),
                (26, 32),
            )
        )

    def checksum(data):
        crc = 0xF597A6CF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
        return crc

    start, extent = 1024 * 1024, 4 * 1024 * 1024
    count = (payload.stat().st_size + extent - 1) // extent
    size = start + count * extent
    pv, vg, lv = (uuid.uuid4().hex for _ in range(3))
    metadata = (
        f'''caelestia {{
 id = "{lvm_id(vg)}"
 seqno = 1
 format = "lvm2"
 status = ["RESIZEABLE", "READ", "WRITE"]
 extent_size = 8192
 max_lv = 0
 max_pv = 0
 physical_volumes {{
  pv0 {{
   id = "{lvm_id(pv)}"
   device = "/dev/test-only"
   status = ["ALLOCATABLE"]
   dev_size = {size // 512}
   pe_start = 2048
   pe_count = {count}
  }}
 }}
 logical_volumes {{
  boot {{
   id = "{lvm_id(lv)}"
   status = ["READ", "WRITE", "VISIBLE"]
   segment_count = 1
   segment1 {{
    start_extent = 0
    extent_count = {count}
    type = "striped"
    stripe_count = 1
    stripes = ["pv0", 0]
   }}
  }}
 }}
}}
'''.encode()
        + b"\0"
    )
    header = bytearray(start)
    struct.pack_into("<8sQII8s", header, 512, b"LABELONE", 1, 0, 32, b"LVM2 001")
    struct.pack_into("<32sQ", header, 544, pv.encode(), size)
    struct.pack_into("<QQQQQQQQ", header, 584, start, 0, 0, 0, 4096, start - 4096, 0, 0)
    struct.pack_into("<I", header, 528, checksum(header[532:1024]))
    struct.pack_into(
        "<I16sIQQ", header, 4096, 0, b" LVM2 x[5A%r0N*>", 1, 4096, start - 4096
    )
    struct.pack_into("<QQII", header, 4136, 512, len(metadata), checksum(metadata), 0)
    struct.pack_into("<I", header, 4096, checksum(header[4100:4608]))
    header[4608 : 4608 + len(metadata)] = metadata
    wrapped = payload.with_suffix(".lvm")
    with wrapped.open("wb") as target, payload.open("rb") as source:
        target.write(header)
        shutil.copyfileobj(source, target)
        target.truncate(size)
    wrapped.replace(payload)


def encrypt_luks(plaintext, image, profile, luks_uuid, sector_size, directory):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    size = plaintext.stat().st_size
    if profile == "luks1":
        sector_size = 512
    volume_key = os.urandom(64)
    key_file, password_file = directory / "volume.key", directory / "password"
    key_file.write_bytes(volume_key)
    password_file.write_text(LUKS_PASSWORD)
    key_file.chmod(0o600)
    password_file.chmod(0o600)
    with image.open("wb") as f:
        f.truncate(size + 32 * 1024 * 1024)
    version, _, pbkdf = profile.partition("-")
    command = [
        "cryptsetup",
        "luksFormat",
        "--batch-mode",
        "--type",
        version,
        "--uuid",
        luks_uuid,
        "--cipher",
        "aes-xts-plain64",
        "--key-size",
        "512",
        "--volume-key-file",
        str(key_file),
        "--key-file",
        str(password_file),
    ]
    if version == "luks2":
        command += ["--sector-size", str(sector_size), "--pbkdf", pbkdf]
    command += ["--pbkdf-force-iterations", "1000" if pbkdf in ("", "pbkdf2") else "4"]
    if pbkdf.startswith("argon2"):
        command += ["--pbkdf-memory", "32768", "--pbkdf-parallel", "1"]
    subprocess.run([*command, str(image)], check=True)
    if version == "luks2":
        metadata = json.loads(
            subprocess.check_output(
                ["cryptsetup", "luksDump", "--dump-json-metadata", str(image)],
                text=True,
            )
        )
        offset = int(metadata["segments"]["0"]["offset"])
    else:
        import struct

        with image.open("rb") as f:
            f.seek(104)
            offset = struct.unpack(">I", f.read(4))[0] * 512
    with plaintext.open("rb") as source, image.open("r+b") as target:
        target.seek(offset)
        for sector in range(size // sector_size):
            encryptor = Cipher(
                algorithms.AES(volume_key),
                modes.XTS((sector * (sector_size // 512)).to_bytes(16, "little")),
            ).encryptor()
            target.write(
                encryptor.update(source.read(sector_size)) + encryptor.finalize()
            )
    key_file.unlink()
    password_file.unlink()


def encrypted_boot(
    out,
    profile,
    luks_uuid,
    layout="direct",
    sector_size=512,
    boot_encrypted=True,
    inner_uuid=None,
):
    """Real LUKS headers and encrypted ext4, without root or device-mapper.

    cryptsetup creates the keyslot. AES-XTS encrypts the disposable filesystem
    sector by sector with its random test volume key. GRUB must unlock it using
    the passphrase. No host devices, credentials or LUKS headers are touched.
    """
    directory = ROOT / "build/luks-fixture"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(mode=0o700)
    tree = directory / "root"
    tree.mkdir()
    shutil.copytree(out / "boot", tree / "boot")
    shutil.copyfile(out / "caelestia-root", tree / "caelestia-root")
    # Secure Boot child/kernel probes are referenced relative to $root.
    for child in out.glob("*.efi"):
        shutil.copyfile(child, tree / child.name)
    plaintext = directory / "filesystem.img"
    image = ROOT / "build/luks-boot.img"
    size = 64 * 1024 * 1024
    with plaintext.open("wb") as f:
        f.truncate(size)
    subprocess.run(
        ["mkfs.ext4", "-q", "-F", "-d", str(tree), str(plaintext)], check=True
    )
    if layout == "lvm-on-luks":
        lvm_volume(plaintext)
        size = plaintext.stat().st_size
    encrypt_luks(
        plaintext, image, profile, inner_uuid or luks_uuid, sector_size, directory
    )
    if inner_uuid:
        inner = directory / "inner.img"
        image.replace(inner)
        encrypt_luks(inner, image, profile, luks_uuid, sector_size, directory)
    # No readable copy may satisfy the loader's search on the EFI test disk.
    if layout == "luks-on-lvm":
        lvm_volume(image)
    if boot_encrypted:
        shutil.rmtree(out / "boot")
        (out / "caelestia-root").unlink()
    (out / "luks.json").write_text(
        json.dumps(
            {
                "uuids": [luks_uuid, *([inner_uuid] if inner_uuid else [])],
                "profile": profile,
            }
        )
    )


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
    if (out / "luks.json").exists():
        command += [
            "-drive",
            f"format=raw,snapshot=on,file={ROOT / 'build/luks-boot.img'}",
        ]
    if (out / "efi-path-probe").exists():
        command += ["-drive", f"format=raw,file=fat:rw:{ROOT / 'build/windows-esp'}"]
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
    f.add_argument("--efi-path-probe", action="store_true")
    f.add_argument("--explicit-efi-device", action="store_true")
    f.add_argument(
        "--encrypted-boot",
        choices=("luks1", "luks2-pbkdf2", "luks2-argon2i", "luks2-argon2id"),
    )
    f.add_argument(
        "--encrypted-root",
        choices=("luks1", "luks2-pbkdf2", "luks2-argon2i", "luks2-argon2id"),
    )
    f.add_argument(
        "--storage-layout",
        choices=("direct", "lvm-on-luks", "luks-on-lvm", "nested-luks"),
        default="direct",
    )
    f.add_argument("--sector-size", type=int, choices=(512, 4096), default=512)
    r = subs.add_parser("run")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--qmp", default=str(ROOT / "build/qmp.sock"))
    r.add_argument("--secure-boot", action="store_true")
    args = p.parse_args()
    if args.command == "fixture":
        if args.encrypted_boot and args.encrypted_root:
            p.error("Choose encrypted boot or encrypted root")
        if args.resolution != "auto" and not re.fullmatch(
            r"\d{3,4}x\d{3,4}", args.resolution
        ):
            p.error("Use WIDTHxHEIGHT or auto")
        if not re.fullmatch(r"\d{3,4}x\d{3,4}", args.native_resolution):
            p.error("Use WIDTHxHEIGHT for the native display resolution")
        fixture(args)
    else:
        run(args)
