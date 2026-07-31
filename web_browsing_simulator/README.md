# Web Browsing Simulator

A browser-based tool that generates realistic, bursty web-browsing traffic between two Pis, as a
complement to the iperf3 apps' sustained-throughput tests. Every instance serves a randomized corpus of
synthetic pages and assets; point one instance at another to simulate a user browsing that "site".

## Setup (once)

```bash
cd web_browsing_simulator

python3 -m venv .venv
source .venv/bin/activate
pip install flask
```

## Run

```bash
source .venv/bin/activate
python app.py
```

Then open **http://<pi-ip>:5004** in a browser.

On first start, the app generates a fresh randomized content corpus (30-50 pages, each with 3-25 assets
of varying size) under `content/` in this directory. It regenerates on every restart, so each test
session sees a different mix of page weights.

## Features

| Feature | Detail |
|---|---|
| Target IP & Port | Type manually (or IP:Port) or click **Scan LAN** to discover other instances on port 5004 |
| Port 80 (default) | Browses through the target's nginx, which serves the generated corpus directly via a static `alias` — the realistic path, and the one that actually exercises nginx |
| Port 5004 | Talks to the target's Flask process directly — useful for a dev-to-dev test with no nginx in front |
| Duration | Minutes; the simulation picks a random page each iteration, fetches its assets with bounded (6-way) concurrency, then idles 2-8s before the next page |
| Live output | Page-load summaries (object count, bytes, load time, effective throughput) stream to the browser in real time via SSE |
| Stop | Halts the running simulation after its current page finishes |

## Deployment note

To get the realistic/efficient serving path in production, `deploy/nginx.conf.example` needs the
`location /webbrowse/content/ { alias ...; }` block pointing at this app's `content/` directory, in
addition to the generic `/webbrowse/` proxy block every other app has. Without it, this app still works
standalone on port 5004 (client and content routes are both served by Flask), just without nginx doing
the static serving.

## Dependencies

- `nmap` (optional, for LAN scan) — `sudo apt-get install nmap`
- Python 3.10+ with `flask`
- No sudo/privileged commands are used by this app.
