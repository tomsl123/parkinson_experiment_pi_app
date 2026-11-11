#!/usr/bin/env bash
# Make the Pi Zero 2 W appear as a USB network adapter (gadget Ethernet) – the wired baseline.
#
#   sudo ./usb_gadget.sh        then reboot
#
# Afterwards connect the Pi's USB *data* port (labelled USB, not PWR) to the computer with a
# data-capable cable. The Pi is 10.55.0.1 and hands the computer an address by DHCP.
# Windows: the device usually appears as "USB Ethernet/RNDIS Gadget"; if it shows up as an
# unknown COM device, pick that driver manually in Device Manager.
# Backups of the two boot files are written next to them as *.bak-linkbench.
set -euo pipefail

CFG=/boot/firmware/config.txt
CMD=/boot/firmware/cmdline.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
[ -f "$CMD" ] || CMD=/boot/cmdline.txt

cp -n "$CFG" "$CFG.bak-linkbench"
cp -n "$CMD" "$CMD.bak-linkbench"

grep -q '^dtoverlay=dwc2' "$CFG" || echo 'dtoverlay=dwc2' >> "$CFG"
grep -q 'modules-load=dwc2,g_ether' "$CMD" || sed -i 's/rootwait/rootwait modules-load=dwc2,g_ether/' "$CMD"

nmcli con delete linkbench-usb >/dev/null 2>&1 || true
nmcli con add type ethernet ifname usb0 con-name linkbench-usb autoconnect yes \
  ipv4.method shared ipv4.addresses 10.55.0.1/24

echo "Done. Reboot the Pi, then plug its USB data port into the computer. Pi address: 10.55.0.1"
