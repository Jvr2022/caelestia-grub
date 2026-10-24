# Caelestia GRUB

A GRUB carousel inspired by [Caelestia](https://github.com/caelestia-dots/shell). Use the arrow keys to choose an OS and Enter to boot.

![Caelestia GRUB](assets/screenshot.png)

## Install

You'll need x86_64 UEFI, a working GRUB installation and a mounted FAT EFI partition. The installer doesn't support encrypted boot storage yet.

**1. Install the dependencies.**

Arch / CachyOS:

```sh
sudo pacman -S --needed base-devel bison flex freetype2 curl efibootmgr grub
```

Debian / Ubuntu:

```sh
sudo apt update
sudo apt install build-essential bison flex pkg-config libfreetype6-dev gettext curl efibootmgr grub-common patch xz-utils
```

With Secure Boot enabled, also install:

```sh
# Arch / CachyOS
sudo pacman -S --needed sbctl sbsigntools openssl jq

# Debian / Ubuntu
sudo apt install sbsigntool openssl jq
```

On Debian / Ubuntu, also install `sbctl` using its [upstream instructions](https://github.com/Foxboron/sbctl#installation).

Secure Boot needs an existing sbctl key enrolled in firmware **db**. The installer checks and uses that key; it doesn't create or enrol keys. Your kernels or UKIs also need trusted signatures. A shim MOK alone isn't enough.

**2. Download and extract this project**, then open a terminal in its folder:

```sh
chmod +x install.sh
./install.sh
```

The script builds GRUB and asks for sudo when installing. It detects your boot paths automatically. If detection fails, specify them yourself, for example:

```sh
./install.sh --esp /boot/efi --config /boot/grub/grub.cfg
```

Use your actual mount point and configuration path. You don't need to copy theme files or run `grub-install` separately.

**3. After installation succeeds, reboot** and select **Caelestia** in your firmware boot menu. The installer keeps your original bootloader available, so you can select it to go back.

If the screen looks stretched, rerun the installer with `--resolution 1920x1080`, using a resolution supported by your firmware. Rerun it after project updates too.

Press **E** to edit, **C** for the console and **Esc** to return.

[Credits](NOTICE.md) · [GPL-3.0-or-later](LICENSE)
