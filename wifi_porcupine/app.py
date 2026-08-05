#!/usr/bin/env python3
"""
Flask app that stresses a WiFi access point by rapidly and randomly
associating / disassociating several *physical* WiFi interfaces (the Pi's
built-in radio plus any USB adapters) against one target SSID, and randomizing
each interface's MAC on every reconnect so the AP sees a constant stream of
brand-new stations. That bloats the AP's association / DHCP-lease / ARP tables
far more than plain reconnect churn -- each interface is a "spine" repeatedly
poking the hub, hence "porcupine".

Every ticked interface churns at once -- concurrency is just however many you
select. A single intensity slider controls only speed: how long each
interface dwells associated before disconnecting and reconnecting with a
fresh MAC.

Association/MAC churn goes through NetworkManager (`nmcli`, one connection
profile per interface with `802-11-wireless.cloned-mac-address random`).
This requires Linux + privileged tooling, so the systemd unit runs this app
as root (see deploy/wifi-porcupine.service). Off-Linux -- e.g. macOS during
development -- every route degrades to a clear JSON error instead of
crashing. See CLAUDE.md's Environment Split section.
"""

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

# --- Naming ---
PROFILE_PREFIX = "porcupine"   # NetworkManager connection profiles: porcupine-<iface>

# --- Intensity model (templated into the UI so the slider bounds can't drift) ---
INTENSITY_RANGE = (1, 10)
DEFAULT_INTENSITY = 5
DWELL_AT_MIN_INTENSITY = (25.0, 45.0)  # (low, high) seconds an interface stays associated at intensity 1
DWELL_AT_MAX_INTENSITY = (2.0, 5.0)    # ... at intensity 10 (fast association storm)
GAP_RANGE = (0.5, 2.0)                 # short idle gap between disassociate and the next reconnect

NMCLI_TIMEOUT = 15
CONNECT_TIMEOUT = 30
MAX_OUTPUT_LINES = 2000

IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# --- Shared state (guarded by run_lock; the log has its own lock) ---
run_lock = threading.Lock()
run_state = {
    "running": False,
    "wifi_mode": None,     # "live" while a run is active
    "enlisted": [],        # interfaces enlisted for this run
    "connected": set(),    # interfaces currently associated
    "config": None,
}
stats = {"reconnects": 0, "errors": 0, "active_interfaces": 0}
workers = []                # per-interface churn threads
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
# Churn engine
# ---------------------------------------------------------------------------
def churn_worker(iface, config):
    """One interface's association/MAC churn loop: connect (new MAC) -> dwell -> disconnect -> gap."""
    dwell_low, dwell_high = compute_dwell_range(config["intensity"])
    while not stop_event.is_set():
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


def duration_timer(minutes):
    """Stop the run once its duration elapses (unless already stopped)."""
    if stop_event.wait(minutes * 60):
        return
    _log("Duration elapsed -- stopping run.")
    threading.Thread(target=stop_run, daemon=True).start()


def start_run(config):
    """Heavy setup for a run: profiles + churn threads. Runs in a thread.

    run_state['running'] has already been set True by the start route, so /status
    reports 'running' immediately even while this setup is still in progress.
    """
    global duration_thread

    _log(
        f"Starting porcupine run: {len(config['interfaces'])} interface(s) -> "
        f"SSID '{config['ssid']}', intensity {config['intensity']}"
    )

    for iface in config["interfaces"]:
        ok, msg = create_profile(iface, config["ssid"], config["password"])
        if not ok:
            _log(f"[{iface}] profile create failed: {friendly(msg)}")

    workers.clear()
    for iface in config["interfaces"]:
        t = threading.Thread(target=churn_worker, args=(iface, config), daemon=True)
        workers.append(t)
        t.start()

    duration_thread = threading.Thread(target=duration_timer, args=(config["duration_minutes"],), daemon=True)
    duration_thread.start()


def stop_run():
    """Stop a run: halt churn, disconnect + delete profiles. Idempotent."""
    with run_lock:
        if not run_state["running"]:
            return False
        run_state["running"] = False
        enlisted = list(run_state["enlisted"])
    stop_event.set()

    for t in workers:
        t.join(timeout=5)

    for iface in enlisted:
        bring_down(iface)
        delete_profile(iface)

    with run_lock:
        run_state["connected"] = set()
        run_state["wifi_mode"] = None
        stats["active_interfaces"] = 0
    _log("Run stopped; interfaces disconnected and profiles removed.")
    stop_event.clear()
    return True


def _sweep_orphans():
    """Best-effort cleanup of leftover profiles from a previous run killed mid-flight (root/Linux only)."""
    if platform.system() != "Linux":
        return
    ok, out, _ = _run(["sudo", "-n", "nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=5)
    if ok:
        for line in out.splitlines():
            name = line.strip()
            if name.startswith(PROFILE_PREFIX + "-"):
                _run(["sudo", "-n", "nmcli", "connection", "delete", name])


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
    )


@app.route("/api/hostname")
def api_hostname():
    return jsonify({"hostname": get_hostname()})


@app.route("/api/interfaces")
def api_interfaces():
    wifi_mode, wifi_reason = detect_wifi_mode()
    return jsonify({
        "interfaces": get_wireless_interfaces(),
        "wifi_supported": wifi_mode is not None,
        "wifi_reason": wifi_reason,
    })


@app.route("/api/status")
def api_status():
    with run_lock:
        payload = {
            "running": run_state["running"],
            "wifi_mode": run_state["wifi_mode"],
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
    global log_total
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

    config = {
        "interfaces": clean,
        "ssid": ssid,
        "password": password,
        "intensity": intensity,
        "duration_minutes": duration,
    }

    with run_lock:
        if run_state["running"]:
            return jsonify({"error": "A run is already in progress."}), 409
        run_state["running"] = True
        run_state["wifi_mode"] = "live"
        run_state["enlisted"] = clean
        run_state["connected"] = set()
        run_state["config"] = config
        stats.update({"reconnects": 0, "errors": 0, "active_interfaces": 0})
    # Reset the log ring buffer so a new run's Live Activity starts clean rather
    # than replaying the previous run's lines (the client polls api/output?since=0).
    with log_lock:
        log_lines.clear()
        log_total = 0
    stop_event.clear()

    threading.Thread(target=start_run, args=(config,), daemon=True).start()
    return jsonify({"status": "starting"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stopped = stop_run()
    return jsonify({"status": "stopped" if stopped else "no run in progress"})


# Sweep any leftover profiles from a run that was killed mid-flight, so restarts are idempotent.
_sweep_orphans()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5010))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    app.run(host="0.0.0.0", port=port, debug=True)
