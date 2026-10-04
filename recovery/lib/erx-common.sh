#!/bin/sh
#
# MTD/UBI helpers for restore_edgeos.sh. Requires die() from the caller.
# PROC_MTD_PATH, SYS_PATH and MTD_DEV_DIR point the tests at fake devices.
PROC_MTD="${PROC_MTD_PATH:-/proc/mtd}"
SYS_FS="${SYS_PATH:-/sys}"

# Partition numbers can differ between devices, so look up partitions by name.
find_mtd_by_name() {
    name="$1"
    awk -v want="\"$name\"" '$4 == want { sub(/:$/, "", $1); print $1 }' "$PROC_MTD"
}

mtd_size_bytes() {
    dev="$1"
    size_hex="$(awk -v part="${dev}:" '$1 == part { print $2 }' "$PROC_MTD")"
    [ -n "$size_hex" ] || die "cannot determine size of $dev"
    printf '%u' "$((0x$size_hex))"
}

# Read through the read-only node (mtdNro) when it exists, so reads can never
# write; /dev/mtdN stays for the write tools.
mtd_read_path() {
    _mtd_path="${MTD_DEV_DIR:-/dev}/${1##*/}"
    if [ -r "${_mtd_path}ro" ]; then
        printf '%s' "${_mtd_path}ro"
    elif [ -r "$_mtd_path" ]; then
        printf '%s' "$_mtd_path"
    else
        die "MTD_READ_NODE_MISSING: neither $_mtd_path nor ${_mtd_path}ro exists"
    fi
}

# Finds the ubiN device already attached to the given MTD index, if any.
# Prints nothing (not an error) if none is attached yet.
find_attached_ubi_device() {
    mtd_index="$1"
    for ubi_sysfs in "$SYS_FS"/class/ubi/ubi[0-9]*; do
        [ -r "$ubi_sysfs/mtd_num" ] || continue
        [ "$(cat "$ubi_sysfs/mtd_num")" = "$mtd_index" ] || continue
        printf '%s' "${ubi_sysfs##*/}"
        return 0
    done
    return 1
}

# Finds the ubiN device for the given MTD index, attaching it first if
# necessary. Dies if it still can't be found/attached.
find_or_attach_ubi_device() {
    mtd_index="$1"
    ubi_device="$(find_attached_ubi_device "$mtd_index" || true)"
    if [ -z "$ubi_device" ]; then
        ubiattach -m "$mtd_index" || die "could not attach UBI on mtd $mtd_index"
        sync
        ubi_device="$(find_attached_ubi_device "$mtd_index" || true)"
    fi
    [ -n "$ubi_device" ] || die "could not attach/find UBI device for mtd $mtd_index"
    printf '%s' "$ubi_device"
}

# Finds a UBI volume by its sysfs "name" file under the given ubiN device.
find_ubi_volume() {
    ubi_device="$1"
    want_name="$2"
    for volume_sysfs in "$SYS_FS/class/ubi/${ubi_device}"_*; do
        [ -r "$volume_sysfs/name" ] || continue
        [ "$(cat "$volume_sysfs/name")" = "$want_name" ] || continue
        printf '%s' "${volume_sysfs##*/}"
        return 0
    done
    return 1
}

# The boot selector is byte 160 of the factory partition: 00 boots the kernel
# in the first 3 MiB, 01 the one in the second.

# Everything set_selector needs, checked before the restore writes anything.
selector_writer_preflight() {
    _index="${1#/dev/mtd}"
    case "$_index" in ''|*[!0-9]*) die "SELECTOR_DEVICE_INVALID: expected /dev/mtdN, got $1" ;; esac
    _sysfs="$SYS_FS/class/mtd/mtd$_index"
    command -v flash_erase >/dev/null 2>&1 || die "SELECTOR_TOOL_MISSING: flash_erase is required"
    command -v nandwrite >/dev/null 2>&1 || die "SELECTOR_TOOL_MISSING: nandwrite is required"
    for _name in writesize erasesize bad_blocks ecc_failures corrected_bits; do
        _value="$(cat "$_sysfs/$_name" 2>/dev/null)" || die "SELECTOR_PREFLIGHT_UNAVAILABLE: $_sysfs/$_name is missing"
        case "$_value" in ''|*[!0-9]*) die "SELECTOR_PREFLIGHT_INVALID: $_name is '$_value'" ;; esac
    done
    _writesize="$(cat "$_sysfs/writesize")"
    ERASESIZE="$(cat "$_sysfs/erasesize")"
    [ "$_writesize" -gt 160 ] || die "SELECTOR_PREFLIGHT_INVALID: NAND writesize is too small"
    [ "$ERASESIZE" -ge "$_writesize" ] && [ "$((ERASESIZE % _writesize))" -eq 0 ] \
        || die "SELECTOR_PREFLIGHT_INVALID: inconsistent NAND erase/write sizes"
    [ "$(cat "$_sysfs/bad_blocks")" -eq 0 ] || die "SELECTOR_BAD_BLOCKS_PRESENT: refusing selector erase on $1"
    mtd_read_path "$1" >/dev/null
}

# Uncorrectable ECC failures stop everything; corrected bitflips are what ECC
# is for, the data is compared byte for byte anyway, so they only warn.
ecc_counters() { cat "$SYS_FS/class/mtd/mtd${1#/dev/mtd}/ecc_failures"; }
corrected_bits() { cat "$SYS_FS/class/mtd/mtd${1#/dev/mtd}/corrected_bits"; }
warn_corrected() {
    [ "$(corrected_bits "$1")" = "$2" ] \
        || printf 'WARNING: NAND corrected bitflips on %s (corrected_bits %s -> %s); the data was verified\n' \
            "$1" "$2" "$(corrected_bits "$1")" >&2
}

# Reads the selector; fails if reading it changed the NAND ECC counters.
read_selector_checked() {
    _ecc_before_read="$(ecc_counters "$1")" || die "SELECTOR_ECC_STATUS_INVALID: MTD ECC counters are unavailable"
    _corrected_before_read="$(corrected_bits "$1")" || die "SELECTOR_ECC_STATUS_INVALID: MTD ECC counters are unavailable"
    _value="$(hexdump -s 160 -n 1 -e '1/1 "%02x"' "$(mtd_read_path "$1")")" \
        || die "SELECTOR_READ_FAILED: unable to read selector byte"
    [ "$(ecc_counters "$1")" = "$_ecc_before_read" ] \
        || die "SELECTOR_ECC_READ_FAILED: uncorrectable NAND ECC failure while reading selector"
    warn_corrected "$1" "$_corrected_before_read"
    printf '%s' "$_value"
}

# set_selector 00|01 /dev/mtdN. A NAND page cannot safely be reprogrammed to
# change one byte, so the whole first erase block is read, patched, erased,
# rewritten with fresh ECC and compared in full.
set_selector() {
    _want="$1"
    _device="$2"
    case "$_want" in 00|01) ;; *) die "SELECTOR_VALUE_UNSUPPORTED: $_want" ;; esac
    selector_writer_preflight "$_device"
    ERASESIZE="$(cat "$SYS_FS/class/mtd/mtd${_device#/dev/mtd}/erasesize")"
    # Left in /tmp if a step fails: they are the evidence for the investigation.
    _block="/tmp/selector-eraseblock.$$"
    _readback="/tmp/selector-eraseblock-readback.$$"
    _ecc_before="$(ecc_counters "$_device")"
    _corrected_before="$(corrected_bits "$_device")"
    dd if="$(mtd_read_path "$_device")" bs="$ERASESIZE" count=1 of="$_block" 2>/dev/null \
        && [ "$(wc -c < "$_block")" -eq "$ERASESIZE" ] \
        || die "SELECTOR_BLOCK_READ_FAILED: could not read the first erase block"
    [ "$(ecc_counters "$_device")" = "$_ecc_before" ] \
        || die "SELECTOR_ECC_READ_FAILED: uncorrectable NAND ECC failure while reading the block"
    _current="$(hexdump -s 160 -n 1 -e '1/1 "%02x"' "$_block")"
    case "$_current" in 00|01) ;; *) die "SELECTOR_VALUE_UNSUPPORTED: found $_current" ;; esac
    if [ "$_current" = "$_want" ]; then
        warn_corrected "$_device" "$_corrected_before"
        rm -f "$_block"
        printf 'SELECTOR_READBACK_VERIFIED: %s (already set)\n' "$_want"
        return 0
    fi
    printf "\\0$_want" | dd of="$_block" bs=1 seek=160 count=1 conv=notrunc 2>/dev/null \
        || die "SELECTOR_BLOCK_PREP_FAILED"
    # From the erase on, the block (MAC addresses, radio calibration, selector)
    # exists only in $_block until it is written back and verified.
    _keep="factory block may be erased or damaged -- do NOT reboot; the intended block is $_block, write it back with: nandwrite -p $_device $_block"
    flash_erase "$_device" 0 1 >/dev/null || die "SELECTOR_ERASE_FAILED: $_keep"
    nandwrite -p "$_device" "$_block" >/dev/null || die "SELECTOR_WRITE_FAILED: $_keep"
    dd if="$(mtd_read_path "$_device")" bs="$ERASESIZE" count=1 of="$_readback" 2>/dev/null \
        || die "SELECTOR_READBACK_FAILED: $_keep"
    cmp -s "$_block" "$_readback" || die "SELECTOR_READBACK_FAILED: NAND erase block mismatch; $_keep"
    [ "$(ecc_counters "$_device")" = "$_ecc_before" ] \
        || die "SELECTOR_ECC_READBACK_FAILED: uncorrectable NAND ECC failure; $_keep"
    warn_corrected "$_device" "$_corrected_before"
    rm -f "$_block" "$_readback"
    printf 'SELECTOR_ERASEBLOCK_READBACK_VERIFIED: %s\n' "$_want"
}
