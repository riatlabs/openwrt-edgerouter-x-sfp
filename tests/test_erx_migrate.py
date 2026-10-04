"""Tests for the erx-migrate host tool.

The end-to-end test swaps `ssh` for a stub that runs the remote command
locally against the fake router from test_erx_flash.py, so the whole path
upload -> hash check -> erx-flash.sh check -> confirmation -> flash runs.
"""
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import shutil
import sys
import tempfile
import tarfile
from pathlib import Path

import pytest

from test_erx_flash import FakeRouter, make_image, sha256

TOOL = Path(__file__).parents[1] / "erx-migrate"
loader = importlib.machinery.SourceFileLoader("erx_migrate", str(TOOL))
spec = importlib.util.spec_from_loader("erx_migrate", loader)
erx = importlib.util.module_from_spec(spec)
loader.exec_module(erx)

# Stub ssh: drop options, keep the command, run it locally with the router's
# absolute paths mapped into the test's fake root ($SANDBOX). Logs every
# remote command.
FAKE_SSH = r"""#!/bin/sh
while [ "$#" -gt 0 ]; do
    case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac
done
shift  # destination
cmd=$(printf '%s' "$*" | sed -E "s#(^|[ \"'=(])/(tmp|proc|etc|sys|usr|opt|dev|config)/#\\1$SANDBOX/\\2/#g; s#$SANDBOX/dev/null#/dev/null#g")
printf '%s\n' "$cmd" >> "$SSH_LOG"
# Like real ssh, forward (and so consume) stdin for every remote command.
case "$cmd" in "cat > "*) exec sh -c "$cmd" ;; esac
cat > /dev/null
exec sh -c "$cmd" < /dev/null
"""


def bridge_tar(path: Path, kernel: bytes, md5: str | None = None) -> Path:
    with tarfile.open(path, "w") as tar:
        members = {
            "compat": b"4\n",
            "vmlinux.tmp": kernel,
            "vmlinux.tmp.md5": (md5 or hashlib.md5(kernel).hexdigest()).encode() + b"\n",
            "squashfs.tmp": b"sq",
            "version.tmp": b"OpenWrt 19.07.10 bridge\n",
        }
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_bridge_tar_accepts_one_slot_kernel(tmp_path):
    erx.verify_bridge_tar(bridge_tar(tmp_path / "b.tar", b"k" * 1000))


@pytest.mark.parametrize(
    ("kernel", "md5", "message"),
    [
        (b"k" * (3 * 1024 * 1024 + 1), None, "kernel slot holds"),
        (b"k" * 1000, "0" * 32, "does not match its vmlinux.tmp.md5"),
    ],
)
def test_bridge_tar_rejects_oversized_or_damaged_kernel(tmp_path, kernel, md5, message):
    with pytest.raises(SystemExit, match=message):
        erx.verify_bridge_tar(bridge_tar(tmp_path / "b.tar", kernel, md5))


def test_bridge_tar_rejects_a_sysupgrade_image(tmp_path):
    path = tmp_path / "sysupgrade.bin"
    make_image(path, b"k", b"r")
    with pytest.raises(SystemExit, match="not an EdgeOS factory tar"):
        erx.verify_bridge_tar(path)


GOOD_FACTS = {
    "board": "e51", "selector": "01", "config_boot": "ok",
    **{f"mtd_{n}": "4" for n in ("eeprom", "Kernel1", "Kernel2", "RootFS")},
    **{f"bad_{n}": "0" for n in ("eeprom", "Kernel1", "Kernel2")},
    **{f"ecc_{n}": "0" for n in ("eeprom", "Kernel1", "Kernel2")},
}


def test_edgeos_check_accepts_healthy_er_x_sfp():
    assert erx.edgeos_problems(GOOD_FACTS) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"board": "e50"}, "expected e51"),
        ({"mtd_Kernel2": ""}, "Kernel2 missing"),
        ({"bad_Kernel1": "2"}, "Kernel1: bad blocks"),
        ({"ecc_eeprom": "1"}, "eeprom: ECC failures"),
        ({"bad_Kernel2": ""}, "Kernel2: bad blocks"),   # unreadable counter fails closed
        ({"selector": ""}, "boot selector"),
        ({"config_boot": ""}, "PoE state is unknown"),
    ],
)
def test_edgeos_check_refuses(change, message):
    problems = erx.edgeos_problems({**GOOD_FACTS, **change})
    assert any(message in p for p in problems), problems


def test_access_config_contains_keys_firewall_rule_and_extra_files(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text("# admins\nssh-ed25519 AAAA admin@host\n\n")
    extra = tmp_path / "device"
    (extra / "etc/config").mkdir(parents=True)
    (extra / "etc/config/olsrd").write_text("config olsrd\n")
    out = tmp_path / "access.tgz"
    erx.build_access_config(keys, extra, out)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        assert tar.extractfile("etc/dropbear/authorized_keys").read() == b"ssh-ed25519 AAAA admin@host\n"
        assert tar.getmember("etc/dropbear/authorized_keys").mode == 0o600
        defaults = tar.extractfile("etc/uci-defaults/99-erx-remote-access").read().decode()
    assert "etc/config/olsrd" in names
    assert "src_ip='fe80::/10'" in defaults and "src='wan'" in defaults
    assert 'PasswordAuth=0' in defaults


def test_access_config_refuses_private_keys(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nssh-ed25519 AAAA x\n")
    with pytest.raises(SystemExit, match="private key"):
        erx.build_access_config(keys, None, tmp_path / "a.tgz")


def run_flash(tmp_path: Path, answer: str, *extra: str, **router_env: str):
    router = FakeRouter(tmp_path, selector=1)
    image = tmp_path / "sysupgrade.bin"
    make_image(image, os.urandom(4000), os.urandom(3000))
    sandbox = tmp_path / "remote"
    (sandbox / "tmp").mkdir(parents=True)
    (router.bin / "ssh").write_text(router_env.pop("FAKE_SSH_OVERRIDE", FAKE_SSH))
    (router.bin / "ssh").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{router.bin}:{os.environ['PATH']}",
        "ERX_TEST_ROOT": str(router.root),
        "TMPDIR": str(tmp_path),
        "ERX_REBOOT_DELAY": "2",   # like the router: last line, pause, reboot
        "CALLS": str(router.calls),
        "SANDBOX": str(sandbox),
        "SSH_LOG": str(tmp_path / "ssh.log"),
        "ERX_STATE_DIR": str(tmp_path / "state"),
        **router_env,
    }
    result = subprocess.run([sys.executable, str(TOOL), "flash", "root@bridge", str(image), *extra],
                            input=answer, capture_output=True, text=True, env=env)
    return result, router, image, (tmp_path / "ssh.log").read_text()


def test_flash_end_to_end_through_ssh(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAA admin\n")
    cfg = tmp_path / "access.tgz"
    erx.build_access_config(keys, None, cfg)
    result, router, image, ssh_log = run_flash(tmp_path, "FLASH\n", "--config", str(cfg))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "check passed" in result.stdout
    assert f"flash {tmp_path}/remote/tmp/sysupgrade.bin {sha256(image)}" in ssh_log
    assert "> " + str(tmp_path / "remote/tmp/erx-flash.log") in ssh_log   # detached, logged
    assert "the router is rebooting into OpenWrt" in result.stdout
    assert router.dev("mtd2")[160] == 0
    assert (router.root / "rootfs_data/sysupgrade.tgz").read_bytes() == cfg.read_bytes()
    assert router.log()[-1].startswith("reboot")


def test_flash_without_confirmation_writes_nothing(tmp_path):
    result, router, _, ssh_log = run_flash(tmp_path, "no\n")
    assert result.returncode != 0
    assert "nothing was changed" in result.stderr
    assert " flash " not in ssh_log
    assert router.log() == []


def test_flash_error_on_the_router_is_reported_not_taken_for_success(tmp_path):
    result, router, _, _ = run_flash(tmp_path, "FLASH\n", CORRUPT="kernel1")
    assert result.returncode != 0
    assert "flash stopped" in result.stderr
    assert "rebooting into OpenWrt" not in result.stdout
    assert not any(line.startswith("reboot") for line in router.log())


def test_ctrl_c_while_following_the_writer_gives_the_reconnect_advice(tmp_path):
    ssh = FAKE_SSH.replace('printf \'%s\\n\' "$cmd" >> "$SSH_LOG"\n',
                           'printf \'%s\\n\' "$cmd" >> "$SSH_LOG"\n'
                           'case "$cmd" in *"tail -n +1 -f"*) kill -INT $PPID; sleep 1 ;; esac\n')
    assert ssh != FAKE_SSH
    result, _, _, _ = run_flash(tmp_path, "FLASH\n", FAKE_SSH_OVERRIDE=ssh)
    assert result.returncode != 0 and "Traceback" not in result.stderr
    assert "interrupted on this host only" in result.stderr and "/tmp/erx-flash.log" in result.stderr


def test_access_config_refuses_keys_openwrt_dropbear_cannot_use(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAA ok\necdsa-sha2-nistp256 AAAA admin2\n")
    with pytest.raises(SystemExit, match="only ssh-ed25519 and ssh-rsa"):
        erx.build_access_config(keys, None, tmp_path / "a.tgz")


# ------------------------------------------------ check / bridge / verify

EDGEOS_MTD = """dev:    size   erasesize  name
mtd4: 00060000 00020000 "eeprom"
mtd5: 00300000 00020000 "{kernel1}"
mtd6: 00300000 00020000 "Kernel2"
mtd7: 0f7c0000 00020000 "RootFS"
"""


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def fake_host(tmp_path: Path) -> tuple[Path, Path, dict]:
    """Fake remote root + PATH with ssh, sudo and the router's tools."""
    root, bin_dir, calls = tmp_path / "remote", tmp_path / "bin", tmp_path / "calls.log"
    (root / "tmp").mkdir(parents=True)
    executable(bin_dir / "ssh", FAKE_SSH)
    executable(bin_dir / "sudo", '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
    executable(bin_dir / "reboot", '#!/bin/sh\necho reboot >> "$CALLS"\n')
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SANDBOX": str(root),
           "SSH_LOG": str(tmp_path / "ssh.log"), "CALLS": str(calls),
           "ERX_STATE_DIR": str(tmp_path / "state")}
    return root, bin_dir, env


def fake_edgeos(tmp_path: Path, *, kernel1="Kernel1", board="e51", bad_kernel2="0", poe_eth4="off"):
    root, bin_dir, env = fake_host(tmp_path)
    (root / "config").mkdir()
    (root / "config/config.boot").write_text(
        "interfaces {\n    ethernet eth0 {\n        description \"uplink with output filter\"\n"
        "        poe {\n            output off\n        }\n    }\n"
        f"    ethernet eth4 {{\n        poe {{\n            output {poe_eth4}\n        }}\n    }}\n}}\n")
    (root / "proc").mkdir()
    (root / "proc/mtd").write_text(EDGEOS_MTD.format(kernel1=kernel1))
    (root / "etc").mkdir()
    (root / "etc/version").write_text("EdgeRouter.ER-e50.v1.10.11\n")
    executable(root / "usr/sbin/ubnt-hal-e", f"#!/bin/sh\necho {board}\n")
    executable(bin_dir / "nanddump", f"""#!/bin/sh
case "$*" in *mtd6*) bad={bad_kernel2} ;; *) bad=0 ;; esac
printf 'ECC failed: 0\\nECC corrected: 0\\nNumber of bad blocks: %s\\n' "$bad"
""")
    (root / "dev").mkdir()
    (root / "dev/mtdblock4").write_bytes(b"\xff" * 160 + b"\x01" + b"\xff" * 100)
    executable(root / "usr/bin/ubnt-upgrade", '#!/bin/sh\necho "ubnt-upgrade $*" >> "$CALLS"\n')
    executable(root / "opt/vyatta/bin/vyatta-op-cmd-wrapper", "#!/bin/sh\necho images\n")
    return env


def run_tool(env: dict, *args: str, answer: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], input=answer,
                          capture_output=True, text=True, env=env)


def calls(env: dict) -> str:
    path = Path(env["CALLS"])
    return path.read_text() if path.exists() else ""


@pytest.mark.parametrize("kernel1", ["Kernel1", "Kernel"])   # EdgeOS 2.x / 1.x
def test_check_accepts_both_edgeos_generations(tmp_path, kernel1):
    result = run_tool(fake_edgeos(tmp_path, kernel1=kernel1), "check", "ubnt@router")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EdgeOS check passed" in result.stdout


@pytest.mark.parametrize(("setup", "message"), [
    ({"board": "e50"}, "expected e51"),
    ({"bad_kernel2": "1"}, "Kernel2: bad blocks = '1'"),
])
def test_check_refuses_other_boards_and_bad_blocks(tmp_path, setup, message):
    result = run_tool(fake_edgeos(tmp_path, **setup), "check", "ubnt@router")
    assert result.returncode == 1
    assert message in result.stdout


def test_bridge_installs_through_ubnt_upgrade_and_reboots_only_after_confirmation(tmp_path):
    env = fake_edgeos(tmp_path)
    tar = bridge_tar(tmp_path / "bridge.tar", b"k" * 1000)
    result = run_tool(env, "bridge", "ubnt@router", str(tar), answer="REBOOT\n")
    assert result.returncode == 0, result.stdout + result.stderr
    log = calls(env).splitlines()
    assert log[0].startswith("ubnt-upgrade --upgrade-noprompt ") and log[-1] == "reboot"

    env2 = fake_edgeos(tmp_path / "second")
    result = run_tool(env2, "bridge", "ubnt@router", str(tar), answer="no\n")
    assert result.returncode != 0 and "reboot" not in calls(env2)
    # ubnt-upgrade has already switched the selector: never claim nothing changed
    assert "nothing was changed" not in result.stderr
    assert "next reboot or power cycle starts the bridge" in result.stderr


def test_bridge_refuses_before_uploading_when_check_fails(tmp_path):
    env = fake_edgeos(tmp_path, bad_kernel2="3")
    tar = bridge_tar(tmp_path / "bridge.tar", b"k" * 1000)
    result = run_tool(env, "bridge", "ubnt@router", str(tar), answer="REBOOT\n")
    assert result.returncode != 0 and "EdgeOS check failed" in result.stderr
    assert calls(env) == ""
    assert not (tmp_path / "remote/tmp/erx-bridge.tar").exists()


def fake_openwrt(tmp_path: Path, *, mtd_kernel='"kernel"', sysupgrade_rc=0):
    root, bin_dir, env = fake_host(tmp_path)
    (root / "proc").mkdir()
    (root / "proc/mtd").write_text(f'dev: size erasesize name\nmtd3: 00600000 00020000 {mtd_kernel}\n')
    (root / "etc").mkdir()
    (root / "etc/openwrt_release").write_text("DISTRIB_RELEASE='25.12.5'\n")
    for i, name in enumerate(("factory", "kernel")):
        d = root / f"sys/class/mtd/mtd{i}"
        d.mkdir(parents=True)
        for f, v in (("name", name), ("bad_blocks", "0"), ("ecc_failures", "0")):
            (d / f).write_text(v + "\n")
    executable(bin_dir / "sysupgrade", f'#!/bin/sh\necho "sysupgrade $*" >> "$CALLS"\nexit {sysupgrade_rc}\n')
    return env


def test_verify_checks_layout_and_runs_sysupgrade_test(tmp_path):
    env = fake_openwrt(tmp_path)
    image = tmp_path / "next.bin"
    image.write_bytes(b"img")
    result = run_tool(env, "verify", "root@router", str(image))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sysupgrade -T" in calls(env)
    assert "OpenWrt verified" in result.stdout


@pytest.mark.parametrize(("setup", "message"), [
    ({"mtd_kernel": '"kernel1"'}, "not OpenWrt's single 6 MiB kernel layout"),
    ({"sysupgrade_rc": 1}, "sysupgrade -T rejected the image"),
])
def test_verify_fails_on_old_layout_or_rejected_image(tmp_path, setup, message):
    env = fake_openwrt(tmp_path, **setup)
    image = tmp_path / "next.bin"
    image.write_bytes(b"img")
    result = run_tool(env, "verify", "root@router", str(image))
    assert result.returncode != 0 and message in result.stderr
    assert not (tmp_path / "remote/tmp/erx-next.bin").exists()


# ------------------------------------------------ access configuration and keys

def test_access_config_keeps_indented_keys_and_key_options(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text('  ssh-ed25519 AAAAC3 admin\nrestrict,from="10.0.0.1, 10.0.0.2" ssh-rsa AAAAB3 ops\n')
    out = tmp_path / "a.tgz"
    erx.build_access_config(keys, None, out)
    with tarfile.open(out) as tar:
        lines = tar.extractfile("etc/dropbear/authorized_keys").read().decode().splitlines()
    assert lines == ["ssh-ed25519 AAAAC3 admin", 'restrict,from="10.0.0.1, 10.0.0.2" ssh-rsa AAAAB3 ops']


@pytest.mark.parametrize(("line", "message"), [
    ('from="10.0.0.1" ecdsa-sha2-nistp256 AAAAE2 x', "only ssh-ed25519 and ssh-rsa"),
    ("this is not a key", "not an authorized_keys line"),
])
def test_access_config_refuses_unusable_or_garbage_lines(tmp_path, line, message):
    keys = tmp_path / "keys"
    keys.write_text(f"ssh-ed25519 AAAAC3 ok\n{line}\n")
    with pytest.raises(SystemExit, match=message):
        erx.build_access_config(keys, None, tmp_path / "a.tgz")


def test_access_config_refuses_a_missing_files_directory(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAAC3 ok\n")
    with pytest.raises(SystemExit, match="is not a directory"):
        erx.build_access_config(keys, tmp_path / "typo", tmp_path / "a.tgz")
    assert not (tmp_path / "a.tgz").exists()


def test_check_says_when_sudo_is_not_passwordless():
    facts = {**GOOD_FACTS, **{f"bad_{n}": "" for n in ("eeprom", "Kernel1", "Kernel2")}}
    assert any("passwordless sudo" in p for p in erx.edgeos_problems(facts))


def test_ssh_socket_dir_stays_short(tmp_path, monkeypatch):
    monkeypatch.setattr(erx, "STATE", tmp_path / ("x" * 80))
    assert len(str(erx.socket_dir())) + len("/%C") + 40 <= 104


@pytest.mark.parametrize(("line", "expected"), [
    ("ssh-ed25519 AAAAC3 admin", "ssh-ed25519"),
    ('restrict,from="10.0.0.1, 10.0.0.2" ssh-rsa AAAAB3 ops', "ssh-rsa"),
    ('environment="X=AAAA",command="run AAAA" ssh-ed25519 AAAAC3 x', "ssh-ed25519"),
    ('from="10.0.0.1" ecdsa-sha2-nistp256 AAAAE2 x', "ecdsa-sha2-nistp256"),
    ("this is not a key", None),
    ('from="unbalanced ssh-ed25519 AAAAC3', None),
])
def test_key_type_follows_the_authorized_keys_format(line, expected):
    assert erx.key_type(line) == expected


# ------------------------------------------------ local input validation

@pytest.mark.parametrize("args", [
    ["bridge", "ubnt@r", "/no/such.tar"],
    ["flash", "root@r", "/no/such.bin"],
    ["verify", "root@r", "/no/such.bin"],
    ["access-config", "--authorized-keys", "/no/such", "-o", "/tmp/unused.tgz"],
])
def test_missing_input_files_are_refused_before_connecting(tmp_path, args):
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "ERX_STATE_DIR": str(tmp_path / "s")}
    (tmp_path / "ssh").write_text("#!/bin/sh\ntouch \"$0.called\"\nexit 1\n")
    (tmp_path / "ssh").chmod(0o755)
    result = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env)
    assert result.returncode == 2 and "no such file" in result.stderr
    assert not (tmp_path / "ssh.called").exists()


def test_eof_at_the_confirmation_cancels_cleanly(tmp_path):
    result, router, _, ssh_log = run_flash(tmp_path, "")      # Ctrl-D
    assert result.returncode != 0 and "nothing was changed" in result.stderr
    assert "Traceback" not in result.stderr and router.log() == []


@pytest.mark.parametrize(("content", "message"), [
    (b"not a tar at all", "not a readable tar file"),
    (None, "does not match its vmlinux.tmp.md5"),     # empty .md5 member
])
def test_broken_bridge_tar_is_refused_cleanly(tmp_path, content, message):
    path = tmp_path / "b.tar"
    if content is not None:
        path.write_bytes(content)
    else:
        bridge_tar(path, b"k" * 100, md5=" ")
    with pytest.raises(SystemExit, match=message):
        erx.verify_bridge_tar(path)


def test_socket_dir_tightens_a_world_writable_directory_of_this_user(tmp_path, monkeypatch):
    monkeypatch.setattr(erx, "STATE", tmp_path / ("x" * 80))
    shared = Path(tempfile.mkdtemp(dir="/tmp"))           # short, like /run/user/UID
    try:
        (shared / "erx-migrate").mkdir()
        (shared / "erx-migrate").chmod(0o777)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(shared))
        assert erx.socket_dir() == shared / "erx-migrate"
        assert (shared / "erx-migrate").stat().st_mode & 0o077 == 0
    finally:
        shutil.rmtree(shared)

def test_access_config_refuses_a_firewall_without_wan_zone(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAAC3 ok\n")
    extra = tmp_path / "device"
    (extra / "etc/config").mkdir(parents=True)
    (extra / "etc/config/firewall").write_text("config zone\n\toption name 'mesh'\n")
    with pytest.raises(SystemExit, match="no zone with option name 'wan'"):
        erx.build_access_config(keys, extra, tmp_path / "a.tgz")
    (extra / "etc/config/firewall").write_text("config zone\n\toption name 'wan'\n\tlist network 'wan'\n")
    erx.build_access_config(keys, extra, tmp_path / "a.tgz")


def test_changed_host_key_gives_the_command_to_fix_it(tmp_path):
    env = fake_edgeos(tmp_path)
    ssh = Path(env["PATH"].split(":")[0]) / "ssh"
    ssh.write_text("#!/bin/sh\necho '@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@' >&2\nexit 255\n")
    result = run_tool(env, "check", "ubnt@fe80::1%eth0")
    assert result.returncode != 0
    assert "ssh-keygen -R 'fe80::1%eth0'" in result.stderr


def test_check_refuses_active_poe_output(tmp_path):
    result = run_tool(fake_edgeos(tmp_path, poe_eth4="24v"), "check", "ubnt@router")
    assert result.returncode == 1
    assert "PoE output is on for eth4" in result.stdout


def test_access_config_output_directory_must_exist(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAAC3 ok\n")
    with pytest.raises(SystemExit, match="does not exist"):
        erx.build_access_config(keys, None, tmp_path / "missing" / "a.tgz")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.DIRTYPE])
def test_bridge_tar_with_non_regular_kernel_member_is_refused(tmp_path, kind):
    path = tmp_path / "b.tar"
    with tarfile.open(path, "w") as tar:
        for name in ("compat", "vmlinux.tmp.md5", "squashfs.tmp", "version.tmp"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        link = tarfile.TarInfo("vmlinux.tmp")
        link.type, link.linkname = kind, "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(SystemExit, match="vmlinux.tmp not regular files"):
        erx.verify_bridge_tar(path)


def test_socket_dir_is_made_private(monkeypatch):
    state = Path(tempfile.mkdtemp(dir="/tmp"))            # short enough to be used directly
    try:
        state.chmod(0o755)
        monkeypatch.setattr(erx, "STATE", state)
        assert erx.socket_dir() == state and state.stat().st_mode & 0o077 == 0
    finally:
        shutil.rmtree(state)


@pytest.mark.parametrize("clash", ["etc/dropbear/authorized_keys", "etc/uci-defaults/99-erx-remote-access"])
def test_access_config_refuses_files_it_generates_itself(tmp_path, clash):
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAAC3 ok\n")
    extra = tmp_path / "device"
    (extra / clash).parent.mkdir(parents=True)
    (extra / clash).write_text("x\n")
    with pytest.raises(SystemExit, match="which access-config generates"):
        erx.build_access_config(keys, extra, tmp_path / "a.tgz")


@pytest.mark.parametrize(("args", "message"), [
    (["flash", "ubnt@fe80::1%eth0", "/etc/hosts"], "use root@ADDRESS"),
    (["verify", "fe80::1%eth0"], "use root@ADDRESS"),
    (["check", "fe80::1%eth0"], "needs the EdgeOS user"),
    (["check", "ubnt@fe80::1"], "add the host's interface, e.g. fe80::1%eth0"),
    (["verify", "root@FE80::211:22ff:fe33:4455"], "is link-local: add the host's interface"),
])
def test_wrong_or_missing_ssh_user_is_refused(tmp_path, args, message):
    result = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                            env={**os.environ, "ERX_STATE_DIR": str(tmp_path / "s")})
    assert result.returncode == 2 and message in result.stderr


# ------------------------------------------------ SSH and bridge validation

def test_every_connection_offers_ssh_rsa(tmp_path, monkeypatch):
    monkeypatch.setattr(erx, "STATE", Path(tempfile.mkdtemp(dir="/tmp")))
    try:
        assert "PubkeyAcceptedKeyTypes=+ssh-rsa" in erx.Router("ubnt@r").options
    finally:
        shutil.rmtree(erx.STATE)


def test_key_comment_that_is_not_utf8_is_accepted(tmp_path):
    keys = tmp_path / "keys"
    keys.write_bytes(b"ssh-ed25519 AAAAC3 M\xfcller\n")
    erx.build_access_config(keys, None, tmp_path / "a.tgz")


def tgz_with(path: Path, name: str) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    return path


@pytest.mark.parametrize("name", ["/etc/passwd", "etc/../../evil", "../x"])
def test_flash_refuses_unsafe_config_paths_before_uploading(tmp_path, name):
    image = tmp_path / "sysupgrade.bin"
    make_image(image, b"k", b"r")
    with pytest.raises(SystemExit, match="unsafe paths"):
        erx.check_local_inputs(image, tgz_with(tmp_path / "a.tgz", name))


def test_flash_refuses_a_non_sysupgrade_file_before_uploading(tmp_path):
    other = tmp_path / "kernel.bin"
    other.write_bytes(b"\x27\x05\x19\x56 not a tar")
    with pytest.raises(SystemExit, match="cannot read .*kernel.bin"):
        erx.check_local_inputs(other, None)


def test_access_config_makes_every_dropbear_instance_key_only():
    assert "@dropbear[0]" not in erx.ACCESS_DEFAULTS
    assert "while uci -q get" in erx.ACCESS_DEFAULTS


def test_a_commented_out_wan_zone_does_not_count(tmp_path):
    files = tmp_path / "files"
    (files / "etc/config").mkdir(parents=True)
    (files / "etc/config/firewall").write_text("config zone\n#\toption name 'wan'\n")
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAA a\n")
    with pytest.raises(SystemExit, match="no zone with option name 'wan'"):
        erx.build_access_config(keys, files, tmp_path / "a.tgz")


def test_macos_metadata_files_are_left_out(tmp_path):
    files = tmp_path / "files"
    (files / "etc/config").mkdir(parents=True)
    (files / "etc/config/network").write_text("config interface 'wan'\n\toption device 'eth0'\n")
    (files / "etc/config/._network").write_bytes(b"\x00\x05\x16\x07")
    (files / ".DS_Store").write_bytes(b"x")
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAA a\n")
    erx.build_access_config(keys, files, tmp_path / "a.tgz")
    with tarfile.open(tmp_path / "a.tgz") as tar:
        names = tar.getnames()
    assert "etc/config/network" in names
    assert not any(n.rsplit("/", 1)[-1].startswith(("._", ".DS_Store")) for n in names)


def access_files(tmp_path, network=None, firewall=None):
    files = tmp_path / "files"
    (files / "etc/config").mkdir(parents=True)
    if network is not None:
        (files / "etc/config/network").write_text(network)
    if firewall is not None:
        (files / "etc/config/firewall").write_text(firewall)
    keys = tmp_path / "keys"
    keys.write_text("ssh-ed25519 AAAA a\n")
    return keys, files


def test_network_that_moves_eth0_out_of_wan_is_refused(tmp_path):
    keys, files = access_files(tmp_path, network=(
        "config interface 'wan'\n\toption device 'eth1'\n"
        "config interface 'mgmt'\n\toption device 'eth0'\n"))
    with pytest.raises(SystemExit, match="eth0 is not in the firewall zone 'wan'"):
        erx.build_access_config(keys, files, tmp_path / "a.tgz")


def test_eth0_in_a_bridge_of_a_wan_zone_network_is_accepted(tmp_path):
    keys, files = access_files(
        tmp_path,
        network=("config device\n\toption name 'br-mesh'\n\tlist ports 'eth0'\n\tlist ports 'eth1'\n"
                 "config interface 'mesh'\n\toption device 'br-mesh'\n"),
        firewall="config zone\n\toption name 'wan'\n\toption network 'wan wan6 mesh'\n")
    erx.build_access_config(keys, files, tmp_path / "a.tgz")


def test_firewall_whose_wan_zone_does_not_cover_eth0_is_refused(tmp_path):
    keys, files = access_files(tmp_path, firewall="config zone\n\toption name 'wan'\n\tlist network 'pppoe'\n")
    with pytest.raises(SystemExit, match="eth0 is not in the firewall zone 'wan'"):
        erx.build_access_config(keys, files, tmp_path / "a.tgz")


def test_symbolic_links_in_files_are_refused(tmp_path):
    keys, files = access_files(tmp_path)
    (files / "etc/config/real").write_text("x\n")
    (files / "etc/config/linked").symlink_to(files / "etc/config/real")
    with pytest.raises(SystemExit, match="symbolic links"):
        erx.build_access_config(keys, files, tmp_path / "a.tgz")


def test_file_modes_lose_group_and_other_write(tmp_path):
    keys, files = access_files(tmp_path)
    for name, mode in (("loose", 0o777), ("plain", 0o666), ("secret", 0o600)):
        (files / "etc/config" / name).write_text("x\n")
        (files / "etc/config" / name).chmod(mode)
    erx.build_access_config(keys, files, tmp_path / "a.tgz")
    with tarfile.open(tmp_path / "a.tgz") as tar:
        modes = {n.rsplit("/", 1)[-1]: tar.getmember(n).mode for n in tar.getnames()}
    assert (modes["loose"], modes["plain"], modes["secret"]) == (0o755, 0o644, 0o600)


@pytest.mark.parametrize(("network", "expected"), [
    ("config interface 'wan'\n\toption device 'eth0'\n", {"wan"}),
    ("config device\n\toption name 'br-wan'\n\toption ports 'eth1 eth0'\n"
     "config interface 'wan'\n\toption device 'br-wan'\n", {"wan"}),
    ("config device\n\toption name 'br-x'\n\tlist ports 'eth0'\n"
     "config bridge-vlan\n\toption device 'br-x'\n\toption vlan '1'\n\tlist ports 'eth0:u*'\n"
     "config interface 'mesh'\n\toption device 'br-x.1'\n", {"mesh"}),
    ("config device\n\toption name 'br-x'\n\tlist ports 'eth0'\n"
     "config bridge-vlan\n\toption device 'br-x'\n\toption vlan '5'\n\tlist ports 'eth0:t'\n"
     "config interface 'mesh'\n\toption device 'br-x.5'\n", set()),
    ("config device\n\toption type '8021q'\n\toption ifname 'eth0'\n\toption vid '100'\n\toption name 'eth0.100'\n"
     "config interface 'wan'\n\toption device 'eth0.100'\n", set()),
    ("config interface 'lan'\n\toption device 'br-lan'\n", set()),
])
def test_networks_on_eth0(network, expected):
    assert erx.networks_on_eth0(erx.uci_sections(network)) == expected


def test_zone_named_only_by_its_section_is_refused_with_the_reason(tmp_path):
    keys, files = access_files(tmp_path, firewall="config zone 'wan'\n\tlist network 'wan'\n")
    with pytest.raises(SystemExit, match="not from the section name"):
        erx.build_access_config(keys, files, tmp_path / "a.tgz")


def test_flash_refuses_config_links_and_oversized_config_before_uploading(tmp_path):
    image = tmp_path / "sysupgrade.bin"
    make_image(image, b"k", b"r")
    cfg = tmp_path / "a.tgz"
    with tarfile.open(cfg, "w:gz") as tar:
        link = tarfile.TarInfo("etc/foo")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/shadow"
        tar.addfile(link)
    with pytest.raises(SystemExit, match="links or special files"):
        erx.check_local_inputs(image, cfg)
    cfg.write_bytes(os.urandom(erx.CONFIG_LIMIT + 1))
    with pytest.raises(SystemExit, match="larger than 4 MiB"):
        erx.check_local_inputs(image, cfg)


def test_bridge_tar_members_must_be_regular_files(tmp_path):
    tar_path = tmp_path / "bridge.tar"
    kernel = b"k" * 100
    with tarfile.open(tar_path, "w") as tar:
        for name, data in {"vmlinux.tmp": kernel, "vmlinux.tmp.md5": hashlib.md5(kernel).hexdigest().encode(),
                           "squashfs.tmp": b"s", "version.tmp": b"v"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        compat = tarfile.TarInfo("compat")
        compat.type = tarfile.DIRTYPE
        tar.addfile(compat)
    with pytest.raises(SystemExit, match="compat not regular files"):
        erx.verify_bridge_tar(tar_path)


def test_bridge_md5_file_may_use_uppercase_hex(tmp_path):
    kernel = b"k" * 100
    path = bridge_tar(tmp_path / "b.tar", kernel, md5=hashlib.md5(kernel).hexdigest().upper())
    erx.verify_bridge_tar(path)


def test_check_refuses_when_config_boot_cannot_be_read(tmp_path):
    env = fake_edgeos(tmp_path)
    (tmp_path / "remote/config/config.boot").unlink()
    result = run_tool(env, "check", "ubnt@router")
    assert result.returncode != 0 and "PoE state is unknown" in result.stdout + result.stderr


def test_compressed_sysupgrade_is_refused_before_uploading(tmp_path):
    plain = tmp_path / "plain.bin"
    make_image(plain, b"k", b"r")
    packed = tmp_path / "packed.bin"
    import gzip
    packed.write_bytes(gzip.compress(plain.read_bytes()))
    with pytest.raises(SystemExit, match="cannot read"):
        erx.check_local_inputs(packed, None)


@pytest.mark.parametrize("line", [
    "ssh-ed25519 AAAAC3 alice's laptop",
    'from="10.0.0.1,fe80::/10",command="echo \\"hi there\\"" ssh-rsa AAAAB3 operator\'s key',
])
def test_key_comments_and_options_with_quotes_are_accepted(line):
    assert erx.key_type(line) in ("ssh-ed25519", "ssh-rsa")


def test_empty_bridge_kernel_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="vmlinux.tmp is empty"):
        erx.verify_bridge_tar(bridge_tar(tmp_path / "b.tar", b""))


def test_verify_learns_the_openwrt_host_key_again_on_every_run(tmp_path, monkeypatch):
    # flash runs against the bridge address (MAC + 1), verify against OpenWrt's
    # (base MAC): a key kept from an earlier OpenWrt at that address must not
    # block the first verify of the new one.
    monkeypatch.setattr(erx, "STATE", tmp_path)
    old = erx.fresh_known_hosts("openwrt", "root@r")
    old.write_text("r ssh-ed25519 AAAAold\n")
    assert not erx.fresh_known_hosts("openwrt", "root@r").exists()


def test_config_with_backslash_names_is_refused(tmp_path):
    image = tmp_path / "sysupgrade.bin"
    make_image(image, b"k", b"r")
    with pytest.raises(SystemExit, match="unsafe paths"):
        erx.check_local_inputs(image, tgz_with(tmp_path / "a.tgz", "etc\\config\\network"))
