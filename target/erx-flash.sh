#!/bin/sh
# erx-flash.sh -- runs on the OpenWrt 19.07 RAM bridge of an EdgeRouter X SFP.
#
#   erx-flash.sh check IMAGE IMAGE_SHA256 [CONFIG_TGZ CONFIG_SHA256]
#   erx-flash.sh flash IMAGE IMAGE_SHA256 [CONFIG_TGZ CONFIG_SHA256]
#
# "check" only reads. "flash" converts the EdgeOS flash layout to OpenWrt's
# single 6 MiB kernel layout and installs IMAGE (any ER-X-SFP sysupgrade
# image, e.g. official 25.12.x):
#
#   1. kernel -> kernel1 (+ kernel2), each part read back byte for byte
#   2. boot selector (factory offset 160) -> 0, the way EdgeOS itself sets it
#   3. EdgeOS UBI volume troot -> OpenWrt rootfs + rootfs_data, read back
#   4. optional CONFIG_TGZ -> rootfs_data/sysupgrade.tgz, restored on first
#      boot (use it to keep the router reachable, see README)
#   5. reboot
#
# Every step stops the script on the first error, before the next write.

set -eu
# The host runs "flash" in the background and follows its log; a dropped SSH
# connection must not stop the writer between two writes.
trap '' HUP

# Tests point R at a fake root; on the router it is empty.
R="${ERX_TEST_ROOT:-}"

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*"; }

MODE="${1:-}"
case "$MODE" in check|flash) ;; *) sed -n '2,6p' "$0" >&2; exit 2 ;; esac
[ "$#" -eq 3 ] || [ "$#" -eq 5 ] || die "expected IMAGE SHA256 [CONFIG SHA256]"

: "${IPKG_INSTROOT:=}"   # /lib/functions.sh reads it; must be set under set -u
. "$R/lib/functions.sh"
include "$R/lib/upgrade"    # find_mtd_part, nand_* helpers, identify_tar
: "${CI_UBIPART:=ubi}" "${CI_ROOTPART:=rootfs}"   # nand.sh's defaults, stated here

# ---------------------------------------------------------------- helpers

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

mtd_index() {           # "kernel1" -> "3", resolved by name from /proc/mtd
    awk -v n="\"$1\"" '$4 == n { sub(/^mtd/, "", $1); sub(/:$/, "", $1); print $1 }' "$R/proc/mtd"
}

mtd_bytes() {           # "kernel1" -> size in bytes
    echo $((0x$(awk -v n="\"$1\"" '$4 == n { print $2 }' "$R/proc/mtd")))
}

mtd_char() { echo "$R/dev/mtd$(mtd_index "$1")"; }   # reads bypass the mtdblock cache

nand_counter() {        # nand_counter kernel1 bad_blocks
    cat "$R/sys/class/mtd/mtd$(mtd_index "$1")/$2" 2>/dev/null || die "cannot read $2 for $1"
}

read_selector() { hexdump -s 160 -n 1 -e '1/1 "%02x"' "$(mtd_char factory)"; }

readback() {            # readback LABEL EXPECTED_FILE DEVICE
    bytes="$(wc -c < "$2" | tr -d '[:space:]')"
    head -c "$bytes" "$3" | cmp -s - "$2" || die "$1 readback does not match what was written"
    echo "$1 readback verified ($bytes bytes, $(sha256_of "$2"))"
}

# ------------------------------------------------------------------ check

[ "$(awk '$2 == "/" { print $3; exit }' "$R/proc/mounts")" = rootfs ] \
    || die "not running from a RAM root; boot the 19.07 bridge first"

# troot cannot be replaced while something has it (or any UBI volume) mounted.
! grep -q -E '^(/dev/)?ubi(block)?[0-9]' "$R/proc/mounts" || die "a UBI volume is mounted; unmount it before migrating"

[ "$(board_name)" = ubnt-erx-sfp ] || die "not the 19.07 bridge on an ER-X-SFP (board: $(board_name))"

for tool in mtd dd tar hexdump cmp sha256sum ubiattach ubirmvol ubiupdatevol ubimkvol; do
    command -v "$tool" >/dev/null || die "missing tool on the bridge: $tool"
done

for part in factory kernel1 kernel2 ubi; do
    [ -n "$(mtd_index "$part")" ] || die "MTD partition '$part' not found: not the EdgeOS layout"
done
kernel1_size="$(mtd_bytes kernel1)"
kernel2_size="$(mtd_bytes kernel2)"
[ "$kernel1_size" -eq 3145728 ] && [ "$kernel2_size" -eq 3145728 ] \
    || die "kernel1/kernel2 are not the old 2 x 3 MiB layout"

for part in factory kernel1 kernel2; do
    [ "$(nand_counter "$part" bad_blocks)" -eq 0 ] || die "$part has bad blocks"
    [ "$(nand_counter "$part" ecc_failures)" -eq 0 ] || die "$part reports ECC failures"
done

selector="$(read_selector)"
case "$selector" in 00|01) ;; *) die "unexpected boot selector value: $selector" ;; esac

# Work on private copies, verified against the hashes the operator supplied.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/erx-flash.XXXXXX")"
# Keep the work files (readbacks, factory block copies) when something fails.
cleanup() {
    status=$?
    if [ "$status" -eq 0 ]; then rm -rf "$WORK"; else echo "work files kept for inspection: $WORK" >&2; fi
}
trap cleanup EXIT
trap 'exit 1' INT TERM
# A hard link costs no RAM (/tmp is tmpfs); cp only if /tmp spans filesystems.
ln "$2" "$WORK/sysupgrade.img" 2>/dev/null || cp "$2" "$WORK/sysupgrade.img"
[ "$(sha256_of "$WORK/sysupgrade.img")" = "$3" ] || die "image SHA-256 mismatch"
CONFIG=""
if [ "$#" -eq 5 ]; then
    cp "$4" "$WORK/sysupgrade.tgz"
    [ "$(sha256_of "$WORK/sysupgrade.tgz")" = "$5" ] || die "config SHA-256 mismatch"
    tar -tzf "$WORK/sysupgrade.tgz" > "$WORK/config.list" || die "config is not a readable .tar.gz"
    # preinit unpacks it into / : no absolute paths, no ".." components
    ! grep -q -E '^/|(^|/)\.\.(/|$)' "$WORK/config.list" || die "config contains unsafe paths"
    # only files and directories: a link could point a later member anywhere
    tar -tvzf "$WORK/sysupgrade.tgz" > "$WORK/config.types" || die "config is not a readable .tar.gz"
    ! grep -q -v -E '^[-d]' "$WORK/config.types" && ! grep -q -E ' -> | link to ' "$WORK/config.types" \
        || die "config contains links or special files"
    CONFIG="$WORK/sysupgrade.tgz"
fi

IMAGE="$WORK/sysupgrade.img"
# From any member: tars need not contain a separate directory entry.
board_dir="$(tar -tf "$IMAGE" | sed -n 's#^\(sysupgrade-[^/]*\)/.*#\1#p' | head -n 1)"
[ "$board_dir" = sysupgrade-ubnt_edgerouter-x-sfp ] \
    || die "image is not an ER-X-SFP sysupgrade image (found '${board_dir:-nothing}')"
tar -xf "$IMAGE" -C "$WORK" "$board_dir/kernel" "$board_dir/root"
KERNEL="$WORK/$board_dir/kernel"
ROOT="$WORK/$board_dir/root"
kernel_length="$(wc -c < "$KERNEL" | tr -d '[:space:]')"
[ "$kernel_length" -gt 0 ] && [ "$kernel_length" -le $((kernel1_size + kernel2_size)) ] \
    || die "kernel ($kernel_length bytes) does not fit kernel1+kernel2"

say "check passed"
echo "  image:    $board_dir, kernel $kernel_length bytes, root $(wc -c < "$ROOT" | tr -d '[:space:]') bytes"
if [ -n "$CONFIG" ]; then
    echo "  config:   restored on first boot"
else
    echo "  config:   none (router boots with image defaults)"
fi
echo "  selector: $selector (00 = kernel1)"
cat "$R/proc/mtd"

[ "$MODE" = flash ] || exit 0

# ------------------------------------------------------------------ flash

say "PHASE:FLASH_START"
# From here on, stopping halfway is worse than finishing: ignore INT/TERM too.
trap '' INT TERM

# Attach UBI before the first write: if it cannot be attached, nothing is
# changed yet and EdgeOS still boots.
ubidev="$(nand_find_ubi "$CI_UBIPART" || true)"
if [ -z "$ubidev" ]; then
    ubiattach -m "$(mtd_index "$CI_UBIPART")" || die "cannot attach UBI on '$CI_UBIPART'; nothing was written"
    ubidev="$(nand_find_ubi "$CI_UBIPART")" || die "UBI attached but not found; nothing was written"
fi

# 1. Kernel: first 3 MiB to kernel1, the rest to kernel2. OpenWrt later sees
#    kernel1+kernel2 as one 6 MiB "kernel" partition.
dd if="$KERNEL" of="$WORK/kernel1.part" bs=1024 count=$((kernel1_size / 1024)) 2>/dev/null
mtd write "$WORK/kernel1.part" kernel1
readback kernel1 "$WORK/kernel1.part" "$(mtd_char kernel1)"
if [ "$kernel_length" -gt "$kernel1_size" ]; then
    dd if="$KERNEL" of="$WORK/kernel2.part" bs=1024 skip=$((kernel1_size / 1024)) 2>/dev/null
    mtd write "$WORK/kernel2.part" kernel2
    readback kernel2 "$WORK/kernel2.part" "$(mtd_char kernel2)"
fi

# 2. Selector: only after the kernel it points to is verified. This is the
#    same one-byte write through mtdblock that EdgeOS's own ubnt-upgrade
#    uses; mtdblock erases and rewrites the whole block with fresh ECC.
#    Guard it: the rest of the block must be unchanged, ECC counters quiet.
if [ "$(read_selector)" != 00 ]; then
    block="$(cat "$R/sys/class/mtd/mtd$(mtd_index factory)/erasesize")"
    case "$block" in ''|*[!0-9]*) die "factory erasesize '$block' is not a number; the selector was not changed" ;; esac
    [ "$block" -gt 160 ] || die "factory erase block ($block bytes) does not hold byte 160; the selector was not changed"
    dd if="$(mtd_char factory)" of="$WORK/factory.now" bs="$block" count=1 2>/dev/null \
        || die "cannot read the factory block; the selector was not changed"
    # An uncorrectable read would put bad data into factory.expected.
    [ "$(nand_counter factory ecc_failures)" -eq 0 ] \
        || die "ECC failure while reading factory; the selector was not changed -- do NOT reboot, investigate"
    { head -c 160 "$WORK/factory.now"; printf '\000'; tail -c +162 "$WORK/factory.now"; } \
        > "$WORK/factory.expected"
    printf '\000' | dd of="$(find_mtd_part factory)" bs=1 seek=160 count=1 conv=notrunc 2>/dev/null
    sync
    dd if="$(mtd_char factory)" of="$WORK/factory.after" bs="$block" count=1 2>/dev/null
    cmp -s "$WORK/factory.expected" "$WORK/factory.after" \
        || die "factory block changed beyond the selector byte -- do NOT reboot, investigate"
    [ "$(nand_counter factory ecc_failures)" -eq 0 ] \
        || die "factory reports ECC failures after the selector write -- do NOT reboot"
fi
[ "$(read_selector)" = 00 ] || die "selector readback is not 00 -- do NOT reboot"
echo "selector readback verified (00)"

# 3. Root filesystem: replace EdgeOS's troot volume with OpenWrt's volumes.
# shellcheck disable=SC2034  # read by the nand_* helpers, not by this file
CI_KERNPART=none   # the kernel is already written above
if [ -n "$(nand_find_volume "$ubidev" troot || true)" ]; then
    ubirmvol "$R/dev/$ubidev" -N troot
fi
root_length="$(wc -c < "$ROOT" | tr -d '[:space:]')"
nand_upgrade_prepare_ubi "$root_length" "$(identify_tar "$IMAGE" "$board_dir/root" "")" "" 0
ubidev="$(nand_find_ubi "$CI_UBIPART")"
rootvol="$(nand_find_volume "$ubidev" "$CI_ROOTPART")"
ubiupdatevol "$R/dev/$rootvol" -s "$root_length" "$ROOT"
readback rootfs "$ROOT" "$R/dev/$rootvol"

# 4. Configuration, restored by OpenWrt's preinit on the first boot.
if [ -n "$CONFIG" ]; then
    cp "$CONFIG" "$WORK/config.expected"        # nand_restore_config moves the file
    nand_restore_config "$CONFIG" || die "could not place the configuration in rootfs_data"
    datavol="$(nand_find_volume "$ubidev" rootfs_data)"
    mkdir "$WORK/rootfs_data"
    mount -t ubifs -o ro "$R/dev/$datavol" "$WORK/rootfs_data" || die "cannot mount rootfs_data to check the configuration"
    placed=yes
    cmp -s "$WORK/config.expected" "$WORK/rootfs_data/sysupgrade.tgz" || placed=no   # not a bare cmp: set -e
    umount "$WORK/rootfs_data"
    [ "$placed" = yes ] || die "configuration readback in rootfs_data does not match"
    echo "configuration readback verified in rootfs_data"
fi

say "all writes verified; rebooting into OpenWrt"
sync
# Give the host's `tail -f` (1 s poll) time to send this line before the
# network goes down.
sleep "${ERX_REBOOT_DELAY:-5}" || sleep 5    # never skip the reboot over a bad value
reboot -f
