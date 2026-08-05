#!/usr/bin/env python3
"""
Flask web app that lists every device currently reachable on the LAN behind
a chosen Bind Interface. Runs a privileged ARP-based host sweep (`nmap -sn`)
so it can see devices with all TCP/UDP ports closed or firewalled, and report
each one's MAC address and vendor -- the narrower, port-scoped `nmap -Pn -p
<port>` probes used elsewhere in this repo (iperf_congestion_generator,
web_browsing_simulator) only find hosts running that app's own service.
"""

import ipaddress
import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime

from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

last_scan = {
    "devices": [],
    "count": 0,
    "cidr": None,
    "bind_interface": None,
    "timestamp": None,
    "error": None,
}


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def get_bindable_interfaces() -> list:
    """Real network interfaces with a live IPv4 address, i.e. usable as a scan-bind
    target -- excludes loopback and anything with no address (down, unconfigured,
    monitor-mode, etc.), since those can't be scanned through anyway. Read-only
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


def get_subnet_cidr(bind_interface: str) -> tuple[str, set]:
    """
    Resolve the IPv4 CIDR of bind_interface (e.g. "192.168.1.0/24") plus the
    set of this machine's own IPv4 addresses, so scan results can flag which
    row is the Pi itself.
    """
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

    return cidr, local_ips


def parse_nmap_hosts(xml_text: str) -> list:
    """Parse `nmap -oX -` output into a list of {ip, mac, vendor, hostname} dicts for up hosts."""
    devices = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return devices

    for host in root.findall("host"):
        status = host.find("status")
        if status is None or status.get("state") != "up":
            continue

        ip = None
        mac = ""
        vendor = ""
        for addr in host.findall("address"):
            addrtype = addr.get("addrtype")
            if addrtype == "ipv4":
                ip = addr.get("addr")
            elif addrtype == "mac":
                mac = addr.get("addr") or ""
                vendor = addr.get("vendor") or ""

        if not ip:
            continue

        hostname = ""
        hostnames_el = host.find("hostnames")
        if hostnames_el is not None:
            hostname_el = hostnames_el.find("hostname")
            if hostname_el is not None:
                hostname = hostname_el.get("name") or ""

        devices.append({"ip": ip, "mac": mac, "vendor": vendor, "hostname": hostname})

    return devices


def scan_lan(bind_interface: str = "wlan0") -> dict:
    """
    Run a privileged ARP-based host sweep of the subnet behind bind_interface
    and return every live device found, each flagged with is_self if it's one
    of this machine's own addresses.
    """
    if not shutil.which("nmap"):
        raise RuntimeError("nmap is not installed. Run: sudo apt-get install nmap")

    detected = get_bindable_interfaces()
    if detected and bind_interface not in detected:
        bind_interface = detected[0]

    cidr, local_ips = get_subnet_cidr(bind_interface)

    # sudo -n (non-interactive): fails fast with a clear error instead of hanging the
    # request if passwordless sudo for nmap hasn't been configured (see README).
    # Reverse-DNS is left enabled (no nmap -n) so the hostname field actually gets
    # populated for devices with a PTR record, at the cost of some added scan latency.
    cmd = ["sudo", "-n", "nmap", "-e", bind_interface, "-sn", "-T4", "-oX", "-", cidr]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("LAN scan timed out")
    except Exception as e:
        raise RuntimeError(f"Failed to run nmap: {e}")

    if result.returncode != 0 and not result.stdout:
        raise RuntimeError(
            result.stderr.strip()
            or "nmap scan failed. Ensure passwordless sudo is configured for nmap (see README)."
        )

    devices = parse_nmap_hosts(result.stdout)
    for device in devices:
        device["is_self"] = device["ip"] in local_ips

    def ip_key(device):
        try:
            return ipaddress.ip_address(device["ip"])
        except ValueError:
            return ipaddress.ip_address("255.255.255.255")

    devices.sort(key=ip_key)

    return {
        "devices": devices,
        "count": len(devices),
        "cidr": cidr,
        "bind_interface": bind_interface,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": None,
    }


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname", methods=["GET"])
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/api/interfaces", methods=["GET"])
def api_interfaces():
    """Real interfaces with a live IPv4 address, for the Bind Interface dropdown."""
    return jsonify({"success": True, "interfaces": get_bindable_interfaces()})


@app.route("/api/devices", methods=["GET"])
def api_devices():
    """Return the most recent scan result without triggering a new scan."""
    return jsonify({"success": True, **last_scan})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Run a fresh LAN scan and cache the result for subsequent /api/devices polls."""
    global last_scan
    data = request.get_json(silent=True) or {}
    bind_interface = data.get("bind_interface") or "wlan0"

    try:
        last_scan = scan_lan(bind_interface=bind_interface)
        return jsonify({"success": True, **last_scan})
    except RuntimeError as e:
        # Keep the previous successful results visible; only the error is fresh.
        last_scan = {**last_scan, "error": str(e)}
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5006, debug=False)
