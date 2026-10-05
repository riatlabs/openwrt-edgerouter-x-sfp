"""Run the boot service against GPIO files and mocked OpenWrt callbacks."""
import os
from pathlib import Path
import subprocess

import pytest

SERVICE = Path(__file__).parents[1] / "build/gpio-switch.sh"


def run_service(tmp_path, enabled, *, failure="", boots=1, sleep_fails=False, direction=False):
    root = tmp_path / "gpio"
    root.mkdir()
    for pin in range(5):
        gpio = root / f"gpio{pin}"
        gpio.mkdir()
        (gpio / "value").write_text("0\n")
        if direction:
            (gpio / "direction").write_text("low\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    # Capture actual GPIO states when the pause occurs, without real sleeps.
    sleep = fake / "sleep"
    sleep.write_text('''#!/bin/sh
printf 'sleep %s:' "$1" >> "$LOG"
for pin in 0 1 2 3 4; do
    if [ -e "$GPIO/gpio$pin/direction" ]; then
        case "$(/bin/cat "$GPIO/gpio$pin/direction")" in high) value=1 ;; *) value=0 ;; esac
    else value=$(/bin/cat "$GPIO/gpio$pin/value"); fi
    printf ' %s' "$value" >> "$LOG"
done
printf '\n' >> "$LOG"
[ "$SLEEP_FAILS" != 1 ]
''')
    cat = fake / "cat"
    cat.write_text('''#!/bin/sh
pin=${1%/value}; pin=${pin##*gpio}
echo "read $pin" >> "$LOG"
if [ "$FAILURE" = "$pin" ]; then echo 0
elif [ -e "${1%/value}/direction" ]; then
    case "$(/bin/cat "${1%/value}/direction")" in high) echo 1 ;; *) echo 0 ;; esac
else /bin/cat "$@"; fi
''')
    sleep.chmod(0o755)
    cat.chmod(0o755)
    service = tmp_path / "service.sh"
    service.write_text(SERVICE.read_text().replace("/sys/class/gpio", str(root)))
    harness = tmp_path / "boot.sh"
    harness.write_text('''#!/bin/sh
. "$SERVICE"
config_load() { :; }
config_get() {
    case "$3" in
        gpio_pin) eval "$1=${2#poe_power_port}" ;;
        name) eval "$1=PoE" ;;
        value)
            case " $ENABLED " in
                *" ${2#poe_power_port} "*) eval "$1=1" ;;
                *) eval "$1=0" ;;
            esac ;;
    esac
}
config_foreach() {
    for pin in 0 1 2 3 4; do "$1" "poe_power_port$pin"; done
}
for boot in $(seq 1 "$BOOTS"); do
    for pin in 0 1 2 3 4; do
        echo 0 > "$GPIO/gpio$pin/value"
        [ ! -e "$GPIO/gpio$pin/direction" ] || echo low > "$GPIO/gpio$pin/direction"
    done
    start_service || exit 1
done
''')
    log = tmp_path / "log"
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}",
           "GPIO": str(root), "LOG": str(log), "SERVICE": str(service),
           "ENABLED": " ".join(map(str, enabled)), "FAILURE": failure,
           "BOOTS": str(boots), "SLEEP_FAILS": "1" if sleep_fails else "0"}
    result = subprocess.run(["sh", str(harness)], env=env, text=True, capture_output=True)
    values = [("1" if (root / f"gpio{pin}/direction").read_text().strip() == "high" else "0")
              if direction else (root / f"gpio{pin}/value").read_text().strip()
              for pin in range(5)]
    return result, log.read_text().splitlines() if log.exists() else [], values


@pytest.mark.parametrize("direction", [False, True])
def test_half_second_pause_between_actual_port_writes(tmp_path, direction):
    result, log, values = run_service(tmp_path, [1, 4], direction=direction)
    assert result.returncode == 0, result.stderr
    assert log == ["read 1", "sleep 0.5: 0 1 0 0 0", "read 4"]
    assert values == ["0", "1", "0", "0", "1"]


@pytest.mark.parametrize("enabled", [[], [4]])
def test_no_pause_for_zero_or_one_enabled_port(tmp_path, enabled):
    result, log, _ = run_service(tmp_path, enabled)
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("sleep") for line in log)


def test_each_boot_repeats_the_stagger(tmp_path):
    result, log, _ = run_service(tmp_path, [0, 2, 4], boots=2)
    assert result.returncode == 0, result.stderr
    one_boot = ["read 0", "sleep 0.5: 1 0 0 0 0", "read 2",
                "sleep 0.5: 1 0 1 0 0", "read 4"]
    assert log == one_boot * 2


def test_readback_failure_stops_before_next_port(tmp_path):
    result, log, values = run_service(tmp_path, [1, 4], failure="1")
    assert result.returncode != 0
    assert log == ["read 1"]
    assert values[4] == "0"


def test_failed_pause_stops_before_next_port(tmp_path):
    result, log, values = run_service(tmp_path, [1, 4], sleep_fails=True)
    assert result.returncode != 0
    assert log == ["read 1", "sleep 0.5: 0 1 0 0 0"]
    assert values[4] == "0"
