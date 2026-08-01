# CLAUDE.md

Guidance for Claude Code when working in this repository.

See `AGENTS.md` for the fuller architectural background and deployment reference; this file covers the
day-to-day working rules. `README.md` holds the end-user install/deploy instructions.

---

## Repository Layout

| Path | What it is |
| --- | --- |
| `wifi_utilization_monitor/` | Flask app — live WiFi channel/spectrum monitor (2.4/5/6 GHz). Port 5000, proxied at `/wifimon/`. |
| `iperf_congestion_generator/` | Flask app — starts/stops `iperf3` client streams, streams output over SSE. Port 5001, proxied at `/iperf/`. |
| `iperf_server_manager/` | Flask app — discovers and controls `iperf3` server daemons and systemd units. Port 5002, proxied at `/iperfserver/`. |
| `wifi_connection_manager/` | Flask app — scans/connects/disconnects WiFi via `nmcli` (NetworkManager). Port 5003, proxied at `/wificonnect/`. |
| `web_browsing_simulator/` | Flask app — simulates bursty web-browsing traffic against another Pi's synthetic page corpus. Port 5004, proxied at `/webbrowse/`. |
| `client_simulator/` | Flask app — simulates many clients behind one WiFi association via netns/veth/bridge NAT, with churn. Port 5005, proxied at `/clientsim/`. |
| `network_device_scanner/` | Flask app — ARP-based LAN device inventory (IP/MAC/vendor) via `nmap -sn` on a chosen Bind Interface. Port 5006, proxied at `/devices/`. |
| `roaming_monitor/` | Flask app — live association-event timeline from `iw event`, with roam timing and decoded 802.11 reason codes. Port 5007, proxied at `/roaming/`. |
| `web_terminal/` | Flask app — thin wrapper framing a `ttyd` browser shell. Port 5008, proxied at `/terminal/`; `ttyd` itself listens on loopback:5009. |
| `www/index.html` | Static landing page served at `/`. No backend. |
| `deploy/` | systemd unit files and `nginx.conf.example`. |
| `tests/` | `unittest` suite, one module per app. |

Each Flask app is self-contained: `app.py` plus a `templates/index.html` that embeds its own CSS and JS.
Only `wifi_utilization_monitor` has separate `static/` assets and a `parser.py`.

---

## Commands

```bash
# Run the test suite (no pytest installed — use unittest)
.venv/bin/python -m unittest discover -s tests

# Run a single app in dev mode (from its own directory, so templates resolve)
cd wifi_utilization_monitor && ../.venv/bin/python app.py        # :5000
cd iperf_congestion_generator && ../.venv/bin/python app.py      # :5001
cd iperf_server_manager && ../.venv/bin/python app.py            # :5002
cd wifi_connection_manager && ../.venv/bin/python app.py         # :5003
cd web_browsing_simulator && ../.venv/bin/python app.py          # :5004
cd client_simulator && ../.venv/bin/python app.py                # :5005
cd network_device_scanner && ../.venv/bin/python app.py          # :5006
cd roaming_monitor && ../.venv/bin/python app.py                 # :5007
cd web_terminal && ../.venv/bin/python app.py                    # :5008
```

Dependencies are only `flask` and `gunicorn` (`requirements.txt`). The venv lives at `.venv/` locally and
`/opt/wifipi/.venv` in production. Do not add third-party packages without asking.

---

## Environment Split — Important

- **Deployment target**: Raspberry Pi OS / Debian. Real `iw`, `systemctl`, `nmcli`, `iperf3`, `nmap`, `/proc/net/wireless`.
- **Development machine**: usually macOS, where none of those behave the same way.

Code that shells out to system tools must degrade gracefully off-Linux rather than crashing — wrap in
`try/except`, check `shutil.which(...)`, or fall back. Tests must pass on macOS with no privileged
tooling present; mock the subprocess boundary instead of requiring the real binary.

Four apps need passwordless sudo in production (rules documented in `README.md` and `AGENTS.md`):
`iw` for the WiFi monitor, `systemctl ... iperf3*` for the server manager, `nmcli` for the connection
manager, and `nmap` for the network device scanner's ARP sweep. The connection manager also requires
NetworkManager to be the active network backend — it does not work against the older
`dhcpcd`/`wpa_supplicant` stack on pre-Bookworm Raspberry Pi OS images.

`client_simulator` needs root for `ip`/`iptables`/`sysctl` (network namespaces, veth, NAT); its systemd
unit runs as `root` by default so no sudoers entry is needed unless it's reconfigured to run as a non-root
user. It detects at runtime whether real namespaces are usable (`detect_mode()` in `app.py`) and falls
back to a plain-thread/urllib simulation otherwise — this is what makes it work on macOS in dev.

`network_device_scanner` invokes `sudo -n nmap -sn ...` (non-interactive, so it fails fast with a clear
error instead of hanging a request) for its ARP-based host sweep. Off-Linux or without the sudoers rule
configured, the scan route returns a JSON error rather than crashing — this is what makes it degrade
gracefully on macOS in dev.

`web_terminal` is the odd one out and deliberately so. It owns **no** terminal logic: the PTY, VT/ANSI
emulation, resize and reconnect all belong to `ttyd`, a separate daemon bound to `127.0.0.1:5009` that
embeds xterm.js. The Flask app only renders the shared header around an iframe and TCP-probes ttyd for a
health check, so it shells out to nothing and needs no privileges. Two consequences worth remembering:

- It is the **only app that runs as a non-root user**, because a browser shell is a far wider surface
  than the constrained `sudo nmcli` / `sudo nmap` the other apps expose. Both `deploy/ttyd.service` and
  `deploy/web-terminal.service` default to `User=pi` under a marked block at the top of `[Service]`, and
  **both must be changed together** to the real account — modern Raspberry Pi OS has no `pi` user. It is
  an in-place edit by design: systemd does not expand environment variables in `User=`, so this cannot be
  driven from an `EnvironmentFile`. Don't try to "improve" it into one. Neither unit sets `Group=`, so
  systemd inherits the account's primary group; adding one back breaks with `status=216/GROUP` whenever
  the primary group isn't named after the user.
- `ttyd` is **not in Debian bookworm or trixie** (only sid), so it is installed manually from upstream's
  static release binary — see `README.md`. Don't "fix" this to `apt install ttyd`; it will fail.

Its nginx block also needs the `$connection_upgrade` map from `deploy/nginx-websocket-map.conf.example`,
which lives in `/etc/nginx/conf.d/` rather than the site config because `map` is only valid in `http`
context. If you add WebSocket routes elsewhere, reuse that map rather than redefining it.

`roaming_monitor` follows `sudo -n iw event -t` in a background thread; it reuses the WiFi monitor's
existing `iw` sudoers rule, so no new privilege is needed. Its start route refuses up front on non-Linux
or when `iw` is absent, so macOS dev gets a clear error rather than a hung thread. Note it attaches the
subprocess to a **pty** rather than a pipe — `iw` block-buffers when it sees a pipe, which would batch
events instead of streaming them; `stdbuf` isn't usable here because `sudo` resets its environment.

---

## UI Requirements

All GUI screens share one design system. When adding UI, match the existing patterns rather than
introducing new ones:

- Glassmorphism over a dark gradient: `--glass-bg`, `--glass-border`, translucent cards, `backdrop-filter: blur(12px)`.
- Fonts: `Outfit` (`--font-main`) for body, `Space Grotesk` (`--font-heading`) for headings and metrics.
- FontAwesome icons on buttons, status indicators, and tabs.
- CSS custom properties are declared in a `:root` block inside each template — reuse the existing
  variable names; do not hardcode colors.

**Every GUI screen must display the hostname.** This is a standing requirement, not a one-off.

- Flask apps: `get_hostname()` in `app.py` wraps `socket.gethostname()`, is passed to `render_template`
  as `hostname`, and renders in the header via the `.host-badge` span. Each app also exposes
  `GET /api/hostname` → `{"hostname": ...}` purely so the static landing page can reuse it.
- `www/index.html` has no backend, so it resolves the host in three escalating steps:
  1. Nginx SSI (`<!--# echo var="hostname" -->`, needs `ssi on;` in `deploy/nginx.conf.example`).
  2. Failing that, `fetch` one of the apps' `/api/hostname` endpoints (same origin via the proxy).
  3. Failing that, `window.location.hostname` — which is often just an IP, so it is the last resort.
- Each app's test module asserts the badge, the hostname, and the endpoint — keep those passing when
  touching headers.

Any new screen added to this project inherits both the design system and the hostname requirement.

---

## Conventions

- Python: stdlib-first, module-level helper functions, docstrings on anything that shells out.
- Keep each app's HTML/CSS/JS inline in its template, matching the file it lives in — this project
  deliberately avoids a build step or shared asset pipeline.
- Add tests to the matching `tests/test_*.py` module. Note the import guard at the top of those files
  (`del sys.modules['app']`) that prevents collisions between the same-named `app` modules across the
  different Flask apps — preserve it.
- Do not commit or push unless asked.
