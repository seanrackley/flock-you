"""
Platform compatibility layer for running the Flock You API on Android (Termux).

Python 3.13 changed ``sys.platform`` from ``"linux"`` to ``"android"`` on
Android. pySerial 3.5 predates that change, so ``serial.tools.list_ports_posix``
no longer recognises the platform and raises ImportError at import time -- which
kills the app before Flask ever starts. ``install_pyserial_shim()`` repairs that
by routing enumeration through pySerial's Linux backend, which is pure
glob + sysfs code and works unmodified on Android.

On top of the import fix this module provides the pieces a phone needs that a
laptop does not:

  * port listings that say *why* a device cannot be opened (Android denies raw
    /dev/tty* access to unprivileged apps),
  * URL-style ports (``socket://host:port``, ``rfc2217://host:port``) so an
    un-rooted phone can reach a sniffer bridged over the network,
  * the phone's own GPS via ``termux-location``, standing in for a USB GPS
    dongle that Android cannot open anyway.
"""

from __future__ import absolute_import

import contextlib
import glob
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import types

# Pseudo-ports that map to a location source instead of a serial device.
TERMUX_LOCATION_PORT = 'termux-location'
BROWSER_GPS_PORT = 'browser-gps'

# Android reports "android" on Python 3.13+, but older builds (and some
# distributions) still say "linux" while clearly running under Android.
IS_ANDROID = (
    sys.platform == 'android'
    or hasattr(sys, 'getandroidapilevel')
    or bool(os.environ.get('ANDROID_ROOT') and os.environ.get('ANDROID_DATA'))
)

IS_TERMUX = 'com.termux' in os.environ.get('PREFIX', '') or os.path.isdir('/data/data/com.termux/files/usr')

# Nodes that Android/Termux may expose beyond the set pySerial's Linux backend
# already globs. These are mostly internal modem/console ports, so they are only
# surfaced when this process can actually open them.
_ANDROID_EXTRA_GLOBS = (
    '/dev/ttyGS*',    # USB gadget serial (phone acting as a USB device)
    '/dev/ttyHS*',    # Qualcomm high-speed UART
    '/dev/ttyHSL*',
    '/dev/ttyMSM*',
)

# Used only if pySerial's Linux backend cannot be imported at all.
_FALLBACK_GLOBS = (
    '/dev/ttyUSB*',
    '/dev/ttyACM*',
    '/dev/ttyXRUSB*',
    '/dev/ttyS*',
    '/dev/rfcomm*',
) + _ANDROID_EXTRA_GLOBS


# - - - - - - - - - - - - - - pySerial import shim - - - - - - - - - - - - - -

def _glob_comports(patterns, include_links=False):
    """Build ListPortInfo entries from raw device globs."""
    from serial.tools import list_ports_common

    devices = []
    for pattern in patterns:
        devices.extend(glob.glob(pattern))
    if include_links:
        devices.extend(list_ports_common.list_links(devices))
    return [list_ports_common.ListPortInfo(d) for d in sorted(set(devices))]


def _android_comports(include_links=False):
    """Enumerate serial ports on Android.

    Android is Linux, so pySerial's Linux backend gives full sysfs detail (VID,
    PID, product strings) for USB-OTG adapters. Only the platform *detection*
    was broken, not the enumeration itself.
    """
    ports = []
    try:
        from serial.tools.list_ports_linux import comports as linux_comports
        ports.extend(linux_comports(include_links))
    except Exception:
        ports.extend(_glob_comports(_FALLBACK_GLOBS, include_links))

    # Add vendor-specific nodes the Linux backend does not glob, but only the
    # ones that are genuinely usable -- a phone has dozens of internal ttys and
    # listing them all would bury the device the user is looking for.
    known = {p.device for p in ports}
    for info in _glob_comports(_ANDROID_EXTRA_GLOBS, include_links):
        if info.device not in known and os.access(info.device, os.R_OK | os.W_OK):
            ports.append(info)

    return ports


def install_pyserial_shim():
    """Make ``import serial.tools.list_ports`` work on Android.

    Must be called *before* anything imports ``serial.tools.list_ports``.
    Returns True if the shim was installed, False if the stock backend already
    works (a newer pySerial, or any non-Android platform).
    """
    if 'serial.tools.list_ports' in sys.modules:
        return False

    import serial.tools

    # Probe the stock backend. pySerial writes a multi-line "don't know how to
    # enumerate ttys" banner to stderr before giving up, so keep it quiet.
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            importlib.import_module('serial.tools.list_ports_posix')
        return False
    except ImportError:
        pass

    shim = types.ModuleType('serial.tools.list_ports_posix')
    shim.__doc__ = 'Android compatibility shim installed by android_compat.py'
    shim.comports = _android_comports
    sys.modules['serial.tools.list_ports_posix'] = shim
    serial.tools.list_ports_posix = shim
    return True


# - - - - - - - - - - - - - - - port listing - - - - - - - - - - - - - - - - -

def _port_note(device):
    """Explain, on Android, why a listed port may not open."""
    if not IS_ANDROID:
        return None
    if os.access(device, os.R_OK | os.W_OK):
        return None
    return ('Android denies raw serial access to unprivileged apps. '
            'Use a rooted device, or bridge the sniffer with ser2net and '
            'connect to socket://host:port instead.')


def list_serial_ports():
    """Return serial ports as JSON-ready dicts, including Android pseudo-ports."""
    import serial.tools.list_ports

    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append({
            'device': port.device,
            'description': port.description,
            'manufacturer': port.manufacturer if port.manufacturer else 'Unknown',
            'product': port.product if port.product else 'Unknown',
            'vid': port.vid,
            'pid': port.pid,
            'accessible': os.access(port.device, os.R_OK | os.W_OK),
            'note': _port_note(port.device),
        })
    return ports


def bridge_state_file():
    return os.environ.get(
        'FLOCKYOU_BRIDGE_STATE',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'usb_bridge.json'))


def _process_alive(pid):
    """True if the pid exists. EPERM still means something is running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def usb_bridge_ports():
    """Offer a running termux_usb_bridge.py as a ready-made port.

    The bridge writes a small state file when it starts listening, so the URL
    can be presented in the dropdown instead of typed by hand. A stale file left
    by a crashed bridge is filtered out by checking the pid.
    """
    path = bridge_state_file()
    try:
        with open(path) as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return []

    host = state.get('host') or '127.0.0.1'
    port = state.get('port')
    if not port or not _process_alive(state.get('pid')):
        return []

    url = 'socket://{}:{}'.format(host, port)
    device = state.get('device') or 'USB CDC-ACM device'
    return [{
        'device': url,
        'description': 'USB bridge - {}'.format(device),
        'manufacturer': 'termux-usb',
        'product': device,
        'vid': None,
        'pid': None,
        'accessible': True,
        'note': 'Served by termux_usb_bridge.py, which must stay running.',
    }]


def gps_pseudo_ports():
    """GPS sources that are not serial devices.

    The browser source is always offered: it needs nothing installed, and it is
    the only option on the Google Play build of Termux, where Termux:API is not
    available. termux-location is listed only when its helper is present.
    """
    sources = [{
        'device': BROWSER_GPS_PORT,
        'description': "Phone's built-in GPS (browser)",
        'manufacturer': 'Browser',
        'product': 'Geolocation API',
        'vid': None,
        'pid': None,
        'accessible': True,
        'note': ('The browser showing this page supplies the position. Requires '
                 'a secure context: works over localhost, but needs HTTPS if you '
                 'load the dashboard from another device.'),
    }]

    if termux_location_available():
        sources.append({
            'device': TERMUX_LOCATION_PORT,
            'description': "Phone's built-in GPS (Termux:API)",
            'manufacturer': 'Android',
            'product': 'Location services',
            'vid': None,
            'pid': None,
            'accessible': True,
            'note': 'Uses the phone GPS. Grant Termux:API location permission first.',
        })

    return sources


# - - - - - - - - - - - - - - opening a port - - - - - - - - - - - - - - - - -

def open_serial(port, baudrate, timeout=1):
    """Open a serial port, or a pySerial URL such as ``socket://host:port``.

    URL support is what makes an un-rooted phone usable: point it at a ser2net
    bridge (or any TCP source of the sniffer's output) over WiFi.
    """
    import serial

    if '://' in port:
        return serial.serial_for_url(port, baudrate=baudrate, timeout=timeout)
    return serial.Serial(port, baudrate, timeout=timeout)


def describe_serial_error(port, exc):
    """Turn a pySerial exception into a message that suggests a way forward."""
    message = str(exc)
    if not IS_ANDROID or '://' in port:
        return message

    lowered = message.lower()
    if 'permission denied' in lowered or isinstance(exc, PermissionError):
        hint = ("Android blocks direct access to {}. Options: run Termux as root, "
                "or bridge the device from another machine with ser2net and "
                "connect to socket://host:port.").format(port)
    elif 'no such file' in lowered or 'could not open port' in lowered:
        usb = termux_usb_devices()
        if usb:
            hint = ("The kernel did not create a serial node for the USB device. "
                    "Android sees {} on the USB bus but Termux cannot expose it as "
                    "a tty without root. Bridge it over the network "
                    "(socket://host:port) instead.").format(', '.join(usb))
        else:
            hint = ("No USB device detected. Check the OTG adapter and cable, and "
                    "confirm the phone supports USB host mode.")
    else:
        return message

    return '{} -- {}'.format(message, hint)


# - - - - - - - - - - - - - - Termux:API bridges - - - - - - - - - - - - - - -

def _run_termux(args, timeout):
    """Run a termux-api helper and return parsed JSON, or None on any failure."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def termux_location_available():
    return shutil.which('termux-location') is not None


def read_termux_location(provider='gps', timeout=45):
    """Read one fix from the phone's GPS, shaped like ``parse_nmea_sentence()``.

    ``termux-location`` blocks until the provider returns a fix, which is why
    the timeout is generous. Returns None if no fix was obtained.
    """
    data = _run_termux(['termux-location', '-p', provider, '-r', 'once'], timeout)
    if data is None:
        # A cold GPS can take longer than the timeout; fall back to the last
        # known fix so the map has something rather than nothing.
        data = _run_termux(['termux-location', '-p', provider, '-r', 'last'], 10)
    if not isinstance(data, dict):
        return None

    latitude = data.get('latitude')
    longitude = data.get('longitude')
    if latitude is None or longitude is None:
        return None

    fix = {
        'latitude': round(float(latitude), 8),
        'longitude': round(float(longitude), 8),
        'altitude': round(float(data.get('altitude') or 0.0), 3),
        'fix_quality': 1,
        'satellites': 0,
        'hdop': data.get('accuracy'),
        'provider': data.get('provider', provider),
    }
    if data.get('accuracy') is not None:
        fix['accuracy'] = data['accuracy']
    return fix


def termux_usb_devices():
    """List USB devices Android can see, for diagnostics. Empty if unavailable."""
    if shutil.which('termux-usb') is None:
        return []
    devices = _run_termux(['termux-usb', '-l'], 10)
    return devices if isinstance(devices, list) else []


# - - - - - - - - - - - - - - - diagnostics - - - - - - - - - - - - - - - - - -

def platform_report():
    """Platform facts worth showing at startup and over the API."""
    report = {
        'platform': sys.platform,
        'python': sys.version.split()[0],
        'is_android': IS_ANDROID,
        'is_termux': IS_TERMUX,
        'pyserial_shim_active': isinstance(
            sys.modules.get('serial.tools.list_ports_posix'), types.ModuleType
        ) and getattr(sys.modules.get('serial.tools.list_ports_posix'), '__file__', None) is None,
    }
    if IS_ANDROID:
        report['termux_api'] = {
            'termux-location': termux_location_available(),
            'termux-usb': shutil.which('termux-usb') is not None,
        }
        report['usb_devices'] = termux_usb_devices()
        report['root'] = os.geteuid() == 0
    return report


def print_startup_banner():
    """Print Android-specific guidance so field problems are diagnosable."""
    if not IS_ANDROID:
        return

    report = platform_report()
    print("Android platform detected (sys.platform={}, Termux={})".format(
        report['platform'], report['is_termux']))
    if report['pyserial_shim_active']:
        print("  pySerial port enumeration shim: active")
    if not report['termux_api']['termux-location']:
        print("  termux-location not found -- install Termux:API app + 'pkg install termux-api'")
        print("  for phone GPS instead of a USB dongle")
    if not report['root']:
        print("  Not running as root: direct /dev/tty* access will likely fail.")
        print("  Use socket://host:port to reach a sniffer bridged over the network.")
    if report['usb_devices']:
        print("  USB devices visible to Android: {}".format(', '.join(report['usb_devices'])))
