#!/data/data/com.termux/files/usr/bin/sh
#
# Launch the USB CDC-ACM bridge through termux-usb, which is what supplies the
# file descriptor Android will let an unprivileged app use.
#
#   ./start-usb-bridge.sh                          # auto-detect the device
#   ./start-usb-bridge.sh /dev/bus/usb/001/002     # or name it
#   ./start-usb-bridge.sh --probe                  # inspect descriptors only
#
# Requires: pkg install libusb termux-api  (plus the Termux:API app)

set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
BRIDGE="$SCRIPT_DIR/termux_usb_bridge.py"

if ! command -v termux-usb >/dev/null 2>&1; then
    echo "termux-usb not found. Run: pkg install termux-api" >&2
    exit 1
fi

DEVICE=""
EXTRA=""
for arg in "$@"; do
    case "$arg" in
        /dev/bus/usb/*) DEVICE="$arg" ;;
        *) EXTRA="$EXTRA $arg" ;;
    esac
done

if [ -z "$DEVICE" ]; then
    DEVICE=$(termux-usb -l | tr -d '[]" ' | tr ',' '\n' | grep '^/dev/bus/usb/' | head -n 1)
    if [ -z "$DEVICE" ]; then
        echo "No USB device found. Check the OTG adapter, then run: termux-usb -l" >&2
        exit 1
    fi
    echo "Using $DEVICE" >&2
fi

# termux-usb runs the given program with the descriptor as its first argument.
# Any extra flags are passed along through a generated launcher.
mkdir -p "$SCRIPT_DIR/data"
LAUNCHER="$SCRIPT_DIR/data/usb-bridge-launcher.sh"
# Resolve the shell rather than hardcoding Termux's prefix, which differs
# between installs and does not exist off-device.
SHELL_PATH=$(command -v sh)
cat > "$LAUNCHER" <<LAUNCH
#!$SHELL_PATH
exec ${PYTHON:-python} "$BRIDGE" "\$1"$EXTRA
LAUNCH
chmod +x "$LAUNCHER"

exec termux-usb -r -e "$LAUNCHER" "$DEVICE"
