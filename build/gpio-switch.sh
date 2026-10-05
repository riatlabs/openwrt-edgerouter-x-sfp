#!/bin/sh /etc/rc.common
# Copyright (C) 2015 OpenWrt.org
# Based on OpenWrt 25.12.5 package/base-files/files/etc/init.d/gpio_switch.
# Space enabled ER-X-SFP PoE outputs by 0.5 seconds on every start/reload.

# OpenWrt rc.common consumes these variables; its BusyBox ash supports local.
# shellcheck disable=SC2034,SC3043
START=94
STOP=10
USE_PROCD=1


load_gpio_switch()
{
	local name
	local gpio_pin
	local value
	local poe=0

	[ "$_erx_gpio_failed" = 0 ] || return 1

	config_get gpio_pin "$1" gpio_pin
	config_get name "$1" name
	config_get value "$1" value 0

	[ -z "$gpio_pin" ] && {
		echo >&2 "Skipping gpio_switch '$name' due to missing gpio_pin"
		_erx_gpio_failed=1
		return 1
	}

	case "$1" in
		poe_power_port[0-4]) [ "$value" = 0 ] || poe=1 ;;
	esac
	if [ "$poe" = 1 ] && [ "$_erx_poe_started" = 1 ]; then
		sleep 0.5 || { _erx_gpio_failed=1; return 1; }
	fi

	local gpio_path
	if [ -n "$(echo "$gpio_pin" | grep -E "^[0-9]+$")" ]; then
		gpio_path="/sys/class/gpio/gpio${gpio_pin}"

		# export GPIO pin for access
		[ -d "$gpio_path" ] || {
			echo "$gpio_pin" >/sys/class/gpio/export
			# we need to wait a bit until the GPIO appears
			[ -d "$gpio_path" ] || sleep 1
		}

		# direction attribute only exists if the kernel supports changing the
		# direction of a GPIO
		if [ -e "${gpio_path}/direction" ]; then
			# set the pin to output with high or low pin value
			{ [ "$value" = "0" ] && echo "low" || echo "high"; } \
				>"$gpio_path/direction"
		else
			{ [ "$value" = "0" ] && echo "0" || echo "1"; } \
				>"$gpio_path/value"
		fi
	else
		gpio_path="/sys/class/gpio/${gpio_pin}"

		[ -d "$gpio_path" ] && {
			{ [ "$value" = "0" ] && echo "0" || echo "1"; } \
				>"$gpio_path/value"
		}
	fi
	if [ "$poe" = 1 ]; then
		if [ "$(cat "$gpio_path/value")" != 1 ]; then
			echo >&2 "PoE GPIO readback failed: $1"
			_erx_gpio_failed=1
			return 1
		fi
		_erx_poe_started=1
	fi
}

service_triggers()
{
	procd_add_reload_trigger "system"
}

start_service()
{
	[ -e /sys/class/gpio/ ] && {
		_erx_gpio_failed=0
		_erx_poe_started=0
		config_load system
		config_foreach load_gpio_switch gpio_switch
		[ "$_erx_gpio_failed" = 0 ]
	}
}
