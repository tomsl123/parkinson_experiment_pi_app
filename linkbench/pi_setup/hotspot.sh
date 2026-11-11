#!/usr/bin/env bash
# Turn the Pi's Wi-Fi into an access point for linkbench (Raspberry Pi OS Bookworm, NetworkManager).
#
#   sudo HOTSPOT_PASS='choose-a-password' ./hotspot.sh [ssid] [channel]
#
# WARNING: wlan0 leaves your normal Wi-Fi while the hotspot is up. If you reach the Pi over
# Wi-Fi SSH, you will lose that session – use the USB gadget link or a keyboard instead.
# The Pi Zero 2 W only has 2.4 GHz Wi-Fi, so the band is fixed to bg.
# Revert:  sudo nmcli con down linkbench-ap   (your old Wi-Fi reconnects automatically)
set -euo pipefail

SSID="${1:-MOVES-BENCH}"
CHANNEL="${2:-6}"
: "${HOTSPOT_PASS:?set HOTSPOT_PASS (at least 8 characters)}"

nmcli con delete linkbench-ap >/dev/null 2>&1 || true
nmcli con add type wifi ifname wlan0 con-name linkbench-ap autoconnect no ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel "$CHANNEL" \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASS"
nmcli con up linkbench-ap

echo "Hotspot '$SSID' is up on channel $CHANNEL. The Pi is 10.42.0.1; the computer gets an address by DHCP."
