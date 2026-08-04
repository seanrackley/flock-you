#!/usr/bin/env python3
"""
USB CDC-ACM to TCP bridge for un-rooted Android phones.

Android binds a kernel driver to a USB serial adapter (creating /dev/ttyACM0)
but leaves the node owned by root with mode 0600, so an unprivileged app can
never open it. Termux:API's ``termux-usb`` sidesteps the kernel entirely: it
asks Android's UsbManager for permission and hands the caller a file descriptor
for the raw USB device.

pySerial cannot consume such a descriptor, so this bridge does the CDC-ACM
protocol itself through libusb and re-exposes the stream over TCP. The dashboard
then connects to it as a normal pySerial URL:

    socket://127.0.0.1:4000

Launch it with ./start-usb-bridge.sh, which handles the termux-usb invocation.
termux-usb execs a single program and passes the descriptor as its only
argument, so the wrapper generates a small launcher to carry any extra flags.

Use --probe first: it prints the device's descriptors and exits, which is the
fastest way to see whether the interfaces look like CDC-ACM.

Requires: pkg install libusb
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import signal
import socket
import struct
import sys
import threading
import time

# Where the running bridge announces itself so the dashboard can offer it in the
# port dropdown instead of making the user type a socket:// URL by hand.
DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data', 'usb_bridge.json')

# - - - - - - - - - - - - - - libusb constants - - - - - - - - - - - - - - - -

LIBUSB_OPTION_NO_DEVICE_DISCOVERY = 2  # a.k.a. WEAK_AUTHORITY; must precede init

LIBUSB_SUCCESS = 0
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_NOT_SUPPORTED = -12

LIBUSB_CLASS_COMM = 0x02       # CDC control interface
LIBUSB_CLASS_DATA = 0x0A       # CDC data interface

LIBUSB_TRANSFER_TYPE_MASK = 0x03
LIBUSB_TRANSFER_TYPE_BULK = 0x02
LIBUSB_ENDPOINT_IN = 0x80

# CDC class requests (USB CDC PSTN subclass)
CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22
CDC_REQUEST_TYPE_OUT = 0x21    # host-to-device | class | interface


# - - - - - - - - - - - - - - libusb structures - - - - - - - - - - - - - - - -

class DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ('bLength', ctypes.c_uint8),
        ('bDescriptorType', ctypes.c_uint8),
        ('bcdUSB', ctypes.c_uint16),
        ('bDeviceClass', ctypes.c_uint8),
        ('bDeviceSubClass', ctypes.c_uint8),
        ('bDeviceProtocol', ctypes.c_uint8),
        ('bMaxPacketSize0', ctypes.c_uint8),
        ('idVendor', ctypes.c_uint16),
        ('idProduct', ctypes.c_uint16),
        ('bcdDevice', ctypes.c_uint16),
        ('iManufacturer', ctypes.c_uint8),
        ('iProduct', ctypes.c_uint8),
        ('iSerialNumber', ctypes.c_uint8),
        ('bNumConfigurations', ctypes.c_uint8),
    ]


class EndpointDescriptor(ctypes.Structure):
    _fields_ = [
        ('bLength', ctypes.c_uint8),
        ('bDescriptorType', ctypes.c_uint8),
        ('bEndpointAddress', ctypes.c_uint8),
        ('bmAttributes', ctypes.c_uint8),
        ('wMaxPacketSize', ctypes.c_uint16),
        ('bInterval', ctypes.c_uint8),
        ('bRefresh', ctypes.c_uint8),
        ('bSynchAddress', ctypes.c_uint8),
        ('extra', ctypes.POINTER(ctypes.c_ubyte)),
        ('extra_length', ctypes.c_int),
    ]


class InterfaceDescriptor(ctypes.Structure):
    _fields_ = [
        ('bLength', ctypes.c_uint8),
        ('bDescriptorType', ctypes.c_uint8),
        ('bInterfaceNumber', ctypes.c_uint8),
        ('bAlternateSetting', ctypes.c_uint8),
        ('bNumEndpoints', ctypes.c_uint8),
        ('bInterfaceClass', ctypes.c_uint8),
        ('bInterfaceSubClass', ctypes.c_uint8),
        ('bInterfaceProtocol', ctypes.c_uint8),
        ('iInterface', ctypes.c_uint8),
        ('endpoint', ctypes.POINTER(EndpointDescriptor)),
        ('extra', ctypes.POINTER(ctypes.c_ubyte)),
        ('extra_length', ctypes.c_int),
    ]


class Interface(ctypes.Structure):
    _fields_ = [
        ('altsetting', ctypes.POINTER(InterfaceDescriptor)),
        ('num_altsetting', ctypes.c_int),
    ]


class ConfigDescriptor(ctypes.Structure):
    _fields_ = [
        ('bLength', ctypes.c_uint8),
        ('bDescriptorType', ctypes.c_uint8),
        ('wTotalLength', ctypes.c_uint16),
        ('bNumInterfaces', ctypes.c_uint8),
        ('bConfigurationValue', ctypes.c_uint8),
        ('iConfiguration', ctypes.c_uint8),
        ('bmAttributes', ctypes.c_uint8),
        ('MaxPower', ctypes.c_uint8),
        ('interface', ctypes.POINTER(Interface)),
        ('extra', ctypes.POINTER(ctypes.c_ubyte)),
        ('extra_length', ctypes.c_int),
    ]


class BridgeError(Exception):
    """Raised with a message that explains what to do about it."""


def write_state(path, host, port, description):
    """Announce this bridge so the dashboard can list it as a selectable port."""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            'host': host,
            'port': port,
            'pid': os.getpid(),
            'device': description,
            'started': time.time(),
        }
        temporary = path + '.tmp'
        with open(temporary, 'w') as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)   # atomic, so a reader never sees half a file
    except OSError as exc:
        print('note: could not write {} ({})'.format(path, exc), file=sys.stderr)


def clear_state(path):
    """Remove our announcement, leaving another bridge's entry alone."""
    if not path:
        return
    try:
        with open(path) as handle:
            if json.load(handle).get('pid') != os.getpid():
                return
        os.remove(path)
    except (OSError, ValueError):
        pass


def load_libusb():
    """Locate libusb. Termux installs it where find_library often cannot see it."""
    candidates = []
    override = os.environ.get('FLOCKYOU_LIBUSB')
    if override:
        candidates.append(override)

    found = ctypes.util.find_library('usb-1.0') or ctypes.util.find_library('usb')
    if found:
        candidates.append(found)

    prefix = os.environ.get('PREFIX', '/data/data/com.termux/files/usr')
    candidates.extend([
        os.path.join(prefix, 'lib', 'libusb-1.0.so'),
        'libusb-1.0.so.0',
        'libusb-1.0.so',
        'libusb-1.0.dylib',
    ])

    attempts = []
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate)
        except OSError as exc:
            attempts.append('{}: {}'.format(candidate, exc))

    raise BridgeError(
        'Could not load libusb. Install it with "pkg install libusb", or set '
        'FLOCKYOU_LIBUSB to the library path.\nTried:\n  ' + '\n  '.join(attempts))


def bind_signatures(lib):
    """Declare argument types so ctypes marshals pointers correctly on 64-bit."""
    lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_init.restype = ctypes.c_int

    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    lib.libusb_exit.restype = None

    # Variadic in C, but every option we pass takes no trailing argument.
    lib.libusb_set_option.restype = ctypes.c_int

    lib.libusb_wrap_sys_device.argtypes = [
        ctypes.c_void_p, ctypes.c_ssize_t, ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_wrap_sys_device.restype = ctypes.c_int

    lib.libusb_get_device.argtypes = [ctypes.c_void_p]
    lib.libusb_get_device.restype = ctypes.c_void_p

    lib.libusb_get_device_descriptor.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(DeviceDescriptor)]
    lib.libusb_get_device_descriptor.restype = ctypes.c_int

    lib.libusb_get_active_config_descriptor.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ConfigDescriptor))]
    lib.libusb_get_active_config_descriptor.restype = ctypes.c_int

    lib.libusb_free_config_descriptor.argtypes = [ctypes.POINTER(ConfigDescriptor)]
    lib.libusb_free_config_descriptor.restype = None

    lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int

    lib.libusb_kernel_driver_active.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_kernel_driver_active.restype = ctypes.c_int

    lib.libusb_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_detach_kernel_driver.restype = ctypes.c_int

    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_claim_interface.restype = ctypes.c_int

    lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_release_interface.restype = ctypes.c_int

    lib.libusb_control_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
        ctypes.c_uint16, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16,
        ctypes.c_uint]
    lib.libusb_control_transfer.restype = ctypes.c_int

    lib.libusb_bulk_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
    lib.libusb_bulk_transfer.restype = ctypes.c_int

    lib.libusb_close.argtypes = [ctypes.c_void_p]
    lib.libusb_close.restype = None

    lib.libusb_error_name.argtypes = [ctypes.c_int]
    lib.libusb_error_name.restype = ctypes.c_char_p

    return lib


# - - - - - - - - - - - - - - device abstraction - - - - - - - - - - - - - - -

class CdcInterfaces(object):
    """The interface and endpoint numbers needed to drive a CDC-ACM device."""

    def __init__(self):
        self.control_interface = None
        self.data_interface = None
        self.endpoint_in = None
        self.endpoint_out = None

    @property
    def usable(self):
        return (self.data_interface is not None
                and self.endpoint_in is not None
                and self.endpoint_out is not None)


class UsbCdcDevice(object):
    """A CDC-ACM device reached through a file descriptor from termux-usb."""

    def __init__(self, lib, verbose=False):
        self.lib = lib
        self.verbose = verbose
        self.context = ctypes.c_void_p()
        self.handle = ctypes.c_void_p()
        self.interfaces = CdcInterfaces()
        self._claimed = []
        self.descriptor = None
        self.layout = []

    def _check(self, code, action):
        if code < 0:
            name = self.lib.libusb_error_name(code)
            raise BridgeError('{} failed: {} ({})'.format(
                action, name.decode() if name else 'unknown', code))
        return code

    def open(self, fd):
        # Android hands us an already-open descriptor. Device discovery would
        # try to scan the USB bus, which an unprivileged app is not allowed to
        # do, so it must be disabled before libusb_init.
        self.lib.libusb_set_option(None, LIBUSB_OPTION_NO_DEVICE_DISCOVERY)
        self._check(self.lib.libusb_init(ctypes.byref(self.context)), 'libusb_init')

        result = self.lib.libusb_wrap_sys_device(
            self.context, ctypes.c_ssize_t(fd), ctypes.byref(self.handle))
        if result < 0:
            raise BridgeError(
                'libusb_wrap_sys_device failed on fd {} ({}). The descriptor must '
                'come from termux-usb -e, and libusb must be 1.0.23 or newer.'
                .format(fd, result))

        self._read_descriptors()

    def _read_descriptors(self):
        device = self.lib.libusb_get_device(self.handle)
        if not device:
            raise BridgeError('libusb_get_device returned NULL')

        descriptor = DeviceDescriptor()
        self._check(self.lib.libusb_get_device_descriptor(device, ctypes.byref(descriptor)),
                    'libusb_get_device_descriptor')
        self.descriptor = descriptor

        config = ctypes.POINTER(ConfigDescriptor)()
        self._check(self.lib.libusb_get_active_config_descriptor(device, ctypes.byref(config)),
                    'libusb_get_active_config_descriptor')

        try:
            self._scan_interfaces(config.contents)
        finally:
            self.lib.libusb_free_config_descriptor(config)

    def _scan_interfaces(self, config):
        """Find the CDC control and data interfaces and the bulk endpoints."""
        self.layout = []

        for index in range(config.bNumInterfaces):
            interface = config.interface[index]
            for alt_index in range(interface.num_altsetting):
                alt = interface.altsetting[alt_index]
                endpoints = []

                for ep_index in range(alt.bNumEndpoints):
                    endpoint = alt.endpoint[ep_index]
                    endpoints.append({
                        'address': endpoint.bEndpointAddress,
                        'attributes': endpoint.bmAttributes,
                        'max_packet_size': endpoint.wMaxPacketSize,
                    })

                self.layout.append({
                    'interface': alt.bInterfaceNumber,
                    'alternate': alt.bAlternateSetting,
                    'class': alt.bInterfaceClass,
                    'subclass': alt.bInterfaceSubClass,
                    'protocol': alt.bInterfaceProtocol,
                    'endpoints': endpoints,
                })

                if alt.bInterfaceClass == LIBUSB_CLASS_COMM and self.interfaces.control_interface is None:
                    self.interfaces.control_interface = alt.bInterfaceNumber

                if alt.bInterfaceClass != LIBUSB_CLASS_DATA:
                    continue

                bulk_in = bulk_out = None
                for endpoint in endpoints:
                    is_bulk = (endpoint['attributes'] & LIBUSB_TRANSFER_TYPE_MASK) == LIBUSB_TRANSFER_TYPE_BULK
                    if not is_bulk:
                        continue
                    if endpoint['address'] & LIBUSB_ENDPOINT_IN:
                        bulk_in = endpoint['address']
                    else:
                        bulk_out = endpoint['address']

                if bulk_in is not None and bulk_out is not None and self.interfaces.data_interface is None:
                    self.interfaces.data_interface = alt.bInterfaceNumber
                    self.interfaces.endpoint_in = bulk_in
                    self.interfaces.endpoint_out = bulk_out

    def summary(self):
        """One-line device identity for the dashboard's port list."""
        if not self.descriptor:
            return 'USB CDC-ACM device'
        return 'USB {:04x}:{:04x}'.format(self.descriptor.idVendor, self.descriptor.idProduct)

    def describe(self):
        lines = []
        if self.descriptor:
            lines.append('Device {:04x}:{:04x}  class={:#04x}  USB {:x}.{:02x}'.format(
                self.descriptor.idVendor, self.descriptor.idProduct,
                self.descriptor.bDeviceClass,
                self.descriptor.bcdUSB >> 8, self.descriptor.bcdUSB & 0xFF))

        for entry in self.layout:
            lines.append(
                '  interface {} alt {}: class={:#04x} subclass={:#04x} protocol={:#04x}'.format(
                    entry['interface'], entry['alternate'], entry['class'],
                    entry['subclass'], entry['protocol']))
            for endpoint in entry['endpoints']:
                transfer = endpoint['attributes'] & LIBUSB_TRANSFER_TYPE_MASK
                kind = {0: 'control', 1: 'isochronous', 2: 'bulk', 3: 'interrupt'}.get(transfer, '?')
                direction = 'IN' if endpoint['address'] & LIBUSB_ENDPOINT_IN else 'OUT'
                lines.append('      endpoint {:#04x} {} {} max_packet={}'.format(
                    endpoint['address'], direction, kind, endpoint['max_packet_size']))

        if self.interfaces.usable:
            lines.append('  -> CDC data interface {} (IN {:#04x}, OUT {:#04x}), control interface {}'.format(
                self.interfaces.data_interface, self.interfaces.endpoint_in,
                self.interfaces.endpoint_out, self.interfaces.control_interface))
        else:
            lines.append('  -> no CDC-ACM data interface with bulk endpoints found')

        return '\n'.join(lines)

    def claim(self):
        if not self.interfaces.usable:
            raise BridgeError(
                'This device does not expose a CDC-ACM data interface with bulk '
                'endpoints, so it is not a standard USB serial device. Run with '
                '--probe and send the output for a closer look.\n\n' + self.describe())

        # Android usually has cdc_acm bound already (that is what created
        # /dev/ttyACM0), so the interface must be detached before it can be
        # claimed. Not all backends support this; fall back to explicit detach.
        self.lib.libusb_set_auto_detach_kernel_driver(self.handle, 1)

        for number in self._interfaces_to_claim():
            result = self.lib.libusb_claim_interface(self.handle, number)
            if result == LIBUSB_ERROR_BUSY:
                self.lib.libusb_detach_kernel_driver(self.handle, number)
                result = self.lib.libusb_claim_interface(self.handle, number)

            if result < 0:
                if number == self.interfaces.control_interface:
                    # The control interface is optional: without it we cannot
                    # set the baud rate, but many devices stream regardless.
                    if self.verbose:
                        print('note: could not claim control interface {} ({})'.format(
                            number, result), file=sys.stderr)
                    continue
                self._check(result, 'claim interface {}'.format(number))
            self._claimed.append(number)

    def _interfaces_to_claim(self):
        numbers = []
        if self.interfaces.control_interface is not None:
            numbers.append(self.interfaces.control_interface)
        if self.interfaces.data_interface not in numbers:
            numbers.append(self.interfaces.data_interface)
        return numbers

    def configure(self, baudrate):
        """SET_LINE_CODING then raise DTR/RTS, as a host would when opening."""
        if self.interfaces.control_interface is None:
            return False

        # dwDTERate, bCharFormat (1 stop bit), bParityType (none), bDataBits (8)
        line_coding = struct.pack('<IBBB', baudrate, 0, 0, 8)
        buffer = (ctypes.c_ubyte * len(line_coding)).from_buffer_copy(line_coding)

        result = self.lib.libusb_control_transfer(
            self.handle, CDC_REQUEST_TYPE_OUT, CDC_SET_LINE_CODING, 0,
            self.interfaces.control_interface, buffer, len(line_coding), 1000)
        if result < 0 and self.verbose:
            print('note: SET_LINE_CODING failed ({})'.format(result), file=sys.stderr)

        # wValue bit 0 = DTR, bit 1 = RTS. Many boards send nothing until DTR.
        state = self.lib.libusb_control_transfer(
            self.handle, CDC_REQUEST_TYPE_OUT, CDC_SET_CONTROL_LINE_STATE, 0x03,
            self.interfaces.control_interface, None, 0, 1000)
        if state < 0 and self.verbose:
            print('note: SET_CONTROL_LINE_STATE failed ({})'.format(state), file=sys.stderr)

        return result >= 0

    def read(self, length=512, timeout_ms=500):
        """Return bytes from the device, or b'' when the read simply timed out."""
        buffer = (ctypes.c_ubyte * length)()
        transferred = ctypes.c_int(0)

        result = self.lib.libusb_bulk_transfer(
            self.handle, ctypes.c_ubyte(self.interfaces.endpoint_in), buffer,
            length, ctypes.byref(transferred), timeout_ms)

        if result == LIBUSB_ERROR_TIMEOUT:
            # A timeout can still deliver a partial transfer.
            return bytes(buffer[:transferred.value]) if transferred.value else b''
        if result == LIBUSB_ERROR_NO_DEVICE:
            raise BridgeError('USB device disconnected')
        if result < 0:
            raise BridgeError('bulk read failed ({})'.format(result))

        return bytes(buffer[:transferred.value])

    def write(self, data, timeout_ms=1000):
        if not data:
            return 0
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        transferred = ctypes.c_int(0)

        result = self.lib.libusb_bulk_transfer(
            self.handle, ctypes.c_ubyte(self.interfaces.endpoint_out), buffer,
            len(data), ctypes.byref(transferred), timeout_ms)
        if result < 0 and result != LIBUSB_ERROR_TIMEOUT:
            raise BridgeError('bulk write failed ({})'.format(result))
        return transferred.value

    def close(self):
        for number in self._claimed:
            self.lib.libusb_release_interface(self.handle, number)
        self._claimed = []
        if self.handle:
            self.lib.libusb_close(self.handle)
            self.handle = ctypes.c_void_p()
        if self.context:
            self.lib.libusb_exit(self.context)
            self.context = ctypes.c_void_p()


# - - - - - - - - - - - - - - - TCP bridging - - - - - - - - - - - - - - - - -

class BridgeServer(object):
    """Fan out one device stream to TCP clients, and relay their writes back.

    The device is injected rather than constructed here so the networking can be
    exercised without USB hardware.
    """

    def __init__(self, device, host='127.0.0.1', port=4000):
        self.device = device
        self.host = host
        self.port = port
        self.clients = []
        self.clients_lock = threading.Lock()
        self.running = False
        self.server_socket = None
        self.bytes_forwarded = 0

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(4)
        self.running = True

        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self.server_socket.getsockname()[1]

    def _accept_loop(self):
        while self.running:
            try:
                client, address = self.server_socket.accept()
            except OSError:
                return

            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.clients_lock:
                self.clients.append(client)
            print('client connected from {}:{}'.format(*address), file=sys.stderr)
            threading.Thread(target=self._client_loop, args=(client,), daemon=True).start()

    def _client_loop(self, client):
        """Relay anything a client sends to the device."""
        try:
            while self.running:
                data = client.recv(256)
                if not data:
                    break
                self.device.write(data)
        except (OSError, BridgeError):
            pass
        finally:
            self._drop_client(client)

    def _drop_client(self, client):
        with self.clients_lock:
            if client in self.clients:
                self.clients.remove(client)
        try:
            client.close()
        except OSError:
            pass

    def broadcast(self, data):
        with self.clients_lock:
            targets = list(self.clients)

        for client in targets:
            try:
                client.sendall(data)
            except OSError:
                self._drop_client(client)

        self.bytes_forwarded += len(data)

    def pump(self):
        """Read the device forever, forwarding everything to connected clients."""
        while self.running:
            data = self.device.read()
            if data:
                self.broadcast(data)

    def stop(self):
        self.running = False
        with self.clients_lock:
            targets = list(self.clients)
        for client in targets:
            self._drop_client(client)
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass


# - - - - - - - - - - - - - - - - - entry point - - - - - - - - - - - - - - - -

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Bridge a USB CDC-ACM device to TCP for un-rooted Android.',
        epilog='Launch through: termux-usb -r -e ./termux_usb_bridge.py /dev/bus/usb/001/002')
    parser.add_argument('fd', type=int, nargs='?',
                        help='file descriptor supplied by termux-usb -e')
    parser.add_argument('--port', type=int, default=4000, help='TCP port to listen on')
    parser.add_argument('--host', default='127.0.0.1', help='address to bind')
    parser.add_argument('--baud', type=int, default=115200, help='line rate to request')
    parser.add_argument('--probe', action='store_true',
                        help='print the device descriptors and exit')
    parser.add_argument('--verbose', action='store_true', help='report non-fatal problems')
    parser.add_argument('--state-file', default=DEFAULT_STATE_FILE,
                        help='file announcing this bridge to the dashboard')
    parser.add_argument('--no-state', action='store_true',
                        help='do not announce the bridge to the dashboard')
    return parser.parse_args(argv)


def install_signal_handlers():
    """Turn termination signals into SystemExit so cleanup still runs.

    Without this, closing the Termux session or killing the process leaves the
    state file behind. Readers tolerate that by checking the pid, but tidying up
    properly is better than relying on the safety net.
    """
    def terminate(signum, frame):
        raise SystemExit(0)

    for name in ('SIGTERM', 'SIGHUP'):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, terminate)
            except (ValueError, OSError):
                pass


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    install_signal_handlers()

    if args.fd is None:
        print(__doc__.strip(), file=sys.stderr)
        print('\nerror: no file descriptor given. This must be run via '
              'termux-usb -e, which supplies one.', file=sys.stderr)
        return 2

    try:
        lib = bind_signatures(load_libusb())
    except BridgeError as exc:
        print('error: {}'.format(exc), file=sys.stderr)
        return 1

    device = UsbCdcDevice(lib, verbose=args.verbose)
    server = None

    try:
        device.open(args.fd)

        if args.probe:
            print(device.describe())
            return 0

        device.claim()
        device.configure(args.baud)

        server = BridgeServer(device, host=args.host, port=args.port)
        port = server.start()
        state_file = None if args.no_state else args.state_file
        write_state(state_file, args.host, port, device.summary())

        print('bridging USB device to socket://{}:{}'.format(args.host, port), file=sys.stderr)
        print('it will appear in the dashboard\'s Sniffer dropdown', file=sys.stderr)
        server.pump()

    except BridgeError as exc:
        print('error: {}'.format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nstopping', file=sys.stderr)
    finally:
        clear_state(None if args.no_state else args.state_file)
        if server:
            server.stop()
        device.close()

    return 0


if __name__ == '__main__':
    sys.exit(main())
