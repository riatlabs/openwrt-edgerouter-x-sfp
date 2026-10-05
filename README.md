# EdgeRouter X SFP: EdgeOS → current OpenWrt, remotely

Moves an Ubiquiti EdgeRouter X SFP from **EdgeOS** (tested: 1.10.11, 2.0.6,
2.0.9) to **current OpenWrt (25.12)** over SSH only: no TFTP, no serial console, no local
re-cabling. EdgeOS first flashes the 19.07 RAM bridge; the bridge then flashes
OpenWrt 25.12, avoiding the 18.06 → 22.03 → 24.10 upgrade chain. It is
meant for an operator who reaches many routers remotely, one at a time. Each
step is started by hand. `bridge` writes the bridge image when invoked and
asks before rebooting; `flash` asks before erasing EdgeOS.

## How it works

```
 EdgeOS 1.10 – 2.0.9           19.07 "bridge" (runs in RAM)        OpenWrt 25.12
 ──────────────────── reboot ──────────────────────────── reboot ─────────────
 erx-migrate bridge            erx-migrate flash                  erx-migrate verify
  • checks board/flash          • writes the new 6 MiB kernel
  • installs the bridge the       layout + your sysupgrade image
    way EdgeOS installs its     • drops access.tgz into the new
    own updates                   config partition
```

1. **EdgeOS installs a tiny OpenWrt 19.07 image** through its own firmware
   updater (`add system image`). This works because the bridge is built in
   EdgeOS's factory format with a kernel that fits one 3 MiB EdgeOS kernel
   slot. EdgeOS switches its boot selector to it; the tool then asks before
   rebooting.
2. **The bridge boots and runs entirely from RAM.** Nothing on flash is in
   use, so the whole flash can be rewritten safely. It answers on IPv6
   link-local on the WAN port (`eth0`) as `root`.
3. **From the bridge, `target/erx-flash.sh` writes OpenWrt**: the kernel
   across both old slots (OpenWrt ≥ 24.10 needs one 6 MiB kernel partition),
   the boot selector, and the root filesystem in place of EdgeOS's. Every
   write is read back before the next one starts.
4. **OpenWrt 25.12 boots.** If `flash` received `--config access.tgz`, OpenWrt
   applies the archive on its first boot. It then accepts the public keys you
   supplied to `access-config` over IPv6 link-local on `eth0`.
   From now on it is a normal OpenWrt: later updates use plain `sysupgrade`.

## Why this approach (and not the others)

| Approach | Result for remote ER-X-SFPs |
|---|---|
| [damadmai/edgemax_openwrt](https://github.com/damadmai/edgemax_openwrt) | Works, but: EdgeOS 2.0.6 + bootloader, then an OpenWrt factory tar, then sysupgrades to 21.02 and 23.05 (several flashes), and after the first OpenWrt boot `eth0` is a firewalled WAN port, so you must re-plug to `eth1`. Not possible remotely. |
| [darkxst/erx-migration](https://github.com/darkxst/erx-migration) | Solves the 3 MiB → 6 MiB kernel layout change, but only from an already running OpenWrt (21.02–23.05). `target/erx-flash.sh` is derived from its `ubnt_erx_migrate.sh`/`stage2`. |
| OpenWrt wiki: initramfs/factory, then sysupgrade | Same re-plugging problem as above, plus a separate 24.10 migration. |
| U-Boot TFTP boot | Needs serial console and a TFTP server on the local segment. Used here only for lab recovery. |
| `kexec` from EdgeOS | Not possible: the EdgeOS 4.14 kernel returns `ENOSYS` for `kexec_load`. |
| Flash directly from running EdgeOS (`pivot_root` out of the root filesystem) | `pivot_root` works, but EdgeOS keeps its root squashfs loop device busy, so the flash can never be released safely. Abandoned. |
| **19.07 RAM bridge (this tool)** | One EdgeOS-native update, one OpenWrt flash, reachable the whole time on the same port. |

## What you need

- A Linux host with Python 3 and OpenSSH ≥ 7.6 (for `accept-new`) that can reach the router's `eth0`
  over IPv6 link-local (for example from a neighbouring node).
  A non-standard SSH port goes into `~/.ssh/config` (`Host`/`Port`); the
  tool passes the router argument to `ssh` unchanged.
- SSH login to EdgeOS as a user with passwordless `sudo` (EdgeOS admin
  users such as `ubnt` have it); `check` reads the NAND counters with
  `sudo -n`.
- The bridge image: `build/build.sh bridge` (see [Building](#building)).
- The final image: an ER-X-SFP sysupgrade image, for example the official
  `openwrt-25.12.x-ramips-mt7621-ubnt_edgerouter-x-sfp-squashfs-sysupgrade.bin`
  or your own from `build/build.sh final` (adds OLSR v1/v2).
- An `access.tgz` with your SSH public keys (next section).

The bridge accepts `root` with the password supplied to `build.sh bridge`.
There is no built-in `admin` password. With `--config access.tgz`, the final
OpenWrt installation uses its keys and disables SSH password login.

## Migrating one router

**If your access uses a PoE-powered antenna, explicitly enable its PoE port
in both the bridge build and `access-config` before migrating.** Follow
[PoE and access through neighbouring nodes](#poe-and-access-through-neighbouring-nodes).

```sh
# 0. once: the first-boot config that keeps OpenWrt reachable on eth0
#    (ssh-ed25519 or ssh-rsa keys only: OpenWrt's Dropbear has no ECDSA;
#    options in front of a key are copied as they are)
./erx-migrate access-config --authorized-keys ~/.ssh/admins.pub -o access.tgz
#    per-device files (network, olsrd, ...) can be added with --files DIR

# 1. read-only: is this an ER-X-SFP with a healthy flash?
./erx-migrate check  'ubnt@fe80::211:22ff:fe33:4455%eth0'

# 2. install the bridge through EdgeOS; REBOOT confirms only the reboot
./erx-migrate bridge 'ubnt@fe80::211:22ff:fe33:4455%eth0' bridge.tar

# 3. from the bridge: write OpenWrt and reboot (asks: FLASH)
#    the bridge uses the router's base MAC + 1 on eth0, so its link-local
#    address differs from EdgeOS's (...4456 instead of ...4455 here)
./erx-migrate flash  'root@fe80::211:22ff:fe33:4456%eth0' sysupgrade.bin --config access.tgz

# 4. no flash writes: is it OpenWrt with the new layout? does sysupgrade -T accept
#    the next image? (the router has a new SSH host key now; verify keeps it
#    in its own known_hosts file under ~/.cache/erx-migrate/)
./erx-migrate verify 'root@fe80::211:22ff:fe33:4455%eth0' sysupgrade.bin
```

`bridge.tar` and `sysupgrade.bin` are local image paths to replace with your
actual files. `build.sh` puts its images under `build/out/`; an official
sysupgrade image can also be used.

`~/.ssh/admins.pub` must contain one or more SSH **public** keys, one per line
in `authorized_keys` format. If you already have an ed25519 public key, you
can use `cp ~/.ssh/id_ed25519.pub ~/.ssh/admins.pub`. `access-config` puts
those keys in `etc/dropbear/authorized_keys` inside `access.tgz`; `flash --config`
transfers the archive for restoration on OpenWrt's first boot. Keep the
private key on your host. For remote access after migration, pass this archive
with `--config` when running `flash`.

Find a router's link-local address with `ping -6 ff02::1%eth0`. After each
reboot give the router a minute or two before the next step; the first boot
of a changed system (bridge, OpenWrt) takes longer than usual. Replace
`eth0` after `%` with the name of your host's interface.

`erx-migrate` keeps one SSH connection per router, so a bridge password is
typed once. The bridge gets a new host key on every boot; `flash` therefore
uses a separate `known_hosts` file under `~/.cache/erx-migrate/`. The writer
on the bridge runs detached from the SSH session and logs to
`/tmp/erx-flash.log`, so a dropped connection does not stop it half-way;
`flash` reports success only when the writer has printed its final line.

### Checks before flash writes

| Step | Refuses when |
|---|---|
| `check`, `bridge` | board is not `e51` (ER-X-SFP); `eeprom`/`Kernel1` (EdgeOS 1.x: `Kernel`)/`Kernel2`/`RootFS` missing; `nanddump` reports bad blocks or ECC failures on `eeprom`, `Kernel1`, `Kernel2`; boot selector unreadable; bridge kernel > 3 MiB or its MD5 wrong |
| `flash` (on the bridge) | not running from RAM; not an ER-X-SFP; not the 2 × 3 MiB layout; bad blocks/ECC failures; image or config hash differs from what the host sent; image is for another board; kernel too large |
| during `flash` | stops at the first write whose readback differs, and says whether it is safe to reboot |

## After the migration

- The router boots OpenWrt with the image's defaults plus `access.tgz`: key-
  only SSH, and IPv6 link-local accepted on `eth0` (WAN). Put the rest of the
  per-device configuration into `access.tgz` (`--files`) or apply it over SSH.
  The access rule matches the firewall zone `wan`: a custom `etc/config/network`
  or `firewall` in `--files` must keep `eth0` in a zone named `wan` with IPv6
  enabled. `access-config` refuses files in which `eth0` (directly or as a
  bridge port) is not in a network of the `wan` zone, and symbolic links; it
  drops group/other write permission.
- Future upgrades: plain `sysupgrade` (`verify` runs `sysupgrade -T` on the
  image you give it; tested with the official 25.12.5 image).

## PoE and access through neighbouring nodes

The default networks keep `eth0` in WAN and `eth1`–`eth4` in LAN. The bridge
and `access.tgz` allow SSH on TCP port 22 from IPv6 link-local addresses in
all firewall zones. The LAN ports use the LAN bridge's link-local address;
it can differ from WAN's address. Custom network files must keep the chosen
management ports up with IPv6 enabled. A routed connection through an antenna
needs a configured address and routes; link-local addresses stay on one
Layer-2 segment. SFP (`eth5`) access has not been verified.

**Passive PoE on `eth0`–`eth4` defaults to off and must be explicitly enabled
for every port that needs to power an antenna.** The EdgeOS PoE settings are
not copied automatically. Before installing the bridge, use the same
comma-separated port list in **both** commands below. Omitting `--poe-ports`
leaves the outputs off in that stage; enabling PoE only in the final config
does not power the antenna while the bridge is running.

For an antenna powered through `eth4`:

```sh
printf '%s\n' 'your-chosen-bridge-password' | build/build.sh bridge --poe-ports eth4
./erx-migrate access-config --authorized-keys ~/.ssh/admins.pub --poe-ports eth4 -o access.tgz
```

Only select ports connected to equipment that supports the router's passive
PoE. The startup helper uses the board's GPIO configuration, enables the
selected ports and checks the GPIO readback. The GPIO start service enables
PoE ports in board configuration order (`eth0` to `eth4` by default), with
a **0.5-second pause between enabled outputs**, on first boot and every
subsequent boot or service restart. Disabled ports add no pause. This also
applies to the RAM bridge. Rebuild the bridge and regenerate `access.tgz`
to include this service; older artifacts do not provide the staggered start.
An antenna may lose power during
a reboot and needs time to start again. GPIO readback alone does not prove
voltage or antenna operation.

On one lab router, the selected GPIO was enabled on both the bridge and
OpenWrt, and remained enabled after an OpenWrt cold boot. A complete test
through a PoE-powered antenna is still outstanding. `check` and `bridge`
accept EdgeOS configurations with PoE enabled. Select the ports you need
in both the bridge build and `access-config` so they are powered again
after each reboot.

## Rollback: why there is no remote way back to EdgeOS

Restoring EdgeOS was done four times in the lab (`recovery/`), but only with
a serial console. Remotely it does not work today, for three reasons:

1. **`flash` deletes EdgeOS from the router.** EdgeOS keeps its kernel in
   the two 3 MiB kernel slots and its whole system (squashfs image plus the
   configuration overlay) in the UBI volume `troot`. `flash` replaces both;
   OpenWrt keeps no copy.
2. **EdgeOS has to come from outside.** Either a per-device backup
   (`kernel12.bin`, 6 MiB, and the `troot` volume, 246 MB) taken *before* the
   migration from a system running in RAM, or a fresh EdgeOS rebuilt from an
   official image (then with factory settings, i.e. without the old
   configuration). `erx-migrate` takes no backup today.
3. **The writer has to run from RAM.** A running OpenWrt uses the very UBI
   volumes that would have to be replaced. In the lab the RAM system is an
   OpenWrt image started by U-Boot over TFTP; choosing that in U-Boot's menu
   needs the serial console, which a remote router does not have.

It is possible in principle and just not built: OpenWrt's own `sysupgrade`
already switches into a RAM root before it writes flash, and the same
mechanism could run the restore remotely; and before `flash`, while the
bridge runs, EdgeOS is still on the flash (only its boot selector and image
names point at the bridge). Neither path is implemented or tested.

Between `bridge` and `flash` the router boots the bridge on every power
cycle. If the bridge does not come up reachable, the router needs a site
visit — which is why `check` refuses anything unusual first.

## Building

Linux only; the OpenWrt build system needs a case-sensitive filesystem and
the usual build packages (`git make gcc g++ python3 rsync unzip wget file
bzip2 patch perl openssl`).

```sh
cd build
printf '%s\n' 'your-chosen-bridge-password' | ./build.sh bridge [--authorized-keys keys.pub]
./build.sh final       # optional: your own 25.12 image with OLSR v1/v2
./build.sh recovery    # only for the lab restore in recovery/
```

The bridge accepts `root` with that password; `--authorized-keys` adds
RSA/ECDSA keys (its Dropbear 2019.78 cannot use ed25519). All builds verify
the pinned OpenWrt commit and that every seeded package survived
`make defconfig`; the bridge build fails if its kernel exceeds 3 MiB.

## Hardware verification

On one ER-X-SFP, the complete migration and serial-console restore were
tested with these starting EdgeOS versions and the official OpenWrt 25.12.5
image:

| Starting EdgeOS | `check` / `bridge` | `flash` | first boot, `verify`, `sysupgrade -T` |
|---|---|---|---|
| 2.0.9-hotfix.7 | done (bridge in Kernel2, selector 01 → 00 written by `flash`) | done | done |
| 2.0.6 | done (bridge in Kernel1) | done | done |
| 1.10.11 (Linux 3.10) | done (bridge in Kernel1) | done | done |

The custom image from `build.sh final`, the recovery image from
`build.sh recovery`, the detached flash writer and both selector-write directions were
also tested on that unit. The factory partition matched its backup after the
selector tests.

Other routers, NAND chips and EdgeOS configurations remain untested. `check`
and `flash` refuse bad blocks or ECC failures in `eeprom`/`factory` and the
kernel slots.

From this directory, run `python3 -m pytest tests`. The tests cover the host
tool, builds, flash writer and restore logic. Before using an image on another
router, check the boot and SSH access on hardware.
