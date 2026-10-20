#!/data/data/com.termux/files/usr/bin/sh
# Makes the watcher start automatically after the phone reboots.
# Needs the Termux:Boot app (F-Droid), opened once after installing.
cd "$(dirname "$0")" || exit 1
mkdir -p ~/.termux/boot
printf '#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\nsh %s/run.sh\n' "$(pwd)" > ~/.termux/boot/start-watcher
chmod +x ~/.termux/boot/start-watcher run.sh
echo "Boot script installed: ~/.termux/boot/start-watcher"
