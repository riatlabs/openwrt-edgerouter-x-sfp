from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).parents[1] / "recovery"
COMMON = ROOT / "lib/erx-common.sh"
BLOCK_SIZE = 131_072


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def run_selector_update(tmp_path: Path, *, omit_nandwrite: bool = False, nandwrite_fails: bool = False,
                        nandwrite_ecc: str = ""):
    mockbin = tmp_path / "bin"
    mockbin.mkdir(parents=True)
    dev = tmp_path / "dev"
    dev.mkdir()
    sysfs = tmp_path / "sys/class/mtd/mtd2"
    sysfs.mkdir(parents=True)
    (sysfs / "writesize").write_text("2048\n")
    (sysfs / "erasesize").write_text(f"{BLOCK_SIZE}\n")
    (sysfs / "bad_blocks").write_text("0\n")
    (sysfs / "ecc_failures").write_text("0\n")
    (sysfs / "corrected_bits").write_text("0\n")

    mtd = dev / "mtd2"
    mtd.write_bytes(b"A" * 160 + b"\x01" + b"B" * (BLOCK_SIZE - 161))
    (dev / "mtd2ro").symlink_to(mtd)

    log = tmp_path / "writes.log"
    write_executable(
        mockbin / "flash_erase",
        "#!/bin/sh\n"
        "printf 'flash_erase %s\\n' \"$*\" >> \"$WRITE_LOG\"\n"
        "[ \"$2\" = 0 ] && [ \"$3\" = 1 ] || exit 41\n"
        f"dd if=/dev/zero bs={BLOCK_SIZE} count=1 2>/dev/null | LC_ALL=C tr '\\000' '\\377' | dd of=\"$TEST_DEV/mtd2\" bs={BLOCK_SIZE} conv=notrunc 2>/dev/null\n",   # erased NAND reads 0xFF
    )
    if not omit_nandwrite:
        write_executable(
            mockbin / "nandwrite",
            "#!/bin/sh\n"
            "printf 'nandwrite %s\\n' \"$*\" >> \"$WRITE_LOG\"\n"
            "[ \"$1\" = -p ] || exit 42\n"
            "[ -z \"$NANDWRITE_FAILS\" ] || exit 5\n"
            "case \"$NANDWRITE_ECC\" in corrected) echo 3 > \"$SYS/corrected_bits\" ;; failed) echo 1 > \"$SYS/ecc_failures\" ;; esac\n"
            "dd if=\"$3\" of=\"$TEST_DEV/mtd2\" bs=131072 conv=notrunc 2>/dev/null\n",
        )

    harness = tmp_path / "run.sh"
    harness.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "die() { echo \"$*\" >&2; exit 1; }\n"
        f'. "{COMMON}"\n'
        'set_selector 00 /dev/mtd2\n'
    )
    harness.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{mockbin}:{env['PATH']}",
            "SYS_PATH": str(tmp_path / "sys"),
            "MTD_DEV_DIR": str(dev),
            "TEST_DEV": str(dev),
            "WRITE_LOG": str(log),
            "NANDWRITE_FAILS": "1" if nandwrite_fails else "",
            "NANDWRITE_ECC": nandwrite_ecc,
            "SYS": str(sysfs),
        }
    )
    result = subprocess.run(["sh", str(harness)], env=env, text=True, capture_output=True)
    return result, mtd, log


def test_selector_update_erases_and_verifies_the_complete_nand_eraseblock(tmp_path: Path):
    result, mtd, log = run_selector_update(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    commands = log.read_text().splitlines()
    assert len(commands) == 2, commands
    assert commands[0] == "flash_erase /dev/mtd2 0 1", commands
    assert commands[1].startswith("nandwrite -p /dev/mtd2 /tmp/selector-eraseblock."), commands
    final = mtd.read_bytes()
    assert len(final) == BLOCK_SIZE
    assert final[160] == 0
    assert final[:160] == b"A" * 160
    assert final[161:] == b"B" * (BLOCK_SIZE - 161)
    assert "SELECTOR_ERASEBLOCK_READBACK_VERIFIED" in result.stdout


def test_missing_nandwrite_aborts_before_erasing_anything(tmp_path: Path):
    result, mtd, log = run_selector_update(tmp_path, omit_nandwrite=True)

    assert result.returncode != 0
    assert "nandwrite" in result.stderr.lower()
    assert not log.exists() or log.read_text() == ""
    assert mtd.read_bytes()[160] == 1


def test_failed_write_after_erase_says_where_the_block_is_and_not_to_reboot(tmp_path: Path):
    result, mtd, _ = run_selector_update(tmp_path, nandwrite_fails=True)
    assert result.returncode != 0
    assert "SELECTOR_WRITE_FAILED" in result.stderr and "do NOT reboot" in result.stderr
    block = Path(result.stderr.split("write it back with: nandwrite -p /dev/mtd2 ")[1].split()[0])
    try:
        data = block.read_bytes()
        assert data[160] == 0 and data[:160] == b"A" * 160 and len(data) == BLOCK_SIZE
    finally:
        block.unlink()


def test_corrected_bitflips_warn_but_uncorrectable_failures_stop(tmp_path: Path):
    result, mtd, _ = run_selector_update(tmp_path / "a", nandwrite_ecc="corrected")
    assert result.returncode == 0, result.stderr
    assert "WARNING: NAND corrected bitflips" in result.stderr and mtd.read_bytes()[160] == 0
    result, _, _ = run_selector_update(tmp_path / "b", nandwrite_ecc="failed")
    assert result.returncode != 0 and "SELECTOR_ECC_READBACK_FAILED" in result.stderr
    for kept in result.stderr.split("nandwrite -p /dev/mtd2 ")[1].split()[:1]:
        Path(kept).unlink()
        Path(kept.replace("selector-eraseblock.", "selector-eraseblock-readback.")).unlink(missing_ok=True)
