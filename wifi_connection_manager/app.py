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


def get_wireless_interfaces() -> list:
    """Return every WiFi-capable device name reported by nmcli, or [] if none."""
    out, err = _run_nmcli(["-t", "-f", "DEVICE,TYPE", "device", "status"])
    if not out:
        return []
    interfaces = []
    for line in out.strip().splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[1] == "wifi":
            interfaces.append(parts[0])
    return interfaces


def get_wireless_interface() -> str:
    """Return the first WiFi-capable device name reported by nmcli, or '' if none."""
    ifaces = get_wireless_interfaces()
    return ifaces[0] if ifaces else ""


def _valid_interface(iface: str) -> bool:
    """True if `iface` is a safe-looking device name that is actually a known wifi radio."""
    if not iface or not re.match(r"^[A-Za-z0-9_.-]+$", iface):
        return False
    return iface in get_wireless_interfaces()


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


MISSING_SECRETS_PATTERN = r"Secrets were required|802-11-wireless-security\.psk|key-mgmt"

CONNECT_ERROR_PATTERNS = [
    (MISSING_SECRETS_PATTERN, "Incorrect password."),
    (r"No network with SSID", "Network not found — try rescanning."),
    (r"connection is not available|activation failed", "Connection attempt failed. The AP may be out of range."),
]


def friendly_connect_error(raw_error: str) -> str:
    for pattern, message in CONNECT_ERROR_PATTERNS:
        if re.search(pattern, raw_error, re.IGNORECASE):
            return message
    return f"Connection failed: {raw_error}"


def _needs_secrets(raw_error: str) -> bool:
    """True if a failed `nmcli device wifi connect` looks like it was rejected for a
    missing/incorrect password rather than any other reason (AP out of range, etc)."""
    return bool(raw_error) and bool(re.search(MISSING_SECRETS_PATTERN, raw_error, re.IGNORECASE))


def _connect_iface(ssid: str, iface: str, password: str):
    """Run `nmcli device wifi connect <ssid> ifname <iface> [password <password>]`.

    Returns (ok, error) — error is nmcli's raw stderr/stdout on failure, None on success.
    """
    args = ["device", "wifi", "connect", ssid, "ifname", iface]
    if password:
        args += ["password", password]
    _, err = _run_nmcli(args, timeout=CONNECT_TIMEOUT)
    return err is None, err


def _saved_psk(name: str):
    """Look up the WPA/WPA2 pre-shared key saved for connection profile `name`, if any.

    Requires root (via sudo) — NetworkManager redacts secrets for unprivileged callers
    even with --show-secrets. Returns (password, error):
    - (psk, None) when a saved profile with a PSK was found.
    - (None, None) when the profile exists but has no PSK (Open network, 802.1x, etc) —
      not an error, just nothing to reuse.
    - (None, error) when no matching profile exists, or the lookup otherwise failed.
    """
    out, err = _run_nmcli(
        ["-s", "-g", "802-11-wireless-security.psk", "connection", "show", name],
        timeout=NMCLI_TIMEOUT,
    )
    if err:
        if re.search(r"no such property|unknown property", err, re.IGNORECASE):
            return None, None
        return None, err

    lines = (out or "").strip().splitlines()
    password = _split_terse(lines[0])[0] if lines else ""
    return (password or None), None


@app.route("/")
def index():
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname")
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/api/interfaces")
def api_interfaces():
    """List every WiFi-capable device nmcli knows about, for the target-interface selector."""
    return jsonify({"success": True, "interfaces": get_wireless_interfaces()})


def interface_status(iface: str) -> dict:
    """Report the current WiFi connection state for a single interface, scoped to it.

    `--rescan no` is deliberate: this only needs the already-associated AP's entry,
    which NetworkManager keeps live from the active connection without a directed
    scan. Without this flag, `device wifi list` may trigger a real over-the-air scan
    (several seconds per radio); with /api/status walking every interface and being
    polled every 10s, that turned into pile-ups of overlapping scans on multi-radio
    boxes -- each new poll's scan colliding with the previous one still in flight.
    """
    out, err = _run_nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ", "device", "wifi", "list",
         "ifname", iface, "--rescan", "no"]
    )
    if err:
        return {"interface": iface, "connected": False, "error": f"Failed to read connection status: {err}"}

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
        return {"interface": iface, "connected": False}

    ip_out, _ = _run_nmcli(["-t", "-f", "IP4.ADDRESS", "device", "show", iface])
    ip_address = None
    if ip_out:
        first_line = ip_out.strip().splitlines()[0] if ip_out.strip() else ""
        ip_field = _split_terse(first_line)
        if len(ip_field) >= 2 and ip_field[1]:
            ip_address = ip_field[1].split("/")[0]

    conn_out, _ = _run_nmcli(["-t", "-f", "GENERAL.CONNECTION", "device", "show", iface])
    connection_name = None
    if conn_out:
        first_line = conn_out.strip().splitlines()[0] if conn_out.strip() else ""
        conn_field = _split_terse(first_line)
        if len(conn_field) >= 2:
            connection_name = conn_field[1]

    return {
        "interface": iface,
        "connected": True,
        "connection_name": connection_name,
        "ssid": connected["ssid"],
        "ip_address": ip_address,
        "signal": connected["signal"],
        "security": connected["security"],
        "channel": connected["channel"],
        "band": connected["band"],
    }


@app.route("/api/status")
def api_status():
    """Report the current WiFi connection state for every wireless interface on the system."""
    interfaces = get_wireless_interfaces()
    if not interfaces:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    return jsonify({"success": True, "interfaces": [interface_status(i) for i in interfaces]})


@app.route("/api/scan")
def api_scan():
    """Scan for nearby WiFi networks on a chosen (or the default) interface."""
    requested_iface = request.args.get("interface")
    if requested_iface:
        if not _valid_interface(requested_iface):
            return jsonify({"success": False, "error": f"Unknown interface: {requested_iface}."}), 400
        iface = requested_iface
    else:
        iface = get_wireless_interface()
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    out, err = _run_nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ,BSSID", "device", "wifi", "list", "ifname", iface, "--rescan", "yes"],
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
    requested_iface = data.get("interface")

    if not ssid:
        return jsonify({"success": False, "error": "SSID is required."}), 400

    if requested_iface:
        if not _valid_interface(requested_iface):
            return jsonify({"success": False, "error": f"Unknown interface: {requested_iface}."}), 400
        iface = requested_iface
    else:
        iface = get_wireless_interface()
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    ok, err = _connect_iface(ssid, iface, password)
    if not ok and not password and _needs_secrets(err):
        # A profile saved for this SSID may be bound to a different interface (e.g. the
        # one it's already connected on) — nmcli then creates or matches a fresh
        # per-device profile with no secret of its own. Reuse the known-good saved PSK
        # explicitly instead of asking the operator to re-type a password already on
        # file; the caller (single-connect flow) only ever reaches this with an empty
        # password when it already believed the SSID was saved.
        saved_password, lookup_err = _saved_psk(ssid)
        if saved_password:
            ok, err = _connect_iface(ssid, iface, saved_password)
        elif lookup_err:
            return jsonify({
                "success": False,
                "needs_password": True,
                "error": f"No saved credentials found for {ssid}.",
            }), 200

    if not ok:
        return jsonify({"success": False, "error": friendly_connect_error(err)}), 200

    return jsonify({"success": True, "message": f"Connected to {ssid}."})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    """Disconnect a wireless interface from its current network."""
    data = request.get_json(silent=True) or {}
    requested_iface = data.get("interface")

    if requested_iface:
        if not _valid_interface(requested_iface):
            return jsonify({"success": False, "error": f"Unknown interface: {requested_iface}."}), 400
        iface = requested_iface
    else:
        iface = get_wireless_interface()
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    out, err = _run_nmcli(["device", "disconnect", iface], timeout=CONNECT_TIMEOUT)
    if err:
        return jsonify({"success": False, "error": f"Disconnect failed: {err}"}), 200

    return jsonify({"success": True, "message": "Disconnected."})


@app.route("/api/connect-all", methods=["POST"])
def api_connect_all():
    """Connect every wireless interface (or just the idle ones) to the same SSID at once."""
    data = request.get_json(silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    password = data.get("password") or ""
    only_idle = data.get("only_idle", True)

    if not ssid:
        return jsonify({"success": False, "error": "SSID is required."}), 400

    ifaces = get_wireless_interfaces()
    if not ifaces:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    if only_idle:
        ifaces = [i for i in ifaces if not interface_status(i)["connected"]]

    if not ifaces:
        return jsonify({"success": True, "results": [], "connected": 0, "failed": 0})

    results = []
    for iface in ifaces:
        ok, err = _connect_iface(ssid, iface, password)
        if not ok and not password and _needs_secrets(err):
            # A profile saved for this SSID (e.g. from the interface that's already
            # connected) doesn't carry over automatically — nmcli binds a fresh
            # per-device profile and needs the secret again. Look the saved PSK up once
            # and reuse it explicitly for this and every remaining interface instead of
            # failing each one with "Incorrect password"; only ask the caller to prompt
            # if nothing is on file at all.
            saved_password, lookup_err = _saved_psk(ssid)
            if saved_password:
                password = saved_password
                ok, err = _connect_iface(ssid, iface, password)
            elif lookup_err:
                return jsonify({
                    "success": False,
                    "needs_password": True,
                    "error": f"No saved credentials found for {ssid}.",
                }), 200
        if ok:
            results.append({"interface": iface, "ok": True, "message": f"Connected to {ssid}."})
        else:
            results.append({"interface": iface, "ok": False, "error": friendly_connect_error(err)})

    connected = sum(1 for r in results if r["ok"])
    failed = len(results) - connected
    return jsonify({"success": True, "results": results, "connected": connected, "failed": failed})


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


@app.route("/api/autoconnect", methods=["POST"])
def api_autoconnect():
    """Enable or disable auto-connect for a saved WiFi connection profile."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    enabled = bool(data.get("enabled"))

    if not name:
        return jsonify({"success": False, "error": "Connection name is required."}), 400

    value = "yes" if enabled else "no"
    out, err = _run_nmcli(["connection", "modify", name, "autoconnect", value], timeout=NMCLI_TIMEOUT)
    if err:
        return jsonify({"success": False, "error": f"Failed to update auto-connect: {err}"}), 200

    return jsonify({
        "success": True,
        "message": f"Auto-connect {'enabled' if enabled else 'disabled'} for {name}.",
    })


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    """Reveal the saved WPA/WPA2 pre-shared key for a saved connection profile.

    Requires root (via sudo) — NetworkManager redacts secrets for unprivileged callers
    even with --show-secrets. Open networks, or profiles with no stored PSK (e.g.
    802.1x enterprise), have no 802-11-wireless-security.psk property; that's reported
    as success with a null password rather than an error.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Connection name is required."}), 400

    password, err = _saved_psk(name)
    if err:
        return jsonify({"success": False, "error": f"Failed to reveal password: {err}"}), 200
    return jsonify({"success": True, "password": password})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5003))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port argument: {sys.argv[1]}. Using default port {port}.", file=sys.stderr)

    app.run(host="0.0.0.0", port=port, debug=True)
