#!/usr/bin/env python3
"""
Flask web app that builds a live timeline of WiFi association events by following
`iw event`, so roams between APs can be timed and disconnects explained.

Complements the other apps rather than overlapping them: wifi_utilization_monitor
scans *neighbouring* BSSes, wifi_connection_manager *controls* the connection --
this one just watches what the supplicant/kernel actually does over time (auth,
assoc, connect, deauth, disconnect, CQM threshold crossings) and reports how long
a transition between two BSSIDs took.
"""

import json
import os
import platform
import pty
import queue
import re
import select
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)

MAX_EVENTS = 500

# 802.11 reason codes (deauth/disassoc). Decoding these is most of the value in the
# timeline -- "reason 3" on its own tells you nothing.
REASON_CODES = {
    1: "Unspecified reason",
    2: "Previous authentication no longer valid",
    3: "Deauthenticated because sending STA is leaving",
    4: "Disassociated due to inactivity",
    5: "Disassociated because AP is unable to handle all associated STAs",
    6: "Class 2 frame received from nonauthenticated STA",
    7: "Class 3 frame received from nonassociated STA",
    8: "Disassociated because sending STA is leaving BSS",
    9: "STA requesting (re)association is not authenticated",
    10: "Disassociated because of unacceptable power capability",
    11: "Disassociated because of unacceptable supported channels",
    13: "Invalid information element",
    14: "Message integrity code (MIC) failure",
    15: "4-Way Handshake timeout",
    16: "Group Key Handshake timeout",
    17: "Information element differs from (re)association request",
    18: "Invalid group cipher",
    19: "Invalid pairwise cipher",
    20: "Invalid AKMP",
    21: "Unsupported RSN information element version",
    22: "Invalid RSN information element capabilities",
    23: "IEEE 802.1X authentication failed",
    24: "Cipher suite rejected because of the security policy",
    34: "Disassociated for unspecified, QoS-related reason",
}

# 802.11 status codes (auth/assoc responses).
STATUS_CODES = {
    0: "Successful",
    1: "Unspecified failure",
    10: "Cannot support all requested capabilities",
    11: "Reassociation denied; cannot confirm association exists",
    12: "Association denied for a reason outside the standard",
    13: "Responding STA does not support the specified auth algorithm",
    15: "Authentication rejected; challenge failure",
    16: "Authentication rejected; timeout waiting for next frame",
    17: "Association denied; AP unable to handle more STAs",
    18: "Association denied; basic rates not supported",
    37: "Requested action is not supported",
}

MAC_RE = r"[0-9a-fA-F:]{17}"
LINE_RE = re.compile(
    r"^(?:(?P<ts>\d+\.\d+):\s*)?(?:(?P<iface>[\w.-]+)\s*(?:\(phy\s*#\d+\))?|phy\s*#\d+):\s*(?P<body>.*)$"
)

# Runtime state. A single gunicorn worker runs this app (see the systemd unit), so
# module-level state is coherent across requests.
monitor_thread = None
stop_event = threading.Event()
state_lock = threading.Lock()
output_queue = queue.Queue()
events = deque(maxlen=MAX_EVENTS)

state = {
    "running": False,
    "interface": None,
    "connected": False,
    "current_bssid": None,
    "started_at": None,
    "error": None,
}

stats = {
    "roams": 0,
    "reconnects": 0,
    "disconnects": 0,
    "last_roam_ms": None,
    "avg_roam_ms": None,
}

_roam_durations = []
_transition_start = None  # monotonic-ish event ts marking the start of a transition
_boot_offset = None       # wall-clock time corresponding to event ts 0


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


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


def _boot_time_offset() -> float:
    """
    Wall-clock time corresponding to `iw event -t` timestamp 0 (system boot).

    `iw event -t` stamps each event with seconds since boot, straight from the kernel.
    Those are used for durations rather than the time Python read the line, because a
    burst of events can arrive in one read and would otherwise all look simultaneous --
    which is exactly the case that matters when timing a sub-second roam.
    """
    try:
        with open("/proc/uptime", "r") as f:
            return time.time() - float(f.read().split()[0])
    except Exception:
        return None


def parse_iw_event(line: str) -> dict:
    """Turn one raw `iw event -t` line into a structured event dict."""
    line = line.strip()
    if not line:
        return None

    event = {
        "raw": line,
        "ts": None,
        "interface": None,
        "type": "other",
        "bssid": None,
        "detail": "",
        "reason_code": None,
        "status_code": None,
    }

    match = LINE_RE.match(line)
    body = match.group("body") if match else line
    if match:
        if match.group("ts"):
            try:
                event["ts"] = float(match.group("ts"))
            except ValueError:
                pass
        event["interface"] = match.group("iface")

    lowered = body.lower()

    connected = re.match(rf"connected to ({MAC_RE})", body, re.I)
    auth = re.match(rf"auth(?:enticate)?\s+({MAC_RE})\s*->\s*({MAC_RE})", body, re.I)
    assoc = re.match(rf"assoc(?:iate)?\s+({MAC_RE})\s*->\s*({MAC_RE})", body, re.I)
    deauth = re.match(rf"deauth\s+({MAC_RE})\s*->\s*({MAC_RE})", body, re.I)
    disassoc = re.match(rf"disassoc\s+({MAC_RE})\s*->\s*({MAC_RE})", body, re.I)

    if connected:
        event["type"] = "connected"
        event["bssid"] = connected.group(1).lower()
        event["detail"] = f"connected to {event['bssid']}"
    elif lowered.startswith("disconnected"):
        event["type"] = "disconnected"
        event["detail"] = body
    elif auth or assoc or deauth or disassoc:
        match_obj = auth or assoc or deauth or disassoc
        event["type"] = (
            "auth" if auth else "assoc" if assoc else "deauth" if deauth else "disassoc"
        )
        # iw prints these frames in both directions, so which MAC is the AP depends on
        # whether the frame was sent or received. Keep both and let the caller decide by
        # asking whether the currently-associated BSSID is among them.
        event["macs"] = [match_obj.group(1).lower(), match_obj.group(2).lower()]
        event["bssid"] = match_obj.group(2).lower()
        event["detail"] = body
    elif lowered.startswith("scan started"):
        event["type"] = "scan"
        event["detail"] = "scan started"
    elif lowered.startswith("scan finished") or lowered.startswith("scan aborted"):
        event["type"] = "scan"
        event["detail"] = body.split(":", 1)[0].strip()
    elif "cqm" in lowered or "rssi went" in lowered:
        event["type"] = "cqm"
        event["detail"] = body
    else:
        event["detail"] = body

    reason = re.search(r"reason:?\s*(\d+)", body, re.I)
    if reason:
        code = int(reason.group(1))
        event["reason_code"] = code
        event["reason_text"] = REASON_CODES.get(code, "Unknown reason code")

    status = re.search(r"status:?\s*(\d+)", body, re.I)
    if status:
        code = int(status.group(1))
        event["status_code"] = code
        event["status_text"] = STATUS_CODES.get(code, "Unknown status code")

    return event


def _wall_clock(event_ts):
    """Format an event's boot-relative timestamp as wall-clock time for display."""
    if event_ts is not None and _boot_offset is not None:
        return datetime.fromtimestamp(_boot_offset + event_ts).strftime("%H:%M:%S.%f")[:-3]
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def process_event(event: dict) -> dict:
    """
    Fold one parsed event into the running state/stats, annotating it with roam timing.

    A transition is timed from the first sign of it -- a deauth/disassoc/disconnect, or
    an auth aimed at a different BSSID (802.11r fast transitions skip the disconnect
    entirely) -- through to the following "connected to" event.
    """
    global _transition_start

    event["time"] = _wall_clock(event.get("ts"))
    ts = event.get("ts")
    etype = event["type"]

    if etype in ("deauth", "disassoc", "disconnected"):
        if etype == "disconnected":
            stats["disconnects"] += 1
            state["connected"] = False
        if _transition_start is None and ts is not None:
            _transition_start = ts

    elif etype in ("auth", "assoc"):
        # 802.11r fast transitions skip the disconnect entirely, so an auth/assoc aimed
        # at anything other than the AP we're already on is the start of a transition.
        current = state.get("current_bssid")
        macs = event.get("macs") or []
        if _transition_start is None and ts is not None and current and current not in macs:
            _transition_start = ts

    elif etype == "connected":
        bssid = event.get("bssid")
        previous = state.get("current_bssid")
        duration_ms = None
        if _transition_start is not None and ts is not None:
            duration_ms = round((ts - _transition_start) * 1000, 1)

        if previous and bssid and previous != bssid:
            event["transition"] = "roam"
            event["from_bssid"] = previous
            stats["roams"] += 1
            if duration_ms is not None:
                event["duration_ms"] = duration_ms
                stats["last_roam_ms"] = duration_ms
                _roam_durations.append(duration_ms)
                stats["avg_roam_ms"] = round(sum(_roam_durations) / len(_roam_durations), 1)
        elif previous and bssid and previous == bssid:
            event["transition"] = "reconnect"
            stats["reconnects"] += 1
            if duration_ms is not None:
                event["duration_ms"] = duration_ms
        else:
            event["transition"] = "initial"

        state["current_bssid"] = bssid
        state["connected"] = True
        _transition_start = None

    return event


def _emit(event: dict) -> None:
    """Record an event and push it to any attached SSE listeners."""
    events.append(event)
    output_queue.put(event)


def monitor_loop(interface: str) -> None:
    """
    Follow `sudo iw event -t` for the lifetime of the monitoring session, keeping only
    the events belonging to `interface`.

    stdout is attached to a pty rather than a pipe: `iw` block-buffers when it detects
    a pipe, which would make events arrive in bursts long after they happened. Wrapping
    with `stdbuf` isn't an option here because sudo resets the environment it relies on,
    and the repo's sudoers rule only grants `iw` anyway.
    """
    global _boot_offset, monitor_thread

    _boot_offset = _boot_time_offset()
    master_fd, slave_fd = pty.openpty()
    proc = None

    try:
        proc = subprocess.Popen(
            ["sudo", "-n", "iw", "event", "-t"],
            stdout=slave_fd, stderr=slave_fd, close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = None

        buffer = ""
        startup_output = ""
        started = time.time()

        while not stop_event.is_set():
            if proc.poll() is not None and not buffer:
                # iw exited on its own -- almost always a missing sudoers rule.
                message = startup_output.strip() or "iw event exited unexpectedly"
                with state_lock:
                    state["error"] = message
                _emit({
                    "type": "error", "detail": message, "raw": message,
                    "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                })
                break

            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if not ready:
                continue

            try:
                chunk = os.read(master_fd, 4096).decode("utf-8", errors="replace")
            except OSError:
                break
            if not chunk:
                break

            if time.time() - started < 2:
                startup_output += chunk

            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                parsed = parse_iw_event(line.strip("\r"))
                if not parsed:
                    continue
                # `iw event` is a netlink listener with no per-interface filter -- it
                # reports every wireless interface on the system. Drop anything from a
                # different radio, otherwise a second interface's association events
                # interleave with this one's and corrupt the roam timing.
                if parsed.get("interface") and parsed["interface"] != interface:
                    continue
                with state_lock:
                    _emit(process_event(parsed))
    except Exception as e:
        with state_lock:
            state["error"] = str(e)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            if slave_fd is not None:
                os.close(slave_fd)
            os.close(master_fd)
        except Exception:
            pass

        with state_lock:
            state["running"] = False
            monitor_thread = None
        output_queue.put(None)
        stop_event.clear()


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname", methods=["GET"])
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/api/interfaces", methods=["GET"])
def api_interfaces():
    return jsonify({"interfaces": get_wireless_interfaces()})


@app.route("/api/status", methods=["GET"])
def api_status():
    with state_lock:
        return jsonify({**state, **stats, "event_count": len(events)})


@app.route("/api/events", methods=["GET"])
def api_events():
    """Recent event history, so a page reload doesn't start from a blank timeline."""
    with state_lock:
        return jsonify({"events": list(events), **state, **stats})


@app.route("/api/start", methods=["POST"])
def api_start():
    global monitor_thread

    with state_lock:
        if state["running"]:
            return jsonify({"error": "Monitoring is already running"}), 409

    data = request.get_json(silent=True) or {}
    interface = (data.get("interface") or "").strip()
    if not interface:
        detected = get_wireless_interfaces()
        if not detected:
            return jsonify({"error": "No wireless interface detected on this system"}), 400
        interface = detected[0]

    if not re.match(r"^[a-zA-Z0-9_.-]+$", interface):
        return jsonify({"error": "Invalid interface name"}), 400

    if platform.system() != "Linux":
        return jsonify({"error": "iw event requires Linux; not available on this host"}), 400
    if not shutil.which("iw"):
        return jsonify({"error": "iw is not installed. Run: sudo apt-get install iw"}), 400

    with state_lock:
        events.clear()
        stats.update({
            "roams": 0, "reconnects": 0, "disconnects": 0,
            "last_roam_ms": None, "avg_roam_ms": None,
        })
        _roam_durations.clear()
        state.update({
            "running": True, "interface": interface, "connected": False,
            "current_bssid": None, "error": None,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    while not output_queue.empty():
        try:
            output_queue.get_nowait()
        except queue.Empty:
            break

    stop_event.clear()
    monitor_thread = threading.Thread(target=monitor_loop, args=(interface,), daemon=True)
    monitor_thread.start()

    return jsonify({"status": "started", "interface": interface})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with state_lock:
        if not state["running"]:
            return jsonify({"status": "not running"})
    stop_event.set()
    return jsonify({"status": "stopping"})


@app.route("/stream")
def stream():
    def generate():
        while True:
            try:
                event = output_queue.get(timeout=30)
                if event is None:
                    break
                yield "data: " + json.dumps(event) + "\n\n"
            except queue.Empty:
                yield "data: {}\n\n"  # keepalive

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5007, debug=False)
