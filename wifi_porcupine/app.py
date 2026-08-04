#!/usr/bin/env python3
"""
Flask app that stresses a WiFi access point by rapidly and randomly
associating / disassociating several *physical* WiFi interfaces (the Pi's
built-in radio plus any USB adapters) against one target SSID, and randomizing
each interface's MAC on every reconnect so the AP sees a constant stream of
brand-new stations. That bloats the AP's association / DHCP-lease / ARP tables
far more than plain reconnect churn -- each interface is a "spine" repeatedly
poking the hub, hence "porcupine".

A single intensity slider scales both how fast interfaces cycle (dwell time)
and how many cycle at once (concurrency): a supervisor thread reshuffles which
enlisted interfaces are in the active churning subset each round.

Optionally, each enlisted interface can also carry a small fleet of `ip netns`
clients that NAT out through it to generate real L3 traffic. This is honestly
NAT'd behind the interface's single radio MAC -- it is *traffic* load, not
additional AP associations. Over WiFi you cannot bridge multiple MACs onto one
station association, so those namespace clients are invisible to the AP at L2;
only the physical-interface association/MAC churn stresses the association side.

Association/MAC churn goes through NetworkManager (`nmcli`, one connection
profile per interface with `802-11-wireless.cloned-mac-address random`); the
netns fleets reuse the client_simulator recipe (bridge + veth + MASQUERADE),
one bridge/subnet per interface. Both require Linux + privileged tooling, so
the systemd unit runs this app as root (see deploy/wifi-porcupine.service).
Off-Linux -- e.g. macOS during development -- every route degrades to a clear
JSON error instead of crashing. See CLAUDE.md's Environment Split section.
"""

import ipaddress
import math
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# --- Naming (kept short: veth/bridge names must stay under the 15-char IFNAMSIZ limit) ---
PROFILE_PREFIX = "porcupine"   # NetworkManager connection profiles: porcupine-<iface>
BRIDGE_PREFIX = "porcbr"       # per-interface netns bridge: porcbr<idx>
NS_PREFIX = "wfporc"           # per-client namespace: wfporc-<idx>-<cid>

# --- Intensity model (templated into the UI so the slider bounds can't drift) ---
INTENSITY_RANGE = (1, 10)
DEFAULT_INTENSITY = 5
DWELL_AT_MIN_INTENSITY = (25.0, 45.0)  # (low, high) seconds an interface stays associated at intensity 1
DWELL_AT_MAX_INTENSITY = (2.0, 5.0)    # ... at intensity 10 (fast association storm)
GAP_RANGE = (0.5, 2.0)                 # short idle gap between disassociate and the next reconnect
RESHUFFLE_INTERVAL_SECONDS = 20        # how often the active churning subset is re-picked

# --- netns traffic multiplier ---
NETNS_PER_IFACE_RANGE = (1, 50)
DEFAULT_NETNS_PER_IFACE = 5
THINK_TIME_RANGE = (2, 8)
REQUEST_TIMEOUT = 10

NMCLI_TIMEOUT = 15
CONNECT_TIMEOUT = 30
MAX_OUTPUT_LINES = 2000

IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# --- Shared state (guarded by run_lock; the log has its own lock) ---
run_lock = threading.Lock()
run_state = {
    "running": False,
    "wifi_mode": None,     # "live" while a run is active
    "netns_mode": None,    # "netns" | "disabled"
    "enlisted": [],        # interfaces enlisted for this run
    "connected": set(),    # interfaces currently associated
    "config": None,
}
stats = {"reconnects": 0, "errors": 0, "requests": 0, "netns_clients": 0, "active_interfaces": 0}
active_ifaces = set()      # the churning subset chosen by the supervisor
fleets = {}                # iface -> {"idx","bridge","subnet","iface","clients":[...]}
workers = []               # per-interface churn threads
supervisor_thread = None
duration_thread = None
stop_event = threading.Event()

log_lock = threading.Lock()
log_lines = deque(maxlen=MAX_OUTPUT_LINES)
log_total = 0


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def _run(cmd, timeout=NMCLI_TIMEOUT):
    """Run a command (already including sudo if needed) and return (ok, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return False, "", str(e)


def _nmcli(args, timeout=NMCLI_TIMEOUT):
    """Run `sudo nmcli <args>`; returns (ok, stdout, stderr). Never raises."""
    if not shutil.which("nmcli"):
        return False, "", "nmcli is not installed on this host (install network-manager)."
    return _run(["sudo", "nmcli"] + args, timeout=timeout)


# ---------------------------------------------------------------------------
# Logging: a bounded ring buffer with a monotonic cursor, so a reloaded page or
# a second tab can replay from wherever it left off instead of losing lines the
# way a destructively-consumed queue would.
# ---------------------------------------------------------------------------
def _log(msg):
    global log_total
    stamp = time.strftime("%H:%M:%S")
    with log_lock:
        log_lines.append(f"[{stamp}] {msg}")
        log_total += 1


def read_output(since: int) -> dict:
    """Return buffered log lines from cursor `since` onward, reporting any drops."""
    with log_lock:
        total = log_total
        buffered = len(log_lines)
        first = total - buffered  # cursor value of log_lines[0]
        since = max(0, min(since, total))
        start = max(since, first)
        chunk = list(log_lines)[start - first:]
    return {"lines": chunk, "next": total, "dropped": max(0, first - since)}


def _interruptible_sleep(duration, extra=None):
    """Sleep up to `duration`s, waking immediately if stop_event (or `extra`) is set."""
    end = time.time() + duration
    while True:
        remaining = end - time.time()
        if remaining <= 0 or stop_event.is_set() or (extra is not None and extra.is_set()):
            return
        time.sleep(min(0.5, remaining))


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------
def get_wireless_interfaces() -> list:
    """List wireless interfaces via `iw dev`, falling back to /proc/net/wireless."""
    interfaces = []
    try:
        output = subprocess.check_output(
            ["iw", "dev"], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="replace")
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Interface"):
                parts = line.split()
                if len(parts) >= 2:
                    interfaces.append(parts[1])
    except Exception:
        pass

    if not interfaces and os.path.exists("/proc/net/wireless"):
        try:
            with open("/proc/net/wireless", "r") as f:
                for line in f.readlines()[2:]:
                    name = line.split(":")[0].strip()
                    if name:
                        interfaces.append(name)
        except Exception:
            pass

    return interfaces


def detect_wifi_mode():
    """Whether the physical-interface association/MAC churn can run. Returns (mode, reason)."""
    if platform.system() != "Linux":
        return None, "not running on Linux"
    if not shutil.which("nmcli"):
        return None, "nmcli is not installed (install network-manager)"
    if not shutil.which("iw"):
        return None, "the 'iw' command is not installed"
    return "live", None


def detect_netns_mode():
    """Whether the optional netns traffic multiplier can run. Returns (mode, reason)."""
    if platform.system() != "Linux":
        return None, "not running on Linux"
    if not shutil.which("ip"):
        return None, "the 'ip' command is not installed"
    ok, _, _ = _run(["sudo", "-n", "ip", "netns", "list"], timeout=5)
    if not ok:
        return None, "passwordless sudo for 'ip' is not available (run as root; see README)"
    return "netns", None


def friendly(raw) -> str:
    """Condense a raw nmcli error to one short human-readable line."""
    raw = (raw or "").strip()
    if not raw:
        return "unknown error"
    low = raw.lower()
    if "secrets were required" in low or "no secrets" in low or "802-1x" in low:
        return "authentication failed (check the password)"
    if "not found" in low or "no network" in low:
        return "target network not found in range"
    if "timeout" in low or "timed out" in low:
        return "association timed out"
    return raw.splitlines()[0][:200]


# ---------------------------------------------------------------------------
# Intensity math (pure helpers, unit-tested)
# ---------------------------------------------------------------------------
def _intensity_fraction(intensity) -> float:
    lo, hi = INTENSITY_RANGE
    return (intensity - lo) / (hi - lo) if hi > lo else 0.0


def compute_dwell_range(intensity):
    """(low, high) association dwell seconds for a given intensity; shrinks as intensity rises."""
    t = _intensity_fraction(intensity)
    low = DWELL_AT_MIN_INTENSITY[0] + (DWELL_AT_MAX_INTENSITY[0] - DWELL_AT_MIN_INTENSITY[0]) * t
    high = DWELL_AT_MIN_INTENSITY[1] + (DWELL_AT_MAX_INTENSITY[1] - DWELL_AT_MIN_INTENSITY[1]) * t
    return (low, high)


def compute_concurrency(n_interfaces, intensity) -> int:
    """How many enlisted interfaces churn simultaneously at a given intensity (>=1 when any)."""
    if n_interfaces <= 0:
        return 0
    _, hi = INTENSITY_RANGE
    return max(1, math.ceil(n_interfaces * intensity / hi))


# ---------------------------------------------------------------------------
# nmcli profiles (one per interface, each with a random cloned MAC)
# ---------------------------------------------------------------------------
def profile_name(iface) -> str:
    return f"{PROFILE_PREFIX}-{iface}"


def build_profile_add_args(iface, ssid, password):
    """Args for `nmcli connection add` creating this interface's porcupine profile.

    `802-11-wireless.cloned-mac-address random` makes NetworkManager pick a fresh
    random MAC on every activation, so each reconnect looks like a new station.
    """
    args = [
        "connection", "add", "type", "wifi",
        "con-name", profile_name(iface),
        "ifname", iface,
        "ssid", ssid,
        "802-11-wireless.cloned-mac-address", "random",
        "connection.autoconnect", "no",
    ]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    return args


def create_profile(iface, ssid, password):
    """Recreate this interface's connection profile from scratch. Returns (ok, message)."""
    _nmcli(["connection", "delete", profile_name(iface)])  # clear any stale profile; ignore errors
    ok, out, err = _nmcli(build_profile_add_args(iface, ssid, password))
    return ok, (err or out)


def delete_profile(iface):
    _nmcli(["connection", "delete", profile_name(iface)])


def bring_up(iface):
    return _nmcli(["connection", "up", profile_name(iface)], timeout=CONNECT_TIMEOUT)


def bring_down(iface):
    return _nmcli(["connection", "down", profile_name(iface)], timeout=CONNECT_TIMEOUT)


def read_iface_mac(iface):
    """Current (possibly cloned) MAC of an interface, read from sysfs. None if unavailable."""
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Optional per-interface netns fleet (bridge + veth + MASQUERADE out the iface).
# Set up once per run and kept up while the physical interface flaps; traffic
# just errors during the disconnected windows, which is realistic.
# ---------------------------------------------------------------------------
def bridge_name(idx) -> str:
    return f"{BRIDGE_PREFIX}{idx}"


def fleet_subnet(idx) -> "ipaddress.IPv4Network":
    return ipaddress.ip_network(f"10.210.{idx}.0/24")


def ns_name(idx, cid) -> str:
    return f"{NS_PREFIX}-{idx}-{cid}"


def veth_names(idx, cid):
    """Host-side and namespace-side veth names, kept under the 15-char IFNAMSIZ limit."""
    return f"pc{idx}h{cid}", f"pc{idx}p{cid}"


def create_client_ns(idx, cid, gateway_ip, subnet):
    """Create one namespace + veth pair attached to the interface's bridge. (ok, ip_or_err)."""
    ns = ns_name(idx, cid)
    host_veth, peer_veth = veth_names(idx, cid)
    ip_addr = str(subnet.network_address + 2 + cid)
    br = bridge_name(idx)
    steps = [
        ["sudo", "ip", "netns", "add", ns],
        ["sudo", "ip", "link", "add", host_veth, "type", "veth", "peer", "name", peer_veth],
        ["sudo", "ip", "link", "set", peer_veth, "netns", ns],
        ["sudo", "ip", "netns", "exec", ns, "ip", "link", "set", peer_veth, "name", "eth0"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "addr", "add", f"{ip_addr}/{subnet.prefixlen}", "dev", "eth0"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "link", "set", "eth0", "up"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"],
        ["sudo", "ip", "netns", "exec", ns, "ip", "route", "add", "default", "via", gateway_ip],
        ["sudo", "ip", "link", "set", host_veth, "master", br],
        ["sudo", "ip", "link", "set", host_veth, "up"],
    ]
    for cmd in steps:
        ok, _, err = _run(cmd)
        if not ok:
            _run(["sudo", "ip", "netns", "delete", ns])  # roll back partial creation
            return False, err.strip() or " ".join(cmd)
    return True, ip_addr


def setup_fleet(iface, idx, count, target_url):
    """Bring up the bridge + NAT + `count` namespace clients for one interface. Returns fleet|None."""
    subnet = fleet_subnet(idx)
    gateway = str(subnet.network_address + 1)
    br = bridge_name(idx)
    steps = [
        ["sudo", "ip", "link", "add", br, "type", "bridge"],
        ["sudo", "ip", "addr", "add", f"{gateway}/{subnet.prefixlen}", "dev", br],
        ["sudo", "ip", "link", "set", br, "up"],
        ["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"],
        ["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", str(subnet), "-o", iface, "-j", "MASQUERADE"],
        ["sudo", "iptables", "-A", "FORWARD", "-i", br, "-o", iface, "-j", "ACCEPT"],
        ["sudo", "iptables", "-A", "FORWARD", "-i", iface, "-o", br,
         "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]
    for cmd in steps:
        ok, _, err = _run(cmd)
        if not ok:
            _log(f"[{iface}] netns bridge setup failed: {err.strip() or ' '.join(cmd)}")
            teardown_fleet({"idx": idx, "bridge": br, "subnet": subnet, "iface": iface, "clients": []})
            return None

    fleet = {"idx": idx, "bridge": br, "subnet": subnet, "iface": iface, "clients": []}
    for cid in range(count):
        ok, ip_or_err = create_client_ns(idx, cid, gateway, subnet)
        if not ok:
            _log(f"[{iface}] netns client {cid} failed: {ip_or_err}")
            continue
        cstop = threading.Event()
        t = threading.Thread(
            target=client_traffic_worker,
            args=(iface, ns_name(idx, cid), ip_or_err, target_url, cstop),
            daemon=True,
        )
        fleet["clients"].append({"ns": ns_name(idx, cid), "ip": ip_or_err, "stop": cstop, "thread": t})
        t.start()
    return fleet


def teardown_fleet(fleet):
    """Best-effort reversal of setup_fleet(); individual failures are ignored."""
    for c in fleet["clients"]:
        c["stop"].set()
    for c in fleet["clients"]:
        c["thread"].join(timeout=3)
        _run(["sudo", "ip", "netns", "delete", c["ns"]])
    subnet, br, iface = fleet["subnet"], fleet["bridge"], fleet["iface"]
    for cmd in [
        ["sudo", "iptables", "-t", "nat", "-D", "POSTROUTING", "-s", str(subnet), "-o", iface, "-j", "MASQUERADE"],
        ["sudo", "iptables", "-D", "FORWARD", "-i", br, "-o", iface, "-j", "ACCEPT"],
        ["sudo", "iptables", "-D", "FORWARD", "-i", iface, "-o", br,
         "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ["sudo", "ip", "link", "del", br],
    ]:
        _run(cmd)


def client_traffic_worker(iface, ns, ip, target_url, cstop):
    """Continuously fetch target_url from inside one namespace, with random think time."""
    while not cstop.is_set() and not stop_event.is_set():
        cmd = ["sudo", "ip", "netns", "exec", ns, "curl", "-s", "-o", "/dev/null",
               "--max-time", str(REQUEST_TIMEOUT), target_url]
        ok, _, _ = _run(cmd, timeout=REQUEST_TIMEOUT + 2)
        with run_lock:
            stats["requests"] += 1
            if not ok:
                stats["errors"] += 1
        _interruptible_sleep(random.uniform(*THINK_TIME_RANGE), cstop)


# ---------------------------------------------------------------------------
# Churn engine
# ---------------------------------------------------------------------------
def churn_worker(iface, config):
    """One interface's association/MAC churn loop: connect (new MAC) -> dwell -> disconnect -> gap."""
    dwell_low, dwell_high = compute_dwell_range(config["intensity"])
    while not stop_event.is_set():
        with run_lock:
            is_active = iface in active_ifaces
        if not is_active:
            _interruptible_sleep(1.0)
            continue

        ok, _, err = bring_up(iface)
        if stop_event.is_set():
            break
        if not ok:
            with run_lock:
                stats["errors"] += 1
            _log(f"[{iface}] connect failed: {friendly(err)}")
            _interruptible_sleep(random.uniform(*GAP_RANGE))
            continue

        mac = read_iface_mac(iface)
        with run_lock:
            stats["reconnects"] += 1
            run_state["connected"].add(iface)
            stats["active_interfaces"] = len(run_state["connected"])
        _log(f"[{iface}] associated as {mac or 'unknown MAC'} -> {config['ssid']}")

        _interruptible_sleep(random.uniform(dwell_low, dwell_high))

        bring_down(iface)
        with run_lock:
            run_state["connected"].discard(iface)
            stats["active_interfaces"] = len(run_state["connected"])
        if not stop_event.is_set():
            _log(f"[{iface}] disassociated")
        _interruptible_sleep(random.uniform(*GAP_RANGE))

    bring_down(iface)  # leave the radio idle on the way out
    with run_lock:
        run_state["connected"].discard(iface)
        stats["active_interfaces"] = len(run_state["connected"])


def supervisor(config):
    """Periodically re-pick which enlisted interfaces are in the active churning subset."""
    enlisted = config["interfaces"]
    while not stop_event.is_set():
        conc = compute_concurrency(len(enlisted), config["intensity"])
        chosen = set(random.sample(enlisted, min(conc, len(enlisted))))
        with run_lock:
            active_ifaces.clear()
            active_ifaces.update(chosen)
        _log(f"churn subset -> {', '.join(sorted(chosen))} ({conc}/{len(enlisted)} interface(s))")
        if stop_event.wait(RESHUFFLE_INTERVAL_SECONDS):
            break


def duration_timer(minutes):
    """Stop the run once its duration elapses (unless already stopped)."""
    if stop_event.wait(minutes * 60):
        return
    _log("Duration elapsed -- stopping run.")
    threading.Thread(target=stop_run, daemon=True).start()


def start_run(config):
    """Heavy setup for a run: profiles, optional netns fleets, churn threads. Runs in a thread.

    run_state['running'] has already been set True by the start route, so /status
    reports 'running' immediately even while this setup is still in progress.
    """
    global supervisor_thread, duration_thread

    _log(
        f"Starting porcupine run: {len(config['interfaces'])} interface(s) -> "
        f"SSID '{config['ssid']}', intensity {config['intensity']}"
        + (f", {config['netns_per_iface']} netns client(s)/iface -> {config['target_url']}"
           if config["netns_enabled"] else "")
    )

    for iface in config["interfaces"]:
        ok, msg = create_profile(iface, config["ssid"], config["password"])
        if not ok:
            _log(f"[{iface}] profile create failed: {friendly(msg)}")

    if config["netns_enabled"]:
        for idx, iface in enumerate(config["interfaces"]):
            fleet = setup_fleet(iface, idx, config["netns_per_iface"], config["target_url"])
            if fleet:
                with run_lock:
                    fleets[iface] = fleet
                    stats["netns_clients"] += len(fleet["clients"])
                _log(f"[{iface}] netns fleet up: {len(fleet['clients'])} client(s) on {fleet['subnet']}")

    workers.clear()
    for iface in config["interfaces"]:
        t = threading.Thread(target=churn_worker, args=(iface, config), daemon=True)
        workers.append(t)
        t.start()

    supervisor_thread = threading.Thread(target=supervisor, args=(config,), daemon=True)
    supervisor_thread.start()
    duration_thread = threading.Thread(target=duration_timer, args=(config["duration_minutes"],), daemon=True)
    duration_thread.start()


def stop_run():
    """Stop a run: halt churn, disconnect + delete profiles, tear down netns fleets. Idempotent."""
    with run_lock:
        if not run_state["running"]:
            return False
        run_state["running"] = False
        enlisted = list(run_state["enlisted"])
        fleet_list = list(fleets.values())
    stop_event.set()

    for t in workers:
        t.join(timeout=5)
    if supervisor_thread:
        supervisor_thread.join(timeout=3)

    for iface in enlisted:
        bring_down(iface)
        delete_profile(iface)

    for fleet in fleet_list:
        teardown_fleet(fleet)

    with run_lock:
        run_state["connected"] = set()
        run_state["wifi_mode"] = None
        run_state["netns_mode"] = None
        active_ifaces.clear()
        fleets.clear()
        stats["active_interfaces"] = 0
    _log("Run stopped; interfaces disconnected and profiles removed.")
    stop_event.clear()
    return True


def _sweep_orphans():
    """Best-effort cleanup of leftovers from a previous run killed mid-flight (root/Linux only)."""
    if platform.system() != "Linux":
        return
    ok, out, _ = _run(["sudo", "-n", "nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=5)
    if ok:
        for line in out.splitlines():
            name = line.strip()
            if name.startswith(PROFILE_PREFIX + "-"):
                _run(["sudo", "-n", "nmcli", "connection", "delete", name])
    ok, out, _ = _run(["sudo", "-n", "ip", "netns", "list"], timeout=5)
    if ok:
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].startswith(NS_PREFIX + "-"):
                _run(["sudo", "-n", "ip", "netns", "delete", parts[0]])
    ok, out, _ = _run(["sudo", "-n", "ip", "-o", "link", "show", "type", "bridge"], timeout=5)
    if ok:
        for line in out.splitlines():
            m = re.match(r"\d+:\s+([^:@]+)", line)
            if m and m.group(1).strip().startswith(BRIDGE_PREFIX):
                _run(["sudo", "-n", "ip", "link", "del", m.group(1).strip()])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        hostname=get_hostname(),
        intensity_min=INTENSITY_RANGE[0],
        intensity_max=INTENSITY_RANGE[1],
        intensity_default=DEFAULT_INTENSITY,
        netns_min=NETNS_PER_IFACE_RANGE[0],
        netns_max=NETNS_PER_IFACE_RANGE[1],
        netns_default=DEFAULT_NETNS_PER_IFACE,
    )


@app.route("/api/hostname")
def api_hostname():
    return jsonify({"hostname": get_hostname()})


@app.route("/api/interfaces")
def api_interfaces():
    wifi_mode, wifi_reason = detect_wifi_mode()
    netns_mode, netns_reason = detect_netns_mode()
    return jsonify({
        "interfaces": get_wireless_interfaces(),
        "wifi_supported": wifi_mode is not None,
        "wifi_reason": wifi_reason,
        "netns_supported": netns_mode is not None,
        "netns_reason": netns_reason,
    })


@app.route("/api/status")
def api_status():
    with run_lock:
        payload = {
            "running": run_state["running"],
            "wifi_mode": run_state["wifi_mode"],
            "netns_mode": run_state["netns_mode"],
            "enlisted": list(run_state["enlisted"]),
        }
        payload.update(stats)
    return jsonify(payload)


@app.route("/api/output")
def api_output():
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    return jsonify(read_output(since))


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}

    with run_lock:
        if run_state["running"]:
            return jsonify({"error": "A run is already in progress."}), 409

    wifi_mode, wifi_reason = detect_wifi_mode()
    if wifi_mode is None:
        return jsonify({"error": f"Cannot start: {wifi_reason}."}), 400

    interfaces = data.get("interfaces") or []
    if not isinstance(interfaces, list) or not interfaces:
        return jsonify({"error": "Select at least one WiFi interface."}), 400

    detected = set(get_wireless_interfaces())
    clean = []
    for iface in interfaces:
        iface = str(iface).strip()
        if not IFACE_RE.match(iface):
            return jsonify({"error": f"Invalid interface name: {iface!r}."}), 400
        if detected and iface not in detected:
            return jsonify({"error": f"{iface} is not a detected WiFi interface."}), 400
        clean.append(iface)

    ssid = (data.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"error": "Target SSID is required."}), 400
    password = data.get("password") or ""

    try:
        intensity = int(data.get("intensity", DEFAULT_INTENSITY))
    except (TypeError, ValueError):
        return jsonify({"error": "Intensity must be a number."}), 400
    if not (INTENSITY_RANGE[0] <= intensity <= INTENSITY_RANGE[1]):
        return jsonify({"error": f"Intensity must be between {INTENSITY_RANGE[0]} and {INTENSITY_RANGE[1]}."}), 400

    try:
        duration = int(data.get("duration_minutes", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "Duration must be a number."}), 400
    if duration < 1:
        return jsonify({"error": "Duration must be at least 1 minute."}), 400

    netns_enabled = bool(data.get("netns_enabled"))
    netns_per = DEFAULT_NETNS_PER_IFACE
    target_url = (data.get("target_url") or "").strip()
    if netns_enabled:
        netns_mode, netns_reason = detect_netns_mode()
        if netns_mode is None:
            return jsonify({"error": f"netns clients unavailable: {netns_reason}."}), 400
        try:
            netns_per = int(data.get("netns_per_iface", DEFAULT_NETNS_PER_IFACE))
        except (TypeError, ValueError):
            return jsonify({"error": "netns clients per interface must be a number."}), 400
        if not (NETNS_PER_IFACE_RANGE[0] <= netns_per <= NETNS_PER_IFACE_RANGE[1]):
            return jsonify({"error": f"netns clients per interface must be between "
                                     f"{NETNS_PER_IFACE_RANGE[0]} and {NETNS_PER_IFACE_RANGE[1]}."}), 400
        if not target_url:
            return jsonify({"error": "A target URL is required when netns clients are enabled."}), 400

    config = {
        "interfaces": clean,
        "ssid": ssid,
        "password": password,
        "intensity": intensity,
        "duration_minutes": duration,
        "netns_enabled": netns_enabled,
        "netns_per_iface": netns_per,
        "target_url": target_url,
    }

    with run_lock:
        if run_state["running"]:
            return jsonify({"error": "A run is already in progress."}), 409
        run_state["running"] = True
        run_state["wifi_mode"] = "live"
        run_state["netns_mode"] = "netns" if netns_enabled else "disabled"
        run_state["enlisted"] = clean
        run_state["connected"] = set()
        run_state["config"] = config
        stats.update({"reconnects": 0, "errors": 0, "requests": 0,
                      "netns_clients": 0, "active_interfaces": 0})
        active_ifaces.clear()
        fleets.clear()
    stop_event.clear()

    threading.Thread(target=start_run, args=(config,), daemon=True).start()
    return jsonify({"status": "starting"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stopped = stop_run()
    return jsonify({"status": "stopped" if stopped else "no run in progress"})


# Sweep any leftovers from a run that was killed mid-flight, so restarts are idempotent.
_sweep_orphans()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5010))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    app.run(host="0.0.0.0", port=port, debug=True)
