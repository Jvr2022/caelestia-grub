#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build and install the complete carousel as an independent x86_64 UEFI loader.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
project=$PWD
version=2.14
archive="$project/.cache/grub-$version.tar.xz"
source_dir="$project/.cache/grub-$version"
build_dir="$project/.cache/grub-build"
checksum=bc8d3c73535b8838d8c8e2654d73edc4e6ae8c8acdb45d5df5dc9a1547446d43
mode=install
stage_dir=
esp=
config=
nvram=1
secure_boot=0
sign_key=
sign_cert=
resolution=
resolution_override=0
unlock_uuids=()
boot_fs=
abstractions=

add_unlock() {
  local uuid=${1//-/} existing
  [[ $uuid =~ ^[[:xdigit:]]{32}$ ]] || die 'Invalid LUKS UUID'
  uuid=${uuid,,}
  for existing in "${unlock_uuids[@]}"; do [[ $existing != "$uuid" ]] || return 0; done
  unlock_uuids+=("$uuid")
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
root() { if (( EUID == 0 )); then "$@"; else sudo -- "$@"; fi; }

validate_luks_header() {
  local device=$1 luks_version metadata
  root cryptsetup isLuks "$device" || die "Cannot read a LUKS header on $device; detached headers and plain dm-crypt boot storage are not supported"
  luks_version=$(root od --endian=big -An -tu2 -j6 -N2 "$device")
  case ${luks_version//[[:space:]]/} in
    1) ;;
    2)
      metadata=$(root cryptsetup luksDump --dump-json-metadata "$device")
      jq -e '(.segments | length) == 1 and
        all(.segments[]; .type == "crypt" and .integrity == null and ((.flags // []) | length) == 0) and
        ((.config.requirements.mandatory // []) | length) == 0 and
        any(.keyslots[]; .type == "luks2" and (.kdf.type == "pbkdf2" or .kdf.type == "argon2i" or .kdf.type == "argon2id"))' \
        <<< "$metadata" >/dev/null || die 'Unsupported encrypted boot layout: integrity, hardware encryption, active reencryption or unsupported keyslots'
      ;;
    *) die 'Unsupported LUKS header version' ;;
  esac
}

validate_boot_mappings() {
  local boot_devices boot_device mappings device resolved slave checked=0
  local -a slaves
  for tool in cryptsetup jq od; do command -v "$tool" >/dev/null || die "Encrypted boot storage requires $tool"; done
  boot_devices=$(root grub-probe --target=device "$config")
  while IFS= read -r boot_device; do
    mappings=$(root lsblk --inverse --json --paths --output PATH,TYPE "$boot_device")
    while IFS= read -r device; do
      resolved=$(readlink -f -- "$device")
      slaves=(/sys/class/block/"${resolved##*/}"/slaves/*)
      [[ ${#slaves[@]} == 1 && -e ${slaves[0]} ]] || die "Cannot determine the backing device of $device"
      slave=${slaves[0]##*/}
      validate_luks_header "/dev/$slave"
      checked=$((checked + 1))
    done < <(jq -r '.. | objects | select(.type? == "crypt") | .path' <<< "$mappings")
  done <<< "$boot_devices"
  (( checked > 0 )) || die 'Could not validate the encrypted boot mappings; nothing was installed'
}
usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Default: build and install the carousel, then create a Caelestia UEFI boot entry.
  --build          Build patched GRUB only; no system changes
  --stage DIR      Assemble a portable QEMU test directory; no system changes
  --esp DIR        EFI System Partition (otherwise detected)
  --config FILE    Existing grub.cfg (otherwise detected)
  --resolution WxH Preferred firmware resolution (otherwise detected; auto allowed)
  --no-nvram       Install files without creating a firmware boot entry
  --unlock UUID    Unlock a LUKS volume before reading grub.cfg (repeatable;
                   boot volumes are detected automatically when installing)
  --sign-key FILE  Test signing key (only with --stage)
  --sign-cert FILE Test signing certificate (only with --stage)
  --help           Show this help

Requires x86_64 UEFI. The existing distro bootloader and grub.cfg are preserved.
Secure Boot: uses an existing sbctl db key already enrolled in firmware.
EOF
}
while (( $# )); do
  case "$1" in
    --build) [[ $mode == install ]] || die 'Choose one build/stage mode'; mode=build; shift ;;
    --stage) [[ $# -ge 2 && $mode == install ]] || die '--stage requires a directory'; mode=stage; stage_dir=$2; shift 2 ;;
    --esp) [[ $# -ge 2 ]] || die '--esp requires a directory'; esp=$2; shift 2 ;;
    --config) [[ $# -ge 2 ]] || die '--config requires a file'; config=$2; shift 2 ;;
    --resolution) [[ $# -ge 2 ]] || die '--resolution requires WIDTHxHEIGHT or auto'; resolution=$2; resolution_override=1; shift 2 ;;
    --no-nvram) nvram=0; shift ;;
    --unlock) [[ $# -ge 2 ]] || die '--unlock requires a LUKS UUID'; add_unlock "$2"; shift 2 ;;
    --sign-key) [[ $# -ge 2 ]] || die '--sign-key requires a file'; sign_key=$2; shift 2 ;;
    --sign-cert) [[ $# -ge 2 ]] || die '--sign-cert requires a file'; sign_cert=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done
[[ -z $resolution || $resolution == auto || $resolution =~ ^[0-9]{3,5}x[0-9]{3,5}$ ]] || die 'Use --resolution WIDTHxHEIGHT or auto'
[[ $(uname -m) == x86_64 ]] || die 'Only x86_64 is supported'
if [[ -n $sign_key || -n $sign_cert ]]; then
  [[ $mode == stage && -f $sign_key && -f $sign_cert ]] || die 'Custom signing keys require --stage and both --sign-key/--sign-cert'
  for tool in sbsign sbverify; do command -v "$tool" >/dev/null || die "Missing signing dependency: $tool"; done
fi

if [[ $mode == install ]]; then
  [[ -d /sys/firmware/efi ]] || die 'Boot the system in UEFI mode before installing'
  for tool in grub-probe grub-mkrelpath findmnt lsblk install; do
    command -v "$tool" >/dev/null || die "Missing dependency: $tool"
  done
  if (( EUID != 0 )); then command -v sudo >/dev/null || die 'Install sudo or run as root'; fi
  if (( nvram )); then command -v efibootmgr >/dev/null || die 'Install efibootmgr or use --no-nvram'; fi
  if [[ -z $config ]]; then
    for candidate in /boot/grub/grub.cfg /boot/grub2/grub.cfg; do
      if root test -f "$candidate"; then config=$candidate; break; fi
    done
  fi
  [[ -n $config && $config == /* ]] || die 'Use --config /absolute/path/to/grub.cfg'
  root test -f "$config" || die "Configuration does not exist: $config"
  if [[ -z $esp ]]; then
    for candidate in /efi /boot/efi /boot; do
      if [[ $(findmnt -rn --mountpoint "$candidate" -o FSTYPE 2>/dev/null || true) == vfat ]]; then esp=$candidate; break; fi
    done
  fi
  [[ -n $esp && $esp == /* ]] || die 'Mount the EFI System Partition or specify --esp'
  [[ $(findmnt -rn --mountpoint "$esp" -o FSTYPE 2>/dev/null || true) == vfat ]] || die "$esp is not a mounted FAT EFI System Partition"
  for variable in /sys/firmware/efi/efivars/SecureBoot-*; do
    [[ -e $variable ]] || continue
    state=$(root od -An -tu1 -j4 -N1 "$variable")
    if [[ ${state//[[:space:]]/} == 1 ]]; then secure_boot=1; fi
  done
  if (( secure_boot )); then
    for tool in sbctl sbverify openssl jq; do command -v "$tool" >/dev/null || die "Secure Boot requires $tool"; done
    # Check the public certificate against db, not merely against PK/KEK or
    # sbctl's file database. Do not create keys or modify firmware enrolment.
    sign_cert=/var/lib/sbctl/keys/db/db.pem
    root test -f "$sign_cert" || die 'No existing sbctl db certificate was found'
    certificate=$(root openssl x509 -in "$sign_cert" -outform DER | base64 -w0)
    enrolled=$(root sbctl --json list-enrolled-keys | jq -r '.DB[]?.Raw')
    trusted=0
    while IFS= read -r item; do
      if [[ $item == "$certificate" ]]; then trusted=1; break; fi
    done <<< "$enrolled"
    (( trusted )) || die 'The sbctl signing certificate is not enrolled in firmware db; no keys were changed'
    printf 'Secure Boot enabled: using the existing enrolled sbctl key.\n'
  fi
  boot_uuid=$(root grub-probe --target=fs_uuid "$config")
  boot_fs=$(root grub-probe --target=fs "$config")
  [[ $boot_fs =~ ^[a-zA-Z0-9_]+$ ]] || die 'Unsupported GRUB filesystem module name'
  # Probe only the filesystem holding grub.cfg, not every encrypted disk.
  # GRUB traverses backing LVM/RAID members in dependency order.
  abstractions=$(root grub-probe --target=abstraction "$config")
  crypto_probe=$(root grub-probe --target=cryptodisk_uuid "$config")
  read -r -a detected_uuids <<< "${crypto_probe//$'\n'/ }"
  manual_uuids=("${unlock_uuids[@]}")
  unlock_uuids=()
  for uuid in "${detected_uuids[@]}"; do add_unlock "$uuid"; done
  for uuid in "${manual_uuids[@]}"; do add_unlock "$uuid"; done
  if [[ " $abstractions " == *' cryptodisk '* && ${#unlock_uuids[@]} == 0 ]]; then
    die 'Encrypted boot filesystem detected, but GRUB could not determine its LUKS UUID'
  fi
  if [[ " $abstractions " == *' cryptodisk '* ]]; then validate_boot_mappings; fi
  config_path=$(root grub-mkrelpath "$config")
  prefix_path="${config_path%/*}/caelestia"
  [[ $boot_uuid =~ ^[a-zA-Z0-9-]+$ ]] || die 'Unsupported boot filesystem UUID'
  [[ $config_path =~ ^/[a-zA-Z0-9_./@:+-]+$ ]] || die 'Unsupported characters in the GRUB configuration path'
  target="$(dirname -- "$config")/caelestia"
  if root test -e "$target" && ! root test -f "$target/.caelestia-managed"; then
    die "Refusing to replace an unmanaged directory: $target"
  fi
  if (( nvram )); then
    esp_device=$(readlink -f -- "$(findmnt -rn --mountpoint "$esp" -o SOURCE)")
    parent=$(lsblk -ndo PKNAME "$esp_device")
    partition=$(cat -- "/sys/class/block/${esp_device##*/}/partition" 2>/dev/null || true)
    [[ -n $parent && $partition =~ ^[0-9]+$ ]] || die 'Cannot determine the ESP disk and partition; use --no-nvram'
  fi
else
  config_path=/boot/grub/grub.cfg
  prefix_path=/boot/grub/caelestia
fi

# Some firmware (including OVMF) exposes modes but no EDID protocol to GRUB.
# Use Linux's preferred mode when connected monitors agree; GRUB validates it
# against GOP and falls back if unavailable. --resolution overrides this hint.
if [[ $mode == install && -z $resolution ]]; then
  connected=()
  for connector in /sys/class/drm/card*-*; do
    [[ -r $connector/status && -r $connector/modes ]] || continue
    if [[ $(<"$connector/status") == connected ]]; then connected+=("$connector"); fi
  done
  preferred=
  for connector in "${connected[@]}"; do
    candidate=
    IFS= read -r candidate < "$connector/modes" || true
    if [[ ! $candidate =~ ^[0-9]{3,5}x[0-9]{3,5}$ || ( -n $preferred && $preferred != "$candidate" ) ]]; then preferred=; break; fi
    preferred=$candidate
  done
  resolution=$preferred
fi
video_mode=${resolution:-auto}
if [[ $video_mode != auto ]]; then video_mode+=,auto; fi

for tool in curl tar sha256sum patch make gcc bison flex pkg-config; do
  command -v "$tool" >/dev/null || die "Missing build dependency: $tool (see README.md)"
done
mkdir -p "$project/.cache" "$build_dir"
if [[ ! -f $archive ]]; then
  curl --fail --location --retry 3 "https://ftp.gnu.org/gnu/grub/grub-$version.tar.xz" -o "$archive.part"
  mv -- "$archive.part" "$archive"
fi
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status || die 'GRUB archive checksum mismatch'
if [[ ! -f $source_dir/configure ]]; then tar -xJf "$archive" -C "$project/.cache"; fi
if patch --dry-run --reverse --silent -p1 -d "$source_dir" < "$project/src/grub.patch" >/dev/null 2>&1; then
  : # Exact patch already applied.
else
  # Rebuild the cached source when a newer project patch replaces an older one.
  if ! patch --dry-run --forward --silent -p1 -d "$source_dir" < "$project/src/grub.patch" >/dev/null 2>&1; then
    tar -xJf "$archive" -C "$project/.cache"
    # This project-created file is absent from the pristine release archive.
    if [[ -f $source_dir/grub-core/gfxmenu/carousel.inc ]]; then
      unlink -- "$source_dir/grub-core/gfxmenu/carousel.inc"
    fi
  fi
  patch --batch --forward -p1 -d "$source_dir" < "$project/src/grub.patch"
fi
if [[ ! -f $build_dir/Makefile ]]; then
  (cd "$build_dir" && "$source_dir/configure" --target=x86_64 --with-platform=efi \
    --disable-werror --disable-nls --disable-grub-mount --disable-device-mapper \
    --disable-liblzma --disable-efiemu > configure.log 2>&1) || {
      tail -40 "$build_dir/configure.log"; die 'GRUB configuration failed';
    }
fi
printf 'Building Caelestia GRUB…\n'
make -C "$build_dir" -j"${JOBS:-8}" > "$build_dir/build.log" 2>&1 || {
  tail -50 "$build_dir/build.log"; die 'GRUB build failed';
}
if [[ $mode == build ]]; then printf 'Built: %s\n' "$build_dir"; exit 0; fi

mkdir -p "$project/build"
work=$(mktemp -d "$project/build/install.XXXXXX")
trap 'if [[ -n ${work:-} && -d $work ]]; then find "$work" -depth -delete; fi' EXIT
payload="$work/payload"
mkdir -p "$payload/x86_64-efi"
cp -a -- "$project/theme" "$payload/theme"
cp -- "$build_dir/grub-core/"*.mod "$build_dir/grub-core/"*.lst "$payload/x86_64-efi/"
printf 'Caelestia GRUB %s\n' "$version" > "$payload/.caelestia-managed"

# Unlock boot storage before searching for the filesystem or reading its menu.
# All unlock code and algorithms are preloaded into the signed EFI image.
# UUIDs are public identifiers. No passwords, key files or volume keys are embedded.
printf '' > "$work/loader.cfg"
if [[ $mode == stage && ${#unlock_uuids[@]} -gt 0 ]]; then
  printf 'serial --unit=0 --speed=115200\nterminal_output console serial\n' >> "$work/loader.cfg"
fi
for uuid in "${unlock_uuids[@]}"; do
  cat >> "$work/loader.cfg" <<EOF
if cryptomount -u $uuid; then
  echo 'Boot volume unlocked.'
else
  echo 'Cannot unlock boot storage. Returning to firmware.'
  sleep --interruptible 5
  exit
fi
EOF
done

# Source the distro-generated menu, then apply the carousel before GRUB shows it.
# A private prefix keeps all dynamically loaded modules from this matching build.
if [[ $mode == stage ]]; then
  printf 'if search --file --set=caelestia_boot /caelestia-root; then\n' >> "$work/loader.cfg"
else
  printf 'if search --fs-uuid --set=caelestia_boot %s; then\n' "$boot_uuid" >> "$work/loader.cfg"
fi
cat >> "$work/loader.cfg" <<EOF
set root=\$caelestia_boot
else
  echo 'Boot filesystem not found. Returning to firmware.'
  sleep --interruptible 5
  exit
fi
set prefix=(\$caelestia_boot)$prefix_path
export prefix
export caelestia_boot
set caelestia_carousel=1
export caelestia_carousel
source (\$caelestia_boot)$config_path
set prefix=(\$caelestia_boot)$prefix_path
# Deactivate an already running distro gfxterm before selecting a new mode.
terminal_output console
if [ -z "\$caelestia_gfxmode" ]; then set caelestia_gfxmode=$video_mode; fi
if [ $resolution_override = 1 ]; then set caelestia_gfxmode=$video_mode; fi
set gfxmode=\$caelestia_gfxmode
set caelestia_carousel=1
export caelestia_carousel
insmod all_video
insmod gfxterm
insmod gfxmenu
insmod png
for size in 12 14 16 20 24 32 40 48 64 80 96 128; do
  loadfont (memdisk)/boot/grub/fonts/caelestia-\$size.pf2
done
for size in 12 14 16 20 24 32 40 48 64 80 96; do
  loadfont (memdisk)/boot/grub/fonts/caelestia-mono-\$size.pf2
done
set gfxterm_font="Caelestia Mono Regular 12"
set color_normal=light-gray/black
set color_highlight=black/light-magenta
terminal_output gfxterm
set theme=\$prefix/theme/carousel.txt
export theme
set caelestia_carousel=1
export caelestia_carousel
set timeout_style=menu
if [ -z "\$timeout" ]; then set timeout=8; fi
if [ "\$timeout" = 0 ]; then set timeout=8; fi
if [ -z "\$caelestia_motion" ]; then set caelestia_motion=1; fi
export caelestia_motion
EOF
"$build_dir/grub-script-check" "$work/loader.cfg"
# Required executable modules are part of the signed PE image. Secure Boot
# refuses arbitrary external .mod files; embedding files in a memdisk alone
# does not make them preloaded, authenticated modules.
modules='part_gpt part_msdos fat btrfs ext2 xfs ntfs iso9660 udf search search_fs_uuid search_fs_file search_label configfile normal linux chain boot loadenv test regexp echo sleep serial terminal gettext gzio xzio lzopio zstd reboot halt keystatus probe all_video gfxterm gfxmenu png jpeg font bitmap bitmap_scale bli'
modules+=' minicmd cryptodisk luks luks2 pbkdf2 argon2 lvm diskfilter mdraid09 mdraid1x'
if [[ -n $boot_fs ]]; then
  [[ -f $build_dir/grub-core/$boot_fs.mod ]] || die "This GRUB build cannot read the boot filesystem: $boot_fs"
  modules+=" $boot_fs"
fi
for abstraction in $abstractions; do
  [[ $abstraction =~ ^[a-zA-Z0-9_]+$ && -f $build_dir/grub-core/$abstraction.mod ]] || die "Unsupported boot storage abstraction: $abstraction"
  modules+=" $abstraction"
done
# Include every cipher/hash shipped by this pinned GRUB. This covers existing
# LUKS algorithms without loading unauthenticated modules from encrypted storage.
for algorithm in "$build_dir/grub-core/"gcry_*.mod; do
  algorithm=${algorithm##*/}
  modules+=" ${algorithm%.mod}"
done
embedded_fonts=()
for font in "$project/theme/"*.pf2; do
  embedded_fonts+=("boot/grub/fonts/${font##*/}=$font")
done
"$build_dir/grub-mkstandalone" -O x86_64-efi -d "$build_dir/grub-core" \
  --locales= --fonts= --modules="$modules" \
  -o "$work/grubx64.efi" "boot/grub/grub.cfg=$work/loader.cfg" "${embedded_fonts[@]}"
if (( secure_boot )); then
  root sbctl sign --output "$work/grubx64.signed.efi" "$work/grubx64.efi"
  root sbverify --cert "$sign_cert" "$work/grubx64.signed.efi"
  root mv -- "$work/grubx64.signed.efi" "$work/grubx64.efi"
elif [[ -n $sign_key ]]; then
  sbsign --key "$sign_key" --cert "$sign_cert" --output "$work/grubx64.signed.efi" "$work/grubx64.efi"
  sbverify --cert "$sign_cert" "$work/grubx64.signed.efi"
  mv -- "$work/grubx64.signed.efi" "$work/grubx64.efi"
fi

if [[ $mode == stage ]]; then
  mkdir -p -- "$stage_dir/EFI/caelestia" "$stage_dir/boot/grub"
  # Stage owns only its dedicated loader and private prefix, never the fixture grub.cfg.
  if [[ -e $stage_dir/boot/grub/caelestia ]]; then
    [[ -f $stage_dir/boot/grub/caelestia/.caelestia-managed ]] || die 'Stage contains an unmanaged caelestia directory'
    find "$stage_dir/boot/grub/caelestia" -depth -delete
  fi
  cp -a -- "$payload" "$stage_dir/boot/grub/caelestia"
  cp -- "$work/grubx64.efi" "$stage_dir/EFI/caelestia/grubx64.efi"
  printf 'Caelestia test filesystem\n' > "$stage_dir/caelestia-root"
  printf 'Staged: %s\n' "$stage_dir"
  exit 0
fi

stamp=$(date +%Y%m%d-%H%M%S)
if root test -f "$target/grubenv"; then root cp -- "$target/grubenv" "$payload/grubenv"
elif root test -f "$(dirname -- "$config")/grubenv"; then root cp -- "$(dirname -- "$config")/grubenv" "$payload/grubenv"; fi
pending="$target.new.$stamp"
root cp -a -- "$payload" "$pending"
if root test -d "$target"; then root mv -- "$target" "$target.backup.$stamp"; fi
root mv -- "$pending" "$target"
root install -d -- "$esp/EFI/caelestia"
if root test -f "$esp/EFI/caelestia/grubx64.efi"; then
  root cp -- "$esp/EFI/caelestia/grubx64.efi" "$esp/EFI/caelestia/grubx64.efi.backup.$stamp"
fi
root install -m644 -- "$work/grubx64.efi" "$esp/EFI/caelestia/grubx64.efi.new"
root mv -- "$esp/EFI/caelestia/grubx64.efi.new" "$esp/EFI/caelestia/grubx64.efi"
if (( secure_boot )); then
  root sbctl sign --save "$esp/EFI/caelestia/grubx64.efi"
fi
if (( nvram )); then
  entries=$(root efibootmgr -v)
  if [[ ${entries,,} != *'\efi\caelestia\grubx64.efi'* ]]; then
    root efibootmgr --create --disk "/dev/$parent" --part "$partition" \
      --label Caelestia --loader '\EFI\caelestia\grubx64.efi'
  fi
fi
printf 'Installed Caelestia carousel.\nMenu: %s\nLoader: %s/EFI/caelestia/grubx64.efi\n' "$config" "$esp"
