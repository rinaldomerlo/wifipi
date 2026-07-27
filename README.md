# WiFiPi

Wireless Testing Environment tools to run on a Raspberry Pi.

---

## Included Applications

1. **WiFi Channel & Utilization Monitor (`wifi_utilization_monitor`)**  
   A Flask web application that visualizes real-time WiFi channel utilization, spectrum coverage across 2.4 GHz, 5 GHz, and 6 GHz bands, and live BSS scanning.
2. **iPerf3 Congestion Generator (`iperf_congestion_generator`)**  
   A browser-based UI to configure, monitor, and control long-running `iperf3` client test streams across network interfaces.

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
sudo chown -R pi:pi /opt/wifipi
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

The WiFi Utilization Monitor executes `sudo iw dev <interface> scan` to collect live wireless scan data. To allow the app user (`pi`) to run scans without a password prompt:

1. Open sudoers configuration:
   ```bash
   sudo visudo
   ```
2. Add the following rule at the end of the file:
   ```text
   pi ALL=(ALL) NOPASSWD: /usr/sbin/iw
   ```
   *(Verify absolute path with `which iw`)*

### Step 4: Set Up Systemd Services

Systemd service files are provided in the `deploy/` directory. Copy them to `/systemd/system/`:

```bash
sudo cp deploy/wifi-monitor.service /etc/systemd/system/
sudo cp deploy/iperf-generator.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Enable and start both services:

```bash
sudo systemctl enable --now wifi-monitor
sudo systemctl enable --now iperf-generator
```

Verify service status:
```bash
sudo systemctl status wifi-monitor
sudo systemctl status iperf-generator
```

### Step 5: Configure Nginx Reverse Proxy

1. Copy or create the Nginx configuration from `deploy/nginx.conf.example` to `/etc/nginx/sites-available/wifipi`:
   ```bash
   sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/wifipi
   ```
2. Enable the site by creating a symbolic link in `sites-enabled`:
   ```bash
   sudo ln -s /etc/nginx/sites-available/wifipi /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   ```
3. Test the Nginx configuration and restart Nginx:
   ```bash
   sudo nginx -t
   sudo systemctl restart nginx
   ```

### Accessing Your Applications

- **WiFi Channel & Utilization Monitor**: Open `http://<pi-ip>/` (Port 80)
- **iPerf3 Congestion Generator**: Open `http://<pi-ip>:8080/` (Port 8080)
