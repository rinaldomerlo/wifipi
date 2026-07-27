# iperf3 Flask Client

A browser-based UI equivalent of `simple_iperf3_start.sh`.  
Run it on a Raspberry Pi and open the page from any device on the same network.

## Setup (once)

```bash
cd iperf_congestion_generator

python3 -m venv .venv
source .venv/bin/activate
pip install flask
```

## Run

```bash
source .venv/bin/activate
python app.py
```

Then open **http://<pi-ip>:5001** in a browser.

## Features

| Feature | Detail |
|---|---|
| Server IP | Type manually or click **Scan LAN** to discover iperf3 servers on port 5201 |
| Duration | Minutes; automatically loops in 24 h chunks for runs > 24 h |
| Interface | `wlan0` or `eth0` |
| Bandwidth | Mbps target, or leave blank for unlimited |
| Live output | iperf3 stdout streams to the browser in real time; newlines are preserved via SSE escaping |
| Stop | Terminates the running iperf3 process immediately |

## Dependencies

- `iperf3` — `sudo apt-get install iperf3`
- `nmap` (optional, for LAN scan) — `sudo apt-get install nmap`
- Python 3.10+ with `flask`
