#!/bin/sh
#
# restore_edgeos.sh -- put EdgeOS back on an EdgeRouter X SFP from a per-device
# backup (kernel12.bin + troot volume). Runs in the OpenWrt 25.12 RAM recovery
# system (see README.md); refuses anything else. troot (246 MB) does not fit
# into the router's RAM, so it is streamed over HTTP and verified by hash.
# Order: troot, kernel, boot selector; each read back. Does not reboot.

set -eu

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
DEV_PATH="${DEV_PATH:-/dev}"
# shellcheck source=lib/erx-common.sh
. "$SCRIPT_DIR/lib/erx-common.sh"

usage() {
    cat <<'EOF' >&2
Usage: ALLOW_DESTRUCTIVE_WRITE=1 ./restore_edgeos.sh --confirm-UBI-WRITE \
    --kernel FILE --troot-url URL --troot-size BYTES --selector 00|01 \
    --expected-kernel-sha256 HASH --expected-troot-sha256 HASH

  --kernel FILE                  the combined Kernel1+Kernel2 backup (kernel12.bin)
  --troot-url URL                the troot UBI volume backup, streamed over HTTP
  --troot-size BYTES             its exact size in bytes
  --selector 00|01               boot selector the router had when the backup was
                                 taken (EdgeOS alternates kernel slots on updates)
  --expected-kernel-sha256 HASH  required -- refuses to write an unverified image
  --expected-troot-sha256 HASH   required -- refuses to write an unverified image
  --confirm-UBI-WRITE            required in addition to ALLOW_DESTRUCTIVE_WRITE=1
EOF
}

case " $* " in *" -h "*|*" --help "*) usage; exit 0 ;; esac
[ "${ALLOW_DESTRUCTIVE_WRITE:-0}" = "1" ] || die "WRITE_GATE_DISABLED: set ALLOW_DESTRUCTIVE_WRITE=1 to authorize restore writes"

CONFIRMED=0
KERNEL_IMG=""
TROOT_URL=""
TROOT_LENGTH=""
EXPECTED_KERNEL_SHA256=""
EXPECTED_TROOT_SHA256=""
WANT_SELECTOR=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-UBI-WRITE) CONFIRMED=1; shift ;;
        --kernel) [ "$#" -ge 2 ] || { usage; exit 2; }; KERNEL_IMG="$2"; shift 2 ;;
        --troot-url) [ "$#" -ge 2 ] || { usage; exit 2; }; TROOT_URL="$2"; shift 2 ;;
        --troot-size) [ "$#" -ge 2 ] || { usage; exit 2; }; TROOT_LENGTH="$(printf %s "$2" | tr -d " \t")"; shift 2 ;;
        --selector) [ "$#" -ge 2 ] || { usage; exit 2; }; WANT_SELECTOR="$2"; shift 2 ;;
        --expected-kernel-sha256) [ "$#" -ge 2 ] || { usage; exit 2; }; EXPECTED_KERNEL_SHA256="$2"; shift 2 ;;
        --expected-troot-sha256) [ "$#" -ge 2 ] || { usage; exit 2; }; EXPECTED_TROOT_SHA256="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
[ "$CONFIRMED" -eq 1 ] || die "CONFIRMATION_REQUIRED: pass --confirm-UBI-WRITE to authorize the restore"
[ -n "$KERNEL_IMG" ] && [ -f "$KERNEL_IMG" ] && [ -r "$KERNEL_IMG" ] || { usage; die "missing or unreadable --kernel file"; }
case "$TROOT_URL" in http://*) ;; *) usage; die "--troot-url must be an http:// URL on the recovery link" ;; esac
case "$TROOT_LENGTH" in ''|*[!0-9]*) die "--troot-size must be a number of bytes" ;; esac
case "$WANT_SELECTOR" in 00|01) ;; *) die "--selector must be 00 or 01 (the value when the backup was taken)" ;; esac
# Both hashes are mandatory: nothing unverified is ever written.
EXPECTED_KERNEL_SHA256="$(printf '%s' "$EXPECTED_KERNEL_SHA256" | tr 'A-F' 'a-f')"
EXPECTED_TROOT_SHA256="$(printf '%s' "$EXPECTED_TROOT_SHA256" | tr 'A-F' 'a-f')"
[ -n "$EXPECTED_KERNEL_SHA256" ] || die "--expected-kernel-sha256 is required"
[ -n "$EXPECTED_TROOT_SHA256" ] || die "--expected-troot-sha256 is required"
printf '%s' "$EXPECTED_KERNEL_SHA256" | grep -Eq '^[0-9a-fA-F]{64}$' || die "--expected-kernel-sha256 must be exactly 64 hex characters"
printf '%s' "$EXPECTED_TROOT_SHA256" | grep -Eq '^[0-9a-fA-F]{64}$' || die "--expected-troot-sha256 must be exactly 64 hex characters"

command -v wget >/dev/null 2>&1 || die "wget is required"
[ "$(id -u)" -eq 0 ] || die "must run as root"

actual_kernel_sha256="$(sha256sum "$KERNEL_IMG" | cut -d' ' -f1)"
[ "$actual_kernel_sha256" = "$EXPECTED_KERNEL_SHA256" ] || die "--kernel does not match --expected-kernel-sha256 (got $actual_kernel_sha256)"
say "Kernel hash verified; streamed troot will be verified by SHA-256 readback."

MOUNTS_FILE="${MOUNTS_PATH:-/proc/mounts}"
CMDLINE_FILE="${CMDLINE_PATH:-/proc/cmdline}"

# find_mtd_by_name, mtd_size_bytes, find_or_attach_ubi_device and
# find_ubi_volume come from lib/erx-common.sh (also sets PROC_MTD, SYS_FS).
MTD_EEPROM="$(find_mtd_by_name factory || true)"
# Only the OpenWrt >= 24.10 recovery image is supported: its device tree
# always shows Kernel1+Kernel2 as one 6 MiB "kernel" partition, whatever is
# on flash. Other systems (EdgeOS, the 19.07 bridge) are refused here, before
# anything is written.
MTD_KERNEL="$(find_mtd_by_name kernel || true)"
[ -n "$MTD_KERNEL" ] || die "no 6 MiB 'kernel' partition: boot the OpenWrt 25.12 RAM recovery image (see README.md)"
MTD_ROOTFS="$(find_mtd_by_name ubi || true)"
for var_name in MTD_EEPROM MTD_ROOTFS; do
    eval "val=\$$var_name"
    [ -n "$val" ] || die "could not find MTD partition for $var_name in /proc/mtd"
done

KERNEL_CAPACITY="$(mtd_size_bytes "$MTD_KERNEL")"
KERNEL_LENGTH="$(wc -c < "$KERNEL_IMG" | tr -d '[:space:]')"
[ "$KERNEL_LENGTH" -eq "$KERNEL_CAPACITY" ] || die "--kernel is $KERNEL_LENGTH bytes, expected exactly $KERNEL_CAPACITY (the kernel partition)"
# The slot the selector will point to must start with a uImage header.
command -v hexdump >/dev/null 2>&1 || die "hexdump is required"
case "$WANT_SELECTOR" in 00) slot_offset=0 ;; *) slot_offset=$((KERNEL_CAPACITY / 2)) ;; esac
slot_magic="$(hexdump -s "$slot_offset" -n 4 -e '4/1 "%02x"' "$KERNEL_IMG")"
[ "$slot_magic" = 27051956 ] \
    || die "KERNEL_SLOT_EMPTY: --selector $WANT_SELECTOR points to byte $slot_offset of --kernel, which holds no uImage ($slot_magic)"
[ "$TROOT_LENGTH" -gt 0 ] || die "TROOT_IMAGE_EMPTY: image size must be positive"

# Refuse to operate against the currently-mounted root. /proc/cmdline's
# ubi.mtd= only catches the plain "this MTD is the kernel cmdline root"
# case; also cross-reference the live mount table against whichever ubi<N>
# device sysfs says is attached to this MTD index, matching the stronger
# check required before any UBI write.
rootfs_mtd_index="${MTD_ROOTFS#mtd}"
if grep -Eq "(^|[[:space:]])ubi\.mtd=(${rootfs_mtd_index}|ubi)([[:space:],]|\$)" "$CMDLINE_FILE" 2>/dev/null; then
    die "refusing: target UBI MTD appears to be the running root filesystem (cmdline)"
fi
for ubi_sysfs in "$SYS_FS"/class/ubi/ubi[0-9]*; do
    [ -r "$ubi_sysfs/mtd_num" ] || continue
    [ "$(cat "$ubi_sysfs/mtd_num")" = "$rootfs_mtd_index" ] || continue
    dev_name="${ubi_sysfs##*/}"
    if grep -Eq "(^|[^0-9a-z])(${dev_name}|ubiblock${dev_name#ubi})(_[0-9]+)?([^0-9a-z]|\$)" "$MOUNTS_FILE" 2>/dev/null; then
        die "refusing: $dev_name (attached to mtd$rootfs_mtd_index) is currently mounted -- this is a live system, not the RAM recovery system (see README.md)"
    fi
done

check_mtd_bad_blocks() {
    _mtd="$1"
    _bad_blocks_file="$SYS_FS/class/mtd/$_mtd/bad_blocks"
    [ -r "$_bad_blocks_file" ] \
        || die "NAND_BAD_BLOCK_COUNT_UNAVAILABLE: missing $_bad_blocks_file"
    _bad_blocks="$(cat "$_bad_blocks_file")"
    case "$_bad_blocks" in
        ''|*[!0-9]*) die "NAND_BAD_BLOCK_COUNT_INVALID: $_mtd reports '$_bad_blocks'" ;;
    esac
    [ "$_bad_blocks" -eq 0 ] \
        || die "NAND_BAD_BLOCK_DETECTED: $_mtd reports $_bad_blocks bad blocks -- refusing restore writes"
}
check_mtd_bad_blocks "$MTD_KERNEL"
check_mtd_bad_blocks "$MTD_EEPROM"
# "/dev/mtdN" names the partition for the lib (it takes N from it); reads go
# through mtd_read_path, which honours MTD_DEV_DIR.
EEPROM_DEVICE="/dev/$MTD_EEPROM"
SELECTOR_BEFORE="$(read_selector_checked "$EEPROM_DEVICE")"
case "$SELECTOR_BEFORE" in
    "$WANT_SELECTOR") say "Selector already $WANT_SELECTOR; no selector write is required." ;;
    00|01) selector_writer_preflight "$EEPROM_DEVICE"; say "Selector eraseblock writer pre-flight passed." ;;
    *) die "SELECTOR_VALUE_UNSUPPORTED: expected 00 or 01, got $SELECTOR_BEFORE" ;;
esac
say "Kernel and selector-partition bad-block pre-flight passed."

command -v ubirmvol >/dev/null 2>&1 || die "ubirmvol is required to prepare a troot volume safely"
command -v ubimkvol >/dev/null 2>&1 || die "ubimkvol is required to prepare a troot volume safely"
command -v ubiupdatevol >/dev/null 2>&1 || die "ubiupdatevol is required to restore troot"
already_attached="$(find_attached_ubi_device "$rootfs_mtd_index" || true)"
ubi_device="$(find_or_attach_ubi_device "$rootfs_mtd_index")"
stream_pid=""
stream_dir=""
cleanup() {
    status=$?
    if [ -n "$stream_pid" ]; then
        kill "$stream_pid" 2>/dev/null || true
        wait "$stream_pid" 2>/dev/null || true
    fi
    [ -z "$stream_dir" ] || rm -rf "$stream_dir"
    # After a failure UBI stays attached: /dev/ubi* and sysfs are the evidence.
    [ "$status" -ne 0 ] || [ -n "$already_attached" ] || ubidetach -m "$rootfs_mtd_index" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' INT TERM   # a trapped signal would otherwise resume the script

troot_vol="$(find_ubi_volume "$ubi_device" troot || true)"
ubi_volume_attr() {
    _volume="$1"
    _attribute="$2"
    _path="$SYS_FS/class/ubi/$_volume/$_attribute"
    [ -r "$_path" ] || die "UBI_SYSFS_ATTRIBUTE_MISSING: $_volume/$_attribute"
    cat "$_path"
}

ubi_sysfs="$SYS_FS/class/ubi/$ubi_device"
usable_leb_size=""
troot_lebs=""
troot_reserved=""
reshape_ubi=0
if [ -n "$troot_vol" ]; then
    usable_leb_size="$(ubi_volume_attr "$troot_vol" usable_eb_size)"
    troot_reserved="$(ubi_volume_attr "$troot_vol" reserved_ebs)"
else
    reshape_ubi=1
    rootfs_vol="$(find_ubi_volume "$ubi_device" rootfs || true)"
    rootfs_data_vol="$(find_ubi_volume "$ubi_device" rootfs_data || true)"
    reserved_sum=0
    for volume_sysfs in "$SYS_FS/class/ubi/${ubi_device}"_*; do
        [ -r "$volume_sysfs/name" ] || continue
        volume_name="$(cat "$volume_sysfs/name")"
        case "$volume_name" in
            rootfs|rootfs_data)
                volume_reserved="$(cat "$volume_sysfs/reserved_ebs")"
                volume_leb="$(cat "$volume_sysfs/usable_eb_size")"
                case "$volume_reserved:$volume_leb" in
                    *[!0-9:]*|:*) die "UBI_GEOMETRY_INVALID: non-numeric geometry for $volume_name" ;;
                esac
                [ "$volume_leb" -gt 0 ] || die "UBI_GEOMETRY_INVALID: zero LEB size for $volume_name"
                if [ -z "$usable_leb_size" ]; then
                    usable_leb_size="$volume_leb"
                elif [ "$usable_leb_size" -ne "$volume_leb" ]; then
                    die "UBI_GEOMETRY_INVALID: volumes use different LEB sizes"
                fi
                reserved_sum=$((reserved_sum + volume_reserved))
                ;;
            *) die "UBI_LAYOUT_UNSUPPORTED: unexpected volume '$volume_name' on $ubi_device" ;;
        esac
    done
    [ -n "$usable_leb_size" ] || {
        # sysfs ubiX/eraseblock_size is the LEB size (ubi->leb_size), not the PEB size
        usable_leb_size="$(cat "$ubi_sysfs/eraseblock_size" 2>/dev/null || true)"
    }
    [ -n "$usable_leb_size" ] || die "UBI_GEOMETRY_UNAVAILABLE: no existing volume exposes usable_eb_size"
    available_lebs="$(cat "$ubi_sysfs/avail_eraseblocks" 2>/dev/null || true)"
    case "$available_lebs" in ''|*[!0-9]*) die "UBI_GEOMETRY_UNAVAILABLE: avail_eraseblocks is missing or invalid" ;; esac
    reclaimable_lebs=$((reserved_sum + available_lebs))
    [ "$TROOT_LENGTH" -gt 0 ] || die "TROOT_IMAGE_EMPTY: image size must be positive"
    [ "$usable_leb_size" -gt 0 ] || die "UBI_GEOMETRY_INVALID: zero LEB size"
    [ "$((TROOT_LENGTH % usable_leb_size))" -eq 0 ] \
        || die "TROOT_IMAGE_LEB_MISMATCH: $TROOT_LENGTH bytes is not a multiple of $usable_leb_size"
    troot_lebs=$((TROOT_LENGTH / usable_leb_size))
    [ "$reclaimable_lebs" -ge "$troot_lebs" ] \
        || die "UBI_CAPACITY_INSUFFICIENT: need $troot_lebs LEBs, can reclaim only $reclaimable_lebs"
    say "UBI plan: replace existing OpenWrt volumes with troot ($troot_lebs LEBs)."
fi

case "$usable_leb_size:$troot_reserved" in
    *[!0-9:]*|:*)
        if [ "$reshape_ubi" -eq 0 ]; then
            die "UBI_GEOMETRY_INVALID: non-numeric troot geometry"
        fi
        ;;
esac
[ "$usable_leb_size" -gt 0 ] || die "UBI_GEOMETRY_INVALID: zero LEB size"
[ "$((TROOT_LENGTH % usable_leb_size))" -eq 0 ] \
    || die "TROOT_IMAGE_LEB_MISMATCH: $TROOT_LENGTH bytes is not a multiple of $usable_leb_size"
troot_lebs=$((TROOT_LENGTH / usable_leb_size))
if [ "$reshape_ubi" -eq 0 ]; then
    case "$troot_reserved" in ''|*[!0-9]*) die "UBI_GEOMETRY_INVALID: invalid troot reserved_ebs" ;; esac
    [ "$troot_reserved" -eq "$troot_lebs" ] \
        || die "TROOT_VOLUME_GEOMETRY_MISMATCH: volume has $troot_reserved LEBs, image needs $troot_lebs"
fi

# All artifact, MTD, NAND, mount, and UBI geometry checks finish before the
# first persistent change. OpenWrt's small rootfs/rootfs_data volumes must be
# replaced as a pair: writing a 1938-LEB EdgeOS image into rootfs alone would
# fail only after an unnecessary kernel write.
if [ "$reshape_ubi" -eq 1 ]; then
    if [ -n "$rootfs_vol" ]; then
        rootfs_block="ubiblock${rootfs_vol#ubi}"
        rootfs_block_sysfs="$SYS_FS/class/block/$rootfs_block"
        if [ -e "$rootfs_block_sysfs" ]; then
            command -v ubiblock >/dev/null 2>&1 \
                || die "ubiblock is required to release the unmounted OpenWrt rootfs block mapping"
            ubiblock -r "$DEV_PATH/$rootfs_vol" \
                || die "UBI_BLOCK_REMOVE_FAILED: could not release $rootfs_block"
            [ ! -e "$rootfs_block_sysfs" ] \
                || die "UBI_BLOCK_REMOVE_FAILED: $rootfs_block still exists after removal"
        fi
    fi
    [ -z "$rootfs_vol" ] || ubirmvol "$DEV_PATH/$ubi_device" -N rootfs
    [ -z "$rootfs_data_vol" ] || ubirmvol "$DEV_PATH/$ubi_device" -N rootfs_data
    [ -z "$(find_ubi_volume "$ubi_device" rootfs || true)" ] \
        || die "UBI_RESHAPE_FAILED: rootfs volume remains after removal"
    [ -z "$(find_ubi_volume "$ubi_device" rootfs_data || true)" ] \
        || die "UBI_RESHAPE_FAILED: rootfs_data volume remains after removal"
    ubimkvol "$DEV_PATH/$ubi_device" -N troot -n 0 -S "$troot_lebs" -t dynamic
    troot_vol="$(find_ubi_volume "$ubi_device" troot || true)"
    [ -n "$troot_vol" ] || die "UBI_RESHAPE_FAILED: troot volume was not created"
    created_lebs="$(ubi_volume_attr "$troot_vol" reserved_ebs)"
    created_leb_size="$(ubi_volume_attr "$troot_vol" usable_eb_size)"
    [ "$created_lebs" = "$troot_lebs" ] && [ "$created_leb_size" = "$usable_leb_size" ] \
        || die "UBI_RESHAPE_FAILED: created troot geometry does not match the plan"
fi

say "Restoring troot UBI volume..."
stream_dir="$(mktemp -d /tmp/erx-troot-stream.XXXXXX)"
stream_fifo="$stream_dir/input"
stream_hash="$stream_dir/sha256"
mkfifo "$stream_fifo"
# ash has no pipefail: record wget's status in a file, the pipeline's own
# status is only that of cut.
({ rc=0; wget -T 30 -O - "$TROOT_URL" </dev/null || rc=$?; echo "$rc" > "$stream_dir/wget-status"; } \
    | tee "$stream_fifo" | sha256sum | cut -d' ' -f1 > "$stream_hash") &
stream_pid=$!
update_ok=yes
ubiupdatevol "$DEV_PATH/$troot_vol" --size="$TROOT_LENGTH" "$stream_fifo" || update_ok=no
if [ "$update_ok" = no ]; then
    # ubiupdatevol may have failed before opening the fifo; a reader lets tee
    # (and wget) end instead of blocking on the open forever.
    ( exec 3<"$stream_fifo" ) </dev/null >/dev/null 2>&1 &
fi
wait "$stream_pid" || true
wget_status="$(cat "$stream_dir/wget-status" 2>/dev/null || echo unknown)"
if [ "$update_ok" = no ]; then
    # A dropped transfer cuts the stream short and fails ubiupdatevol too; a
    # wget killed by SIGPIPE (141) was only cut off by ubiupdatevol stopping.
    case "$wget_status" in
        0|141) ;;
        *) die "TROOT_STREAM_UPDATE_FAILED: wget also failed (status $wget_status), check $TROOT_URL and the link first; do not write kernel or reboot" ;;
    esac
    die "TROOT_STREAM_UPDATE_FAILED: do not write kernel or reboot; recovery remains in RAM"
fi
# ubiupdatevol took exactly --troot-size bytes; SIGPIPE means the server had more.
[ "$wget_status" != 141 ] \
    || die "TROOT_DOWNLOAD_TOO_LONG: $TROOT_URL is longer than --troot-size; do not write kernel or reboot"
[ "$wget_status" = 0 ] \
    || die "TROOT_DOWNLOAD_FAILED: wget did not complete $TROOT_URL (status $wget_status); do not write kernel or reboot"
actual_troot_sha256="$(cat "$stream_hash")"
[ "$actual_troot_sha256" = "$EXPECTED_TROOT_SHA256" ] \
    || die "TROOT_STREAM_HASH_MISMATCH: got $actual_troot_sha256; do not write kernel or reboot"
stream_pid=""
sync
actual_troot_sha256="$(head -c "$TROOT_LENGTH" "$DEV_PATH/$troot_vol" | sha256sum | cut -d' ' -f1)"
[ "$actual_troot_sha256" = "$EXPECTED_TROOT_SHA256" ] \
    || die "troot readback verification failed -- do not write kernel or reboot"
say "troot readback verified."

say "Writing kernel..."
command -v mtd >/dev/null 2>&1 || die "mtd is required to write the kernel partition"
mtd -f write "$KERNEL_IMG" kernel >/dev/null || die "kernel write failed"
kernel_readback="/tmp/kernel.readback.$$"
dd if="$(mtd_read_path "/dev/$MTD_KERNEL")" bs=4096 count="$((KERNEL_LENGTH / 4096))" \
    of="$kernel_readback" 2>/dev/null || die "kernel readback could not be read"
[ "$(wc -c < "$kernel_readback" | tr -d '[:space:]')" -eq "$KERNEL_LENGTH" ] \
    || die "kernel readback was short"
cmp -s "$KERNEL_IMG" "$kernel_readback" || die "kernel readback verification failed"
rm -f "$kernel_readback"
say "kernel readback verified."

say "Setting the boot selector to $WANT_SELECTOR..."
[ "$(read_selector_checked "$EEPROM_DEVICE")" = "$WANT_SELECTOR" ] \
    || set_selector "$WANT_SELECTOR" "$EEPROM_DEVICE"
SELECTOR_AFTER="$(read_selector_checked "$EEPROM_DEVICE")"
[ "$SELECTOR_AFTER" = "$WANT_SELECTOR" ] \
    || die "SELECTOR_READBACK_FAILED: expected $WANT_SELECTOR, got $SELECTOR_AFTER"
say "SELECTOR_READBACK_VERIFIED: $WANT_SELECTOR"

say "EdgeOS rollback complete and fully verified."
say "This script does not reboot on its own. When ready, run:"
say "  sync && reboot -f"
