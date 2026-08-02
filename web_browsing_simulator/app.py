#!/usr/bin/env python3
"""
Flask web app that simulates realistic web-browsing traffic between two Pis,
as a complement to the iperf3 apps' sustained-throughput testing. Every
instance serves a randomized corpus of synthetic "pages" (see content_gen.py)
and can also drive traffic against another instance's corpus: fetch a random
page's HTML, then its assets with bounded concurrency (mimicking a browser's
per-host connection limit), then idle for a random think-time before the next
page -- a bursty pattern instead of iperf3's steady stream. An "intensity"
slider (1-10) runs that many such sessions concurrently, each with its own
independent randomization, so overall load scales without flattening it out.
"""

import ipaddress
import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_from_directory

# Adjust path to import content_gen.py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from content_gen import ensure_content_corpus

app = Flask(__name__)

CONTENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content")
ensure_content_corpus(CONTENT_DIR)

THINK_TIME_RANGE = (2, 8)
ASSET_CONCURRENCY = 6
CONTENT_PORT = 5004

# "Intensity" is the number of concurrent simulated browsing sessions -- each one
# independently picks its own random pages and think-times, so turning intensity up
# scales the overall load without reducing the randomization of any single session.
INTENSITY_RANGE = (1, 10)
DEFAULT_INTENSITY = 3

# Global state for the running simulation. active_sessions is only ever mutated inside
# run_browsing_loop itself (mirroring the iperf apps' Popen-object-is-None check) so
# that it accurately reflects how many session threads' bodies are actually running.
stop_event = threading.Event()
active_sessions = 0
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


def scan_for_servers(bind_interface: str = "wlan0") -> list[dict]:
    """Scan the LAN for other Pis running this app (port 5004 open), excluding local host IPs."""
    if not shutil.which("nmap"):
        raise RuntimeError("nmap is not installed. Run: sudo apt-get install nmap")

    if bind_interface not in ("wlan0", "eth0"):
        bind_interface = "wlan0"

    local_ips = {"127.0.0.1"}

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
                    try:
                        ip_str = str(ipaddress.ip_interface(parts[1]).ip)
                        if not ip_str.startswith("127."):
                            local_ips.add(ip_str)
                    except Exception:
                        pass
    except Exception:
        pass

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
                    try:
                        iface_obj = ipaddress.ip_interface(parts[1])
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

    cmd = ["nmap", "-e", bind_interface, "-Pn", "-p", str(CONTENT_PORT), "--open", "-n", "-T4", "-oG", "-", cidr]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    found = []
    for line in result.stdout.splitlines():
        if line.startswith("Host:") and "/open" in line:
            parts = line.split()
            if len(parts) >= 2:
                ip = parts[1]
                if ip not in local_ips:
                    found.append({"ip": ip, "port": CONTENT_PORT})

    def ip_key(item):
        try:
            return (0, socket.inet_aton(item["ip"]))
        except Exception:
            return (1, item["ip"])

    found.sort(key=ip_key)
    return found


def content_url(target_ip: str, target_port: int, relative_path: str) -> str:
    """
    Build the URL for a piece of generated content on the target.

    Port 5004 is this app's own Flask port, reachable directly in local dev
    (no nginx in front) at /content/... . Any other port is assumed to be
    the target's nginx (typically 80), where /webbrowse/content/ is aliased
    straight to the generated corpus directory -- the realistic "browsing
    the internet" path that actually exercises nginx's static file serving.
    """
    prefix = "/content/" if target_port == CONTENT_PORT else "/webbrowse/content/"
    return f"http://{target_ip}:{target_port}{prefix}{relative_path}"


def fetch(url: str, timeout: int = 10) -> int:
    """GET a URL and return the number of bytes read, discarding the body."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return len(resp.read())


def run_browsing_loop(session_id: int, target_ip: str, target_port: int, duration_minutes: int) -> None:
    """
    Run one simulated browsing session in a background thread, streaming summaries to
    output_queue. Multiple sessions run concurrently (one per intensity level) and are
    each fully independent -- their own random page picks and think-times -- so raising
    intensity scales overall load without touching the randomization of any one session.
    """
    global active_sessions

    with test_lock:
        active_sessions += 1

    end_time = time.time() + duration_minutes * 60
    tag = f"[s{session_id}] "

    try:
        manifest_url = content_url(target_ip, target_port, "manifest.json")
        output_queue.put(f"{tag}Fetching manifest: {manifest_url}\n")
        with urllib.request.urlopen(manifest_url, timeout=10) as resp:
            manifest = json.loads(resp.read().decode())

        pages = manifest.get("pages") or {}
        if not pages:
            output_queue.put(f"{tag}Target manifest has no pages.\n")
            return

        page_ids = list(pages.keys())
        output_queue.put(f"{tag}Loaded manifest: {len(page_ids)} pages available.\n")

        while time.time() < end_time and not stop_event.is_set():
            page_id = random.choice(page_ids)
            page = pages[page_id]
            assets = page.get("assets") or []

            page_start = time.time()
            total_bytes = fetch(content_url(target_ip, target_port, page["html_path"]))

            with ThreadPoolExecutor(max_workers=ASSET_CONCURRENCY) as executor:
                futures = [
                    executor.submit(fetch, content_url(target_ip, target_port, asset["path"]))
                    for asset in assets
                ]
                for future in futures:
                    total_bytes += future.result()

            elapsed_s = time.time() - page_start
            throughput_kbps = (total_bytes * 8 / 1000) / elapsed_s if elapsed_s > 0 else 0
            output_queue.put(
                f"{tag}{page_id}: {len(assets) + 1} objects, {total_bytes / 1024:.1f} KB, "
                f"{elapsed_s * 1000:.0f} ms, {throughput_kbps:.0f} kbps\n"
            )

            think_time = random.uniform(*THINK_TIME_RANGE)
            slept = 0.0
            while slept < think_time and not stop_event.is_set() and time.time() < end_time:
                step = min(0.5, think_time - slept)
                time.sleep(step)
                slept += step

        output_queue.put(f"{tag}session finished.\n")
    except urllib.error.URLError as e:
        output_queue.put(f"{tag}Error reaching target: {e}\n")
    except Exception as e:
        output_queue.put(f"{tag}Error: {e}\n")
    finally:
        with test_lock:
            active_sessions -= 1
            all_done = active_sessions == 0
        if all_done:
            output_queue.put("\nAll browsing sessions completed.\n")
            output_queue.put(None)  # sentinel


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname", methods=["GET"])
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/content/manifest.json")
def content_manifest():
    return send_from_directory(CONTENT_DIR, "manifest.json")


@app.route("/content/pages/<path:filename>")
def content_page(filename):
    return send_from_directory(os.path.join(CONTENT_DIR, "pages"), filename)


@app.route("/content/assets/<path:filename>")
def content_asset(filename):
    return send_from_directory(os.path.join(CONTENT_DIR, "assets"), filename)


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
        if active_sessions > 0:
            return jsonify({"error": "A test is already running"}), 409

    data = request.json or {}
    raw_target_ip = (data.get("target_ip") or "").strip()
    raw_target_port = data.get("target_port") or 80

    if ":" in raw_target_ip:
        parts = raw_target_ip.split(":", 1)
        target_ip = parts[0].strip()
        try:
            target_port = int(parts[1].strip())
        except ValueError:
            target_port = 80
    else:
        target_ip = raw_target_ip
        try:
            target_port = int(raw_target_port)
        except (ValueError, TypeError):
            target_port = 80

    try:
        duration_minutes = int(data.get("duration_minutes") or 10)
    except (ValueError, TypeError):
        return jsonify({"error": "Duration must be an integer"}), 400

    try:
        intensity = int(data.get("intensity") or DEFAULT_INTENSITY)
    except (ValueError, TypeError):
        return jsonify({"error": "Intensity must be an integer"}), 400

    if not target_ip or not is_valid_ip(target_ip):
        return jsonify({"error": "Invalid target IP address"}), 400
    if not (1 <= target_port <= 65535):
        return jsonify({"error": "Target port must be between 1 and 65535"}), 400
    if duration_minutes < 1:
        return jsonify({"error": "Duration must be at least 1 minute"}), 400
    if not (INTENSITY_RANGE[0] <= intensity <= INTENSITY_RANGE[1]):
        return jsonify({"error": f"Intensity must be between {INTENSITY_RANGE[0]} and {INTENSITY_RANGE[1]}"}), 400

    while not output_queue.empty():
        try:
            output_queue.get_nowait()
        except queue.Empty:
            break

    stop_event.clear()
    for session_id in range(1, intensity + 1):
        thread = threading.Thread(
            target=run_browsing_loop,
            args=(session_id, target_ip, target_port, duration_minutes),
            daemon=True
        )
        thread.start()

    return jsonify({"status": "started", "sessions": intensity})


@app.route("/status")
def status():
    """
    Report whether a simulation is currently running, and how many concurrent
    browsing sessions are live.

    The simulation lives in this process, not in the browser page, so a reloaded
    or reopened page needs to ask rather than assume it is idle. `active_sessions`
    is the same value /start and /stop already gate on.
    """
    with test_lock:
        return jsonify({"running": active_sessions > 0, "active_sessions": active_sessions})


@app.route("/stop", methods=["POST"])
def stop():
    with test_lock:
        if active_sessions > 0:
            stop_event.set()
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
    app.run(host="0.0.0.0", port=5004, debug=False)
