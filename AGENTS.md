# AGENTS.md — Project Knowledge & Learnings

This document summarizes the architecture, key components, design standards, and production deployment guidelines for the **WiFiPi** repository, accumulated across development sessions.

---

## 1. Project Overview

**WiFiPi** is a collection of wireless testing and monitoring tools designed to run natively on a Raspberry Pi. The repository contains standalone Flask web applications with a unified modern UI and production deployment configurations.

### Environment & Development Context
- **Target / Deployment Environment**: Raspberry Pi running Linux (Raspberry Pi OS / Debian). This environment provides native access to hardware wireless interfaces (`iw`), `systemd` process management, and networking utilities (`iperf3`, `nginx`).
- **Development / IDE Environment**: Typically macOS (or PC workstation). Code editing, static analysis, unit testing, and IDE workflows frequently take place on Mac or PC developer workstations. Development logic and unit tests should account for platform differences (e.g., non-Linux OS lacking `systemctl` or Linux `/proc`) gracefully using mocks or conditional fallbacks to optimize developer efficiency and maintain utility across environments.

Project Root Structure:
- `www/` — Default static landing webpage (`index.html`).
- `wifi_utilization_monitor/` — Real-time WiFi channel utilization and spectrum analyzer.
- `iperf_congestion_generator/` — Browser-based controller for long-running `iperf3` test streams.
- `iperf_server_manager/` — Web interface to view, start, stop, restart, and monitor `iperf3` server daemons and systemd services.
- `deploy/` — Systemd service units and Nginx reverse proxy configuration.
- `requirements.txt` — Python dependencies (Flask, Gunicorn).

---

## 2. Included Applications

### A. WiFi Channel & Utilization Monitor (`wifi_utilization_monitor`)
- **Purpose**: Visualizes live WiFi channel utilization, spectrum coverage across **2.4 GHz, 5 GHz, and 6 GHz** bands, and BSS scanning results.
- **Backend (`app.py`, `parser.py`)**: Executes wireless scan commands and parses raw output.
- **System Privilege Requirement**: Performs live scans via `sudo iw dev <interface> scan`. 
  - *Sudoers Rule*: Requires passwordless sudo configuration for the local app user (e.g. `jenkins` or `pi`):
    ```text
    <username> ALL=(ALL) NOPASSWD: /usr/sbin/iw
    ```
- **Frontend (`static/js/app.js`, `templates/index.html`)**:
  - Configurable refresh rate options (10s, 30s, 1m, 2m, 5m).
  - Multi-band support (2.4, 5, 6 GHz, and All Bands).
  - *Initialization Note*: Band visibility and chart container wrappers (`wrapper6`, `wrapperUtil6`, etc.) must explicitly invoke `updateBandVisibility()` during DOM initialization (`initElements()`) to ensure 6 GHz graphs display correctly on initial page load.

### B. iPerf3 Congestion Generator (`iperf_congestion_generator`)
- **Purpose**: Provides a web interface to start, monitor, and stop `iperf3` client test streams across network interfaces.
- **Real-time Streaming**: Uses Server-Sent Events (SSE) to stream live stdout/stderr from `iperf3` processes to the browser UI.

### C. iPerf3 Server Manager (`iperf_server_manager`)
- **Purpose**: Provides a web interface to discover, launch, stop, restart, and monitor running `iperf3` server daemons and systemd service units (e.g. `iperf3-5202.service`).
- **Management Capabilities**: Discovers active processes via `ps`, parses port numbers, manages systemd services via `systemctl`, and verifies port availability matrix.
- **System Privilege Requirement**: Controls systemd service units via `sudo systemctl [start|stop|restart] iperf3*`.
  - *Sudoers Rule*: Requires passwordless sudo configuration for systemctl commands when running under a non-root account:
    ```text
    <username> ALL=(ALL) NOPASSWD: /usr/bin/systemctl start iperf3*, /usr/bin/systemctl stop iperf3*, /usr/bin/systemctl restart iperf3*, /usr/bin/systemctl is-active iperf3*
    ```

---

## 3. UI/UX Design Standards

Both applications share a unified design system:
- **Design Style**: Modern glassmorphism with dark gradient backgrounds, translucent cards, and subtle borders.
- **Typography**: Google Fonts (`Outfit` for headings/accents, `Space Grotesk` for body/mono metrics).
- **Icons**: FontAwesome icons for buttons, status indicators, and navigation tabs.
- **Hostname Display (mandatory)**: *Every* GUI screen must show the host it is served from, so a browser
  tab can always be traced back to a specific Raspberry Pi.
  - Flask apps expose `get_hostname()` (a guarded `socket.gethostname()`) in `app.py`, pass it to
    `render_template(..., hostname=...)`, and render it in the `.app-header` as a `.host-badge` pill.
    They also serve `GET /api/hostname` so the static landing page can obtain the same value.
  - `www/index.html` is static with no backend and therefore resolves the host in three steps:
    Nginx SSI (`<!--# echo var="hostname" -->`, requires `ssi on;`), then a `fetch` of an app's
    `/api/hostname`, then `window.location.hostname` as a last resort. Only the first two yield the
    machine's true host name — the third reports whatever address was typed, usually an IP.
  - Each app's test module asserts the badge, the host name, and the `/api/hostname` endpoint.
  - Any newly added screen inherits this requirement.

---

## 4. Production Deployment (Raspberry Pi)

Production deployments avoid Flask development debug mode (`python3 app.py`) in favor of a robust WSGI stack:

1. **WSGI Server**: **Gunicorn** (`gunicorn>=20.1.0`)
   - WiFi Monitor worker: bound to `0.0.0.0:5000` (2 workers).
   - iPerf Generator worker: bound to `0.0.0.0:5001` (1 worker, multi-threaded for SSE streaming).
   - iPerf Server Manager worker: bound to `0.0.0.0:5002` (2 workers).
2. **Process Management**: **systemd** services located in `deploy/`:
   - `wifi-monitor.service`
   - `iperf-generator.service`
   - `iperf-server-manager.service`
3. **Reverse Proxy**: **Nginx** (`deploy/nginx.conf.example`)
   - Port `80` (Root `/`): Serves default static landing page (`/opt/wifipi/www/index.html`) with cards/links to all tools.
   - Port `80` (Subpath `/wifimon/`): Proxies to WiFi Monitor (`127.0.0.1:5000`).
   - Port `80` (Subpath `/iperf/`): Proxies to iPerf Generator (`127.0.0.1:5001`) with buffering disabled (`proxy_buffering off`, `chunked_transfer_encoding on`) for real-time SSE streaming.
   - Port `80` (Subpath `/iperfserver/`): Proxies to iPerf Server Manager (`127.0.0.1:5002`).

---

## 5. Maintenance & History Notes

- **Git Repository State**: The repository was re-initialized with a clean initial commit to purge all historical references.
- **Python Environment**: Managed via a shared virtual environment (`.venv`) at `/opt/wifipi/.venv`.
