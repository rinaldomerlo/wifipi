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
4. **WiFi Connection Manager (`wifi_connection_manager`)**  
   A web interface to scan for nearby WiFi networks, connect or disconnect the wireless interface, and manage saved network profiles via NetworkManager (`nmcli`).
5. **Web Browsing Simulator (`web_browsing_simulator`)**  
   A browser-based tool that simulates realistic, bursty web-browsing traffic (random page loads with think-time between them) against another Pi's randomized synthetic page corpus, complementing the iperf3 apps' sustained-throughput tests.
6. **Default Landing Webpage (`www`)**  
   A static landing page (`www/index.html`) served at root (`/`) providing direct access cards/links to all tools in the platform.

---

## Production Deployment on Raspberry Pi (Gunicorn + Nginx + Systemd)

Instead of running applications in Flask development debug mode (`python3 app.py`), production deployment on a Raspberry Pi uses **Gunicorn** as the WSGI HTTP server, managed by **systemd** services, and reverse-proxied by **Nginx**.

### Step 1: Install System Dependencies

Update your system and install Nginx, iperf3, nmap, NetworkManager, python3-pip, and python3-venv:

```bash
sudo apt-get update
sudo apt-get install -y nginx iperf3 nmap network-manager python3-pip python3-venv
```

*Note*: Raspberry Pi OS Bookworm (and later) ships with NetworkManager as the default network backend, so `nmcli` is likely already present. Older images (Bullseye and earlier) use `dhcpcd` + `wpa_supplicant` instead — the WiFi Connection Manager app requires NetworkManager and will not work with that older stack without migrating to it first.

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

### Step 3: Configure Passwordless Sudo (WiFi Monitor, iPerf Server Manager & WiFi Connection Manager)

The platform applications require administrative privileges for specific system-level commands when executed under a non-root user (e.g. `$USER`, `jenkins`, or `pi`):
1. **WiFi Utilization Monitor**: Executes `sudo iw dev <interface> scan` to collect live wireless scan data.
2. **iPerf3 Server Manager**: Executes `sudo systemctl [start|stop|restart]` to manage `iperf3` server systemd service units.
3. **WiFi Connection Manager**: Executes `sudo nmcli ...` to scan, connect, disconnect, and manage saved WiFi profiles via NetworkManager.

The **Web Browsing Simulator** needs none of this — it only shells out to `nmap` for its optional LAN scan, same as the iPerf3 Congestion Generator.

To allow the app user to execute these commands without a password prompt:

1. Open sudoers configuration:
   ```bash
   sudo visudo
   ```
2. Add the following rules at the end of the file (replace `$USER` with your actual username if needed):
   ```text
   $USER ALL=(ALL) NOPASSWD: /usr/sbin/iw
   $USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start iperf3*, /usr/bin/systemctl stop iperf3*, /usr/bin/systemctl restart iperf3*, /usr/bin/systemctl is-active iperf3*, /bin/systemctl start iperf3*, /bin/systemctl stop iperf3*, /bin/systemctl restart iperf3*, /bin/systemctl is-active iperf3*
   $USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli
   ```
   *(Verify absolute binary paths on your distribution using `which iw`, `which systemctl`, and `which nmcli`)*

### Step 4: Set Up Systemd Services

Systemd service files are provided in the `deploy/` directory. Copy them to `/etc/systemd/system/`:

```bash
sudo cp deploy/wifi-monitor.service /etc/systemd/system/
sudo cp deploy/iperf-generator.service /etc/systemd/system/
sudo cp deploy/iperf-server-manager.service /etc/systemd/system/
sudo cp deploy/wifi-connection-manager.service /etc/systemd/system/
sudo cp deploy/web-browsing-simulator.service /etc/systemd/system/
sudo systemctl daemon-reload
```

*Note on Service Users*: The unit files in `deploy/` run as `root` by default so they work across any Linux distribution/user setup without missing-user errors. If you prefer to run services under a non-root account (e.g. `User=jenkins` or `User=pi`), edit the service files in `/etc/systemd/system/` to uncomment and update the `User=` and `Group=` parameters. (Setting `User=` to a non-existent user will cause systemd to fail with `status=217/USER`).

Enable and start services:

```bash
sudo systemctl enable --now wifi-monitor
sudo systemctl enable --now iperf-generator
sudo systemctl enable --now iperf-server-manager
sudo systemctl enable --now wifi-connection-manager
sudo systemctl enable --now web-browsing-simulator
```

Verify service status:
```bash
sudo systemctl status wifi-monitor
sudo systemctl status iperf-generator
sudo systemctl status iperf-server-manager
sudo systemctl status wifi-connection-manager
sudo systemctl status web-browsing-simulator
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

The default static landing webpage is located in `/opt/wifipi/www/index.html`. Nginx serves this page directly on Root (`/`) and proxies requests for `/wifimon/`, `/iperf/`, `/iperfserver/`, `/wificonnect/`, and `/webbrowse/` to the respective backend Flask apps.

For `/webbrowse/` specifically, Nginx also serves the app's generated synthetic content directly as
static files via an `alias` block (`/webbrowse/content/` → `/opt/wifipi/web_browsing_simulator/content/`)
instead of proxying it through Python — since that content is just bulk random bytes used to generate
realistic browsing traffic, there's no reason to pay the Python/WSGI overhead for it. This means the
Nginx worker user (commonly `www-data`) needs read access to `/opt/wifipi/web_browsing_simulator/content/`,
same as it already needs for `/opt/wifipi/www`.

1. Copy the Nginx configuration template from `deploy/nginx.conf.example` to `/etc/nginx/sites-available/wifipi`:
   ```bash
   sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/wifipi
   ```

2. Enable the site configuration by creating a symbolic link in `sites-enabled` and removing the default Nginx site:
   ```bash
   sudo ln -s /etc/nginx/sites-available/wifipi /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   ```

3. Test the Nginx configuration and restart Nginx:
   ```bash
   sudo nginx -t
   sudo systemctl restart nginx
   ```

> **Note on the host name display**: every page shows the host it is served from. The three Flask apps
> report it directly. The static landing page has no backend, so it relies on the `ssi on;` directive in
> the root `location` block of `nginx.conf.example` to print the Pi's real host name. If you are
> upgrading from an older configuration, re-copy the template (step 1) so the landing page shows the
> host name rather than falling back to the IP address you typed in the URL bar.

### Accessing Your Applications

All applications are served over standard HTTP (Port 80) via path routing:

- **Default Webpage (Root `/`)**: Open `http://<pi-ip>/` (Static landing page in `/opt/wifipi/www` with links to all tools)
- **WiFi Channel & Utilization Monitor**: Open `http://<pi-ip>/wifimon/` (Subpath `/wifimon/` reverse-proxied to Gunicorn on port 5000)
- **iPerf3 Congestion Generator**: Open `http://<pi-ip>/iperf/` (Subpath `/iperf/` reverse-proxied to Gunicorn on port 5001)
- **iPerf3 Server Manager**: Open `http://<pi-ip>/iperfserver/` (Subpath `/iperfserver/` reverse-proxied to Gunicorn on port 5002)
- **WiFi Connection Manager**: Open `http://<pi-ip>/wificonnect/` (Subpath `/wificonnect/` reverse-proxied to Gunicorn on port 5003)
- **Web Browsing Simulator**: Open `http://<pi-ip>/webbrowse/` (Subpath `/webbrowse/` reverse-proxied to Gunicorn on port 5004, with `/webbrowse/content/` served directly by Nginx)
