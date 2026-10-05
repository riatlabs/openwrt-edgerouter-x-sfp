#!/bin/sh
# First-boot defaults of the 19.07 RAM bridge (installed by build.sh bridge).
# The bridge runs from RAM between the EdgeOS reboot and the OpenWrt flash;
# it must be reachable over the same port the operator used for EdgeOS.

# Root password (MD5-crypt; filled in by build.sh).
sed -i 's|^root:[^:]*:|root:@ROOT_HASH@:|' /etc/shadow

# SSH as root with the password (and with keys, if the build added any).
uci set dropbear.@dropbear[0].PasswordAuth=1
uci set dropbear.@dropbear[0].RootPasswordAuth=1
uci commit dropbear

# Accept all IPv6 link-local traffic on the WAN port (eth0): the operator
# reaches the router through that port and cannot re-plug the cable.
uci -q delete firewall.bridge_link_local
uci set firewall.bridge_link_local=rule
uci set firewall.bridge_link_local.name='Allow-IPv6-link-local-WAN'
uci set firewall.bridge_link_local.src='wan'
uci set firewall.bridge_link_local.src_ip='fe80::/10'
uci set firewall.bridge_link_local.family='ipv6'
uci set firewall.bridge_link_local.target='ACCEPT'
uci commit firewall
# LAN and WAN keep their separate networks; SSH is accepted from either.
uci -q delete firewall.bridge_ssh
uci set firewall.bridge_ssh=rule
uci set firewall.bridge_ssh.name='Allow-SSH-link-local'
uci set firewall.bridge_ssh.src='*'
uci set firewall.bridge_ssh.src_ip='fe80::/10'
uci set firewall.bridge_ssh.family='ipv6'
uci set firewall.bridge_ssh.proto='tcp'
uci set firewall.bridge_ssh.dest_port='22'
uci set firewall.bridge_ssh.target='ACCEPT'
uci commit firewall

if [ -s /etc/erx-migrate/poe-ports ]; then
    # Port names were validated by build.sh; the helper validates again.
    ports=$(cat /etc/erx-migrate/poe-ports) || exit 1
    # Intentional splitting of the validated, space-separated port names.
    # shellcheck disable=SC2086
    sh /usr/lib/erx-migrate/poe-setup.sh $ports || exit 1
fi
exit 0
