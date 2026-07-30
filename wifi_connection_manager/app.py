#!/usr/bin/env python3
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

NMCLI_TIMEOUT = 15
CONNECT_TIMEOUT = 30


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def _nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


def _run_nmcli(args, timeout=NMCLI_TIMEOUT):
    """Run `sudo nmcli <args>` and return (stdout, error). stdout is None on failure."""
    if not _nmcli_available():
        return None, "nmcli is not installed on this host. Run: sudo apt-get install network-manager"

    cmd = ["sudo", "nmcli"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None, (result.stderr or result.stdout or "nmcli command failed").strip()
        return result.stdout, None
    except subprocess.TimeoutExpired:
        return None, f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return None, f"Failed to execute nmcli: {str(e)}"


def _split_terse(line: str) -> list:
    """Split a colon-delimited nmcli -t line, honoring backslash-escaped colons."""
    fields = re.split(r"(?<!\\):", line)
    return [f.replace("\\:", ":").replace("\\\\", "\\") for f in fields]


def get_wireless_interface() -> str:
    """Return the first WiFi-capable device name reported by nmcli, or '' if none."""
    out, err = _run_nmcli(["-t", "-f", "DEVICE,TYPE", "device", "status"])
    if not out:
        return ""
    for line in out.strip().splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return ""


def classify_security(security_field: str) -> str:
    sec = (security_field or "").strip()
    if not sec or sec == "--":
        return "Open"
    return sec.split()[0] if " " not in sec else sec


def classify_band(freq_mhz):
    if freq_mhz is None:
        return None
    if freq_mhz < 2500:
        return "2.4GHz"
    if freq_mhz < 5900:
        return "5GHz"
    return "6GHz"


CONNECT_ERROR_PATTERNS = [
    (r"Secrets were required|802-11-wireless-security\.psk|key-mgmt", "Incorrect password."),
    (r"No network with SSID", "Network not found — try rescanning."),
    (r"connection is not available|activation failed", "Connection attempt failed. The AP may be out of range."),
]


def friendly_connect_error(raw_error: str) -> str:
    for pattern, message in CONNECT_ERROR_PATTERNS:
        if re.search(pattern, raw_error, re.IGNORECASE):
            return message
    return f"Connection failed: {raw_error}"


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname")
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/api/status")
def api_status():
    """Report the current WiFi connection state for the detected interface."""
    iface = get_wireless_interface()
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    out, err = _run_nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ", "device", "wifi", "list"]
    )
    if err:
        return jsonify({"success": False, "error": f"Failed to read connection status: {err}"}), 200

    connected = None
    for line in (out or "").strip().splitlines():
        parts = _split_terse(line)
        if len(parts) < 6:
            continue
        in_use, ssid, signal, security, chan, freq = parts[:6]
        if in_use.strip() == "*" and ssid:
            freq_mhz = int(freq.split()[0]) if freq.split() else None
            connected = {
                "ssid": ssid,
                "signal": int(signal) if signal.isdigit() else None,
                "security": classify_security(security),
                "channel": int(chan) if chan.isdigit() else None,
                "band": classify_band(freq_mhz),
            }
            break

    if not connected:
        return jsonify({"success": True, "connected": False, "interface": iface})

    ip_out, _ = _run_nmcli(["-t", "-f", "IP4.ADDRESS", "device", "show", iface])
    ip_address = None
    if ip_out:
        first_line = ip_out.strip().splitlines()[0] if ip_out.strip() else ""
        ip_field = _split_terse(first_line)
        if len(ip_field) >= 2 and ip_field[1]:
            ip_address = ip_field[1].split("/")[0]

    uptime_out, _ = _run_nmcli(["-t", "-f", "GENERAL.CONNECTION", "device", "show", iface])
    connection_name = None
    if uptime_out:
        first_line = uptime_out.strip().splitlines()[0] if uptime_out.strip() else ""
        conn_field = _split_terse(first_line)
        if len(conn_field) >= 2:
            connection_name = conn_field[1]

    return jsonify({
        "success": True,
        "connected": True,
        "interface": iface,
        "connection_name": connection_name,
        "ssid": connected["ssid"],
        "ip_address": ip_address,
        "signal": connected["signal"],
        "security": connected["security"],
        "channel": connected["channel"],
        "band": connected["band"],
    })


@app.route("/api/scan")
def api_scan():
    """Scan for nearby WiFi networks on the detected interface."""
    iface = get_wireless_interface()
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    out, err = _run_nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ,BSSID", "device", "wifi", "list", "--rescan", "yes"],
        timeout=NMCLI_TIMEOUT,
    )
    if err:
        return jsonify({"success": False, "error": f"Scan failed: {err}"}), 200

    networks = []
    for line in (out or "").strip().splitlines():
        parts = _split_terse(line)
        if len(parts) < 7:
            continue
        in_use, ssid, signal, security, chan, freq, bssid = parts[:7]
        if not ssid:
            continue
        freq_mhz = int(freq.split()[0]) if freq.split() and freq.split()[0].isdigit() else None
        networks.append({
            "ssid": ssid,
            "bssid": bssid,
            "connected": in_use.strip() == "*",
            "signal": int(signal) if signal.isdigit() else 0,
            "security": classify_security(security),
            "channel": int(chan) if chan.isdigit() else None,
            "band": classify_band(freq_mhz),
        })

    # De-duplicate SSIDs seen on multiple BSSIDs (e.g. mesh/repeater setups).
    # The connected BSSID always wins, regardless of signal, so the "Connected"
    # state isn't lost behind a stronger unconnected AP sharing the same SSID.
    best_by_ssid = {}
    for net in networks:
        existing = best_by_ssid.get(net["ssid"])
        if not existing:
            best_by_ssid[net["ssid"]] = net
        elif net["connected"] and not existing["connected"]:
            best_by_ssid[net["ssid"]] = net
        elif net["connected"] == existing["connected"] and net["signal"] > existing["signal"]:
            best_by_ssid[net["ssid"]] = net
    deduped = sorted(best_by_ssid.values(), key=lambda n: n["signal"], reverse=True)

    return jsonify({
        "success": True,
        "interface": iface,
        "networks": deduped,
        "meta": {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "total": len(deduped)},
    })


@app.route("/api/saved")
def api_saved():
    """List saved WiFi connection profiles known to NetworkManager."""
    out, err = _run_nmcli(["-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"])
    if err:
        return jsonify({"success": False, "error": f"Failed to read saved networks: {err}"}), 200

    saved = []
    for line in (out or "").strip().splitlines():
        parts = _split_terse(line)
        if len(parts) < 3:
            continue
        name, conn_type, autoconnect = parts[:3]
        if conn_type != "802-11-wireless":
            continue
        saved.append({"name": name, "autoconnect": autoconnect.strip().lower() in ("yes", "true")})

    return jsonify({"success": True, "saved": saved})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Connect to a WiFi network, optionally supplying a password for secured networks."""
    data = request.get_json(silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    password = data.get("password") or ""

    if not ssid:
        return jsonify({"success": False, "error": "SSID is required."}), 400

    iface = get_wireless_interface()
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    args = ["device", "wifi", "connect", ssid, "ifname", iface]
    if password:
        args += ["password", password]

    out, err = _run_nmcli(args, timeout=CONNECT_TIMEOUT)
    if err:
        return jsonify({"success": False, "error": friendly_connect_error(err)}), 200

    return jsonify({"success": True, "message": f"Connected to {ssid}."})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    """Disconnect the wireless interface from its current network."""
    iface = get_wireless_interface()
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    out, err = _run_nmcli(["device", "disconnect", iface], timeout=CONNECT_TIMEOUT)
    if err:
        return jsonify({"success": False, "error": f"Disconnect failed: {err}"}), 200

    return jsonify({"success": True, "message": "Disconnected."})


@app.route("/api/forget", methods=["POST"])
def api_forget():
    """Delete a saved WiFi connection profile by name."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Connection name is required."}), 400

    out, err = _run_nmcli(["connection", "delete", name], timeout=NMCLI_TIMEOUT)
    if err:
        return jsonify({"success": False, "error": f"Failed to forget network: {err}"}), 200

    return jsonify({"success": True, "message": f"Forgot {name}."})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5003))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port argument: {sys.argv[1]}. Using default port {port}.", file=sys.stderr)

    app.run(host="0.0.0.0", port=port, debug=True)
