#!/usr/bin/env python3
"""
Flask web app equivalent of simple_iperf3_start.sh.
Provides a browser UI to configure and launch an iperf3 client test.
"""

import itertools
import subprocess
import threading
import shutil
import re
import socket
import time
import ipaddress
from collections import deque
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Several tests can run at once -- generating congestion against more than one
# server (or more than one port on the same server) is the point of this app, so
# state is a registry keyed by test id rather than a single global process.
MAX_CONCURRENT_TESTS = 8
MAX_OUTPUT_LINES = 2000

tests = {}  # test_id -> dict (see _new_test)
test_lock = threading.Lock()
_test_id_counter = itertools.count(1)


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def is_valid_ip(ip: str) -> bool:
    pattern = r"^\d{1,3}(\.\d{1,3}){3}$"
    return bool(re.match(pattern, ip))


def get_bindable_interfaces() -> list:
    """Real network interfaces with a live IPv4 address, i.e. usable as a scan/bind
    target -- excludes loopback and anything with no address (down, unconfigured,
    monitor-mode, etc.), since those can't be scanned or bound to anyway. Read-only
    (`ip addr show`), so no privilege is needed. Returns [] off-Linux or without
    iproute2 installed; callers treat that as "can't verify" rather than "none exist".
    """
    if not shutil.which("ip"):
        return []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []

    interfaces = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] not in interfaces:
            interfaces.append(parts[1])
    return interfaces


def scan_for_servers(bind_interface: str = "wlan0", ports: str = "5201-5210") -> list[dict]:
    """Scan the LAN for iperf3 servers on port range 5201-5210 specifically on bind_interface, excluding local host IPs."""
    if not shutil.which("nmap"):
        raise RuntimeError("nmap is not installed. Run: sudo apt-get install nmap")

    detected = get_bindable_interfaces()
    if detected and bind_interface not in detected:
        bind_interface = detected[0]

    local_ips = set()
    local_ips.add("127.0.0.1")

    # Collect local host IPs across all interfaces
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                parts = line.split()
                if len(parts) >= 2:
                    cidr_str = parts[1]
                    try:
                        iface_obj = ipaddress.ip_interface(cidr_str)
                        ip_str = str(iface_obj.ip)
                        if not ip_str.startswith("127."):
                            local_ips.add(ip_str)
                    except Exception:
                        pass
    except Exception:
        pass

    # Detect CIDR subnet specifically for bind_interface
    cidr = None
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", bind_interface],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                parts = line.split()
                if len(parts) >= 2:
                    cidr_str = parts[1]
                    try:
                        iface_obj = ipaddress.ip_interface(cidr_str)
                        ip_str = str(iface_obj.ip)
                        if not ip_str.startswith("127."):
                            local_ips.add(ip_str)
                            cidr = str(iface_obj.network)
                            break
                    except Exception:
                        pass
    except Exception:
        pass

    if not cidr:
        raise RuntimeError(f"No active IPv4 address found on interface {bind_interface}")

    cmd = ["nmap", "-e", bind_interface, "-Pn", "-p", ports, "--open", "-n", "-T4", "-oG", "-", cidr]
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=60
    )

    found = []
    for line in result.stdout.splitlines():
        if line.startswith("Host:") and "/open" in line:
            parts = line.split()
            if len(parts) >= 2:
                ip = parts[1]
                if ip not in local_ips:
                    open_ports = re.findall(r'(\d+)/open', line)
                    for port_str in open_ports:
                        found.append({"ip": ip, "port": int(port_str)})

    def ip_key(item):
        ip_str = item["ip"]
        try:
            return (0, socket.inet_aton(ip_str), item["port"])
        except Exception:
            return (1, ip_str, item["port"])

    found.sort(key=ip_key)
    return found


def _new_test(config) -> dict:
    """
    Create and register a test record. Caller must hold test_lock.

    `lines` is a bounded ring buffer rather than a queue: a queue is consumed
    destructively by whoever reads it, which is why a reloaded page used to lose
    everything and two open tabs stole each other's output. A buffer lets any
    number of readers replay from wherever they left off.

    `total_lines` counts every line ever appended, including ones the deque has
    since dropped, so it acts as a stable cursor -- indexes into `lines` shift as
    old entries fall off the front, but `total_lines` never goes backwards.
    """
    test_id = f"t{next(_test_id_counter)}"
    tests[test_id] = {
        "id": test_id,
        "server_ip": config["server_ip"],
        "server_port": config["server_port"],
        "bind_interface": config["bind_interface"],
        "duration_minutes": config["duration_minutes"],
        "bandwidth_mbps": config["bandwidth_mbps"],
        "status": "running",
        "started_at": time.time(),
        "ended_at": None,
        "process": None,
        "lines": deque(maxlen=MAX_OUTPUT_LINES),
        "total_lines": 0,
        "stop_requested": False,
    }
    return tests[test_id]


def _emit(test, line):
    """Append one output line to a test's buffer, under the lock."""
    with test_lock:
        test["lines"].append(line.rstrip("\n"))
        test["total_lines"] += 1


def test_summary(test) -> dict:
    """Public view of a test, for the tab strip. Caller must hold test_lock."""
    return {
        "id": test["id"],
        "server_ip": test["server_ip"],
        "server_port": test["server_port"],
        "bind_interface": test["bind_interface"],
        "duration_minutes": test["duration_minutes"],
        "bandwidth_mbps": test["bandwidth_mbps"],
        "status": test["status"],
        "started_at": test["started_at"],
        "ended_at": test["ended_at"],
        "total_lines": test["total_lines"],
    }


def read_output(test, since: int) -> dict:
    """
    Return buffered lines from cursor `since` onwards. Caller must hold test_lock.

    If the ring buffer has already dropped past what the caller asked for, report
    how many lines were lost rather than silently renumbering -- the UI says so
    instead of appearing to skip.
    """
    total = test["total_lines"]
    buffered = len(test["lines"])
    first_buffered = total - buffered  # cursor value of lines[0]

    since = max(0, min(since, total))
    start = max(since, first_buffered)
    chunk = list(test["lines"])[start - first_buffered:]

    return {
        "lines": chunk,
        "next": total,
        "dropped": max(0, first_buffered - since),
        "status": test["status"],
    }


def run_iperf3(test, server_ip, server_port, duration_minutes, bind_interface, bandwidth_mbps):
    """Run iperf3 in a background thread, buffering output into this test's record."""
    max_duration = 86400
    total_seconds = duration_minutes * 60
    remaining = total_seconds
    run_number = 0
    final_status = "finished"

    bandwidth_arg = ["-b", f"{bandwidth_mbps}M"] if bandwidth_mbps else []

    try:
        while remaining > 0:
            with test_lock:
                if test["stop_requested"]:
                    break

            run_number += 1
            run_seconds = min(remaining, max_duration)

            if total_seconds > max_duration:
                _emit(test, f"--- Run {run_number}: {run_seconds}s of {total_seconds}s total ---")

            cmd = [
                      "stdbuf", "-oL",
                      "iperf3",
                      "-c", server_ip,
                      "-p", str(server_port),
                      "--bind-dev", bind_interface,
                      "-t", str(run_seconds),
                  ] + bandwidth_arg

            _emit(test, f"$ {' '.join(cmd)}")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            with test_lock:
                test["process"] = proc

            for line in proc.stdout:
                _emit(test, line)

            proc.wait()
            rc = proc.returncode

            with test_lock:
                test["process"] = None
                stopped = test["stop_requested"]

            if stopped:
                final_status = "stopped"
                break
            if rc != 0:
                _emit(test, f"iperf3 exited with code {rc}")
                final_status = "error"
                break

            remaining -= run_seconds

        if final_status == "finished":
            _emit(test, "All iperf3 run(s) completed.")
        elif final_status == "stopped":
            _emit(test, "Test stopped.")
    except Exception as e:
        _emit(test, f"Error: {e}")
        final_status = "error"
    finally:
        with test_lock:
            test["status"] = final_status
            test["ended_at"] = time.time()
            test["process"] = None


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname", methods=["GET"])
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/interfaces")
def list_interfaces():
    """Real interfaces with a live IPv4 address, for the Bind Interface dropdown."""
    return jsonify({"success": True, "interfaces": get_bindable_interfaces()})


@app.route("/scan", methods=["POST"])
def scan():
    try:
        data = request.get_json(silent=True) or {}
        bind_interface = data.get("bind_interface") or "wlan0"
        servers = scan_for_servers(bind_interface=bind_interface)
        return jsonify({"servers": servers})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/start", methods=["POST"])
def start():
    with test_lock:
        running = sum(1 for t in tests.values() if t["status"] == "running")
    if running >= MAX_CONCURRENT_TESTS:
        return jsonify({
            "error": f"Already running {running} tests (limit {MAX_CONCURRENT_TESTS}). Stop one first."
        }), 409

    data = request.json or {}
    raw_server_ip = (data.get("server_ip") or "").strip()
    raw_server_port = data.get("server_port") or 5201

    if ":" in raw_server_ip:
        parts = raw_server_ip.split(":", 1)
        server_ip = parts[0].strip()
        try:
            server_port = int(parts[1].strip())
        except ValueError:
            server_port = 5201
    else:
        server_ip = raw_server_ip
        try:
            server_port = int(raw_server_port)
        except (ValueError, TypeError):
            server_port = 5201

    try:
        duration_minutes = int(data.get("duration_minutes") or 60)
    except (ValueError, TypeError):
        return jsonify({"error": "Duration must be an integer"}), 400

    bind_interface = data.get("bind_interface") or "wlan0"
    bandwidth_mbps = str(data.get("bandwidth_mbps") or "").strip()

    if not server_ip or not is_valid_ip(server_ip):
        return jsonify({"error": "Invalid server IP address"}), 400
    if not (1 <= server_port <= 65535):
        return jsonify({"error": "Server port must be between 1 and 65535"}), 400
    detected = get_bindable_interfaces()
    if detected and bind_interface not in detected:
        return jsonify({"error": f"Unknown interface: {bind_interface}."}), 400
    if duration_minutes < 1:
        return jsonify({"error": "Duration must be at least 1 minute"}), 400

    with test_lock:
        test = _new_test({
            "server_ip": server_ip,
            "server_port": server_port,
            "bind_interface": bind_interface,
            "duration_minutes": duration_minutes,
            "bandwidth_mbps": bandwidth_mbps,
        })
        summary = test_summary(test)

    thread = threading.Thread(
        target=run_iperf3,
        args=(test, server_ip, server_port, duration_minutes, bind_interface, bandwidth_mbps),
        daemon=True
    )
    thread.start()

    return jsonify({"status": "started", "test_id": test["id"], "test": summary})


@app.route("/tests")
def list_tests():
    """
    Every test this process knows about, newest last.

    The tests live here, not in the browser page, so a reloaded or reopened page
    calls this to rebuild its tab strip rather than assuming nothing is running.
    """
    with test_lock:
        return jsonify({
            "tests": [test_summary(t) for t in tests.values()],
            "max_concurrent": MAX_CONCURRENT_TESTS,
        })


@app.route("/tests/<test_id>/output")
def test_output(test_id):
    """Buffered output from cursor `since` onwards, for one test."""
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0

    with test_lock:
        test = tests.get(test_id)
        if test is None:
            return jsonify({"error": "Unknown test"}), 404
        return jsonify(read_output(test, since))


@app.route("/tests/<test_id>/stop", methods=["POST"])
def stop_test(test_id):
    """Stop one test. Terminating iperf3 ends the run loop in its thread."""
    with test_lock:
        test = tests.get(test_id)
        if test is None:
            return jsonify({"error": "Unknown test"}), 404
        if test["status"] != "running":
            return jsonify({"status": test["status"]})
        test["stop_requested"] = True
        proc = test["process"]

    if proc is not None:
        proc.terminate()
    return jsonify({"status": "stopping"})


@app.route("/tests/<test_id>", methods=["DELETE"])
def forget_test(test_id):
    """Drop a finished test from the registry, clearing its tab."""
    with test_lock:
        test = tests.get(test_id)
        if test is None:
            return jsonify({"error": "Unknown test"}), 404
        if test["status"] == "running":
            return jsonify({"error": "Test is still running"}), 409
        del tests[test_id]
    return jsonify({"status": "removed"})


@app.route("/stop", methods=["POST"])
def stop():
    """Stop every running test."""
    with test_lock:
        running = [t for t in tests.values() if t["status"] == "running"]
        for t in running:
            t["stop_requested"] = True
        procs = [t["process"] for t in running if t["process"] is not None]

    for proc in procs:
        proc.terminate()

    if not running:
        return jsonify({"status": "no test running"})
    return jsonify({"status": "stopping", "count": len(running)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
