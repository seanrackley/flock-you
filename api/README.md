# Flock You Web Dashboard

A Flask-based web dashboard for real-time monitoring and analysis of Flock Safety device detections with GPS integration.

## Features

### Real-Time Detection Monitoring
- **Live Updates**: Real-time detection display via WebSocket
- **Detection Filtering**: Filter by detection method (WiFi, BLE, MAC, Device Name)
- **Statistics Dashboard**: Overview of detection counts and types
- **Detailed View**: Complete device information for each detection

### GPS Integration
- **GPS Dongle Support**: Connect USB GPS dongles for location tracking
- **NMEA Parsing**: Automatic parsing of GPS coordinates
- **Location Tagging**: Each detection can include GPS coordinates
- **Satellite Information**: Display GPS fix quality and satellite count

### Data Export
- **CSV Export**: Download detection data in CSV format
- **KML Export**: Generate Google Earth compatible KML files
- **GPS Coordinates**: Include latitude, longitude, and altitude
- **Timestamped Files**: Automatic filename generation with timestamps

## Installation

### Prerequisites
- Python 3.8 or higher
- USB GPS dongle (optional, for location tracking)

### Setup
1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the application**:
   ```bash
   python flockyou.py
   ```

3. **Access the dashboard**:
   Open your browser and navigate to `http://localhost:5000`

## Running on Android (Termux)

The dashboard runs on Android under Termux. Two things differ from a desktop:
port enumeration needed a compatibility shim, and Android restricts serial
access.

### Setup

```bash
pkg install python termux-api
pip install -r requirements.txt
python flockyou.py
```

Then open `http://localhost:5000` in the phone's browser.

`android_compat.py` handles the platform differences. Python 3.13 changed
`sys.platform` from `"linux"` to `"android"`, and pySerial 3.5 predates that, so
`import serial.tools.list_ports` aborts at import time with *"no implementation
for your platform"*. The shim routes enumeration through pySerial's Linux
backend, which works unmodified on Android. It installs itself only when the
stock backend is broken, so desktop behaviour is unchanged.

### Getting sniffer data onto the phone

Android does not give unprivileged apps raw access to `/dev/tty*`, so a USB-OTG
sniffer usually cannot be opened directly. In order of preference:

| Setup | How to connect |
|---|---|
| Rooted phone, USB OTG | Select `/dev/ttyUSB0` or `/dev/ttyACM0` as usual |
| Un-rooted phone | Bridge the sniffer from another machine with `ser2net`, then enter `socket://<host>:<port>` as the port |
| Offline analysis | Use the JSON/CSV/KML import buttons with files exported from the ESP32 dashboard |

Any pySerial URL works where a device path is expected, including
`socket://host:port` and `rfc2217://host:port`. The port dropdown lists devices
that exist but cannot be opened, with a note explaining why, and failed
connections return a message suggesting the workaround that applies.

**An empty dropdown is the normal result on an un-rooted phone.** It means the
kernel never created a `/dev/tty*` node for the adapter, so there is nothing to
list and no permission to grant — the device is simply not reachable from
userspace. Confirm with `ls -l /dev/tty*`: if there is no `ttyUSB*` or `ttyACM*`
entry, use the `socket://` bridge or import exported files instead.

`GET /api/platform` reports what the server detected: platform, whether the shim
is active, root status, available Termux:API helpers, and USB devices Android
can see. Start there when something will not connect.

### GPS from the phone

Android will not open a USB GPS dongle either, but the phone has its own
receiver. There are two ways to reach it, and both feed the same matching,
validation and export paths a serial NMEA dongle would use, including an
`accuracy` value in metres.

**`browser-gps` — works everywhere, nothing to install.** The browser showing
the dashboard supplies the position via the Geolocation API. Select it in the
GPS dropdown and allow location access when prompted. This is the only option on
the **Google Play build of Termux**, where Termux:API does not exist yet: the
`termux-api` package installs the command-line shims, but they fail with
*"Termux:API is not yet available on Google Play"* because the companion app
cannot be installed.

Browsers only expose geolocation in a secure context. Loading the dashboard on
the phone itself at `http://localhost:5000` qualifies. Loading it from another
device over the LAN does not, and the browser will refuse without HTTPS.

**`termux-location` — needs Termux:API.** Listed only when the helper is
present. Requires the **Termux:API app** (from F-Droid, not merely the
`termux-api` package) plus location permission. Termux and Termux:API must come
from the same source, since Android blocks them from communicating when their
signing keys differ.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `FLOCKYOU_HOST` | `0.0.0.0` | Bind address. Set to `127.0.0.1` to keep the dashboard off the local network |
| `FLOCKYOU_PORT` | `5000` | Listen port |
| `FLOCKYOU_VERBOSE` | off | Socket.IO packet logging, off by default because it is costly on a phone |
| `SECRET_KEY` | dev key | Flask secret key |

Data and export paths are resolved relative to `flockyou.py`, so the server can
be started from any directory — useful in Termux, which starts you in `$HOME`.

## Usage

### Basic Operation
1. **Start the web server** using the command above
2. **Connect your Flock You device** and ensure it's sending JSON data
3. **View detections** in real-time on the dashboard
4. **Filter detections** using the dropdown menu
5. **Export data** using the export buttons

### GPS Setup
1. **Connect GPS dongle** to your computer via USB
2. **Select GPS port** from the dropdown in the header
3. **Click "Connect"** to establish GPS connection
4. **Monitor GPS status** via the status indicator
5. **Detections will automatically include GPS data** when available

### Data Export
- **CSV Export**: Downloads a CSV file with all detection data
- **KML Export**: Downloads a KML file for viewing in Google Earth
- **GPS Data**: Both formats include GPS coordinates when available

## API Endpoints

### Detection Management
- `GET /api/detections` - Get all detections (with optional filtering)
- `POST /api/detections` - Add new detection from Flock You device
- `POST /api/clear` - Clear all detections

### GPS Management
- `GET /api/gps/ports` - Get available serial ports
- `POST /api/gps/connect` - Connect to GPS dongle
- `POST /api/gps/disconnect` - Disconnect GPS dongle

### Data Export
- `GET /api/export/csv` - Export detections as CSV
- `GET /api/export/kml` - Export detections as KML

## Integration with Flock You Device

The web dashboard is designed to receive JSON detection data from the Flock You ESP32 device. The device should send POST requests to `/api/detections` with JSON data in the following format:

```json
{
  "timestamp": 12345,
  "detection_time": "12.345s",
  "protocol": "wifi",
  "detection_method": "probe_request",
  "ssid": "Flock_Camera_001",
  "mac_address": "aa:bb:cc:dd:ee:ff",
  "rssi": -65,
  "signal_strength": "MEDIUM",
  "channel": 6
}
```

## GPS Dongle Compatibility

The dashboard supports standard NMEA GPS dongles that output GPGGA sentences. Compatible devices include:
- USB GPS receivers
- Bluetooth GPS modules (when connected via USB adapter)
- Serial GPS modules

## File Structure
```
webapp/
├── app.py              # Main Flask application
├── requirements.txt    # Python dependencies
├── templates/
│   └── index.html     # Web dashboard template
├── exports/           # Generated export files
└── README.md         # This file
```

## Troubleshooting

### GPS Connection Issues
- Ensure GPS dongle is properly connected
- Check that the correct serial port is selected
- Verify GPS dongle is powered and has satellite fix
- Check system permissions for serial port access

### No Detections Displayed
- Verify Flock You device is running and connected
- Check network connectivity between device and server
- Ensure device is sending data to correct endpoint
- Check browser console for JavaScript errors

### Export Issues
- Ensure `exports/` directory exists and is writable
- Check available disk space
- Verify file permissions

## Security Notes

- The dashboard runs on `0.0.0.0:5000` by default (accessible from any network)
- Consider using a reverse proxy (nginx) for production deployment
- Implement authentication if needed for multi-user environments
- The Flask secret key should be changed in production

## Development

### Adding New Features
- Modify `app.py` for backend functionality
- Update `templates/index.html` for frontend changes
- Add new API endpoints as needed
- Update requirements.txt for new dependencies

### Testing
- Test GPS functionality with actual GPS dongle
- Verify export functionality with sample data
- Test real-time updates with multiple browser windows
- Validate JSON data format compatibility
