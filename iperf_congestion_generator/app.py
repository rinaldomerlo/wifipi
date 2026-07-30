#!/usr/bin/env python3
"""
Flask web app equivalent of simple_iperf3_start.sh.
Provides a browser UI to configure and launch an iperf3 client test.
"""

import subprocess
import threading
import queue
import shutil
import re
import socket
import ipaddress
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)

# Global state for the running test
test_process = None
test_lock = threading.Lock()
output_queue = queue.Queue()


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def is_valid_ip(ip: str) -> bool:
    pattern = r"^\d{1,3}(\.\d{1,3}){3}$"
    return bool(re.match(pattern, ip))


def scan_for_servers(bind_interface: str = "wlan0", ports: str = "5201-5210") -> list[dict]:
    """Scan the LAN for iperf3 servers on port range 5201-5210 specifically on bind_interface, excluding local host IPs."""
    if not shutil.which("nmap"):
        raise RuntimeError("nmap is not installed. Run: sudo apt-get install nmap")

    if bind_interface not in ("wlan0", "eth0"):
        bind_interface = "wlan0"

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


def run_iperf3(server_ip, server_port, duration_minutes, bind_interface, bandwidth_mbps):
    """Run iperf3 in a background thread, streaming output to output_queue."""
    global test_process

    max_duration = 86400
    total_seconds = duration_minutes * 60
    remaining = total_seconds
    run_number = 0

    bandwidth_arg = ["-b", f"{bandwidth_mbps}M"] if bandwidth_mbps else []

    try:
        while remaining > 0:
            run_number += 1
            run_seconds = min(remaining, max_duration)

            if total_seconds > max_duration:
                output_queue.put(f"--- Run {run_number}: {run_seconds}s of {total_seconds}s total ---\n")

            cmd = [
                      "stdbuf", "-oL",
                      "iperf3",
                      "-c", server_ip,
                      "-p", str(server_port),
                      "--bind-dev", bind_interface,
                      "-t", str(run_seconds),
                  ] + bandwidth_arg

            output_queue.put(f"$ {' '.join(cmd)}\n")

            with test_lock:
                test_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )

            for line in test_process.stdout:
                output_queue.put(line)

            test_process.wait()
            rc = test_process.returncode

            with test_lock:
                test_process = None

            if rc != 0:
                output_queue.put(f"\niperf3 exited with code {rc}\n")
                break

            remaining -= run_seconds

        output_queue.put("\nAll iperf3 run(s) completed.\n")
    except Exception as e:
        output_queue.put(f"\nError: {e}\n")
    finally:
        output_queue.put(None)  # sentinel


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


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
    global test_process

    with test_lock:
        if test_process is not None:
            return jsonify({"error": "A test is already running"}), 409

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
    if bind_interface not in ("wlan0", "eth0"):
        return jsonify({"error": "Interface must be wlan0 or eth0"}), 400
    if duration_minutes < 1:
        return jsonify({"error": "Duration must be at least 1 minute"}), 400

    # Clear old output
    while not output_queue.empty():
        try:
            output_queue.get_nowait()
        except queue.Empty:
            break

    thread = threading.Thread(
        target=run_iperf3,
        args=(server_ip, server_port, duration_minutes, bind_interface, bandwidth_mbps),
        daemon=True
    )
    thread.start()

    return jsonify({"status": "started"})


@app.route("/stop", methods=["POST"])
def stop():
    with test_lock:
        if test_process is not None:
            test_process.terminate()
            return jsonify({"status": "stopped"})
    return jsonify({"status": "no test running"})


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
    app.run(host="0.0.0.0", port=5001, debug=False)
