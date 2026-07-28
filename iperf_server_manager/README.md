# iPerf3 Server Manager

A Flask-based web application to manage, start, stop, restart, and monitor `iperf3` server daemons and systemd service units on a Raspberry Pi.

---

## Features

| Feature | Description |
|---|---|
| **Process Discovery** | Automatically scans running processes for `iperf3 --server` instances, displaying PID, user, port, uptime, and command line options. |
| **Systemd Unit Management** | Discovers installed systemd services (e.g. `iperf3.service`, `iperf3-5202.service`, `iperf3-5203.service`, `iperf3-5204.service`) and allows start, stop, and restart operations. |
| **Ad-Hoc Launch** | Launch new `iperf3` server daemons on any port (1–65535) with customizable report intervals, bind IP, and one-off mode. |
| **Port Matrix & Conflict Detection** | Check port availability across common ports (5201–5210) to prevent port collision before launching. |
| **Auto-Refresh UI** | Configurable UI refresh intervals (5s, 10s, 30s) or manual refresh. |

---

## Quick Start

```bash
cd iperf_server_manager
python3 app.py
```

Open **http://<pi-ip>:5002** in a web browser.

---

## System Privileges & Sudo Setup

Managing systemd services (`iperf3.service`, `iperf3-5202.service`, etc.) via `systemctl start/stop/restart` requires root or passwordless sudo privileges when the app runs under a non-root user (e.g. `jenkins` or `pi`).

Add the following rule to `/etc/sudoers` using `sudo visudo` (replace `youruser` with the app execution user):

```text
youruser ALL=(ALL) NOPASSWD: /usr/bin/systemctl start iperf3*, /usr/bin/systemctl stop iperf3*, /usr/bin/systemctl restart iperf3*, /usr/bin/systemctl is-active iperf3*
```

---

## API Endpoints

- `GET /api/servers` — Returns list of running iperf3 processes and detected systemd units.
- `POST /api/servers/start` — Launch new iperf3 server daemon or systemd unit (`{"port": 5202, "interval": 0}`).
- `POST /api/servers/stop` — Terminate iperf3 server process or systemd unit (`{"pid": 12345}` or `{"unit": "iperf3-5202.service"}`).
- `POST /api/servers/restart` — Restart systemd unit or process on port (`{"unit": "iperf3-5202.service"}`).
- `GET /api/ports/check?start_port=5201&end_port=5210` — Scan port availability status.
