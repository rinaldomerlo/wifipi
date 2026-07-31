#!/usr/bin/env python3
"""
Flask app that simulates many independent clients behind a single WiFi
association ("NAT mode" from the design doc). Each simulated client gets its
own Linux network namespace, connected via a veth pair to an internal bridge
(wfsim-br0) that is NAT'd (MASQUERADE) out through the real wlan0/eth0
interface. The physical interface is never added to the bridge itself, so
this is ordinary L3 NAT through the one WiFi station -- the router only ever
sees the Pi's single MAC/IP -- while the Pi's own kernel still does real
per-client routing/ARP/conntrack work, and a churn engine periodically tears
down and recreates a fraction of clients to simulate devices joining and
leaving.

Creating namespaces requires Linux + root (or passwordless sudo for `ip`).
Where that isn't available -- e.g. macOS during development -- the app falls
back to a "simulated" mode that runs the same client/churn lifecycle with
plain threads and urllib instead of real namespaces, so the control flow is
still exercisable. See CLAUDE.md's Environment Split section.
"""

import ipaddress
import itertools
import platform
import queue
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)

NS_PREFIX = "wfsim"
BRIDGE_NAME = "wfsim-br0"
SUBNET = ipaddress.ip_network("10.200.0.0/16")
GATEWAY_IP = str(SUBNET.network_address + 1)

CLIENT_COUNT_RANGE = (1, 500)
DEFAULT_CLIENT_COUNT = 20
CHURN_RATE_RANGE = (0, 50)  # percent of active clients replaced per churn interval
DEFAULT_CHURN_RATE = 10
CHURN_INTERVAL_SECONDS = 60
THINK_TIME_RANGE = (2, 8)
REQUEST_TIMEOUT = 10
DEFAULT_DNS_TARGETS = ["example.com", "cloudflare.com", "wikipedia.org"]

sim_lock = threading.Lock()
sim_running = False
sim_clients = {}  # client_id -> {"stop_event": Event, "thread": Thread, "ip": str}
sim_stats = {"requests": 0, "errors": 0, "churn_events": 0}
sim_context = {"mode": None, "bind_interface": None}
sim_id_counter = itertools.count(1)
stop_event = threading.Event()
output_queue = queue.Queue()


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def _run(cmd, timeout=10):
    """Run a command (already including sudo if needed) and return (ok, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return False, "", str(e)


def detect_mode(bind_interface):
    """Decide whether real network namespaces can be used on this host, or whether to
    fall back to the plain-thread/urllib simulation. Returns (mode, reason_if_simulated)."""
    if platform.system() != "Linux":
        return "simulated", "not running on Linux"
    if not shutil.which("ip"):
        return "simulated", "the 'ip' command is not installed"
    ok, _, _ = _run(["sudo", "-n", "ip", "netns", "list"], timeout=5)
    if not ok:
        return "simulated", "passwordless sudo for 'ip' is not configured (see README)"
    return "netns", None


def ns_name(client_id: int) -> str:
    return f"{NS_PREFIX}-{client_id}"


def veth_names(client_id: int):
    """Host-side and namespace-side veth interface names, kept under the 15-char IFNAMSIZ limit."""
    return f"{NS_PREFIX}{client_id}h", f"{NS_PREFIX}{client_id}p"


def client_ip(client_id: int) -> str:
    return str(SUBNET.network_address + 1 + client_id)


def setup_bridge(bind_interface):
    """Create the internal bridge and NAT it out bind_interface. bind_interface itself is
    never added to the bridge -- only veth host-ends are -- so this is ordinary L3 NAT
    through the single WiFi association, not an attempt to bridge multiple MACs onto the
    radio (which is what makes macvlan unworkable over WiFi in the first place)."""
    steps = [
        ["sudo", "ip", "link", "add", BRIDGE_NAME, "type", "bridge"],
        ["sudo", "ip", "addr", "add", f"{GATEWAY_IP}/{SUBNET.prefixlen}", "dev", BRIDGE_NAME],
        ["sudo", "ip", "link", "set", BRIDGE_NAME, "up"],
        ["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"],
        ["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", str(SUBNET), "-o", bind_interface, "-j", "MASQUERADE"],
        ["sudo", "iptables", "-A", "FORWARD", "-i", BRIDGE_NAME, "-o", bind_interface, "-j", "ACCEPT"],
        ["sudo", "iptables", "-A", "FORWARD", "-i", bind_interface, "-o", BRIDGE_NAME,
         "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]
    for cmd in steps:
        ok, _, err = _run(cmd)
        if not ok:
            return False, err.strip() or " ".join(cmd)
    return True, None


def teardown_bridge(bind_interface):
    """Best-effort reversal of setup_bridge(); individual failures are ignored since some
    rules may already be gone (e.g. after a partial setup failure)."""
    steps = [
        ["sudo", "iptables", "-t", "nat", "-D", "POSTROUTING", "-s", str(SUBNET), "-o", bind_interface, "-j", "MASQUERADE"],
        ["sudo", "iptables", "-D", "FORWARD", "-i", BRIDGE_NAME, "-o", bind_interface, "-j", "ACCEPT"],
        ["sudo", "iptables", "-D", "FORWARD", "-i", bind_interface, "-o", BRIDGE_NAME,
         "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ["sudo", "ip", "link", "del", BRIDGE_NAME],
    ]
    for cmd in steps:
        _run(cmd)


def create_client_namespace(client_id, bind_interface):
    """Create one client's namespace + veth pair + bridge attachment. Returns (ok, ip_or_error)."""
    ns = ns_name(client_id)
    host_veth, peer_veth = veth_names(client_id)
    ip_addr = client_ip(client_id)
    steps = [
        ["sudo", "ip", "netns", "add", ns],
        ["sudo", "ip", "link", "add", host_veth, "type", "veth", "peer", "name", peer_veth],
        ["sudo", "ip", "link", "set", peer_veth, "netns", ns],
        ["sudo", "ip", "netns", "exec", ns, "ip", "link", "set", peer_veth, "name", "eth0"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "addr", "add", f"{ip_addr}/{SUBNET.prefixlen}", "dev", "eth0"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "link", "set", "eth0", "up"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "route", "add", "default", "via", GATEWAY_IP],
        ["sudo", "ip", "link", "set", host_veth, "master", BRIDGE_NAME],
        ["sudo", "ip", "link", "set", host_veth, "up"],
    ]
    for cmd in steps:
        ok, _, err = _run(cmd)
        if not ok:
            delete_client_namespace(client_id)
            return False, err.strip() or " ".join(cmd)
    return True, ip_addr


def delete_client_namespace(client_id):
    """Deleting the namespace also destroys both ends of its veth pair."""
    _run(["sudo", "ip", "netns", "delete", ns_name(client_id)])


def _curl_in_namespace(ns, url):
    cmd = ["sudo", "ip", "netns", "exec", ns, "curl", "-s", "-o", "/dev/null",
           "-w", "%{size_download}", "--max-time", str(REQUEST_TIMEOUT), url]
    ok, out, err = _run(cmd, timeout=REQUEST_TIMEOUT + 2)
    if not ok:
        raise RuntimeError(err.strip() or "curl failed")
    return out.strip()


def _dig_in_namespace(ns, dns_server, name):
    cmd = ["sudo", "ip", "netns", "exec", ns, "dig", "+time=5", "+tries=1", "+short"]
    if dns_server:
        cmd.append(f"@{dns_server}")
    cmd.append(name)
    ok, out, _ = _run(cmd, timeout=REQUEST_TIMEOUT)
    return ok and bool(out.strip())


def _fetch_http(url):
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
        return str(len(resp.read()))


def _resolve_dns(name):
    try:
        socket.gethostbyname(name)
        return True
    except OSError:
        return False


def run_client_worker(client_id, ns, ip_addr, config, client_stop_event):
    """Repeatedly issue Tier-1 HTTP/DNS requests as one simulated client until stopped."""
    target_url = config["target_url"]
    dns_targets = config["dns_targets"]
    dns_server = config["dns_server"]
    mode = config["mode"]
    tag = f"[{client_id}:{ip_addr}] "

    while not client_stop_event.is_set() and not stop_event.is_set():
        action = random.choice(("http", "dns")) if (target_url and dns_targets) else (
            "http" if target_url else "dns" if dns_targets else None
        )
        try:
            if action == "http":
                size = _curl_in_namespace(ns, target_url) if mode == "netns" else _fetch_http(target_url)
                output_queue.put(f"{tag}GET {target_url} -> {size} bytes\n")
            elif action == "dns":
                name = random.choice(dns_targets)
                ok = _dig_in_namespace(ns, dns_server, name) if mode == "netns" else _resolve_dns(name)
                output_queue.put(f"{tag}dig {name} @{dns_server or 'default'} -> {'ok' if ok else 'no answer'}\n")
            else:
                time.sleep(1)
                continue
            with sim_lock:
                sim_stats["requests"] += 1
        except Exception as e:
            with sim_lock:
                sim_stats["errors"] += 1
            output_queue.put(f"{tag}error: {e}\n")

        think_time = random.uniform(*THINK_TIME_RANGE)
        slept = 0.0
        while slept < think_time and not client_stop_event.is_set() and not stop_event.is_set():
            step = min(0.5, think_time - slept)
            time.sleep(step)
            slept += step


def spawn_client(config):
    """Create one client (namespace + worker thread in netns mode, worker thread alone in
    simulated mode) and register it in sim_clients. Returns the client_id, or None on failure."""
    client_id = next(sim_id_counter)
    ns = ns_name(client_id)

    if config["mode"] == "netns":
        ok, result = create_client_namespace(client_id, config["bind_interface"])
        if not ok:
            output_queue.put(f"[client {client_id}] failed to create namespace: {result}\n")
            return None
        ip_addr = result
    else:
        ip_addr = "(simulated)"

    client_stop_event = threading.Event()
    thread = threading.Thread(
        target=run_client_worker,
        args=(client_id, ns, ip_addr, config, client_stop_event),
        daemon=True,
    )
    with sim_lock:
        sim_clients[client_id] = {"stop_event": client_stop_event, "thread": thread, "ip": ip_addr}
    thread.start()
    return client_id


def retire_client(client_id, mode):
    with sim_lock:
        entry = sim_clients.pop(client_id, None)
    if not entry:
        return
    entry["stop_event"].set()
    entry["thread"].join(timeout=5)
    if mode == "netns":
        delete_client_namespace(client_id)


def compute_churn_count(active_count, churn_rate_percent):
    """How many clients to cycle (retire + replace) this churn interval."""
    return round(active_count * churn_rate_percent / 100)


def run_churn_engine(config):
    """Every CHURN_INTERVAL_SECONDS, retire a random subset of active clients and spawn
    replacements -- 'N clients disappear, N clients reappear' -- keeping the total count
    steady while cycling identities (fresh namespace, fresh IP, fresh conntrack entries)."""
    while not stop_event.wait(CHURN_INTERVAL_SECONDS):
        with sim_lock:
            active_ids = list(sim_clients.keys())
        churn_count = compute_churn_count(len(active_ids), config["churn_rate_percent"])
        if churn_count <= 0:
            continue
        victims = random.sample(active_ids, min(churn_count, len(active_ids)))
        for client_id in victims:
            retire_client(client_id, config["mode"])
        if stop_event.is_set():
            break
        for _ in victims:
            spawn_client(config)
        with sim_lock:
            sim_stats["churn_events"] += len(victims)
        output_queue.put(f"[churn] cycled {len(victims)} client(s)\n")


def start_simulation(config):
    """Runs in its own background thread (spinning up many namespaces takes real wall-clock
    time) -- sets up the bridge/NAT if in netns mode, spawns all clients, starts the churn
    engine, then waits out the configured duration before stopping itself."""
    global sim_running

    mode, reason = detect_mode(config["bind_interface"])
    config["mode"] = mode

    with sim_lock:
        sim_running = True
        sim_context["mode"] = mode
        sim_context["bind_interface"] = config["bind_interface"]
        sim_stats.update({"requests": 0, "errors": 0, "churn_events": 0})

    if mode == "simulated":
        output_queue.put(
            f"Running in SIMULATED mode ({reason}) -- traffic is generated directly from "
            "this process, without real network namespaces.\n"
        )
    else:
        output_queue.put(
            f"Running in NETNS mode via {config['bind_interface']} -- setting up bridge "
            f"{BRIDGE_NAME} ({SUBNET})...\n"
        )
        ok, err = setup_bridge(config["bind_interface"])
        if not ok:
            output_queue.put(f"Failed to set up bridge/NAT: {err}\nFalling back to SIMULATED mode.\n")
            mode = config["mode"] = "simulated"
            with sim_lock:
                sim_context["mode"] = mode

    output_queue.put(f"Spawning {config['client_count']} client(s)...\n")
    for _ in range(config["client_count"]):
        if stop_event.is_set():
            break
        spawn_client(config)
    with sim_lock:
        active = len(sim_clients)
    output_queue.put(f"{active} client(s) active.\n")

    churn_thread = threading.Thread(target=run_churn_engine, args=(config,), daemon=True)
    churn_thread.start()

    end_time = time.time() + config["duration_minutes"] * 60
    while time.time() < end_time and not stop_event.is_set():
        time.sleep(1)

    if not stop_event.is_set():
        output_queue.put("Duration elapsed.\n")
        stop_simulation()


def stop_simulation():
    global sim_running
    with sim_lock:
        if not sim_running:
            return False
        sim_running = False
        client_ids = list(sim_clients.keys())
        mode = sim_context.get("mode")
        bind_interface = sim_context.get("bind_interface")

    stop_event.set()
    output_queue.put("Stopping simulation, tearing down clients...\n")

    for client_id in client_ids:
        retire_client(client_id, mode)

    if mode == "netns":
        teardown_bridge(bind_interface)

    output_queue.put("Simulation stopped.\n")
    output_queue.put(None)
    stop_event.clear()
    return True


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname", methods=["GET"])
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/status")
def status():
    with sim_lock:
        return jsonify({
            "running": sim_running,
            "mode": sim_context.get("mode"),
            "active_clients": len(sim_clients),
            **sim_stats,
        })


@app.route("/start", methods=["POST"])
def start():
    with sim_lock:
        if sim_running:
            return jsonify({"error": "A simulation is already running"}), 409

    data = request.json or {}

    try:
        client_count = int(data.get("client_count") or DEFAULT_CLIENT_COUNT)
    except (ValueError, TypeError):
        return jsonify({"error": "Client count must be an integer"}), 400
    try:
        raw_churn = data.get("churn_rate_percent")
        churn_rate_percent = int(raw_churn) if raw_churn is not None else DEFAULT_CHURN_RATE
    except (ValueError, TypeError):
        return jsonify({"error": "Churn rate must be an integer"}), 400
    try:
        duration_minutes = int(data.get("duration_minutes") or 60)
    except (ValueError, TypeError):
        return jsonify({"error": "Duration must be an integer"}), 400

    target_url = (data.get("target_url") or "").strip()
    dns_server = (data.get("dns_server") or "").strip()
    raw_dns_targets = data.get("dns_targets")
    if isinstance(raw_dns_targets, str):
        dns_targets = [d.strip() for d in raw_dns_targets.split(",") if d.strip()]
    elif isinstance(raw_dns_targets, list):
        dns_targets = [str(d).strip() for d in raw_dns_targets if str(d).strip()]
    else:
        dns_targets = list(DEFAULT_DNS_TARGETS)
    bind_interface = data.get("bind_interface") or "wlan0"

    if not (CLIENT_COUNT_RANGE[0] <= client_count <= CLIENT_COUNT_RANGE[1]):
        return jsonify({"error": f"Client count must be between {CLIENT_COUNT_RANGE[0]} and {CLIENT_COUNT_RANGE[1]}"}), 400
    if not (CHURN_RATE_RANGE[0] <= churn_rate_percent <= CHURN_RATE_RANGE[1]):
        return jsonify({"error": f"Churn rate must be between {CHURN_RATE_RANGE[0]} and {CHURN_RATE_RANGE[1]}"}), 400
    if duration_minutes < 1:
        return jsonify({"error": "Duration must be at least 1 minute"}), 400
    if bind_interface not in ("wlan0", "eth0"):
        return jsonify({"error": "Interface must be wlan0 or eth0"}), 400
    if not target_url and not dns_targets:
        return jsonify({"error": "Provide a target URL and/or DNS targets"}), 400
    if target_url and not re.match(r"^https?://", target_url):
        return jsonify({"error": "Target URL must start with http:// or https://"}), 400

    while not output_queue.empty():
        try:
            output_queue.get_nowait()
        except queue.Empty:
            break

    stop_event.clear()
    config = {
        "client_count": client_count,
        "churn_rate_percent": churn_rate_percent,
        "duration_minutes": duration_minutes,
        "target_url": target_url,
        "dns_server": dns_server,
        "dns_targets": dns_targets,
        "bind_interface": bind_interface,
    }
    thread = threading.Thread(target=start_simulation, args=(config,), daemon=True)
    thread.start()

    return jsonify({"status": "starting"})


@app.route("/stop", methods=["POST"])
def stop():
    stopped = stop_simulation()
    return jsonify({"status": "stopped" if stopped else "no simulation running"})


@app.route("/stream")
def stream():
    def generate():
        while True:
            try:
                line = output_queue.get(timeout=30)
                if line is None:
                    break
                yield "data: " + line.rstrip("\n").replace("\n", "\\n") + "\n\n"
            except queue.Empty:
                yield "data: \n\n"  # keepalive

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False)
