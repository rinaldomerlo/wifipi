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
```

Dependencies are only `flask` and `gunicorn` (`requirements.txt`). The venv lives at `.venv/` locally and
`/opt/wifipi/.venv` in production. Do not add third-party packages without asking.

---

## Environment Split — Important

- **Deployment target**: Raspberry Pi OS / Debian. Real `iw`, `systemctl`, `iperf3`, `nmap`, `/proc/net/wireless`.
- **Development machine**: usually macOS, where none of those behave the same way.

Code that shells out to system tools must degrade gracefully off-Linux rather than crashing — wrap in
`try/except`, check `shutil.which(...)`, or fall back. Tests must pass on macOS with no privileged
tooling present; mock the subprocess boundary instead of requiring the real binary.

Two apps need passwordless sudo in production (rules documented in `README.md` and `AGENTS.md`):
`iw` for the WiFi monitor, and `systemctl ... iperf3*` for the server manager.

---

## UI Requirements

All four GUI screens share one design system. When adding UI, match the existing patterns rather than
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
  (`del sys.modules['app']`) that prevents collisions between the three same-named `app` modules —
  preserve it.
- Do not commit or push unless asked.
