# Restoring EdgeOS (lab procedure, needs a serial console)

There is no remote way back from OpenWrt to EdgeOS. This procedure restores
EdgeOS on a router you can reach with a serial console (3.3 V TTL, 57600 8N1,
no flow control) and a TFTP server on the same Ethernet segment as `eth0`.
It was run successfully from the OpenWrt 25.12 flash layout on 2026-09-27.

The serial console is `ttyS1` for U-Boot/EdgeOS but `ttyS0` inside OpenWrt,
hence `console=ttyS0` in the boot arguments below.

Below, `ROUTER_IP` and `HOST_IP` are two free addresses of one subnet on
the segment that links your host to the router's `eth0`.

## What you need per router

A backup taken **before** the migration, from a RAM recovery system (never
from running EdgeOS, whose root filesystem changes while you read it):

| File | Size | Content |
|---|---|---|
| `kernel12.bin` | 6,291,456 bytes | `Kernel1` + `Kernel2` |
| `troot-known-good.volume` | Record its exact byte count | the UBI volume `troot` |
| boot selector | `00` or `01` | byte 160 of `factory` at backup time: which kernel slot EdgeOS was running from |

Keep the SHA-256 values and the selector with the backup; `restore_edgeos.sh`
refuses to write without them. The selector matters because EdgeOS switches
kernel slots on every update: `troot` only fits the kernel that was running.

## Steps

1. **Boot a RAM recovery system.** Power-cycle the router and press `4` in
   the U-Boot menu (command line). Never use `1` (it writes to flash on this
   board) and never run `saveenv`. Then, with the recovery image from
   `build/build.sh recovery` on your TFTP server (the official initramfs
   lacks `nand-utils`):

   ```
   setenv ipaddr ROUTER_IP
   setenv serverip HOST_IP
   tftpboot 0x80A00000 openwrt-ramips-mt7621-ubnt_edgerouter-x-sfp-initramfs-kernel.bin
   setenv bootargs console=ttyS0,57600 rootfstype=squashfs,jffs2
   bootm 0x80A00000
   ```

   Check that the transferred byte count equals the file size before `bootm`.

2. **Reach it.** Wait until the network is up (`br-lan` port messages on the
   console, about 30 s after the kernel starts), then run
   `ip addr add ROUTER_IP/24 dev eth0` and `/etc/init.d/firewall stop`
   (`eth0` is in the WAN zone; the RAM system is gone after the next reboot).
   Until the boot has settled, `netifd` can remove the address again: wait
   ~10 s and check with `ip addr show eth0` before connecting. Copy
   `restore_edgeos.sh`, `lib/` and `kernel12.bin` to `/tmp`.

3. **Serve `troot` over HTTP** (it does not fit in RAM):
   `python3 -m http.server 8080 --bind HOST_IP --directory BACKUP_DIR`

4. **Restore:**

   ```sh
   ALLOW_DESTRUCTIVE_WRITE=1 sh /tmp/restore_edgeos.sh --confirm-UBI-WRITE \
     --kernel /tmp/kernel12.bin \
     --troot-url http://HOST_IP:8080/troot-known-good.volume \
     --troot-size TROOT_SIZE_IN_BYTES \
     --selector SELECTOR \
     --expected-kernel-sha256 KERNEL_SHA256 \
     --expected-troot-sha256 TROOT_SHA256
   ```

5. `sync && reboot -f`, then check EdgeOS on the console and on the network.

## What the script does, in order

1. Verifies both hashes; requires the recovery system's partition names
   (one 6 MiB `kernel`, `ubi`, `factory`) and refuses anything else; refuses
   bad blocks, a mounted UBI root, and a UBI geometry that does not fit the
   backup.
2. On the OpenWrt layout: removes `rootfs` and `rootfs_data`, creates `troot`
   with exactly the backup's size, streams it in and verifies the readback.
3. Writes the kernel backup and verifies it byte for byte.
4. Sets the boot selector (byte 160 of `factory`) to the backup's value if
   it differs, by rewriting that one erase block with fresh ECC and comparing
   the whole block afterwards.
5. Does not reboot by itself.
