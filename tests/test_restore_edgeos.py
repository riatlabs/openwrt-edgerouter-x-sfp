from hashlib import sha256
from pathlib import Path
import os
import subprocess
import pytest


ROOT = Path(__file__).parents[1] / "recovery"
RESTORE = ROOT / "restore_edgeos.sh"


# Kernel1 + Kernel2 backup: a uImage header at the start of each 3 MiB slot.
KERNEL12 = (b"\x27\x05\x19\x56" + b"K" * (3_145_728 - 4)) * 2


def write_executable(path: Path, content: str):
    path.write_text(content)
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("bad_partition", "expected_error"),
    [
        (None, "TROOT_IMAGE_LEB_MISMATCH"),
        # without volumes the LEB size comes from the device's eraseblock_size,
        # which the kernel defines as the LEB size (PEB minus the UBI headers)
        ("no-volumes", "is not a multiple of 126976"),
        ("mtd2", "NAND_BAD_BLOCK_DETECTED"),
        ("mtd3", "NAND_BAD_BLOCK_DETECTED"),
        ("missing-mtd2", "NAND_BAD_BLOCK_COUNT_UNAVAILABLE"),
        ("missing-mtd3", "NAND_BAD_BLOCK_COUNT_UNAVAILABLE"),
        ("mounted-ubiblock", "is currently mounted"),   # a squashfs root on ubiblock0_0
    ],
)
def test_bad_mtd_state_or_troot_geometry_aborts_before_any_write(
    tmp_path: Path, bad_partition: str | None, expected_error: str
):
    mockbin = tmp_path / "bin"
    mockbin.mkdir()
    destructive_log = tmp_path / "destructive-calls.log"

    write_executable(mockbin / "id", "#!/bin/sh\necho 0\n")
    write_executable(mockbin / "wget", "#!/bin/sh\ncat \"$TEST_TROOT\"\n")
    for command in ("mtd", "ubirmvol", "ubimkvol", "ubiupdatevol"):
        write_executable(
            mockbin / command,
            "#!/bin/sh\n"
            f"printf '%s %s\\n' '{command}' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
            + ("echo 'mock: UBI volume is too small' >&2\nexit 1\n" if command == "ubiupdatevol" else "exit 0\n"),
        )

    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(KERNEL12)
    troot = tmp_path / "invalid-troot.volume"
    troot.write_bytes(b"not-a-logical-LEB-image")

    proc_mtd = tmp_path / "proc-mtd"
    proc_mtd.write_text(
        "dev:    size   erasesize  name\n"
        'mtd0: 00080000 00020000 "Bootloader"\n'
        'mtd1: 00060000 00020000 "Config"\n'
        'mtd2: 00060000 00020000 "factory"\n'
        'mtd3: 00600000 00020000 "kernel"\n'
        'mtd4: 0f8c0000 00020000 "ubi"\n'
    )
    sysfs = tmp_path / "sys"
    ubi_device = sysfs / "class/ubi/ubi0"
    ubi_device.mkdir(parents=True)
    for mtd_name in ("mtd2", "mtd3"):
        mtd_sysfs = sysfs / "class/mtd" / mtd_name
        mtd_sysfs.mkdir(parents=True)
        if bad_partition != f"missing-{mtd_name}":
            (mtd_sysfs / "bad_blocks").write_text(
                "1\n" if mtd_name == bad_partition else "0\n"
            )
    (sysfs / "class/mtd/mtd2/writesize").write_text("2048\n")
    (sysfs / "class/mtd/mtd2/erasesize").write_text("131072\n")
    (sysfs / "class/mtd/mtd2/ecc_failures").write_text("0\n")
    (sysfs / "class/mtd/mtd2/corrected_bits").write_text("0\n")
    (ubi_device / "mtd_num").write_text("4\n")
    (ubi_device / "avail_eraseblocks").write_text("1\n")
    (ubi_device / "eraseblock_size").write_text("126976\n")
    volumes = () if bad_partition == "no-volumes" else ((0, "rootfs", 25), (1, "rootfs_data", 1913))
    for index, name, reserved in volumes:
        volume = sysfs / f"class/ubi/ubi0_{index}"
        volume.mkdir()
        (volume / "name").write_text(f"{name}\n")
        (volume / "data_bytes").write_text("1\n")
        (volume / "reserved_ebs").write_text(f"{reserved}\n")
        (volume / "usable_eb_size").write_text("126976\n")

    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "mtd3").write_bytes(b"mock kernel MTD")
    (dev / "mtd2ro").write_bytes(b"\x00" * 131_072)

    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/ubiblock0_0 /rom squashfs ro 0 0\n" if bad_partition == "mounted-ubiblock"
                      else "/dev/root /rom squashfs ro 0 0\n")
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=ttyS0\n")

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{mockbin}:{environment['PATH']}",
            "DESTRUCTIVE_LOG": str(destructive_log),
            "ALLOW_DESTRUCTIVE_WRITE": "1",
            "TEST_TROOT": str(troot),
            "PROC_MTD_PATH": str(proc_mtd),
            "SYS_PATH": str(sysfs),
            "MOUNTS_PATH": str(mounts),
            "CMDLINE_PATH": str(cmdline),
            "MTD_DEV_DIR": str(tmp_path / "dev"),
        }
    )
    result = subprocess.run(
        [
            "sh",
            str(RESTORE),
            "--confirm-UBI-WRITE",
            "--kernel",
            str(kernel),
            "--troot-url",
            "http://test/troot",
            "--troot-size",
            f"  {troot.stat().st_size}",  # as BSD wc -c prints it
            "--selector",
            "00",
            "--expected-kernel-sha256",
            sha256(kernel.read_bytes()).hexdigest(),
            "--expected-troot-sha256",
            sha256(troot.read_bytes()).hexdigest(),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not destructive_log.exists() or destructive_log.read_text() == ""


@pytest.mark.parametrize(
    ("corrupt_kernel", "initial_selector", "missing_selector_tool"),
    [
        (True, 1, None),
        (False, 1, None),
        (False, 0, None),
        (False, 1, "flash_erase"),
        (False, 1, "nandwrite"),
    ],
)
def test_unified_kernel_restore_uses_byte_readback_before_selector_write(
    tmp_path: Path, corrupt_kernel: bool, initial_selector: int, missing_selector_tool: str | None
):
    mockbin = tmp_path / "bin"
    mockbin.mkdir()
    dev = tmp_path / "dev"
    dev.mkdir()
    sysfs = tmp_path / "sys"
    ubi = sysfs / "class/ubi/ubi0"
    volume = sysfs / "class/ubi/ubi0_0"
    mtd_factory = sysfs / "class/mtd/mtd2"
    mtd_kernel = sysfs / "class/mtd/mtd3"
    for directory in (ubi, volume, mtd_factory, mtd_kernel):
        directory.mkdir(parents=True)
    (mtd_factory / "bad_blocks").write_text("0\n")
    (mtd_kernel / "bad_blocks").write_text("0\n")
    (mtd_factory / "ecc_failures").write_text("0\n")
    (mtd_factory / "corrected_bits").write_text("0\n")
    (mtd_factory / "erasesize").write_text("131072\n")
    (mtd_factory / "writesize").write_text("2048\n")

    destructive_log = tmp_path / "destructive-calls.log"
    write_executable(mockbin / "id", "#!/bin/sh\necho 0\n")
    write_executable(mockbin / "wget", "#!/bin/sh\ncat \"$TEST_TROOT\"\n")
    write_executable(
        mockbin / "ubiupdatevol",
        "#!/bin/sh\n"
        "printf 'ubiupdatevol %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        "cp \"$3\" \"$TEST_DEV/ubi0_0\"\n",
    )
    for command in ("ubirmvol", "ubimkvol"):
        write_executable(
            mockbin / command,
            "#!/bin/sh\n"
            f"printf '{command} %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n",
        )
    write_executable(
        mockbin / "mtd",
        "#!/bin/sh\n"
        "printf 'mtd %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        "if [ \"$1\" = -f ] && [ \"$2\" = write ]; then\n"
        "  cp \"$3\" \"$TEST_DEV/mtd3\"\n"
        "  if [ \"$CORRUPT_KERNEL\" = 1 ]; then printf X | dd of=\"$TEST_DEV/mtd3\" bs=1 seek=0 conv=notrunc 2>/dev/null; fi\n"
        "fi\n"
        "exit 0\n",
    )
    if missing_selector_tool != "flash_erase":
        write_executable(
            mockbin / "flash_erase",
            "#!/bin/sh\n"
            "printf 'flash_erase %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
            "[ \"$2\" = 0 ] && [ \"$3\" = 1 ] || exit 41\n"
            "dd if=/dev/zero of=\"$TEST_DEV/mtd2ro\" bs=131072 count=1 conv=notrunc 2>/dev/null\n",
        )
    if missing_selector_tool != "nandwrite":
        write_executable(
            mockbin / "nandwrite",
            "#!/bin/sh\n"
            "printf 'nandwrite %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
            "[ \"$1\" = -p ] || exit 42\n"
            "cp \"$3\" \"$TEST_DEV/mtd2ro\"\n",
        )
    write_executable(
        mockbin / "hexdump",
        "#!/bin/sh\n"
        "offset=$2; count=$4; file=$7\n"
        "dd if=\"$file\" bs=1 skip=\"$offset\" count=\"$count\" 2>/dev/null | od -An -tx1 | tr -d ' \\n'\n",
    )

    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(KERNEL12)
    troot = tmp_path / "troot.volume"
    troot.write_bytes(b"T" * 126_976)
    (dev / "mtd3").write_bytes(b"\xff" * 6_291_456)
    (dev / "mtd2ro").write_bytes(
        b"\x00" * 160 + bytes([initial_selector]) + b"\x00" * (131_072 - 161)
    )
    (dev / "ubi0_0").write_bytes(b"")
    (ubi / "mtd_num").write_text("4\n")
    (ubi / "avail_eraseblocks").write_text("0\n")
    (volume / "name").write_text("troot\n")
    (volume / "reserved_ebs").write_text("1\n")
    (volume / "usable_eb_size").write_text("126976\n")
    proc_mtd = tmp_path / "proc-mtd"
    proc_mtd.write_text(
        "dev:    size   erasesize  name\n"
        'mtd0: 00080000 00020000 "u-boot"\n'
        'mtd1: 00060000 00020000 "u-boot-env"\n'
        'mtd2: 00060000 00020000 "factory"\n'
        'mtd3: 00600000 00020000 "kernel"\n'
        'mtd4: 0f7c0000 00020000 "ubi"\n'
    )
    (tmp_path / "mounts").write_text("/dev/root /rom squashfs ro 0 0\n")
    (tmp_path / "cmdline").write_text("console=ttyS0,57600\n")

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{mockbin}:{environment['PATH']}",
            "DESTRUCTIVE_LOG": str(destructive_log),
            "TEST_DEV": str(dev),
            "CORRUPT_KERNEL": "1" if corrupt_kernel else "0",
            "ALLOW_DESTRUCTIVE_WRITE": "1",
            "TEST_TROOT": str(troot),
            "PROC_MTD_PATH": str(proc_mtd),
            "SYS_PATH": str(sysfs),
            "MOUNTS_PATH": str(tmp_path / "mounts"),
            "CMDLINE_PATH": str(tmp_path / "cmdline"),
            "MTD_DEV_DIR": str(dev),
            "DEV_PATH": str(dev),
        }
    )
    result = subprocess.run(
        [
            "sh",
            str(RESTORE),
            "--confirm-UBI-WRITE",
            "--kernel",
            str(kernel),
            "--troot-url",
            "http://test/troot",
            "--troot-size",
            str(troot.stat().st_size),
            "--selector",
            "00",
            "--expected-kernel-sha256",
            sha256(kernel.read_bytes()).hexdigest(),
            "--expected-troot-sha256",
            sha256(troot.read_bytes()).hexdigest(),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    calls = destructive_log.read_text() if destructive_log.exists() else ""
    if missing_selector_tool is not None:
        assert result.returncode != 0, result.stdout + result.stderr
        assert f"SELECTOR_TOOL_MISSING: {missing_selector_tool}" in result.stderr
        assert not destructive_log.exists() or destructive_log.read_text() == "", (
            "missing selector writer must be caught before any UBI/kernel write"
        )
    elif corrupt_kernel:
        assert "mtd -f write" in calls
        assert f"mtd -f write {kernel} kernel" in calls
        assert "mtd verify" not in calls
        assert result.returncode != 0, result.stdout + result.stderr
        assert "kernel readback verification failed" in result.stderr
        assert "flash_erase" not in calls, "selector block must not erase after a failed kernel readback"
    else:
        assert "mtd -f write" in calls
        assert f"mtd -f write {kernel} kernel" in calls
        assert "mtd verify" not in calls
        assert result.returncode == 0, result.stdout + result.stderr
        assert "EdgeOS rollback complete and fully verified." in result.stdout
        assert ("flash_erase /dev/mtd2 0 1" in calls) == (initial_selector != 0)
        assert ("nandwrite -p /dev/mtd2 " in calls) == (initial_selector != 0)
        assert (dev / "mtd2ro").read_bytes()[160] == 0


# "truncated": the dropped transfer also makes ubiupdatevol fail, as on hardware
# "early": ubiupdatevol fails without opening the fifo, the download is fine
@pytest.mark.parametrize("wget_fails", ["", "after-data", "truncated", "early"])
def test_reshape_removes_unmounted_ubiblock_before_removing_rootfs_volume(tmp_path: Path, wget_fails: str):
    mockbin = tmp_path / "bin"
    mockbin.mkdir()
    dev = tmp_path / "dev"
    dev.mkdir()
    sysfs = tmp_path / "sys"
    ubi = sysfs / "class/ubi/ubi0"
    block = sysfs / "class/block/ubiblock0_0"
    mtd_factory = sysfs / "class/mtd/mtd2"
    mtd_kernel = sysfs / "class/mtd/mtd3"
    for directory in (ubi, block, mtd_factory, mtd_kernel):
        directory.mkdir(parents=True)
    (mtd_factory / "bad_blocks").write_text("0\n")
    (mtd_kernel / "bad_blocks").write_text("0\n")
    (mtd_factory / "writesize").write_text("2048\n")
    (mtd_factory / "erasesize").write_text("131072\n")
    (mtd_factory / "ecc_failures").write_text("0\n")
    (mtd_factory / "corrected_bits").write_text("0\n")
    (ubi / "avail_eraseblocks").write_text("0\n")   # mtd_num appears when ubiattach runs

    for index, name, reserved in ((0, "rootfs", 1), (1, "rootfs_data", 1)):
        volume = sysfs / f"class/ubi/ubi0_{index}"
        volume.mkdir()
        (volume / "name").write_text(f"{name}\n")
        (volume / "reserved_ebs").write_text(f"{reserved}\n")
        (volume / "data_bytes").write_text("1\n")
        (volume / "usable_eb_size").write_text("126976\n")
        (dev / f"ubi0_{index}").write_bytes(b"")

    log = tmp_path / "destructive-calls.log"
    write_executable(mockbin / "id", "#!/bin/sh\necho 0\n")
    write_executable(mockbin / "wget", "#!/bin/sh\ncat \"$TEST_TROOT\"\n")
    for command in ("ubiattach", "ubidetach"):
        write_executable(
            mockbin / command,
            "#!/bin/sh\n"
            f"printf '{command} %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
            + ('printf "4\\n" > "$TEST_SYS/class/ubi/ubi0/mtd_num"\n' if command == "ubiattach" else ""),
        )
    write_executable(
        mockbin / "ubiblock",
        "#!/bin/sh\n"
        "printf 'ubiblock %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        "rm -r \"$TEST_SYS/class/block/ubiblock0_0\"\n",
    )
    write_executable(
        mockbin / "ubirmvol",
        "#!/bin/sh\n"
        "printf 'ubirmvol %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -N ]; then name=$2; shift 2; else shift; fi\n"
        "done\n"
        "case $name in rootfs) n=0 ;; rootfs_data) n=1 ;; *) exit 2 ;; esac\n"
        "rm -r \"$TEST_SYS/class/ubi/ubi0_$n\" \"$TEST_DEV/ubi0_$n\"\n",
    )
    write_executable(
        mockbin / "ubimkvol",
        "#!/bin/sh\n"
        "printf 'ubimkvol %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        "mkdir \"$TEST_SYS/class/ubi/ubi0_0\"\n"
        "printf 'troot\\n' > \"$TEST_SYS/class/ubi/ubi0_0/name\"\n"
        "printf '1\\n' > \"$TEST_SYS/class/ubi/ubi0_0/reserved_ebs\"\n"
        "printf '0\\n' > \"$TEST_SYS/class/ubi/ubi0_0/data_bytes\"\n"
        "printf '126976\\n' > \"$TEST_SYS/class/ubi/ubi0_0/usable_eb_size\"\n"
        ": > \"$TEST_DEV/ubi0_0\"\n",
    )
    write_executable(
        mockbin / "wget",
        "#!/bin/sh\n"
        "printf 'wget %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        "cat \"$TEST_TROOT\"\n"
        '[ -z "$WGET_FAILS" ] || [ "$WGET_FAILS" = early ] || exit 1\n',   # data arrived, then the transfer failed
    )
    write_executable(
        mockbin / "ubiupdatevol",
        "#!/bin/sh\n"
        "printf 'ubiupdatevol %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        '[ "$WGET_FAILS" != early ] || exit 1\n'
        "cp \"$3\" \"$1\"\n"
        '[ "$WGET_FAILS" != truncated ] || exit 1\n',
    )
    write_executable(
        mockbin / "mtd",
        "#!/bin/sh\n"
        "printf 'mtd %s\\n' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
        "if [ \"$1\" = -f ] && [ \"$2\" = write ]; then cp \"$3\" \"$TEST_DEV/mtd3\"; fi\n",
    )
    write_executable(
        mockbin / "hexdump",
        "#!/bin/sh\n"
        "dd if=\"$7\" bs=1 skip=\"$2\" count=\"$4\" 2>/dev/null | od -An -tx1 | tr -d ' \\n'\n",
    )

    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(KERNEL12)
    troot = tmp_path / "troot.volume"
    troot.write_bytes(b"T" * 126_976)
    (dev / "mtd3").write_bytes(b"\xff" * 6_291_456)
    (dev / "mtd2ro").write_bytes(b"\x00" * 131_072)
    proc_mtd = tmp_path / "proc-mtd"
    proc_mtd.write_text(
        "dev:    size   erasesize  name\n"
        'mtd0: 00080000 00020000 "u-boot"\n'
        'mtd1: 00060000 00020000 "u-boot-env"\n'
        'mtd2: 00060000 00020000 "factory"\n'
        'mtd3: 00600000 00020000 "kernel"\n'
        'mtd4: 0f7c0000 00020000 "ubi"\n'
    )
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/root /rom squashfs ro 0 0\n")
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=ttyS0,57600\n")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{mockbin}:{environment['PATH']}",
            "DESTRUCTIVE_LOG": str(log),
            "TEST_DEV": str(dev),
            "TEST_SYS": str(sysfs),
            "TEST_TROOT": str(troot),
            "WGET_FAILS": wget_fails,
            "ALLOW_DESTRUCTIVE_WRITE": "1",
            "TEST_TROOT": str(troot),
            "PROC_MTD_PATH": str(proc_mtd),
            "SYS_PATH": str(sysfs),
            "MOUNTS_PATH": str(mounts),
            "CMDLINE_PATH": str(cmdline),
            "MTD_DEV_DIR": str(dev),
            "DEV_PATH": str(dev),
        }
    )
    result = subprocess.run(
        [
            "sh",
            str(RESTORE),
            "--confirm-UBI-WRITE",
            "--kernel",
            str(kernel),
            "--troot-url",
            "http://test/troot",
            "--troot-size",
            str(troot.stat().st_size),
            "--selector",
            "00",
            "--expected-kernel-sha256",
            sha256(kernel.read_bytes()).hexdigest(),
            "--expected-troot-sha256",
            sha256(troot.read_bytes()).hexdigest(),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,   # a blocked download pipeline must not hang the restore
    )

    if wget_fails:
        expected = {"after-data": "TROOT_DOWNLOAD_FAILED",
                    "truncated": "wget also failed (status 1)",
                    "early": "TROOT_STREAM_UPDATE_FAILED: do not write kernel"}[wget_fails]
        assert result.returncode != 0 and expected in result.stderr, result.stderr
        assert "ubidetach" not in log.read_text(), "UBI stays attached for the investigation"
        assert not any(line.startswith("mtd ") for line in log.read_text().splitlines()), \
            "no kernel write after a failed download"
        return
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert "wget -T 30 -O - http://test/troot" in calls
    assert calls[0] == "ubiattach -m 4" and calls[-1] == "ubidetach -m 4"
    block_remove = next(i for i, call in enumerate(calls) if call.startswith("ubiblock -r"))
    rootfs_remove = next(
        i for i, call in enumerate(calls) if call.startswith(f"ubirmvol {dev}/ubi0 -N rootfs")
    )
    assert block_remove < rootfs_remove
    assert "EdgeOS rollback complete and fully verified." in result.stdout


def test_non_recovery_layout_is_refused_before_any_write(
    tmp_path: Path,
):
    mockbin = tmp_path / "bin"
    mockbin.mkdir()
    destructive_log = tmp_path / "destructive-calls.log"
    write_executable(mockbin / "id", "#!/bin/sh\necho 0\n")
    write_executable(mockbin / "wget", "#!/bin/sh\ncat \"$TEST_TROOT\"\n")
    for command in (
        "mtd",
        "mtd_debug",
        "ubirmvol",
        "ubimkvol",
        "ubiupdatevol",
        "ubiblock",
        "nandwrite",
        "flash_eraseall",
    ):
        write_executable(
            mockbin / command,
            "#!/bin/sh\n"
            f"printf '%s %s\\n' '{command}' \"$*\" >> \"$DESTRUCTIVE_LOG\"\n"
            "exit 0\n",
        )

    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(KERNEL12)
    troot = tmp_path / "troot.volume"
    troot.write_bytes(b"T")
    proc_mtd = tmp_path / "proc-mtd"
    proc_mtd.write_text(
        "dev:    size   erasesize  name\n"
        'mtd0: 00080000 00020000 "u-boot"\n'
        'mtd1: 00060000 00020000 "u-boot-env"\n'
        'mtd2: 00060000 00020000 "factory"\n'
        'mtd3: 00300000 00020000 "kernel1"\n'
        'mtd4: 00300000 00020000 "kernel2"\n'
        'mtd5: 0f7c0000 00020000 "ubi"\n'
    )
    sysfs = tmp_path / "sys"
    bad_blocks = {
        "mtd2": 0,
        "mtd3": 1,
        "mtd4": 0,
    }
    for mtd_name, count in bad_blocks.items():
        mtd_sysfs = sysfs / "class/mtd" / mtd_name
        mtd_sysfs.mkdir(parents=True)
        (mtd_sysfs / "bad_blocks").write_text(f"{count}\n")
    mounts = tmp_path / "mounts"
    mounts.write_text("rootfs / rootfs rw 0 0\n")
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=ttyS0,57600 rootfstype=squashfs,jffs2\n")

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{mockbin}:{environment['PATH']}",
            "DESTRUCTIVE_LOG": str(destructive_log),
            "ALLOW_DESTRUCTIVE_WRITE": "1",
            "TEST_TROOT": str(troot),
            "PROC_MTD_PATH": str(proc_mtd),
            "SYS_PATH": str(sysfs),
            "MOUNTS_PATH": str(mounts),
            "CMDLINE_PATH": str(cmdline),
            "MTD_DEV_DIR": str(tmp_path / "dev"),
            "DEV_PATH": str(tmp_path / "dev"),
        }
    )
    result = subprocess.run(
        [
            "sh",
            str(RESTORE),
            "--confirm-UBI-WRITE",
            "--kernel",
            str(kernel),
            "--troot-url",
            "http://test/troot",
            "--troot-size",
            str(troot.stat().st_size),
            "--selector",
            "00",
            "--expected-kernel-sha256",
            sha256(kernel.read_bytes()).hexdigest(),
            "--expected-troot-sha256",
            sha256(troot.read_bytes()).hexdigest(),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    # The 19.07 bridge (and EdgeOS) show kernel1/kernel2; restoring from there
    # would write troot and then have no kernel writer. Refuse up front.
    assert result.returncode != 0
    assert "no 6 MiB 'kernel' partition" in result.stderr
    assert not destructive_log.exists() or destructive_log.read_text() == ""


def _run_restore_args(tmp_path: Path, env_extra: dict, *args: str):
    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(b"k")
    return subprocess.run(
        ["sh", str(RESTORE), *args] if args else ["sh", str(RESTORE)],
        capture_output=True, text=True, check=False,
        env={**os.environ, **env_extra},
    )


def test_restore_refuses_without_write_gate(tmp_path: Path):
    result = _run_restore_args(tmp_path, {}, "--confirm-UBI-WRITE")
    assert result.returncode != 0
    assert "WRITE_GATE_DISABLED" in result.stderr


def test_restore_requires_both_expected_hashes(tmp_path: Path):
    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(b"k")
    troot = tmp_path / "troot.volume"
    troot.write_bytes(b"t")
    result = _run_restore_args(
        tmp_path, {"ALLOW_DESTRUCTIVE_WRITE": "1"},
        "--confirm-UBI-WRITE", "--kernel", str(kernel),
        "--troot-url", "http://test/troot", "--troot-size", "1", "--selector", "00",
        "--expected-kernel-sha256", "0" * 64,
    )
    assert result.returncode != 0
    assert "--expected-troot-sha256 is required" in result.stderr


def test_uppercase_hashes_are_accepted(tmp_path: Path):
    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(b"k")
    result = subprocess.run(
        ["sh", str(RESTORE), "--confirm-UBI-WRITE", "--kernel", str(kernel),
         "--troot-url", "http://t/troot", "--troot-size", "1", "--selector", "00",
         "--expected-kernel-sha256", sha256(b"k").hexdigest().upper(),
         "--expected-troot-sha256", "A" * 64],
        capture_output=True, text=True, check=False,
        env={**os.environ, "ALLOW_DESTRUCTIVE_WRITE": "1", "PROC_MTD_PATH": str(tmp_path / "none")},
    )
    assert "does not match --expected-kernel-sha256" not in result.stderr, result.stderr


@pytest.mark.parametrize("selector", ["00", "01"])
def test_selected_kernel_slot_without_uimage_is_refused_before_any_write(tmp_path: Path, selector: str):
    empty_slot = b"\xff" * 3_145_728
    mockbin = tmp_path / "bin"
    mockbin.mkdir()
    write_executable(mockbin / "id", "#!/bin/sh\necho 0\n")
    kernel = tmp_path / "kernel12.bin"
    kernel.write_bytes(KERNEL12[:3_145_728] + empty_slot if selector == "01" else empty_slot + KERNEL12[3_145_728:])
    proc_mtd = tmp_path / "proc-mtd"
    proc_mtd.write_text(
        "dev:    size   erasesize  name\n"
        'mtd2: 00060000 00020000 "factory"\n'
        'mtd3: 00600000 00020000 "kernel"\n'
        'mtd4: 0f7c0000 00020000 "ubi"\n'
    )
    result = subprocess.run(
        ["sh", str(RESTORE), "--confirm-UBI-WRITE", "--kernel", str(kernel), "--troot-url", "http://test/t",
         "--troot-size", "126976", "--selector", selector,
         "--expected-kernel-sha256", sha256(kernel.read_bytes()).hexdigest(), "--expected-troot-sha256", "0" * 64],
        env={**os.environ, "PATH": f"{mockbin}:{os.environ['PATH']}",
             "ALLOW_DESTRUCTIVE_WRITE": "1", "PROC_MTD_PATH": str(proc_mtd),
             "SYS_PATH": str(tmp_path / "sys"), "MTD_DEV_DIR": str(tmp_path), "DEV_PATH": str(tmp_path)},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0 and "KERNEL_SLOT_EMPTY" in result.stderr, result.stderr
