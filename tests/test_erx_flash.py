"""Behavioral tests for target/erx-flash.sh against a fake EdgeOS-layout router.

Every flash tool is a mock that logs its call; the fake MTD devices are plain
files, so readbacks and the selector byte are checked for real.
"""
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "target" / "erx-flash.sh"
MIB = 1024 * 1024
BLOCK = 4096  # fake erase block; the real one is 128 KiB

PROC_MTD = """dev:    size   erasesize  name
mtd0: 00080000 00020000 "u-boot"
mtd1: 00060000 00020000 "u-boot-env"
mtd2: 00060000 00020000 "factory"
mtd3: 00300000 00020000 "kernel1"
mtd4: 00300000 00020000 "kernel2"
mtd5: 0f7c0000 00020000 "ubi"
"""

FUNCTIONS_SH = """
board_name() { echo "${FAKE_BOARD:-ubnt-erx-sfp}"; }
include() { for f in "$1"/*.sh; do . "$f"; done; }
"""

NAND_SH = """
CI_UBIPART=ubi
CI_ROOTPART=rootfs
log() { echo "$*" >> "$CALLS"; }
find_mtd_part() {
    idx=$(awk -v n="\\"$1\\"" '$4 == n { sub(/^mtd/, "", $1); sub(/:$/, "", $1); print $1 }' "$R/proc/mtd")
    echo "$R/dev/mtdblock$idx"
}
nand_find_ubi() { echo ubi0; }
nand_find_volume() {
    case "$2" in
        troot) [ -e "$R/dev/ubi0_troot" ] && echo ubi0_troot || return 1 ;;
        rootfs) echo ubi0_0 ;;
        rootfs_data) echo ubi0_1 ;;
        *) return 1 ;;
    esac
}
identify_tar() { echo squashfs; }
nand_upgrade_prepare_ubi() { log "nand_upgrade_prepare_ubi $*"; : > "$R/dev/ubi0_0"; }
nand_restore_config() {
    log "nand_restore_config"; mkdir -p "$R/rootfs_data"; mv "$1" "$R/rootfs_data/sysupgrade.tgz"
    [ -z "${CONFIG_DAMAGE:-}" ] || printf 'X' >> "$R/rootfs_data/sysupgrade.tgz"
}
"""

MOCKS = {
    # mtd write FILE NAME -> copy into the fake device, optionally corrupted
    "mtd": """#!/bin/sh
echo "mtd $*" >> "$CALLS"
idx=$(awk -v n="\\"$3\\"" '$4 == n { sub(/^mtd/, "", $1); sub(/:$/, "", $1); print $1 }' "$ERX_TEST_ROOT/proc/mtd")
dd if="$2" of="$ERX_TEST_ROOT/dev/mtd$idx" conv=notrunc 2>/dev/null
[ "${CORRUPT:-}" = "$3" ] && printf 'X' | dd of="$ERX_TEST_ROOT/dev/mtd$idx" bs=1 seek=100 conv=notrunc 2>/dev/null
exit 0
""",
    "ubiattach": '#!/bin/sh\necho "ubiattach $*" >> "$CALLS"\n',
    "ubirmvol": '#!/bin/sh\necho "ubirmvol $*" >> "$CALLS"\nrm -f "$ERX_TEST_ROOT/dev/ubi0_troot"\n',
    "ubimkvol": '#!/bin/sh\necho "ubimkvol $*" >> "$CALLS"\n',
    # ubiupdatevol DEV -s LEN FILE
    "ubiupdatevol": '#!/bin/sh\necho "ubiupdatevol $*" >> "$CALLS"\ncp "$4" "$1"\n',
    "reboot": '#!/bin/sh\necho "reboot $*" >> "$CALLS"\n',
    # mount -t ubifs -o ro DEV DIR -> show the fake rootfs_data at DIR
    "mount": '#!/bin/sh\nrmdir "$6" && ln -s "$ERX_TEST_ROOT/rootfs_data" "$6"\n',
    "umount": '#!/bin/sh\nrm "$1" && mkdir "$1"\n',
    # sync can simulate a selector write that damaged the rest of the block
    "sync": """#!/bin/sh
[ "${SELECTOR_DAMAGE:-}" = 1 ] && printf 'Z' | dd of="$ERX_TEST_ROOT/dev/mtd2" bs=1 seek=7 conv=notrunc 2>/dev/null
exit 0
""",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_image(path: Path, kernel: bytes, root: bytes, board: str = "ubnt_edgerouter-x-sfp",
               dir_entry: bool = True) -> None:
    with tarfile.open(path, "w") as tar:
        if dir_entry:
            d = tarfile.TarInfo(f"sysupgrade-{board}/")
            d.type, d.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(d)
        for name, data in (("CONTROL", b"BOARD=x\n"), ("kernel", kernel), ("root", root)):
            info = tarfile.TarInfo(f"sysupgrade-{board}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class FakeRouter:
    def __init__(self, tmp: Path, *, selector: int = 1, bad_kernel1: int = 0, root_fs: str = "rootfs"):
        self.root = tmp / "root"
        self.calls = tmp / "calls.log"
        r = self.root
        (r / "proc").mkdir(parents=True)
        (r / "proc/mtd").write_text(PROC_MTD)
        (r / "proc/mounts").write_text(f"{root_fs} / {root_fs} rw 0 0\n")
        (r / "lib/upgrade").mkdir(parents=True)
        (r / "lib/functions.sh").write_text(FUNCTIONS_SH)
        (r / "lib/upgrade/nand.sh").write_text(NAND_SH)
        dev = r / "dev"
        dev.mkdir()
        factory = bytearray(os.urandom(3 * BLOCK))
        factory[160] = selector
        (dev / "mtd2").write_bytes(bytes(factory))
        (dev / "mtd3").write_bytes(b"\xff" * (3 * MIB))
        (dev / "mtd4").write_bytes(b"\xff" * (3 * MIB))
        for i in (2, 3, 4):
            (dev / f"mtdblock{i}").symlink_to(dev / f"mtd{i}")
        (dev / "ubi0_troot").write_bytes(b"edgeos")
        for i in range(6):
            s = r / f"sys/class/mtd/mtd{i}"
            s.mkdir(parents=True)
            (s / "bad_blocks").write_text(f"{bad_kernel1 if i == 3 else 0}\n")
            (s / "ecc_failures").write_text("0\n")
            (s / "erasesize").write_text(f"{BLOCK}\n")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in MOCKS.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "ERX_TEST_ROOT": str(self.root),
                "TMPDIR": str(self.root.parent),
                "ERX_REBOOT_DELAY": "0",
                "CALLS": str(self.calls),
                **env,
            },
            check=False,
        )

    def log(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def dev(self, name: str) -> bytes:
        return (self.root / "dev" / name).read_bytes()


@pytest.fixture
def image(tmp_path: Path):
    kernel = os.urandom(3 * MIB + 5000)  # spans kernel1 and kernel2
    root = os.urandom(20000)
    path = tmp_path / "sysupgrade.bin"
    make_image(path, kernel, root)
    return path, kernel, root


def config_tgz(tmp_path: Path) -> Path:
    path = tmp_path / "access.tgz"
    with tarfile.open(path, "w:gz") as tar:
        data = b"ssh-ed25519 AAAA test\n"
        info = tarfile.TarInfo("etc/dropbear/authorized_keys")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return path


def test_check_reads_only(tmp_path, image):
    router = FakeRouter(tmp_path)
    path, _, _ = image
    result = router.run("check", str(path), sha256(path))
    assert result.returncode == 0, result.stderr
    assert "check passed" in result.stdout
    assert "selector: 01" in result.stdout
    assert router.log() == []


def test_flash_writes_kernel_selector_root_config_then_reboots(tmp_path, image):
    router = FakeRouter(tmp_path, selector=1)
    path, kernel, root = image
    cfg = config_tgz(tmp_path)
    result = router.run("flash", str(path), sha256(path), str(cfg), sha256(cfg))
    assert result.returncode == 0, result.stderr
    assert router.dev("mtd3") == kernel[: 3 * MIB]
    assert router.dev("mtd4")[: len(kernel) - 3 * MIB] == kernel[3 * MIB :]
    assert router.dev("mtd2")[160] == 0
    assert router.dev("ubi0_0") == root
    assert (router.root / "rootfs_data/sysupgrade.tgz").read_bytes() == cfg.read_bytes()
    log = router.log()
    order = [next(i for i, line in enumerate(log) if line.startswith(p))
             for p in ("mtd write", "ubirmvol", "nand_upgrade_prepare_ubi", "ubiupdatevol",
                       "nand_restore_config", "reboot")]
    assert order == sorted(order)
    assert "PHASE:FLASH_START" in result.stdout


def test_flash_without_config_keeps_image_defaults(tmp_path, image):
    router = FakeRouter(tmp_path, selector=0)
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path))
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("nand_restore_config") for line in router.log())
    assert router.log()[-1].startswith("reboot")


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ({"bad_kernel1": 1}, "kernel1 has bad blocks"),
        ({"root_fs": "ubifs"}, "not running from a RAM root"),
    ],
)
def test_refuses_unsafe_router_state_before_any_write(tmp_path, image, setup, expected):
    router = FakeRouter(tmp_path, **setup)
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path))
    assert result.returncode != 0
    assert expected in result.stderr
    assert router.log() == []


def test_refuses_wrong_hash_before_any_write(tmp_path, image):
    router = FakeRouter(tmp_path)
    path, _, _ = image
    result = router.run("flash", str(path), "0" * 64)
    assert result.returncode != 0
    assert "image SHA-256 mismatch" in result.stderr
    assert router.log() == []


def test_refuses_image_for_another_board(tmp_path):
    router = FakeRouter(tmp_path)
    path = tmp_path / "other.bin"
    make_image(path, b"k" * 100, b"r" * 100, board="ubnt_edgerouter-x")
    result = router.run("flash", str(path), sha256(path))
    assert result.returncode != 0
    assert "not an ER-X-SFP sysupgrade image" in result.stderr
    assert router.log() == []


def test_kernel_readback_mismatch_stops_before_selector_and_rootfs(tmp_path, image):
    router = FakeRouter(tmp_path, selector=1)
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path), CORRUPT="kernel1")
    assert result.returncode != 0
    assert "kernel1 readback does not match" in result.stderr
    assert router.dev("mtd2")[160] == 1
    assert [line.split()[0] for line in router.log()] == ["mtd"]


def test_selector_write_that_damages_the_block_stops_before_rootfs(tmp_path, image):
    router = FakeRouter(tmp_path, selector=1)
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path), SELECTOR_DAMAGE="1")
    assert result.returncode != 0
    assert "factory block changed beyond the selector byte" in result.stderr
    assert not any(line.startswith(("ubirmvol", "ubiupdatevol", "reboot")) for line in router.log())
    # the block copies stay behind for the investigation
    kept = Path(result.stderr.split("work files kept for inspection: ")[1].split()[0])
    assert (kept / "factory.expected").exists() and (kept / "factory.after").exists()
    shutil.rmtree(kept)


def test_image_without_directory_entry_is_accepted(tmp_path):
    router = FakeRouter(tmp_path)
    path = tmp_path / "flat.bin"
    make_image(path, b"k" * 1000, b"r" * 1000, dir_entry=False)
    result = router.run("check", str(path), sha256(path))
    assert result.returncode == 0, result.stderr
    assert "sysupgrade-ubnt_edgerouter-x-sfp" in result.stdout


def test_config_readback_mismatch_stops_before_reboot(tmp_path, image):
    router = FakeRouter(tmp_path, selector=1)
    path, _, _ = image
    cfg = config_tgz(tmp_path)
    result = router.run("flash", str(path), sha256(path), str(cfg), sha256(cfg), CONFIG_DAMAGE="1")
    assert result.returncode != 0
    assert "configuration readback in rootfs_data does not match" in result.stderr
    assert not any(line.startswith("reboot") for line in router.log())


def test_router_refuses_config_with_unsafe_paths(tmp_path, image):
    router = FakeRouter(tmp_path)
    path, _, _ = image
    cfg = tmp_path / "bad.tgz"
    with tarfile.open(cfg, "w:gz") as tar:
        info = tarfile.TarInfo("../etc/shadow")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    result = router.run("flash", str(path), sha256(path), str(cfg), sha256(cfg))
    assert result.returncode != 0 and "unsafe paths" in result.stderr
    assert router.log() == []


def test_bad_reboot_delay_value_does_not_skip_the_reboot(tmp_path, image):
    router = FakeRouter(tmp_path, selector=0)
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path), ERX_REBOOT_DELAY="soon")
    assert router.log()[-1].startswith("reboot"), result.stderr


def test_ubi_that_cannot_be_attached_stops_before_any_write(tmp_path, image):
    router = FakeRouter(tmp_path, selector=0)
    nand = router.root / "lib/upgrade/nand.sh"
    nand.write_text(nand.read_text().replace("nand_find_ubi() { echo ubi0; }", "nand_find_ubi() { return 1; }"))
    (router.bin / "ubiattach").write_text('#!/bin/sh\necho "ubiattach $*" >> "$CALLS"\nexit 1\n')
    factory = router.dev("mtd2")
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path))
    assert result.returncode != 0 and "nothing was written" in result.stderr
    assert [line.split()[0] for line in router.log()] == ["ubiattach"]
    assert router.dev("mtd2") == factory and router.dev("mtd3") == b"\xff" * (3 * MIB)


def test_ecc_failure_while_reading_factory_stops_before_the_selector_write(tmp_path, image):
    router = FakeRouter(tmp_path, selector=1)
    ecc = router.root / "sys/class/mtd/mtd2/ecc_failures"
    real_dd = shutil.which("dd")
    (router.bin / "dd").write_text(
        "#!/bin/sh\n"
        f'"{real_dd}" "$@" || exit\n'
        f'case "$*" in *factory.now*) echo 1 > "{ecc}" ;; esac\n')
    (router.bin / "dd").chmod(0o755)
    factory = router.dev("mtd2")
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path))
    assert result.returncode != 0 and "selector was not changed" in result.stderr
    assert router.dev("mtd2") == factory
    assert not any(line.startswith("reboot") for line in router.log())


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_router_refuses_config_with_links(tmp_path, image, kind):
    router = FakeRouter(tmp_path)
    path, _, _ = image
    cfg = tmp_path / "links.tgz"
    with tarfile.open(cfg, "w:gz") as tar:
        info = tarfile.TarInfo("etc/config/network")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
        link = tarfile.TarInfo("etc/shadow-link")
        link.type, link.linkname = kind, "/etc/shadow" if kind == tarfile.SYMTYPE else "etc/config/network"
        tar.addfile(link)
    result = router.run("flash", str(path), sha256(path), str(cfg), sha256(cfg))
    assert result.returncode != 0 and "links or special files" in result.stderr
    assert router.log() == []


def test_mounted_ubi_volume_is_refused_before_any_write(tmp_path, image):
    router = FakeRouter(tmp_path, selector=1)
    with open(router.root / "proc/mounts", "a") as mounts:
        mounts.write("ubi0:troot /mnt ubifs ro 0 0\n")
    path, _, _ = image
    result = router.run("flash", str(path), sha256(path))
    assert result.returncode != 0 and "UBI volume is mounted" in result.stderr
    assert router.log() == []
