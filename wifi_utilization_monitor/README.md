# WIFIMON - WiFi Spectrum & Channel Monitor

A Flask web application that visualizes real-time WiFi channel utilization and spectrum coverage on a configurable refresh rate.

## Features

- **Live Spectrum Map**: Draws overlapping parabolic domes representing detected Access Points (BSSIDs). Center frequency, signal strength (height), and channel width (dome span) are mapped accurately to the frequency spectrum (2.4 GHz, 5 GHz, and 6 GHz bands).
- **Channel Utilization Grid**: Visualizes occupancy rates (percent busy) per channel, utilizing BSS Load metrics when available or AP density averages otherwise. Colors grade from green (quiet) to pink/red (congested).
- **Live Scanning**: Queries wireless devices on Linux hosts and executes real `sudo iw dev <interface> scan` commands to monitor active airwaves.
- **Bi-directional Highlighting**: Hovering over a spectral dome highlights its detailed list entry below, and hovering over a table row highlights the corresponding dome in the graph. Click to lock highlights.
- **Search & Filters**: Quickly search by SSID, BSSID, or Vendor, and filter by Band or Security encryption.

## Project Structure

- [app.py](app.py): Flask application server handling the scan API and interface discovery.
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

For live scanning, the application executes `sudo iw dev <iface> scan`. Since `iw scan` requires root privileges and is typically run under a non-root user (e.g. `pi` or `admin`), you should configure passwordless `sudo` specifically for the `iw` command so that the Flask process does not hang waiting for a password prompt.

1. Open the sudoers configuration:
   ```bash
   sudo visudo
   ```

2. Add the following rule at the end of the file (replace `youruser` with the username running the Flask app, e.g., `pi`):
   ```text
   youruser ALL=(ALL) NOPASSWD: /usr/sbin/iw
   ```
   *(Note: The absolute path of `iw` can be verified by running `which iw` on your system).*
