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
- `iperf_congestion_generator/` — Browser-based controller for long-running `iperf3` test streams; runs several at once, each with its own tab and output buffer.
- `iperf_server_manager/` — Web interface to view, start, stop, restart, and monitor `iperf3` server daemons and systemd services.
- `wifi_connection_manager/` — Web interface to scan, connect, disconnect, and manage saved WiFi networks via NetworkManager (`nmcli`).
- `web_browsing_simulator/` — Simulates realistic, bursty web-browsing traffic against another Pi's randomized synthetic page corpus, as a complement to `iperf_congestion_generator`'s sustained-throughput streams.
- `client_simulator/` — Simulates many independent clients behind a single WiFi association using isolated Linux network namespaces NAT'd out one radio, with a churn engine cycling client identities.
- `network_device_scanner/` — ARP-based LAN device inventory (IP/MAC/vendor/hostname) via a privileged `nmap -sn` sweep of a chosen Bind Interface's subnet.
- `roaming_monitor/` — Live association-event timeline from `iw event`, timing roams between BSSIDs and decoding 802.11 reason codes.
- `web_terminal/` — Thin Flask wrapper framing a `ttyd` browser shell, so the Pi's command line is reachable from the same UI as the rest of the suite.
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
- **Bind Interface detection**: `get_bindable_interfaces()` (`GET /interfaces`) lists real interfaces with a
  live IPv4 address via `ip -4 -o addr show scope global` (read-only, no privilege needed) rather than a
  hardcoded `wlan0`/`eth0` allowlist — matters on boxes with more than one radio (e.g. `wlan0`-`wlan4` on
  the porcupine Pi) or a non-default interface name. `[]` (off-Linux, no iproute2) is treated as "can't
  verify" everywhere it's consulted, not "no interfaces exist" — the static `wlan0`/`eth0` `<option>`s
  already in the template stay as a fallback the dropdown never fully loses.

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
  - **Multi-interface**: `get_wireless_interfaces()` enumerates every wifi-typed device from
    `nmcli device status`, not just the first — a Pi with a USB dongle alongside its built-in radio gets
    both. `GET /api/interfaces` exposes the list for the frontend's target-interface `<select>`.
  - `GET /api/status` — per-interface connection status for *every* detected WiFi device, via the shared
    `interface_status(iface)` helper, each scoped with `nmcli ... device wifi list ifname <iface>` so one
    radio's scan results can't be mistaken for another's.
  - `GET /api/scan` — `nmcli device wifi list ifname <iface> --rescan yes` against a chosen interface
    (`?interface=`, default the first detected device; validated by `_valid_interface()` against the live
    device list plus a `^[A-Za-z0-9_.-]+$` shape check before being interpolated into the nmcli command),
    de-duplicated by SSID: the connected BSSID always wins (so mesh/repeater setups with one SSID on
    multiple APs don't lose the "Connected" state behind a stronger unconnected AP), otherwise the
    strongest signal wins.
  - `GET /api/saved` / `POST /api/forget` — saved connection profiles (`nmcli connection show`/`delete`).
  - `POST /api/autoconnect` — enable/disable NetworkManager's auto-connect per saved profile
    (`nmcli connection modify <name> autoconnect yes|no`), surfaced as a toggle switch in the Saved
    Networks table — useful for test-lab setups where unattended reconnection is unwanted.
  - `POST /api/connect` / `POST /api/disconnect` — `nmcli device wifi connect` / `nmcli device disconnect`,
    both accepting an optional `interface` in the JSON body (validated the same way as `/api/scan`;
    defaults to the first detected device). nmcli's stderr is mapped to a friendlier message (e.g.
    "Secrets were required..." → "Incorrect password.") by `friendly_connect_error()`. The frontend skips
    the password modal when the SSID is open or matches an already-saved profile (NetworkManager already
    has secrets for the latter).
  - `POST /api/connect-all` — bulk-connects every wireless interface (or, with `only_idle` — the default
    — just the ones not already associated) to one SSID/password in a simple loop, returning per-interface
    `{interface, ok, message|error}` results plus `connected`/`failed` counts. Drives the "Connect all
    idle" button in the Available Networks header.
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
- **Bind Interface detection**: same `get_bindable_interfaces()`/`GET /interfaces` pattern as
  `iperf_congestion_generator` — see section B.

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
- **Bind Interface detection**: `get_bindable_interfaces()`/`GET /api/interfaces` (`/api/`-prefixed here,
  matching this app's own route convention, unlike the unprefixed `/interfaces` in section B/E) — same
  underlying pattern otherwise.
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
- **Seeding the current BSSID**: `iw event` only reports transitions as they occur, so a session
  started while already associated has nothing to learn the current BSSID from. `get_current_link()`
  reads it from `iw dev <iface> link` (unprivileged) at start and emits a `baseline` timeline entry.
  This is correctness, not cosmetics: `current_bssid` is what classifies the next connect, so without
  seeding, the first roam of every session read as an `initial` connect and went uncounted and untimed.
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

### I. Web Terminal (`web_terminal`)
- **Purpose**: Puts an interactive shell on the Pi in the same browser UI as everything else, for the
  commands the purpose-built apps don't cover — inspecting logs, running one-off `iw`/`ip` invocations,
  editing a config — without switching to an SSH client.
- **Architecture — the app owns no terminal logic**: the requirement was to use an off-the-shelf emulator,
  not write one. [`ttyd`](https://github.com/tsl0922/ttyd) runs as a separate daemon on
  `127.0.0.1:5009`, embedding xterm.js and handling PTY allocation, VT/ANSI emulation, resize, reconnect,
  binary safety and flow control. `web_terminal/app.py` renders the shared header around an
  `<iframe src="tty/">` and exposes `GET /api/status`, a 0.5s TCP probe of that port. It shells out to
  nothing and needs no privileges — which is also why its test module has no subprocess to mock.
- **Why not a hand-rolled emulator**: `flask-sock` + stdlib `pty` was the runner-up (pip-only install, no
  iframe) but moves every PTY edge case in-house: controlling-terminal setup via `pty.fork()`, incremental
  UTF-8 decoding across read boundaries, zombie reaping, and a thread budget for long-lived sockets.
  `terminado` (Jupyter's) is well proven but Tornado-based — a second web framework for one screen.
  `pyte`, the established *pure-Python* emulator, is the wrong layer entirely: it maintains a server-side
  screen buffer, so using it would mean pushing full screen frames and writing a bespoke JS renderer,
  losing local echo, scrollback and selection. Its real use is screen-scraping TUIs.
- **ttyd flags are load-bearing** (`deploy/ttyd.service`): `--writable` is mandatory since ttyd is
  **read-only by default** and silently accepts no keystrokes without it; `--base-path /terminal/tty`
  makes ttyd emit URLs correct for the subpath, without which the frame loads but the WebSocket connects
  to the wrong path and hangs blank; `--interface lo` keeps port 5009 off the LAN so Nginx is the only
  route in; `-t theme=...` themes the embedded xterm.js to the WiFiPi palette so the frame doesn't read
  as pasted in.
- **Nginx**: two locations, with `/terminal/tty/` winning on longest-prefix over `/terminal/`. Its
  `proxy_pass` deliberately has **no URI part**, so the path reaches ttyd unmodified and matches
  `--base-path`. Needs the `$connection_upgrade` map from `deploy/nginx-websocket-map.conf.example`,
  which must go in `/etc/nginx/conf.d/` because `map` is only valid in `http` context while the site
  config is a bare `server { }` block. `proxy_read_timeout 86400` stops idle sessions being dropped.
- **Installation**: `ttyd` is **not packaged in Debian bookworm or trixie** (only sid), so it is installed
  manually from upstream's static release binary — see `README.md`. Do not change this to
  `apt install ttyd`.
- **System Privilege Requirement — none, and deliberately less than its siblings**: this is the only app
  whose units set a non-root `User=`. Both units default to `pi` under a marked block at the top of their
  `[Service]` section and must be changed together, since modern Raspberry Pi OS images have no `pi`
  account. The value is edited in place because systemd does not expand environment variables in `User=`.
  Neither unit sets `Group=` — systemd falls back to the account's primary group from `/etc/passwd`,
  which is correct everywhere; a hardcoded `Group=` matching the username fails with `status=216/GROUP`
  on images where the primary group differs. The app has no authentication of its own; `ttyd`'s
  `--credential user:password` is available if it's ever wanted.

### J. WiFi Porcupine (`wifi_porcupine`)
- **Purpose**: Stress an AP's *association* side — the tables the other generators never touch. It enlists
  several physical WiFi interfaces (the built-in radio plus USB adapters) and rapidly, randomly associates
  and disassociates each against one target SSID, optionally randomizing the interface's MAC on every
  reconnect so the hub sees a constant stream of brand-new stations bloating its association / DHCP-lease /
  ARP tables. Each interface is a "spine" repeatedly poking the AP.
- **Association/MAC churn**: one NetworkManager profile per interface (`porcupine-<iface>`). A `randomize_mac`
  toggle in the UI (default on) controls whether the profile is created with
  `802-11-wireless.cloned-mac-address random` (`build_profile_add_args`) — off, the interface churns under
  its own real MAC. A per-interface daemon thread loops connect → dwell → disconnect → gap; the (possibly
  cloned) MAC is read back from `/sys/class/net/<iface>/address` and logged. Profiles are deleted on stop.
- **Churn shape**: three orthogonal sliders, not one. **Presence** (5–95%) is the duty cycle — what
  fraction of each cycle the interface stays associated. **Churn rate** is a fine-grained position (1–100)
  mapped *geometrically* to reconnects/min (`churn_rate_from_pos`, `RATE_AT_MIN`..`RATE_AT_MAX`). **Variability**
  (0–100) sets the gamma *shape* of the per-cycle draw (`gamma_shape_from_variability`) — 0 metronomic, 100
  bursty — without moving Presence or the rate. `compute_durations(presence, churn)` turns the first two into
  a per-cycle ON (associated) and OFF (idle gap) mean: `period = 60/rate`, `ON = presence·period`,
  `OFF = (1-presence)·period` minus the fixed scan+DHCP reconnect cost (`OFFLINE_ESTIMATE_SECONDS`, which
  `bring_up` already spends), floored at `MIN_DWELL`/`GAP_MIN`. So a low Presence + slow rate is a quiet
  household device (mostly disconnected); a high rate is an association storm. Concurrency is not derived from
  any of them: every ticked interface churns simultaneously in its own daemon thread. A random initial jitter
  (0..full cycle) plus a per-run per-interface speed `bias` and the long-tailed per-cycle draw keep interfaces
  desynchronized from cycle 1 rather than all connecting in lockstep because `start_run` launches them
  together. All slider bounds and the model constants are templated into the page so the JS
  (`churn_rate_from_pos`/`computeDurations`/`achievableRate`, mirroring the Python) can show a live "connected
  ~X, off ~Y · ≈Z reconnects/min" readout — with the achievable rate capped honestly once the fixed reconnect
  cost dominates at the top of the churn slider. Keep the JS and the Python helpers in sync if either changes.
- **Target SSID convenience**: `GET /api/scan` scans one interface (`--rescan yes`; fine here since it's a
  single one-off request, not something polled on a timer like `wifi_connection_manager`'s status endpoint)
  and returns a deduped, signal-sorted network list purely so the SSID field can be filled by clicking
  instead of typing blind. Picking a network then calls `GET /api/saved-password?ssid=` , which searches
  saved NetworkManager wifi profiles for one whose `802-11-wireless.ssid` property matches (matched on the
  SSID property, not the profile name) and reveals its PSK (`find_saved_password`) to auto-fill the password
  field if this Pi already has that network configured (e.g. via `wifi_connection_manager`). Deliberately
  not a hard dependency on `wifi_connection_manager` being installed — both endpoints degrade to "nothing
  found" rather than erroring, and porcupine still creates its own disposable profile regardless of where
  the password came from.
- **Log**: a bounded `deque` + monotonic cursor (like `iperf_congestion_generator`), polled at
  `GET /api/output?since=`, so reloads/reattach and multiple tabs replay cleanly rather than stealing a
  destructive stream. The page reattaches to a run already in progress on load.
- **Degradation & idempotency**: refuses up front off-Linux or without `nmcli`/`iw`, returning a clear JSON
  error so macOS dev never crashes. A best-effort startup sweep removes leftover `porcupine-*` profiles so a
  hard restart is idempotent.
- **Single-instance guard**: `run_state["running"]` only stops a second run *within this process*; it's
  invisible to a second OS process (a stray manual `python app.py` left running next to the systemd service,
  a duplicate deploy). `acquire_run_lock()`/`release_run_lock()` add a machine-wide `flock()` on
  `<tmp>/wifi-porcupine.lock`, taken alongside `run_state["running"]` in `/api/start` and dropped at the end
  of `stop_run()`. flock is per-open-file-description and OS-released on process exit (even a crash/SIGKILL),
  so there's no PID file staleness to detect or sweep on restart. A rejected start's error message includes
  whatever the current holder stamped into the lock file (PID + target SSID) purely for diagnostics.
- **System Privilege Requirement**: needs only `nmcli` (+ `iw` for interface listing). The unit still runs
  as **root** by default, so **no new sudoers entry** is required (commands still go via `sudo`, a no-op
  under root). Requires NetworkManager as the active backend, same as `wifi_connection_manager`.

### K. Reboot Manager (`reboot_manager`)
- **Purpose**: The one app whose entire job is to take the host down. Shows uptime and platform, and
  reboots this Pi (`sudo systemctl reboot`, falling back to `sudo reboot` if `systemctl` isn't on PATH)
  from a single confirmed action.
- **Confirmation is layered, not single-point**: the browser UI swaps the reboot button for an inline,
  cancellable 5-second countdown bar rather than firing immediately or using a blocking native `confirm()`
  (which the other apps use for less consequential actions like killing one iperf3 PID). The countdown
  auto-POSTs `/api/reboot` with `{"confirm": "REBOOT"}` when it reaches zero unless cancelled. The API
  independently rejects any request missing that exact token, so a stray or scripted POST — bypassing the
  UI entirely — can't take the host down by accident.
- **The reboot itself runs from a background thread** (`_do_reboot`), started after the route returns
  `202`, with a short (`REBOOT_DELAY_SECONDS`) sleep before the actual command runs. This exists solely so
  the HTTP response has time to flush to the client before the process — and the whole host — goes down;
  without it, the client can be left hanging on a connection that never completes.
- **`reboot_state["pending"]`**, guarded by `reboot_lock`, rejects a second `/api/reboot` call (`409`)
  while one is already in flight. Gunicorn is configured `--workers 1` for this app specifically so that
  in-process guard actually holds across every request, the same reasoning as the WiFi Monitor's
  single-worker scan lock.
- **After a successful trigger**, the page hides the action card and polls `GET /api/hostname` every 4s
  (after an initial 8s grace period so it doesn't catch the host still up and declare victory early) until
  it answers again, then reports "back online" with a reload link — useful feedback since the browser tab
  itself goes dark for the duration.
- **System Privilege Requirement**: needs only `systemctl` (or `reboot`) on PATH. The unit runs as **root**
  by default like `client_simulator` and `wifi_porcupine`, so **no new sudoers entry** is required
  (commands still go via `sudo`, a no-op under root). Refuses up front off-Linux or without a reboot
  mechanism on PATH, returning a clear JSON error — critical here specifically, since the alternative on a
  macOS dev machine would be either a crash or, worse, actually rebooting the developer's laptop.

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
   - WiFi Monitor worker: bound to `0.0.0.0:5000` (1 worker, multi-threaded -- must stay a single
     process, since it coalesces concurrent `/api/scan` requests behind an in-process lock + short
     cache to stop multiple dashboard viewers from racing each other into `iw`'s "device is busy").
   - iPerf Generator worker: bound to `0.0.0.0:5001` (1 worker, multi-threaded for SSE streaming).
   - iPerf Server Manager worker: bound to `0.0.0.0:5002` (2 workers).
   - WiFi Connection Manager worker: bound to `0.0.0.0:5003` (2 workers).
   - Web Browsing Simulator worker: bound to `0.0.0.0:5004` (1 worker, multi-threaded for SSE streaming).
   - Client Simulator worker: bound to `0.0.0.0:5005` (1 worker, multi-threaded for SSE streaming; runs as root by default since it needs `ip`/`iptables`/`sysctl`).
   - Network Device Scanner worker: bound to `0.0.0.0:5006` (1 worker; `sudo -n nmap` needs a sudoers rule since this app runs as the app user, not root).
   - WiFi Roaming Monitor worker: bound to `0.0.0.0:5007` (1 worker, multi-threaded for SSE streaming; reuses the existing `iw` sudoers rule).
   - Web Terminal wrapper: bound to `0.0.0.0:5008` (1 worker, multi-threaded; runs as `User=pi`, needs no privileges).
   - `ttyd` backend: bound to `127.0.0.1:5009` only, never the LAN (also `User=pi`; not a Gunicorn app — a standalone daemon installed to `/usr/local/bin/ttyd`).
   - WiFi Porcupine worker: bound to `0.0.0.0:5010` (1 worker, multi-threaded; runs as root by default since it needs `nmcli`/`ip`/`iptables`/`sysctl`).
   - Reboot Manager worker: bound to `0.0.0.0:5011` (1 worker, multi-threaded — must stay a single process so its in-process reboot-pending guard holds across requests; runs as root by default since it needs `systemctl reboot`/`reboot`).
2. **Process Management**: **systemd** services located in `deploy/`:
   - `wifi-monitor.service`
   - `iperf-generator.service`
   - `iperf-server-manager.service`
   - `wifi-connection-manager.service`
   - `web-browsing-simulator.service`
   - `client-simulator.service`
   - `network-device-scanner.service`
   - `roaming-monitor.service`
   - `web-terminal.service`
   - `ttyd.service`
   - `wifi-porcupine.service`
   - `reboot-manager.service`
3. **Reverse Proxy**: **Nginx** (`deploy/nginx.conf.example`) — the site config is app-independent: it
   serves the landing page and `include`s per-app snippets glob-matched from `/etc/nginx/wifipi.d/*.conf`.
   Each app's proxy block is its own snippet, `deploy/nginx.d/<app>.conf`, installed per Pi to match
   whichever units that Pi runs — an app that isn't installed is a clean 404 rather than a 502 from a
   proxy pointing at a dead backend. The landing page (`www/index.html`) self-discovers which apps are
   live by probing each card's `/<app>/api/hostname` in parallel, so none of this needs per-Pi editing.
   The subpaths below enumerate every snippet the suite can install, as a reference:
   - Port `80` (Root `/`): Serves default static landing page (`/opt/wifipi/www/index.html`) with cards/links to all tools.
   - Port `80` (Subpath `/wifimon/`): Proxies to WiFi Monitor (`127.0.0.1:5000`).
   - Port `80` (Subpath `/iperf/`): Proxies to iPerf Generator (`127.0.0.1:5001`). No longer uses SSE — output is polled from per-test ring buffers — so the buffering directives there are vestigial and harmless.
   - Port `80` (Subpath `/iperfserver/`): Proxies to iPerf Server Manager (`127.0.0.1:5002`).
   - Port `80` (Subpath `/wificonnect/`): Proxies to WiFi Connection Manager (`127.0.0.1:5003`).
   - Port `80` (Subpath `/webbrowse/`): Proxies to Web Browsing Simulator (`127.0.0.1:5004`) with buffering disabled for SSE, except `/webbrowse/content/` which is served directly by Nginx via `alias` (bypassing Python) from `/opt/wifipi/web_browsing_simulator/content/`.
   - Port `80` (Subpath `/clientsim/`): Proxies to Client Simulator (`127.0.0.1:5005`) with buffering disabled for SSE.
   - Port `80` (Subpath `/devices/`): Proxies to Network Device Scanner (`127.0.0.1:5006`).
   - Port `80` (Subpath `/roaming/`): Proxies to WiFi Roaming Monitor (`127.0.0.1:5007`) with buffering disabled for SSE.
   - Port `80` (Subpath `/terminal/`): Proxies to the Web Terminal wrapper (`127.0.0.1:5008`).
   - Port `80` (Subpath `/terminal/tty/`): Proxies to `ttyd` (`127.0.0.1:5009`) with WebSocket upgrade headers, requiring the `$connection_upgrade` map installed to `/etc/nginx/conf.d/`.
   - Port `80` (Subpath `/porcupine/`): Proxies to WiFi Porcupine (`127.0.0.1:5010`). Plain proxy — output is polled from a ring buffer, no SSE.
   - Port `80` (Subpath `/reboot/`): Proxies to Reboot Manager (`127.0.0.1:5011`). Plain proxy — status is polled, no SSE.

---

## 5. Maintenance & History Notes

- **Git Repository State**: The repository was re-initialized with a clean initial commit to purge all historical references.
- **Python Environment**: Managed via a shared virtual environment (`.venv`) at `/opt/wifipi/.venv`.
