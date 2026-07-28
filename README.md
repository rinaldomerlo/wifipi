# WiFiPi

Wireless Testing Environment tools to run on a Raspberry Pi.

---

## Included Applications

1. **WiFi Channel & Utilization Monitor (`wifi_utilization_monitor`)**  
   A Flask web application that visualizes real-time WiFi channel utilization, spectrum coverage across 2.4 GHz, 5 GHz, and 6 GHz bands, and live BSS scanning.
2. **iPerf3 Congestion Generator (`iperf_congestion_generator`)**  
   A browser-based UI to configure, monitor, and control long-running `iperf3` client test streams across network interfaces.
3. **iPerf3 Server Manager (`iperf_server_manager`)**  
   A web interface to discover, launch, stop, restart, and monitor running `iperf3` server daemons and systemd services across ports.
4. **Default Landing Webpage (`www`)**  
   A static landing page (`www/index.html`) served at root (`/`) providing direct access cards/links to all tools in the platform.

---

## Production Deployment on Raspberry Pi (Gunicorn + Nginx + Systemd)

Instead of running applications in Flask development debug mode (`python3 app.py`), production deployment on a Raspberry Pi uses **Gunicorn** as the WSGI HTTP server, managed by **systemd** services, and reverse-proxied by **Nginx**.

### Step 1: Install System Dependencies

Update your system and install Nginx, iperf3, nmap, python3-pip, and python3-venv:

```bash
sudo apt-get update
sudo apt-get install -y nginx iperf3 nmap python3-pip python3-venv
```

### Step 2: Clone / Setup Project in System-Wide Directory

Clone or place the `wifipi` repository in `/opt/wifipi`:

```bash
sudo mkdir -p /opt/wifipi
sudo chown -R $USER:$USER /opt/wifipi
git clone https://github.com/rinaldomerlo/wifipi.git /opt/wifipi
cd /opt/wifipi
```

Create a shared Python virtual environment and install dependencies (including Gunicorn):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3: Configure Passwordless Sudo for WiFi Scanning

The WiFi Utilization Monitor executes `sudo iw dev <interface> scan` to collect live wireless scan data. To allow the app user (e.g. `$USER`, `jenkins`, or `pi`) to run scans without a password prompt:

1. Open sudoers configuration:
   ```bash
   sudo visudo
   ```
2. Add the following rule at the end of the file (replace `$USER` with your actual username if needed):
   ```text
   $USER ALL=(ALL) NOPASSWD: /usr/sbin/iw
   ```
   *(Verify absolute path with `which iw`)*

### Step 4: Set Up Systemd Services

Systemd service files are provided in the `deploy/` directory. Copy them to `/etc/systemd/system/`:

```bash
sudo cp deploy/wifi-monitor.service /etc/systemd/system/
sudo cp deploy/iperf-generator.service /etc/systemd/system/
sudo cp deploy/iperf-server-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
```

*Note on Service Users*: The unit files in `deploy/` run as `root` by default so they work across any Linux distribution/user setup without missing-user errors. If you prefer to run services under a non-root account (e.g. `User=jenkins` or `User=pi`), edit the service files in `/etc/systemd/system/` to uncomment and update the `User=` and `Group=` parameters. (Setting `User=` to a non-existent user will cause systemd to fail with `status=217/USER`).

Enable and start services:

```bash
sudo systemctl enable --now wifi-monitor
sudo systemctl enable --now iperf-generator
sudo systemctl enable --now iperf-server-manager
```

Verify service status:
```bash
sudo systemctl status wifi-monitor
sudo systemctl status iperf-generator
sudo systemctl status iperf-server-manager
```

#### Multi-Port iPerf3 Server Services (Optional)

In addition to the web application services, example systemd service files are provided in `deploy/` for persistent multi-port `iperf3` server daemons (ports 5202, 5203, 5204):

```bash
sudo cp deploy/iperf3-5202.service /etc/systemd/system/
sudo cp deploy/iperf3-5203.service /etc/systemd/system/
sudo cp deploy/iperf3-5204.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iperf3-5202 iperf3-5203 iperf3-5204
```

These multi-port iPerf3 server daemons will automatically be discovered and can be managed directly through the **iPerf3 Server Manager** UI (`/iperfserver/`).

### Step 5: Configure Nginx Reverse Proxy & Default Landing Page

The default static landing webpage is located in `/opt/wifipi/www/index.html`. Nginx serves this page directly on Root (`/`) and proxies requests for `/wifimon/` and `/iperf/` to the respective backend Flask apps.

1. Ensure the static web directory `/opt/wifipi/www` has readable permissions for Nginx (`www-data`):
   ```bash
   sudo chmod -R 755 /opt/wifipi/www
   ```

2. Copy the Nginx configuration template from `deploy/nginx.conf.example` to `/etc/nginx/sites-available/wifipi`:
   ```bash
   sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/wifipi
   ```

3. Enable the site configuration by creating a symbolic link in `sites-enabled` and removing the default Nginx site:
   ```bash
   sudo ln -s /etc/nginx/sites-available/wifipi /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   ```

4. Test the Nginx configuration and restart Nginx:
   ```bash
   sudo nginx -t
   sudo systemctl restart nginx
   ```

### Accessing Your Applications

All applications are served over standard HTTP (Port 80) via path routing:

- **Default Webpage (Root `/`)**: Open `http://<pi-ip>/` (Static landing page in `/opt/wifipi/www` with links to all tools)
- **WiFi Channel & Utilization Monitor**: Open `http://<pi-ip>/wifimon/` (Subpath `/wifimon/` reverse-proxied to Gunicorn on port 5000)
- **iPerf3 Congestion Generator**: Open `http://<pi-ip>/iperf/` (Subpath `/iperf/` reverse-proxied to Gunicorn on port 5001)
- **iPerf3 Server Manager**: Open `http://<pi-ip>/iperfserver/` (Subpath `/iperfserver/` reverse-proxied to Gunicorn on port 5002)
