"""build.sh input checks, run up to the build-host check (uname is faked)."""
import os
import subprocess
from pathlib import Path

import pytest

BUILD = Path(__file__).parents[1] / "build" / "build.sh"


def run_bridge(tmp_path: Path, keys: str | None, password: str = "secret\n"):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\necho NotLinux\n")
    (fake / "uname").chmod(0o755)
    args = ["bash", str(BUILD), "bridge"]
    if keys is not None:
        (tmp_path / "keys").write_text(keys)
        args += ["--authorized-keys", str(tmp_path / "keys")]
    return subprocess.run(args, input=password, capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})


@pytest.mark.parametrize("keys", [
    None,
    "  ssh-rsa AAAAB3 admin\n# comment\n",
    'from="10.0.0.1" ecdsa-sha2-nistp256 AAAAE2 ops\n',
])
def test_bridge_inputs_accepted_up_to_the_build_host_check(tmp_path, keys):
    result = run_bridge(tmp_path, keys)
    assert "OpenWrt builds need Linux" in result.stderr, result.stderr


@pytest.mark.parametrize(("keys", "password", "message"), [
    ("ssh-ed25519 AAAAC3 admin\n", "secret\n", "takes only RSA/ECDSA keys"),
    ("# only a comment\n", "secret\n", "has no keys"),
    (None, "\n", "empty bridge root password"),
])
def test_bridge_refuses_bad_inputs(tmp_path, keys, password, message):
    result = run_bridge(tmp_path, keys, password)
    assert result.returncode != 0 and message in result.stderr, result.stderr


def run_kind(tmp_path: Path, *args: str):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\necho NotLinux\n")
    (fake / "uname").chmod(0o755)
    return subprocess.run(["bash", str(BUILD), *args], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})


@pytest.mark.parametrize("kind", ["final", "recovery"])
def test_final_and_recovery_take_no_inputs_and_reach_the_build_host_check(tmp_path, kind):
    result = run_kind(tmp_path, kind)
    assert "OpenWrt builds need Linux" in result.stderr, result.stderr


@pytest.mark.parametrize("kind", ["final", "recovery"])
def test_final_and_recovery_refuse_extra_arguments(tmp_path, kind):
    result = run_kind(tmp_path, kind, "--authorized-keys", "k")
    assert result.returncode != 0 and "unknown argument: --authorized-keys" in result.stderr


@pytest.mark.parametrize("args", [(), ("production",)])
def test_unknown_kind_prints_usage(tmp_path, args):
    result = run_kind(tmp_path, *args)
    assert result.returncode == 2 and "build.sh bridge" in result.stdout


def test_every_seed_selects_its_device_and_has_unix_line_endings():
    build = BUILD.parent
    profiles = {
        "bridge": "CONFIG_TARGET_ramips_mt7621_DEVICE_ubnt-erx-sfp=y",
        "final": "CONFIG_TARGET_ramips_mt7621_DEVICE_ubnt_edgerouter-x-sfp=y",
        "recovery": "CONFIG_TARGET_ramips_mt7621_DEVICE_ubnt_edgerouter-x-sfp=y",
    }
    for kind, profile in profiles.items():
        seed = (build / f"{kind}.seed").read_bytes()
        assert b"\r" not in seed
        assert profile in seed.decode().splitlines(), kind
    assert "CONFIG_PACKAGE_nand-utils=y" in (build / "recovery.seed").read_text().splitlines()
