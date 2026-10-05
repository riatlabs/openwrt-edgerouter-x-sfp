"""PoE setup against mocked UCI, service and GPIO readbacks."""
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).parents[1] / "build/poe-setup.sh"


def run_setup(tmp_path, ports, *, missing="", readback="1"):
    log = tmp_path / "writes"
    fake = tmp_path / "bin"
    fake.mkdir()
    uci = fake / "uci"
    uci.write_text("""#!/bin/sh
[ "$1" != -q ] || shift
if [ "$1" = get ]; then
    case "$2" in
        *"$MISSING"*) [ -z "$MISSING" ] || exit 1 ;;
    esac
    case "$2" in
        *.gpio_pin) echo 123 ;;
        *) echo gpio_switch ;;
    esac
else
    echo "$*" >> "$WRITE_LOG"
fi
""")
    uci.chmod(0o755)
    service = fake / "gpio_switch"
    service.write_text('#!/bin/sh\necho "service $*" >> "$WRITE_LOG"\n')
    service.chmod(0o755)
    gpio = tmp_path / "gpio123"
    gpio.mkdir()
    (gpio / "value").write_text(readback)
    script = tmp_path / "setup.sh"
    script.write_text(SCRIPT.read_text().replace("/etc/init.d/gpio_switch", str(service)))
    result = subprocess.run(["sh", str(script), *ports], capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}",
                                 "WRITE_LOG": str(log), "MISSING": missing,
                                 "ERX_GPIO_SYSFS": str(tmp_path)})
    return result, log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize(("ports", "missing"), [
    ([], ""), (["eth4", "eth5"], ""), (["eth4", "eth2"], "port2"),
    (["eth4"], "gpio_pin"), (["eth4;reboot"], ""),
])
def test_bad_inputs_do_not_change_power(tmp_path, ports, missing):
    result, writes = run_setup(tmp_path, ports, missing=missing)
    assert result.returncode != 0
    assert writes == []


def test_selected_ports_are_enabled_and_read_back(tmp_path):
    result, writes = run_setup(tmp_path, ["eth1", "eth4"])
    assert result.returncode == 0, result.stderr
    assert writes == ["set system.poe_power_port1.value=1", "set system.poe_power_port4.value=1",
                      "commit system", "service restart"]
    assert "readback verified" in result.stdout


def test_failed_readback_is_not_reported_as_success(tmp_path):
    result, writes = run_setup(tmp_path, ["eth4"], readback="0")
    assert writes and result.returncode != 0
    assert "not enabled" in result.stderr
