# AGENTS.md — Project Knowledge & Learnings

This document summarizes the architecture, key components, design standards, and production deployment guidelines for the **WiFiPi** repository, accumulated across development sessions.

---

## 1. Project Overview

**WiFiPi** is a collection of wireless testing and monitoring tools designed to run natively on a Raspberry Pi. The repository contains standalone Flask web applications with a unified modern UI and production deployment configurations.

### Environment & Development Context
- **Target / Deployment Environment**: Raspberry Pi running Linux (Raspberry Pi OS / Debian). This environment provides native access to hardware wireless interfaces (`iw`), `systemd` process management, NetworkManager (`nmcli`), and networking utilities (`iperf3`, `nginx`).
- **Development / IDE Environment**: Typically macOS (or PC workstation). Code editing, static analysis, unit testing, and IDE workflows frequently take place on Mac or PC developer workstations. Development logic and unit tests should account for platform differences (e.g., non-Linux OS lacking `systemctl` or Linux `/proc`) gracefully using mocks or conditional fallbacks to optimize developer efficiency and maintain utility across environments.

Project Root Structure:
- `www/` — Default static landing webpage (`index.html`).
- `wifi_utilization_monitor/` — Real-time WiFi channel utilization and spectrum analyzer.
- `iperf_congestion_generator/` — Browser-based controller for long-running `iperf3` test streams.
- `iperf_server_manager/` — Web interface to view, start, stop, restart, and monitor `iperf3` server daemons and systemd services.
- `wifi_connection_manager/` — Web interface to scan, connect, disconnect, and manage saved WiFi networks via NetworkManager (`nmcli`).
- `web_browsing_simulator/` — Simulates realistic, bursty web-browsing traffic against another Pi's randomized synthetic page corpus, as a complement to `iperf_congestion_generator`'s sustained-throughput streams.
- `client_simulator/` — Simulates many independent clients behind a single WiFi association using isolated Linux network namespaces NAT'd out one radio, with a churn engine cycling client identities.
- `network_device_scanner/` — ARP-based LAN device inventory (IP/MAC/vendor/hostname) via a privileged `nmap -sn` sweep of a chosen Bind Interface's subnet.
- `roaming_monitor/` — Live association-event timeline from `iw event`, timing roams between BSSIDs and decoding 802.11 reason codes.
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
  - Configurable refresh rate options (3s, 5s, 10s, 30s, 1m, 2m, 5m, manual) — a live scan
    (`sudo iw dev <interface> scan`) takes several seconds by itself (~3s observed on a Pi); the 1s/2s
    options were removed since they could never actually be honored. Note that at 3s the scan can still
    occasionally run long enough for a tick to be silently skipped by the frontend's `isScanning` guard
    (`static/js/app.js:554-558`) — that's expected, not a bug.
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

### D. WiFi Connection Manager (`wifi_connection_manager`)
- **Purpose**: Provides a web interface to scan for nearby WiFi networks, connect/disconnect the wireless
  interface, and manage (and forget) saved network profiles.
- **Backend**: A thin wrapper around NetworkManager's `nmcli` CLI, entirely — no `wpa_supplicant` or
  `dhcpcd` interaction. All output is parsed from `nmcli -t` (terse, colon-delimited) fields; colons
  embedded in SSIDs are backslash-escaped by `nmcli` and unescaped by `_split_terse()` in `app.py`.
  - `GET /api/status` — current connection details for the detected WiFi device.
  - `GET /api/scan` — `nmcli device wifi list --rescan yes`, de-duplicated by SSID: the connected BSSID
    always wins (so mesh/repeater setups with one SSID on multiple APs don't lose the "Connected" state
    behind a stronger unconnected AP), otherwise the strongest signal wins.
  - `GET /api/saved` / `POST /api/forget` — saved connection profiles (`nmcli connection show`/`delete`).
  - `POST /api/autoconnect` — enable/disable NetworkManager's auto-connect per saved profile
    (`nmcli connection modify <name> autoconnect yes|no`), surfaced as a toggle switch in the Saved
    Networks table — useful for test-lab setups where unattended reconnection is unwanted.
  - `POST /api/connect` / `POST /api/disconnect` — `nmcli device wifi connect` / `nmcli device disconnect`.
    nmcli's stderr is mapped to a friendlier message (e.g. "Secrets were required..." → "Incorrect password.")
    by `friendly_connect_error()`. The frontend skips the password modal when the SSID is open or matches
    an already-saved profile (NetworkManager already has secrets for the latter).
- **System Privilege Requirement**: Requires NetworkManager (`nmcli`) to be the active network backend —
  it will not work against the older `dhcpcd`/`wpa_supplicant` stack on pre-Bookworm Raspberry Pi OS images.
  Executes all state-changing calls via `sudo nmcli ...`.
  - *Sudoers Rule*: Requires passwordless sudo configuration for the local app user:
    ```text
    <username> ALL=(ALL) NOPASSWD: /usr/bin/nmcli
    ```

### E. Web Browsing Simulator (`web_browsing_simulator`)
- **Purpose**: Simulates realistic web-browsing traffic (bursty page-load-then-idle requests, not
  iperf3's sustained stream) between two Pis, to see how the WiFi link behaves under everyday-shaped
  load rather than raw throughput saturation.
- **Content model (`content_gen.py`)**: On startup, generates a randomized corpus of 30-50 synthetic
  "pages" to disk (`content/manifest.json`, `content/pages/<id>.html`, `content/assets/<page>/<id>.<ext>`),
  each with 3-25 assets of varying type/size drawn from ranges that resemble real page composition (HTML,
  CSS, JS, images, with an occasional larger "hero" image). Bytes are random and meaningless — only the
  size/type/count distribution matters. The corpus regenerates on every process restart so test sessions
  vary. File extensions are chosen to match declared content types because Nginx's static `alias` serving
  infers `Content-Type` from the extension.
- **Serving path**: In production, Nginx aliases `/webbrowse/content/` directly to the generated
  directory, bypassing Python for the actual byte transfer (see `deploy/nginx.conf.example`). `app.py`
  also serves the same files via `send_from_directory` at `/content/...` so `python app.py` alone works
  in local dev with no Nginx in front.
- **Client (browsing loop)**: Fetches the target's manifest once, then repeatedly picks a **random**
  page (not round-robin, so traffic shape varies), fetches its HTML then its assets through a bounded
  `ThreadPoolExecutor(max_workers=6)` (mimicking a browser's per-host connection limit), and idles a
  random 2-8s "think time" between pages. Streams page-load summaries to the browser via SSE, matching
  `iperf_congestion_generator`'s Start/Stop/live-output UX.
- **Target addressing**: LAN scan (`scan_for_servers`) discovers other Pis by nmap-ing the single port
  5004 — that only identifies which hosts have this app installed. The actual simulated fetches default
  to **port 80** (`http://<ip>/webbrowse/content/...`, through the target's Nginx) since that's the
  realistic "browsing the internet" path and the one that exercises Nginx's static serving; port 5004 is
  also accepted for a no-Nginx dev-to-dev test.
- **System Privilege Requirement**: None — only shells out to `nmap` for the optional LAN scan, same as
  `iperf_congestion_generator`.

### F. Client Simulator (`client_simulator`)
- **Purpose**: Simulates many independent clients behind a single WiFi association, per the "NAT mode"
  design in the original architecture doc for this feature — one physical WiFi association carrying
  hundreds of logically separate client identities, rather than macvlan's attempt to give each client its
  own MAC on the radio (which doesn't work over 802.11 STA associations; see the doc history for why).
- **Networking model**: Each simulated client gets its own Linux network namespace (`wfsim-<id>`),
  connected via a veth pair to an internal bridge (`wfsim-br0`, subnet `10.200.0.0/16`) that is NAT'd
  (`iptables -t nat -A POSTROUTING ... -j MASQUERADE`) out the real `wlan0`/`eth0` interface. The physical
  interface is **never** added to the bridge itself — only veth host-ends are — so this is ordinary L3 NAT
  through the one WiFi station (same mechanism a home router uses for its whole LAN), not an attempt to
  bridge multiple MACs onto the radio. The router only ever sees the Pi's single station/IP; the Pi's own
  kernel still does real per-client routing/ARP/conntrack work.
- **Traffic generation (Tier 1 only)**: each client's worker thread alternates `curl` (HTTP, via
  `ip netns exec <ns> curl ...`) and `dig` (DNS, via `ip netns exec <ns> dig @<dns_server> <name>`) with a
  randomized 2-8s think-time between requests, mirroring `web_browsing_simulator`'s bursty-not-sustained
  pattern. Headless-browser (Playwright) tiers from the original design doc were deliberately left out —
  they'd add a third-party dependency, which this repo avoids without asking first.
- **Churn engine**: every 60s, retires and replaces `churn_rate_percent` of active clients (namespace
  delete + recreate under a fresh id/IP), keeping the total count steady while cycling identities — "N
  clients disappear, N reappear" — to exercise the Pi's conntrack/ARP tables the way real devices
  sleeping/roaming would.
- **Environment split**: `detect_mode()` probes `platform.system() == "Linux"`, `ip` on `PATH`, and
  `sudo -n ip netns list` (passwordless sudo) to decide between real `netns` mode and a `simulated`
  fallback that runs the identical client/churn lifecycle with plain threads and `urllib`/`socket` instead
  of real namespaces — this is what lets the app run on macOS during development, and it's also the
  automatic fallback in production if bridge/NAT setup fails partway through.
- **System Privilege Requirement**: Executes `sudo ip ...`, `sudo iptables ...`, and `sudo sysctl -w
  net.ipv4.ip_forward=1` to create/destroy namespaces, veth pairs, and the NAT bridge. The systemd unit
  runs as `root` by default (see `deploy/client-simulator.service`), which already satisfies this; a
  sudoers rule is only needed if it's reconfigured to run as a non-root user:
  ```text
  <username> ALL=(ALL) NOPASSWD: /usr/sbin/ip, /usr/sbin/iptables, /usr/sbin/sysctl
  ```
  Also expects `curl` and `dig` (package `dnsutils`) to be installed, since traffic generation shells out
  to them inside each namespace rather than using Python's HTTP/DNS stack directly.

### G. Network Device Scanner (`network_device_scanner`)
- **Purpose**: Inventories every device currently reachable on the LAN behind a chosen Bind Interface —
  IP, MAC address, vendor, and (if resolvable) hostname. Distinct from the narrow, port-scoped `nmap`
  probes in `iperf_congestion_generator` and `web_browsing_simulator`, which only discover other Pis
  running their own service; this app does a real ARP-based host sweep, so it also sees devices with
  every TCP/UDP port closed or firewalled (phones, IoT, etc.).
- **Scan model**: `scan_lan()` resolves the bind interface's subnet CIDR the same way
  `web_browsing_simulator.scan_for_servers()` does (`ip -4 addr show <iface>`, parsed with the
  `ipaddress` module), then runs `sudo -n nmap -e <iface> -sn -T4 -oX - <cidr>` — `-sn` is a ping/ARP
  sweep (no port scan), `-oX -` emits XML to stdout instead of the grepable format used elsewhere in
  this repo, because XML reliably carries the `<address addrtype="mac" vendor="...">` element that
  grepable output doesn't. Reverse-DNS is left enabled (no nmap `-n`) so the `hostname` field is
  populated for devices with a PTR record, at the cost of some added scan latency per responding host.
  `parse_nmap_hosts()` parses that XML with stdlib `xml.etree.ElementTree` (no new dependency) into
  `{ip, mac, vendor, hostname, is_self}` dicts, keeping only hosts with `<status state="up">`; `is_self`
  flags the Pi's own address so the UI can label it.
  - Note: `sudo nmap -sn <cidr>` alone is *not* meaningfully different — run as root against a subnet on
    a directly-connected interface, nmap already defaults to ARP-based discovery. The extra flags here
    (`-e`, `-T4`, `-oX -`) are about interface pinning, speed, and structured output, not detection power.
- **Privilege model**: unlike the unprivileged, port-scoped `nmap` calls elsewhere in this repo, an ARP
  sweep needs raw-socket access, so the scan shells out via `sudo -n nmap ...`. The `-n` on `sudo` makes
  it non-interactive — it fails fast with a clear stderr message instead of hanging the request if the
  sudoers rule isn't configured, which is what lets it degrade gracefully off-Linux (e.g. macOS dev,
  where the sudo prompt has no TTY to read from).
- **Caching**: the last scan result is kept in a module-level `last_scan` dict so `GET /api/devices` can
  return it instantly (used for the initial page load and the UI's auto-refresh poll) without forcing a
  new privileged scan on every request; `POST /api/scan` is what actually triggers `scan_lan()`.
- **System Privilege Requirement**: Executes `sudo nmap -sn ...`. Needs a sudoers rule even when running
  as a non-root user (its systemd unit runs as the app user, not root, since unlike `client_simulator` it
  has no other need for root):
  ```text
  <username> ALL=(ALL) NOPASSWD: /usr/bin/nmap
  ```

### H. WiFi Roaming Monitor (`roaming_monitor`)
- **Purpose**: Builds a live timeline of association events for one wireless interface, so roams between
  BSSIDs can be *timed* and disconnects *explained*. Distinct from the other wireless apps:
  `wifi_utilization_monitor` scans neighbouring BSSes, `wifi_connection_manager` controls the connection
  — this one only observes what the kernel/supplicant actually did, over time.
- **Test context**: the Pi sits static in an RF chamber with a **variable attenuator**, which is what
  makes roaming testable at all — fading one AP down and another up forces the transitions this app
  measures. Without programmable attenuation (or multiple switchable APs) a stationary station rarely
  roams and the roam statistics stay empty; the disconnect/reason-code timeline is still useful there.
- **Event source**: follows `sudo -n iw event -t` in a background thread. stdout is attached to a **pty**
  rather than a pipe, because `iw` block-buffers on a pipe and would deliver events in bursts long after
  they occurred — fatal when timing sub-second roams. `stdbuf -oL` is not an option here: `sudo` resets
  the environment `stdbuf` relies on, and the sudoers rule only grants `iw` anyway.
- **Timestamps**: `iw event -t` stamps events with kernel seconds-since-boot, and those are used for all
  durations rather than the time Python read the line — a burst arriving in one `read()` would otherwise
  look simultaneous. `_boot_time_offset()` reads `/proc/uptime` once to map them to wall-clock for display.
- **Roam timing (`process_event`)**: the clock starts at the first sign of a transition — a
  deauth/disassoc/disconnect, *or* an auth/assoc involving anything other than the current BSSID, since
  802.11r fast transitions skip the disconnect entirely — and stops at the following `connected to`. A
  connect to a *different* BSSID is a `roam`; to the *same* one, a `reconnect`; with no prior BSSID, an
  `initial` connect. Because `iw` prints auth/assoc frames in both directions, `parse_iw_event()` keeps
  **both** MACs (`macs`) and the caller asks whether the current BSSID is among them, rather than
  assuming which side is the AP.
- **Reason/status decoding**: `REASON_CODES` and `STATUS_CODES` map 802.11 numbers to text — most of the
  diagnostic value in the timeline, since "reason 15" alone means nothing but "4-Way Handshake timeout"
  points straight at a PSK/key-exchange problem. Unknown codes are labelled, never dropped.
- **System Privilege Requirement**: Executes `sudo iw event -t`, covered by the **existing** `iw` sudoers
  rule already required by `wifi_utilization_monitor` — no new privilege to configure.

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
   - WiFi Connection Manager worker: bound to `0.0.0.0:5003` (2 workers).
   - Web Browsing Simulator worker: bound to `0.0.0.0:5004` (1 worker, multi-threaded for SSE streaming).
   - Client Simulator worker: bound to `0.0.0.0:5005` (1 worker, multi-threaded for SSE streaming; runs as root by default since it needs `ip`/`iptables`/`sysctl`).
   - Network Device Scanner worker: bound to `0.0.0.0:5006` (1 worker; `sudo -n nmap` needs a sudoers rule since this app runs as the app user, not root).
   - WiFi Roaming Monitor worker: bound to `0.0.0.0:5007` (1 worker, multi-threaded for SSE streaming; reuses the existing `iw` sudoers rule).
2. **Process Management**: **systemd** services located in `deploy/`:
   - `wifi-monitor.service`
   - `iperf-generator.service`
   - `iperf-server-manager.service`
   - `wifi-connection-manager.service`
   - `web-browsing-simulator.service`
   - `client-simulator.service`
   - `network-device-scanner.service`
   - `roaming-monitor.service`
3. **Reverse Proxy**: **Nginx** (`deploy/nginx.conf.example`)
   - Port `80` (Root `/`): Serves default static landing page (`/opt/wifipi/www/index.html`) with cards/links to all tools.
   - Port `80` (Subpath `/wifimon/`): Proxies to WiFi Monitor (`127.0.0.1:5000`).
   - Port `80` (Subpath `/iperf/`): Proxies to iPerf Generator (`127.0.0.1:5001`) with buffering disabled (`proxy_buffering off`, `chunked_transfer_encoding on`) for real-time SSE streaming.
   - Port `80` (Subpath `/iperfserver/`): Proxies to iPerf Server Manager (`127.0.0.1:5002`).
   - Port `80` (Subpath `/wificonnect/`): Proxies to WiFi Connection Manager (`127.0.0.1:5003`).
   - Port `80` (Subpath `/webbrowse/`): Proxies to Web Browsing Simulator (`127.0.0.1:5004`) with buffering disabled for SSE, except `/webbrowse/content/` which is served directly by Nginx via `alias` (bypassing Python) from `/opt/wifipi/web_browsing_simulator/content/`.
   - Port `80` (Subpath `/clientsim/`): Proxies to Client Simulator (`127.0.0.1:5005`) with buffering disabled for SSE.
   - Port `80` (Subpath `/devices/`): Proxies to Network Device Scanner (`127.0.0.1:5006`).
   - Port `80` (Subpath `/roaming/`): Proxies to WiFi Roaming Monitor (`127.0.0.1:5007`) with buffering disabled for SSE.

---

## 5. Maintenance & History Notes

- **Git Repository State**: The repository was re-initialized with a clean initial commit to purge all historical references.
- **Python Environment**: Managed via a shared virtual environment (`.venv`) at `/opt/wifipi/.venv`.
