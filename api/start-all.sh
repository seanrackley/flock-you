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
    # Kill the supervisor first so it does not relaunch the bridge we are about
    # to stop.
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

# Keep the bridge running across cable disconnects. Android hands out the USB
# descriptor for one termux-usb session only, so a replug cannot be recovered
# from inside the bridge -- it has to be relaunched.
supervise_bridge() {
    failures=0
    launches=0
    while :; do
        # termux-usb -l raises no permission dialog, so polling for the device
        # keeps an unplugged cable quiet instead of prompting repeatedly.
        if ! termux-usb -l 2>/dev/null | grep -q '/dev/bus/usb/'; then
            sleep 3
            continue
        fi

        launches=$((launches + 1))
        echo "[bridge] launch #$launches - accept the Android USB prompt if it appears"
        started=$(date +%s)
        # shellcheck disable=SC2086
        sh "$SCRIPT_DIR/start-usb-bridge.sh" $BRIDGE_ARGS
        status=$?
        ran=$(( $(date +%s) - started ))
        echo "[bridge] launch #$launches exited with status $status after ${ran}s"

        # Exit status 3 means the cable was pulled, which is not a fault however
        # briefly the bridge ran -- a loose connector in a vehicle can flap
        # repeatedly and must not exhaust the retry budget. Anything else that
        # dies quickly is a real failure to start.
        if [ "$status" = "3" ] || [ "$ran" -ge 15 ]; then
            failures=0
        else
            failures=$((failures + 1))
        fi

        if [ "$failures" -ge 3 ]; then
            echo ""
            echo "bridge failed 3 times in a row - not retrying."
            echo "check ./start-usb-bridge.sh --probe, then rerun ./start-all.sh"
            return
        fi

        [ "$ran" -ge 15 ] && echo "[bridge] watching for the device to come back..."
        sleep $((failures * 5 + 2))
    done
}

if [ "$WANT_BRIDGE" = "1" ]; then
    # Give the dashboard a moment so its startup output is not interleaved with
    # the USB permission prompt.
    sleep 2
    echo "starting USB bridge..."
    case " $BRIDGE_ARGS " in
        *" --probe "*)
            # --probe exits by design, so supervising it would loop forever.
            # shellcheck disable=SC2086
            sh "$SCRIPT_DIR/start-usb-bridge.sh" $BRIDGE_ARGS &
            ;;
        *)
            supervise_bridge &
            ;;
    esac
    BRIDGE_PID=$!
fi

echo ""
echo "dashboard: http://localhost:${FLOCKYOU_PORT:-5000}"
echo "press Ctrl+C to stop everything"
echo ""

# The dashboard is what we wait on: if the bridge stops for good, imports,
# exports and browser GPS all still work without a sniffer.
wait "$DASHBOARD_PID"
