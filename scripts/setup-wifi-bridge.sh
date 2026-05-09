#!/bin/bash
# Configure the Pi as a single-radio AP+STA WiFi bridge.
#
# Why: the Midea WiFi dongle's pairing app (NetHome Plus) requires a
# WPA2-protected SSID, but the hangar WiFi may be open. This script runs the
# Pi as a client on the hangar WiFi (wlan0) while broadcasting a
# WPA2-protected SSID (uap0) on the same radio. The Midea dongle pairs to
# uap0, NATs out through wlan0 for the one-time cloud handshake, then lives
# on uap0 permanently. The Pi reaches it directly across the AP subnet.
#
# Constraint: single-radio AP+STA must share a channel. AP_CHAN must match
# the hangar WiFi's current channel or hostapd will fail to start.
#
# Usage:
#   sudo AP_PASS=<pw> AP_CHAN=<n> ./scripts/setup-wifi-bridge.sh
#
# Run on the Pi after wlan0 is already associated to the hangar WiFi.

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

AP_SSID="${AP_SSID:-hvac-pair}"
AP_PASS="${AP_PASS:-}"
AP_CHAN="${AP_CHAN:-}"
AP_NET="${AP_NET:-192.168.50}"

if [ ${#AP_PASS} -lt 8 ]; then
  echo "Set AP_PASS to a string of 8+ chars (WPA2 minimum)." >&2
  echo "Example: sudo AP_PASS=mypassword AP_CHAN=3 $0" >&2
  exit 1
fi

if [ -z "$AP_CHAN" ]; then
  echo "AP_CHAN must match the hangar WiFi's channel." >&2
  echo "Find it with: iw dev wlan0 link | grep freq" >&2
  echo "  2412->1, 2417->2, 2422->3, 2427->4, 2432->5, 2437->6," >&2
  echo "  2442->7, 2447->8, 2452->9, 2457->10, 2462->11" >&2
  exit 1
fi

echo "Installing packages..."
apt update
DEBIAN_FRONTEND=noninteractive apt install -y hostapd dnsmasq iptables-persistent
systemctl unmask hostapd
systemctl stop hostapd dnsmasq || true

echo "Writing /etc/systemd/system/uap0.service..."
cat > /etc/systemd/system/uap0.service <<EOF
[Unit]
Description=Create uap0 virtual AP interface
After=sys-subsystem-net-devices-wlan0.device
Wants=sys-subsystem-net-devices-wlan0.device
Before=hostapd.service dnsmasq.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/iw dev wlan0 interface add uap0 type __ap
ExecStart=/sbin/ip addr add ${AP_NET}.1/24 dev uap0
ExecStart=/sbin/ip link set uap0 up
ExecStop=/sbin/iw dev uap0 del

[Install]
WantedBy=multi-user.target
EOF

echo "Writing /etc/hostapd/hostapd.conf..."
# logger_*_level=4 (warning+) suppresses per-association info noise.
# Defensive against any cause of dongle assoc/disassoc cycling — without
# it, a flap (e.g. the UFW DHCP-block bug fixed below) produces thousands
# of hostapd lines/day and fills log2ram's 128M tmpfs.
cat > /etc/hostapd/hostapd.conf <<EOF
interface=uap0
driver=nl80211
ssid=${AP_SSID}
hw_mode=g
channel=${AP_CHAN}
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=${AP_PASS}
logger_syslog=-1
logger_syslog_level=4
logger_stdout=-1
logger_stdout_level=4
EOF
sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

echo "Writing /etc/dnsmasq.d/uap0.conf..."
cat > /etc/dnsmasq.d/uap0.conf <<EOF
interface=uap0
bind-interfaces
dhcp-range=${AP_NET}.50,${AP_NET}.150,255.255.255.0,12h
dhcp-option=3,${AP_NET}.1
dhcp-option=6,1.1.1.1,8.8.8.8
EOF

if systemctl is-active --quiet NetworkManager; then
  echo "Telling NetworkManager to leave uap0 alone..."
  cat > /etc/NetworkManager/conf.d/uap0-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:uap0
EOF
  systemctl reload NetworkManager
fi

if systemctl is-active --quiet dhcpcd; then
  if ! grep -q "denyinterfaces uap0" /etc/dhcpcd.conf; then
    echo "Telling dhcpcd to leave uap0 alone..."
    echo "denyinterfaces uap0" >> /etc/dhcpcd.conf
  fi
fi

echo "Enabling IP forwarding and NAT..."
sed -i 's|^#net.ipv4.ip_forward=1|net.ipv4.ip_forward=1|' /etc/sysctl.conf
sysctl -w net.ipv4.ip_forward=1 >/dev/null

iptables -t nat -C POSTROUTING -o wlan0 -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE
iptables -C FORWARD -i wlan0 -o uap0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -i wlan0 -o uap0 -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -C FORWARD -i uap0 -o wlan0 -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -i uap0 -o wlan0 -j ACCEPT
netfilter-persistent save >/dev/null

# UFW's default after.rules silently DROPs inbound UDP/67 to suppress nuisance
# broadcast spam — and that drop fires before user-defined rules unless they're
# loaded into ufw-user-input. Without this, the dongle's DHCPDISCOVERs never
# reach dnsmasq, the dongle never gets an IP, and it cycles assoc/disassoc
# every ~31s (the 1+2+4+8+16s DHCPDISCOVER backoff). See CLAUDE.md "WiFi bridge"
# section for the diagnostic recipe; this took a day to find the first time.
if systemctl is-active --quiet ufw; then
  echo "Allowing uap0 traffic through UFW (else DHCP server is silently dropped)..."
  ufw allow in on uap0 >/dev/null
  ufw reload >/dev/null
fi

echo "Enabling services..."
systemctl daemon-reload
systemctl enable uap0 hostapd dnsmasq

cat <<EOF

Setup complete. Reboot to bring up the bridge:
  sudo reboot

After reboot, verify:
  ip addr show uap0                # should show ${AP_NET}.1
  iw dev uap0 info                 # type AP
  systemctl status hostapd dnsmasq # both active
  iw dev wlan0 link                # still associated to hangar WiFi
  ping -c2 1.1.1.1                 # internet works

SSID: ${AP_SSID}  channel: ${AP_CHAN}  subnet: ${AP_NET}.0/24
EOF
