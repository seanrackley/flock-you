#!/data/data/com.termux/files/usr/bin/sh
#
# Run the dashboard and the USB bridge together in a single Termux session.
#
# The bridge cannot be merged into the dashboard process: termux-usb only hands
# the USB file descriptor to a program it launches itself, so the bridge has to
# be a child of termux-usb. This script supervises both instead, so there is one
# session to watch and one Ctrl+C to stop everything.
#
#   ./start-all.sh                       # dashboard + bridge
#   ./start-all.sh --no-bridge           # dashboard only
#   ./start-all.sh /dev/bus/usb/001/002  # name the USB device explicitly
#
# Anything else is passed through to the bridge (--port, --baud, --verbose).

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python}

WANT_BRIDGE=1
BRIDGE_ARGS=""
for arg in "$@"; do
    case "$arg" in
        --no-bridge) WANT_BRIDGE=0 ;;
        *) BRIDGE_ARGS="$BRIDGE_ARGS $arg" ;;
    esac
done

DASHBOARD_PID=""
BRIDGE_PID=""

cleanup() {
    trap - INT TERM EXIT
    echo ""
    echo "shutting down..."
    [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null
    [ -n "$DASHBOARD_PID" ] && kill "$DASHBOARD_PID" 2>/dev/null
    # termux-usb runs the bridge as its own child, which may outlive its parent.
    pkill -f termux_usb_bridge.py 2>/dev/null
    exit 0
}
trap cleanup INT TERM EXIT

echo "starting dashboard..."
$PYTHON "$SCRIPT_DIR/flockyou.py" &
DASHBOARD_PID=$!

if [ "$WANT_BRIDGE" = "1" ]; then
    # Give the dashboard a moment so its startup output is not interleaved with
    # the USB permission prompt.
    sleep 2
    echo "starting USB bridge..."
    # shellcheck disable=SC2086
    sh "$SCRIPT_DIR/start-usb-bridge.sh" $BRIDGE_ARGS &
    BRIDGE_PID=$!
fi

echo ""
echo "dashboard: http://localhost:${FLOCKYOU_PORT:-5000}"
echo "press Ctrl+C to stop everything"
echo ""

# If the bridge exits (no device, unplugged, not CDC-ACM) the dashboard carries
# on: imports, exports and browser GPS all still work without a sniffer.
wait "$DASHBOARD_PID"
