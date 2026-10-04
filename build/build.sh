#!/usr/bin/env bash
# build.sh -- build the images used by erx-migrate and recovery/. Linux only.
#
#   ./build.sh bridge   OpenWrt 19.07.10 RAM bridge (EdgeOS factory tar)
#                       root password from stdin, optional --authorized-keys FILE
#   ./build.sh final    OpenWrt 25.12.5 sysupgrade image with OLSR v1/v2
#                       (optional: an official 25.12 sysupgrade image works too)
#   ./build.sh recovery OpenWrt 25.12.5 RAM recovery image for recovery/ (lab)
#
# Output goes to ./out/<kind>/. Build trees live in ./work/ (large, reusable).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
KIND="${1:-}"; shift || true

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

case "$KIND" in
    bridge)
        REF=v19.07.10; SHA=d03dc49943db1d02ff89e056a18eefe5e2d51dec
        PROFILE=CONFIG_TARGET_ramips_mt7621_DEVICE_ubnt-erx-sfp
        ARTIFACT=openwrt-ramips-mt7621-ubnt-erx-sfp-initramfs-factory.tar ;;
    final)
        REF=v25.12.5; SHA=f0a60eee2fe051741c643ea6118718aae1ef17fb
        PROFILE=CONFIG_TARGET_ramips_mt7621_DEVICE_ubnt_edgerouter-x-sfp
        ARTIFACT=openwrt-ramips-mt7621-ubnt_edgerouter-x-sfp-squashfs-sysupgrade.bin
        ROUTING_URL=https://github.com/parasew/routing.git
        ROUTING_SHA=e714af7785e3f1b1105b2c9a0dfa9c7df209587d ;;
    recovery)
        REF=v25.12.5; SHA=f0a60eee2fe051741c643ea6118718aae1ef17fb
        PROFILE=CONFIG_TARGET_ramips_mt7621_DEVICE_ubnt_edgerouter-x-sfp
        ARTIFACT=openwrt-ramips-mt7621-ubnt_edgerouter-x-sfp-initramfs-kernel.bin ;;
    *) sed -n '2,10p' "$0"; exit 2 ;;
esac

FILES="$(mktemp -d)"
trap 'rm -rf "$FILES"' EXIT

# ---------------------------------------------------------------- per kind

if [[ "$KIND" == bridge ]]; then
    # The bridge's only job is remote access after the EdgeOS reboot:
    # root login over IPv6 link-local on the WAN port (eth0).
    AUTHORIZED_KEYS=""
    if [[ "${1:-}" == --authorized-keys ]]; then
        [[ -n "${2:-}" ]] || die "--authorized-keys needs a file"
        AUTHORIZED_KEYS="$2"; shift 2
    fi
    [[ -z "${AUTHORIZED_KEYS}" && "${1:-}" != "" ]] && die "unknown argument: $1"
    command -v openssl >/dev/null || die "missing tool: openssl"
    [[ ! -t 0 ]] || die "pipe the bridge root password on stdin, e.g. printf '%s\n' admin | ./build.sh bridge"
    IFS= read -r password || true
    [[ -n "$password" ]] || die "empty bridge root password"
    # OpenWrt 19.07's size-optimised musl only verifies MD5-crypt hashes.
    hash="$(printf '%s\n' "$password" | openssl passwd -1 -stdin)"
    unset password
    mkdir -p "$FILES/etc/uci-defaults"
    sed "s|@ROOT_HASH@|$hash|" "$HERE/bridge-access.sh" > "$FILES/etc/uci-defaults/99-bridge-access"
    if [[ -n "$AUTHORIZED_KEYS" ]]; then
        [[ -f "$AUTHORIZED_KEYS" ]] || die "no such file: $AUTHORIZED_KEYS"
        # Dropbear 2019.78 knows RSA and ECDSA, not ed25519. Options in front
        # of a key are kept, indentation and comments dropped.
        keys="$FILES/etc/dropbear/authorized_keys"
        mkdir -p "$(dirname "$keys")"
        tr -d '\r' < "$AUTHORIZED_KEYS" | grep -v -E '^[[:space:]]*(#|$)' | sed -E 's/^[[:space:]]+//' > "$keys" || true
        [[ -s "$keys" ]] || die "$AUTHORIZED_KEYS has no keys"
        if unusable="$(grep -v -E '(^|[[:space:]])(ssh-rsa|ecdsa-sha2-nistp(256|384|521))[[:space:]]+AAAA' "$keys")"; then
            die "the 19.07 bridge's Dropbear takes only RSA/ECDSA keys; not usable: ${unusable:0:60}"
        fi
        chmod 600 "$keys"
    fi
fi

[[ "$KIND" == bridge || "$#" -eq 0 ]] || die "unknown argument: $1"

# ------------------------------------------------------------- build host

[[ "$(uname -s)" == Linux ]] || die "OpenWrt builds need Linux"
JOBS="${JOBS:-$(nproc)}"
[[ "$(id -u)" -ne 0 ]] || die "do not build as root"
for tool in git make gcc g++ python3 rsync unzip wget file bzip2 patch diff perl openssl; do
    command -v "$tool" >/dev/null || die "missing build tool: $tool"
done

mkdir -p "$HERE/work"
free_gb=$(( $(df -Pk "$HERE/work" | awk 'NR == 2 { print $4 }') / 1048576 ))
(( free_gb >= 20 )) || say "WARNING: only ${free_gb} GB free in $HERE/work; a first build of one image needs about 20 GB"
TREE="$HERE/work/openwrt-$REF-$KIND"   # one tree per image: no objects shared between package sets
OUT="$HERE/out/$KIND"

# ------------------------------------------------------------ source tree

if [[ ! -d "$TREE/.git" ]]; then
    say "cloning OpenWrt $REF"
    git clone https://github.com/openwrt/openwrt.git "$TREE"
fi
cd "$TREE"
git rev-parse --verify -q HEAD >/dev/null || die "$TREE is an incomplete clone; remove it and run again"
git fetch --tags origin
git checkout -f --detach "$REF"
git clean -fdq -e dl -e staging_dir -e build_dir -e key-build\* -e feeds
[[ "$(git rev-parse HEAD)" == "$SHA" ]] || die "$REF is not the pinned commit $SHA"
# All trees share one download directory (archive names carry their versions).
mkdir -p "$HERE/work/dl"
if [[ ! -L dl ]]; then
    if [[ -d dl ]]; then cp -an dl/. "$HERE/work/dl/" && rm -rf dl; fi
    ln -s ../dl dl
fi

if [[ "$KIND" == bridge ]]; then
    # 19.07 predates Python 3 only hosts and current GCC.
    patch --batch -p1 < "$HERE/patches/1907-python3-host-prereq.patch"
    patch --batch -p1 < "$HERE/patches/1907-scons-python3.patch"
    # -p keeps the mtime: OpenWrt hashes patch mtimes into the host cmake stamp
    cp -p "$HERE/patches/1907-cmake-cstdint.patch" tools/cmake/patches/150-cstdint.patch
elif [[ "$KIND" == final ]]; then
    say "feeds (routing pinned to $ROUTING_SHA)"
    # The release tag pins every feed to a commit (name^sha); only routing is
    # replaced by the pinned fork with OONF olsrd2.
    sed -E "s|^src-git(-full)? routing .*|src-git routing $ROUTING_URL^$ROUTING_SHA|" \
        feeds.conf.default > feeds.conf
    if grep -E '^src-git' feeds.conf | grep -v -E '\^[0-9a-f]{40}[[:space:]]*$'; then
        die "the feed(s) above are not pinned to a commit"
    fi
    ./scripts/feeds update -a
    ./scripts/feeds install -a
    git -C feeds/routing checkout -f -q "$ROUTING_SHA"   # drop patches from a previous run
    [[ "$(git -C feeds/routing rev-parse HEAD)" == "$ROUTING_SHA" ]] || die "routing feed not at $ROUTING_SHA"
    patch --batch -d feeds/routing/olsrd -p1 < "$HERE/patches/olsrd-nameservice-hosts.patch"
fi

rm -rf files && cp -a "$FILES" files

say "configuration"
tr -d '\r' < "$HERE/$KIND.seed" > .config   # tolerate CRLF seeds
make defconfig
grep -qx "$PROFILE=y" .config || die "device profile $PROFILE is not selected"
while read -r line; do   # every seeded package must survive defconfig
    grep -qx -- "$line" .config || die "seed line lost in defconfig: $line"
done < <(tr -d '\r' < "$HERE/$KIND.seed" | grep -E '^CONFIG_(PACKAGE|DROPBEAR)_[^=]+=y$')
while read -r symbol; do   # and every disabled one (kernel size options) stays off
    ! grep -q -E "^$symbol=[ym]$" .config || die "seed disables $symbol, defconfig enabled it"
done < <(tr -d '\r' < "$HERE/$KIND.seed" | sed -n -E 's/^# (CONFIG_[A-Za-z0-9_]+) is not set$/\1/p')

# ------------------------------------------------------------------ build

say "building host tools"
make -j"$JOBS" download
make -j"$JOBS" tools/compile toolchain/compile || make -j1 V=s tools/compile toolchain/compile

# In nested user namespaces (Nix buildFHSEnv, bubblewrap) chown returns
# EINVAL, which fakeroot does not fake; use a real user namespace instead.
fakeroot_bin=staging_dir/host/bin/fakeroot
probe="$(mktemp)"
if ! "$fakeroot_bin" chown root:root "$probe" 2>/dev/null && unshare --user --map-root-user true 2>/dev/null; then
    say "fakeroot cannot chown here; wrapping it with unshare --map-root-user"
    printf '#!/bin/sh\nexec %s --user --map-root-user -- "$@"\n' "$(command -v unshare)" > "$fakeroot_bin"
fi
rm -f "$probe"

say "building $KIND image"
make -j"$JOBS" world || make -j1 V=s world

# ----------------------------------------------------------------- output

image="bin/targets/ramips/mt7621/$ARTIFACT"
[[ -f "$image" ]] || die "expected artifact missing: $image"
if [[ "$KIND" == bridge ]]; then
    kernel_bytes="$(tar -xOf "$image" vmlinux.tmp | wc -c)"
    (( kernel_bytes > 0 && kernel_bytes <= 3145728 )) || die "bridge kernel is $kernel_bytes bytes; an EdgeOS slot holds 3145728"
fi
rm -rf "$OUT" && mkdir -p "$OUT"   # only this build's files
cp "$image" "$OUT/"
cp .config "$OUT/config.built"
(cd "$OUT" && sha256sum "$ARTIFACT" config.built > sha256sums)
say "done: $OUT/$ARTIFACT"
cat "$OUT/sha256sums"
