# AGENTS.md — Project Knowledge & Learnings

This document summarizes the architecture, key components, design standards, and production deployment guidelines for the **WiFiPi** repository, accumulated across development sessions.

---

## 1. Project Overview

**WiFiPi** is a collection of wireless testing and monitoring tools designed to run natively on a Raspberry Pi. The repository contains two standalone Flask web applications with a unified modern UI and production deployment configurations.

Project Root Structure:
- `wifi_utilization_monitor/` — Real-time WiFi channel utilization and spectrum analyzer.
- `iperf_congestion_generator/` — Browser-based controller for long-running `iperf3` test streams.
- `deploy/` — Systemd service units and Nginx reverse proxy configuration.
- `requirements.txt` — Python dependencies (Flask, Gunicorn).

---

## 2. Included Applications

### A. WiFi Channel & Utilization Monitor (`wifi_utilization_monitor`)
- **Purpose**: Visualizes live WiFi channel utilization, spectrum coverage across **2.4 GHz, 5 GHz, and 6 GHz** bands, and BSS scanning results.
- **Backend (`app.py`, `parser.py`)**: Executes wireless scan commands and parses raw output.
- **System Privilege Requirement**: Performs live scans via `sudo iw dev <interface> scan`. 
  - *Sudoers Rule*: Requires passwordless sudo configuration for the `pi` user:
    ```text
    pi ALL=(ALL) NOPASSWD: /usr/sbin/iw
    ```
- **Frontend (`static/js/app.js`, `templates/index.html`)**:
  - Configurable refresh rate options (10s, 30s, 1m, 2m, 5m).
  - Multi-band support (2.4, 5, 6 GHz, and All Bands).
  - *Initialization Note*: Band visibility and chart container wrappers (`wrapper6`, `wrapperUtil6`, etc.) must explicitly invoke `updateBandVisibility()` during DOM initialization (`initElements()`) to ensure 6 GHz graphs display correctly on initial page load.

### B. iPerf3 Congestion Generator (`iperf_congestion_generator`)
- **Purpose**: Provides a web interface to start, monitor, and stop `iperf3` client test streams across network interfaces.
- **Real-time Streaming**: Uses Server-Sent Events (SSE) to stream live stdout/stderr from `iperf3` processes to the browser UI.

---

## 3. UI/UX Design Standards

Both applications share a unified design system:
- **Design Style**: Modern glassmorphism with dark gradient backgrounds, translucent cards, and subtle borders.
- **Typography**: Google Fonts (`Outfit` for headings/accents, `Space Grotesk` for body/mono metrics).
- **Icons**: FontAwesome icons for buttons, status indicators, and navigation tabs.

---

## 4. Production Deployment (Raspberry Pi)

Production deployments avoid Flask development debug mode (`python3 app.py`) in favor of a robust WSGI stack:

1. **WSGI Server**: **Gunicorn** (`gunicorn>=20.1.0`)
   - WiFi Monitor worker: bound to `127.0.0.1:8000` (2 workers).
   - iPerf Generator worker: bound to `127.0.0.1:8001` (1 worker, multi-threaded for SSE streaming).
2. **Process Management**: **systemd** services located in `deploy/`:
   - `wifi-monitor.service`
   - `iperf-generator.service`
3. **Reverse Proxy**: **Nginx** (`deploy/nginx.conf.example`)
   - Port `80`: Proxies to WiFi Monitor (`127.0.0.1:8000`).
   - Port `8080`: Proxies to iPerf Generator (`127.0.0.1:8001`) with buffering disabled (`proxy_buffering off`, `chunked_transfer_encoding on`) for real-time SSE streaming.

---

## 5. Maintenance & History Notes

- **Git Repository State**: The repository was re-initialized with a clean initial commit to purge all historical references to legacy test frameworks (`etienne_test_framework`). All references are strictly sanitized.
- **Python Environment**: Managed via a shared virtual environment (`.venv`) at `/opt/wifipi/.venv`.
