# WiFi Connection Manager

A Flask-based web application to scan for nearby WiFi networks, connect or disconnect the wireless interface, and manage saved network profiles on a Raspberry Pi via NetworkManager (`nmcli`).

---

## Features

| Feature | Description |
|---|---|
| **Connection Status** | Live view of the current connection: SSID, IP address, signal strength, security, band, and channel. |
| **Network Scanning** | Scans for nearby WiFi networks via `nmcli device wifi list`, de-duplicated by SSID and sorted by signal strength. |
| **Connect / Disconnect** | Connect to open networks immediately, or secured networks via a password prompt. Disconnect the active connection with one click. |
| **Saved Networks** | Lists NetworkManager connection profiles, with the ability to reconnect or forget (delete) them. |

---

## Quick Start

```bash
cd wifi_connection_manager
python3 app.py
```

Open **http://<pi-ip>:5003** in a web browser.

Requires NetworkManager (`nmcli`) to be the active network backend. Raspberry Pi OS Bookworm and later
ship with it by default; older `dhcpcd`/`wpa_supplicant`-based images are not supported.

---

## System Privileges & Sudo Setup

Scanning, connecting, disconnecting, and managing profiles via `nmcli` requires root or passwordless
sudo privileges when the app runs under a non-root user (e.g. `jenkins` or `pi`).

Add the following rule to `/etc/sudoers` using `sudo visudo` (replace `youruser` with the app execution user):

```text
youruser ALL=(ALL) NOPASSWD: /usr/bin/nmcli
```

---

## API Endpoints

- `GET /api/status` — Current connection details for the detected WiFi device.
- `GET /api/scan` — Scan for nearby networks (`nmcli device wifi list --rescan yes`).
- `GET /api/saved` — List saved WiFi connection profiles.
- `POST /api/connect` — Connect to a network (`{"ssid": "HomeNetwork", "password": "optional"}`).
- `POST /api/disconnect` — Disconnect the wireless interface from its current network.
- `POST /api/forget` — Delete a saved connection profile (`{"name": "HomeNetwork"}`).
