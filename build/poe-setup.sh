#!/bin/sh
# Enable only the passive-PoE ports chosen by the operator.
set -eu

die() { echo "PoE setup: $*" >&2; exit 1; }
[ "$#" -gt 0 ] || die "no ports specified"

# Check every argument and board-generated section before changing anything.
for port in "$@"; do
    case "$port" in eth[0-4]) ;; *) die "invalid port: $port" ;; esac
    section="system.poe_power_port${port#eth}"
    [ "$(uci -q get "$section")" = gpio_switch ] || die "missing GPIO switch for $port"
    pin=$(uci -q get "$section.gpio_pin") || die "missing GPIO for $port"
    case "$pin" in ''|*[!0-9]*) die "invalid GPIO for $port" ;; esac
done

for port in "$@"; do
    uci set "system.poe_power_port${port#eth}.value=1"
done
uci commit system
/etc/init.d/gpio_switch restart

# GPIO numbers come from the board configuration, not a fixed kernel layout.
for port in "$@"; do
    pin=$(uci -q get "system.poe_power_port${port#eth}.gpio_pin")
    value=$(cat "${ERX_GPIO_SYSFS:-/sys/class/gpio}/gpio$pin/value") || die "cannot read $port GPIO"
    [ "$value" = 1 ] || die "$port GPIO is not enabled"
done
echo "PoE GPIO readback verified: $*"
