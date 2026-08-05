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
   A web interface to scan for nearby WiFi networks, connect or disconnect the wireless interface, and manage saved network profiles via NetworkManager (`nmcli`). Handles multiple wifi interfaces (e.g. a USB dongle alongside the built-in radio): per-interface status in an Interfaces panel, a target-interface selector for scanning/connecting, and a "Connect all idle" bulk action to join every unassociated radio to one SSID at once.
5. **Web Browsing Simulator (`web_browsing_simulator`)**  
   A browser-based tool that simulates realistic, bursty web-browsing traffic (random page loads with think-time between them) against another Pi's randomized synthetic page corpus, complementing the iperf3 apps' sustained-throughput tests.
6. **Client Simulator (`client_simulator`)**  
   A browser-based tool that simulates many independent clients behind a single WiFi association: each simulated client gets its own Linux network namespace connected via a veth pair to an internal bridge that is NAT'd out the real wlan0/eth0 interface, so the router only ever sees the Pi's one station while the Pi's own kernel still does real per-client routing/ARP/conntrack work. A churn engine periodically retires and recreates a fraction of clients to simulate devices joining and leaving. Falls back to a lightweight thread-based simulation when real network namespaces aren't available (e.g. macOS development).
7. **Network Device Scanner (`network_device_scanner`)**  
   A browser-based tool that inventories every device currently reachable on the LAN behind a chosen Bind Interface — IP address, MAC address, vendor, and hostname — via a privileged ARP-based `nmap -sn` sweep, so it finds devices even when every port they expose is closed or firewalled.
8. **WiFi Roaming Monitor (`roaming_monitor`)**  
   A browser-based live timeline of association events (`iw event`) for a chosen wireless interface: authentication, association, connection, deauthentication and disconnection, each timestamped from the kernel and streamed to the browser. Measures how long a roam between two BSSIDs actually took — including 802.11r fast transitions that skip the disconnect entirely — and decodes 802.11 reason codes so a drop reports "4-Way Handshake timeout" rather than "reason 15". Intended for chamber testing where a variable attenuator is used to force transitions between APs.
9. **Web Terminal (`web_terminal`)**  
   A browser-based interactive shell on the Pi, for the commands the other apps don't cover. The terminal itself is [ttyd](https://github.com/tsl0922/ttyd) — a mature daemon embedding xterm.js that handles the PTY, VT/ANSI emulation, resize and reconnect — bound to loopback and framed by a thin Flask wrapper that supplies the shared WiFiPi header and hostname badge. Unlike every other app here it runs as a non-root user. Requires a one-off manual install of `ttyd` (see below).
10. **WiFi Porcupine (`wifi_porcupine`)**  
   A browser-based tool that stresses an access point by rapidly and randomly associating and disassociating several physical WiFi interfaces (the Pi's built-in radio plus USB adapters) against one target SSID, randomizing each interface's MAC on every reconnect (via NetworkManager's `cloned-mac-address`) so the hub sees a constant stream of brand-new stations — bloating its association, DHCP-lease and ARP tables. A single intensity slider scales both churn speed (dwell time) and concurrency (how many interfaces churn at once). Optionally, each interface can also carry a small fleet of `ip netns` clients NAT'd out through it to add real HTTP traffic load — though those clients ride the interface's single radio MAC and so are not seen by the AP as separate associations. Refuses gracefully off-Linux or without NetworkManager (e.g. macOS development).
11. **Default Landing Webpage (`www`)**  
   A static landing page (`www/index.html`) served at root (`/`) providing direct access cards/links to all tools in the platform.

---

## Production Deployment on Raspberry Pi (Gunicorn + Nginx + Systemd)

Instead of running applications in Flask development debug mode (`python3 app.py`), production deployment on a Raspberry Pi uses **Gunicorn** as the WSGI HTTP server, managed by **systemd** services, and reverse-proxied by **Nginx**.

### Step 1: Install System Dependencies

Update your system and install Nginx, iperf3, nmap, NetworkManager, python3-pip, and python3-venv:

```bash
sudo apt-get update
sudo apt-get install -y nginx iperf3 nmap network-manager python3-pip python3-venv iproute2 iptables dnsutils curl
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

### Step 3: Configure Passwordless Sudo (WiFi Monitor, iPerf Server Manager, WiFi Connection Manager & Network Device Scanner)

The platform applications require administrative privileges for specific system-level commands when executed under a non-root user (e.g. `$USER`, `jenkins`, or `pi`):
1. **WiFi Utilization Monitor** (and **WiFi Roaming Monitor**): Executes `sudo iw dev <interface> scan` to collect live wireless scan data. The Roaming Monitor uses the same `iw` rule to run `sudo iw event -t`, so it needs no additional sudoers entry.
2. **iPerf3 Server Manager**: Executes `sudo systemctl [start|stop|restart]` to manage `iperf3` server systemd service units.
3. **WiFi Connection Manager**: Executes `sudo nmcli ...` to scan, connect, disconnect, and manage saved WiFi profiles via NetworkManager.
4. **Client Simulator**: Executes `sudo ip ...`, `sudo iptables ...`, and `sudo sysctl ...` to create/destroy network namespaces, veth pairs, and the NAT bridge. Only needed if the service runs as a non-root user — the unit file runs as `root` by default (see Step 4 below), in which case no sudoers entry is required. This app also expects `curl`, `dig` (package `dnsutils`), and `iproute2` (for `ip`) to be installed.
5. **Network Device Scanner**: Executes `sudo -n nmap -sn ...` (non-interactive) for its ARP-based LAN device sweep — this requires raw-socket access, unlike the unprivileged, port-scoped `nmap` scans used by the iPerf3 Congestion Generator and Web Browsing Simulator (see below). Its unit file runs as the app user, not root, so this sudoers rule is always required.

The **iPerf3 Congestion Generator** and **Web Browsing Simulator** need none of this for their own `nmap` use — they only run unprivileged, port-scoped scans to find other Pis running the same app.

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
   $USER ALL=(ALL) NOPASSWD: /usr/sbin/ip, /usr/sbin/iptables, /usr/sbin/sysctl
   $USER ALL=(ALL) NOPASSWD: /usr/bin/nmap
   ```
   *(Verify absolute binary paths on your distribution using `which iw`, `which systemctl`, `which nmcli`, `which ip`, `which iptables`, `which sysctl`, and `which nmap`. The fourth rule is only needed if Client Simulator runs as a non-root user; it runs as `root` by default. The `nmap` rule is always needed for Network Device Scanner, which runs as the app user.)*

### Step 4: Set Up Systemd Services

Systemd service files are provided in the `deploy/` directory. Copy them to `/etc/systemd/system/`:

#### Prerequisite for the Web Terminal: install `ttyd`

The Web Terminal is the only app with a dependency outside the Python virtualenv. **`ttyd` is not packaged in Debian Bookworm or Trixie** (only in `sid`), so `apt install ttyd` will fail on Raspberry Pi OS. Install upstream's static release binary instead:

```bash
wget -O $HOME/Downloads/ttyd.aarch64 https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.aarch64
sudo install -m 755 $HOME/Downloads/ttyd.aarch64 /usr/local/bin/ttyd
ttyd --version
```

The binary only needs to reach `/usr/local/bin` — download it outside the repo (`$HOME/Downloads`, which
Raspberry Pi OS always has) rather than into `/opt/wifipi`, so it doesn't linger as an untracked file in
the working tree.

Check the [releases page](https://github.com/tsl0922/ttyd/releases) for the current version and to verify the checksum.

The Web Terminal has no authentication, so anyone who can reach the Pi gets a shell. If you want a password on it, add `--credential user:password` to the `ExecStart` line in `deploy/ttyd.service`.

#### Install only the apps this Pi should run

The suite is modular: each Pi runs only the subset of tools you want it to (a monitor Pi, a
traffic-generator Pi, a porcupine Pi, and so on). Every app is one self-contained systemd unit — the Web
Terminal is two — so you install just the units you want and pair each with its nginx snippet in Step 5.
**You never edit the landing page per Pi**: it auto-detects which apps are actually running and shows only
those cards.

| App | systemd unit(s) | nginx snippet |
| --- | --- | --- |
| WiFi Utilization Monitor | `wifi-monitor` | `wifimon.conf` |
| iPerf3 Congestion Generator | `iperf-generator` | `iperf.conf` |
| iPerf3 Server Manager | `iperf-server-manager` | `iperfserver.conf` |
| WiFi Connection Manager | `wifi-connection-manager` | `wificonnect.conf` |
| Web Browsing Simulator | `web-browsing-simulator` | `webbrowse.conf` |
| Client Simulator | `client-simulator` | `clientsim.conf` |
| Network Device Scanner | `network-device-scanner` | `devices.conf` |
| WiFi Roaming Monitor | `roaming-monitor` | `roaming.conf` |
| Web Terminal | `web-terminal` + `ttyd` | `terminal.conf` (+ WebSocket map) |
| WiFi Porcupine | `wifi-porcupine` | `porcupine.conf` |

Copy the units you want. For example, a **monitor Pi**:

```bash
for u in wifi-monitor roaming-monitor network-device-scanner wifi-connection-manager; do
    sudo cp deploy/$u.service /etc/systemd/system/
done
sudo systemctl daemon-reload
```

…or a **traffic-generator Pi**:

```bash
for u in iperf-generator web-browsing-simulator client-simulator wifi-porcupine; do
    sudo cp deploy/$u.service /etc/systemd/system/
done
sudo systemctl daemon-reload
```

To run the whole suite on one box, copy them all (remember the Web Terminal is `web-terminal` **and**
`ttyd`, and needs the `ttyd` binary installed above).

*Note on Service Users*: The unit files in `deploy/` run as `root` by default so they work across any Linux distribution/user setup without missing-user errors. If you prefer to run services under a non-root account (e.g. `User=jenkins` or `User=pi`), edit the service files in `/etc/systemd/system/` to uncomment and update the `User=` and `Group=` parameters. (Setting `User=` to a non-existent user will cause systemd to fail with `status=217/USER`).

#### Set the Web Terminal's user account

`ttyd.service` and `web-terminal.service` both default to `User=pi`, but Raspberry Pi OS Bookworm and later no longer create a `pi` user — so unless you deliberately named your account `pi`, change it in **both** files. Each has a clearly marked block at the top of its `[Service]` section; edit those, or set both at once:

```bash
TERM_USER=<your-username>
sudo sed -i "s/^User=pi$/User=$TERM_USER/" \
    /etc/systemd/system/ttyd.service /etc/systemd/system/web-terminal.service
sudo systemctl daemon-reload
```

This is an in-place edit rather than a config setting because systemd does not expand environment variables in `User=`. Pointing it at a non-existent account fails with `status=217/USER`.

Note that neither unit sets `Group=`, so systemd uses the account's primary group from `/etc/passwd`. Don't add one: on images where the primary group isn't named after the user, a hardcoded `Group=` fails with `status=216/GROUP`. Check yours with `id <your-username>` if you're curious. If a unit has already failed repeatedly, systemd latches its rate limiter and you need `sudo systemctl reset-failed ttyd web-terminal` before it will start again.

Enable and start only the units you copied in, e.g. for the **monitor Pi** from above:

```bash
sudo systemctl enable --now wifi-monitor roaming-monitor network-device-scanner wifi-connection-manager
```

Verify status the same way, one unit (or several) at a time: `sudo systemctl status <unit>` and, for
deeper logs, `sudo journalctl -u <unit> -e`.

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

The default static landing webpage is located in `/opt/wifipi/www/index.html`. The main Nginx site config
(`deploy/nginx.conf.example`) is app-independent: it just serves the landing page on Root (`/`) and
`include`s every snippet dropped into `/etc/nginx/wifipi.d/*.conf`. Each app's proxy block lives in its own
snippet under `deploy/nginx.d/<app>.conf` (see the table in Step 4), so you install only the snippets for
the apps running on this Pi — an app you didn't install is simply a 404, not a 502 from a proxy pointing at
a dead backend. The landing page auto-detects which apps answer, so none of this requires per-Pi editing
of `www/index.html`.

For `/webbrowse/` specifically, `webbrowse.conf` also serves the app's generated synthetic content directly
as static files via an `alias` block (`/webbrowse/content/` → `/opt/wifipi/web_browsing_simulator/content/`)
instead of proxying it through Python — since that content is just bulk random bytes used to generate
realistic browsing traffic, there's no reason to pay the Python/WSGI overhead for it. This means the
Nginx worker user (commonly `www-data`) needs read access to `/opt/wifipi/web_browsing_simulator/content/`,
same as it already needs for `/opt/wifipi/www`.

1. Copy the Nginx configuration template from `deploy/nginx.conf.example` to `/etc/nginx/sites-available/wifipi`, and create the directory the per-app snippets get installed into:
   ```bash
   sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/wifipi
   sudo mkdir -p /etc/nginx/wifipi.d
   ```

2. Copy in only the snippets for the apps you installed in Step 4. For example, the same **monitor Pi**:
   ```bash
   for a in wifimon roaming devices wificonnect; do
       sudo cp deploy/nginx.d/$a.conf /etc/nginx/wifipi.d/
   done
   ```

3. Install the WebSocket upgrade map — but only if you installed the Web Terminal. This has to be a
   separate file: nginx's `map` directive is only valid in `http { }` context, and Debian's nginx includes
   `/etc/nginx/conf.d/*.conf` at that level:
   ```bash
   sudo cp deploy/nginx-websocket-map.conf.example /etc/nginx/conf.d/websocket-upgrade.conf
   ```
   Skip this if you are also skipping the Web Terminal — but if `terminal.conf` is present in
   `/etc/nginx/wifipi.d/` without this map, `nginx -t` fails with `unknown "connection_upgrade" variable`.

4. Enable the site configuration by creating a symbolic link in `sites-enabled` and removing the default Nginx site:
   ```bash
   sudo ln -s /etc/nginx/sites-available/wifipi /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   ```

5. Test the Nginx configuration, then enable and (re)start Nginx:
   ```bash
   sudo nginx -t
   sudo systemctl enable --now nginx
   sudo systemctl reload nginx
   ```
   `enable --now` makes sure Nginx starts on boot and is running; `reload` picks up the config you just
   installed even if Nginx was already running and enabled from a prior setup.

> **Note on the host name display**: every page shows the host it is served from. The three Flask apps
> report it directly. The static landing page has no backend, so it relies on the `ssi on;` directive in
> the root `location` block of `nginx.conf.example` to print the Pi's real host name. If you are
> upgrading from an older configuration, re-copy the template (step 1) so the landing page shows the
> host name rather than falling back to the IP address you typed in the URL bar.

### Accessing Your Applications

Only the tools actually installed and running on a given Pi appear as cards on its landing page — the
rest are simply absent, not shown as broken links. The full list below is a reference for every app the
suite can host, across any Pi:

All applications are served over standard HTTP (Port 80) via path routing:

- **Default Webpage (Root `/`)**: Open `http://<pi-ip>/` (Static landing page in `/opt/wifipi/www` with links to all tools)
- **WiFi Channel & Utilization Monitor**: Open `http://<pi-ip>/wifimon/` (Subpath `/wifimon/` reverse-proxied to Gunicorn on port 5000)
- **iPerf3 Congestion Generator**: Open `http://<pi-ip>/iperf/` (Subpath `/iperf/` reverse-proxied to Gunicorn on port 5001)
- **iPerf3 Server Manager**: Open `http://<pi-ip>/iperfserver/` (Subpath `/iperfserver/` reverse-proxied to Gunicorn on port 5002)
- **WiFi Connection Manager**: Open `http://<pi-ip>/wificonnect/` (Subpath `/wificonnect/` reverse-proxied to Gunicorn on port 5003)
- **Web Browsing Simulator**: Open `http://<pi-ip>/webbrowse/` (Subpath `/webbrowse/` reverse-proxied to Gunicorn on port 5004, with `/webbrowse/content/` served directly by Nginx)
- **Client Simulator**: Open `http://<pi-ip>/clientsim/` (Subpath `/clientsim/` reverse-proxied to Gunicorn on port 5005)
- **Network Device Scanner**: Open `http://<pi-ip>/devices/` (Subpath `/devices/` reverse-proxied to Gunicorn on port 5006)
- **WiFi Roaming Monitor**: Open `http://<pi-ip>/roaming/` (Subpath `/roaming/` reverse-proxied to Gunicorn on port 5007)
- **Web Terminal**: Open `http://<pi-ip>/terminal/` (Subpath `/terminal/` reverse-proxied to Gunicorn on port 5008, with `/terminal/tty/` reverse-proxied to the loopback-bound `ttyd` on port 5009)
- **WiFi Porcupine**: Open `http://<pi-ip>/porcupine/` (Subpath `/porcupine/` reverse-proxied to Gunicorn on port 5010)
