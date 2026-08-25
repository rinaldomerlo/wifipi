# WIFIMON - WiFi Spectrum & Channel Monitor

A Flask web application that visualizes real-time WiFi channel utilization and spectrum coverage on a configurable refresh rate.

## Features

- **Live Spectrum Map**: Draws overlapping parabolic domes representing detected Access Points (BSSIDs). Center frequency, signal strength (height), and channel width (dome span) are mapped accurately to the frequency spectrum (2.4 GHz, 5 GHz, and 6 GHz bands).
- **Channel Utilization Grid**: Visualizes occupancy rates (percent busy) per channel, utilizing BSS Load metrics when available or AP density averages otherwise. Colors grade from green (quiet) to pink/red (congested).
- **Live Scanning & Unassociated Enforcement**: Queries wireless devices on Linux hosts and executes real `sudo iw dev <interface> scan` commands to monitor active airwaves. Ensures the Pi is disconnected before scanning, and provides a one-click disconnect banner if an active WiFi connection is detected.
- **Bi-directional Highlighting**: Hovering over a spectral dome highlights its detailed list entry below, and hovering over a table row highlights the corresponding dome in the graph. Click to lock highlights.
- **Search & Filters**: Quickly search by SSID, BSSID, or Vendor, and filter by Band or Security encryption.
- **Host Power Controls**: Header navigation contains direct shortcuts to Reboot and Shutdown the Pi.

## Project Structure

- [app.py](app.py): Flask application server handling the scan API, interface discovery, and disconnect endpoint.
- [parser.py](parser.py): Decoupled parser containing the BSS data extract routines.
- [templates/index.html](templates/index.html): HTML5 layout template.
- [static/css/styles.css](static/css/styles.css): Custom dark-mode style sheets.
- [static/js/app.js](static/js/app.js): Javascript client-side event loops, rendering, and interaction engines.

## Installation & Running

1. **Install Dependencies**:
   Ensure you have Python 3 installed. Navigate to the wifi_utilization_monitor directory and install the requirements:
   ```bash
   cd wifi_utilization_monitor
   pip install -r requirements.txt
   ```

2. **Start the Flask App**:
   ```bash
   python3 app.py [port]
   ```
   By default, the server runs on port **5000**. You can customize the port by passing it as a command line argument (e.g., `python3 app.py 5055`). Open your browser and navigate to:
   [http://localhost:5000](http://localhost:5000) (or the port specified)

## Configuring Live Scanning (Linux/Raspberry Pi)

For live scanning and disconnecting active links, the application executes `sudo iw dev <iface> scan`, `sudo iw dev <iface> disconnect`, and `sudo nmcli device disconnect <iface>`. Since these require root privileges and the app is typically run under a non-root user (e.g. `pi` or `jenkins`), configure passwordless `sudo` for `iw` and `nmcli`:

1. Open the sudoers configuration:
   ```bash
   sudo visudo
   ```

2. Add the following rule at the end of the file (replace `youruser` with the username running the Flask app, e.g., `pi`):
   ```text
   youruser ALL=(ALL) NOPASSWD: /usr/sbin/iw, /usr/bin/nmcli
   ```
   *(Note: The absolute paths of `iw` and `nmcli` can be verified by running `which iw` and `which nmcli` on your system).*
