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


def classify_wifi_generation(
    freq_mhz=None,
    bitrate_str=None,
    raw_ies=None,
    band=None,
) -> tuple:
    """Classify WiFi generation (e.g. 'Wi-Fi 6') and standard (e.g. '802.11ax').

    Returns (wifi_generation, protocol) or (None, None).
    Supports Wi-Fi 1 through Wi-Fi 7:
    - Wi-Fi 7: 802.11be (EHT)
    - Wi-Fi 6E: 802.11ax (HE on 6GHz)
    - Wi-Fi 6: 802.11ax (HE on 2.4/5GHz)
    - Wi-Fi 5: 802.11ac (VHT on 5GHz)
    - Wi-Fi 4: 802.11n (HT on 2.4/5GHz)
    - Wi-Fi 3: 802.11g (OFDM on 2.4GHz)
    - Wi-Fi 2: 802.11a (OFDM on 5GHz)
    - Wi-Fi 1: 802.11b (DSSS/CCK on 2.4GHz)
    """
    effective_band = band or (classify_band(freq_mhz) if freq_mhz is not None else None)

    # 1. Check raw_ies / BSS block capabilities if provided
    if raw_ies:
        if isinstance(raw_ies, dict):
            ie_text = " ".join(str(v) for v in raw_ies.values()) + " " + " ".join(raw_ies.keys())
        else:
            ie_text = str(raw_ies)

        if re.search(r'\bEHT capabilities\b|\bEHT operation\b|\bEHT\b', ie_text, re.I):
            return "Wi-Fi 7", "802.11be"
        if re.search(r'\bHE capabilities\b|\bHE operation\b|\bHE\b', ie_text, re.I):
            if (freq_mhz and freq_mhz >= 5925) or effective_band == "6GHz":
                return "Wi-Fi 6E", "802.11ax"
            return "Wi-Fi 6", "802.11ax"
        if re.search(r'\bVHT capabilities\b|\bVHT operation\b|\bVHT\b', ie_text, re.I):
            return "Wi-Fi 5", "802.11ac"
        if re.search(r'\bHT capabilities\b|\bHT operation\b|\bHT\b', ie_text, re.I):
            return "Wi-Fi 4", "802.11n"

    # 2. Check bitrate string (MCS, protocol indicators, numerical throughput)
    if bitrate_str:
        bs = str(bitrate_str)
        if re.search(r'\bEHT\b|EHT-MCS|802\.11be', bs, re.I):
            return "Wi-Fi 7", "802.11be"
        if re.search(r'\bHE\b|HE-MCS|802\.11ax', bs, re.I):
            if (freq_mhz and freq_mhz >= 5925) or effective_band == "6GHz":
                return "Wi-Fi 6E", "802.11ax"
            return "Wi-Fi 6", "802.11ax"
        if re.search(r'\bVHT\b|VHT-MCS|802\.11ac', bs, re.I):
            return "Wi-Fi 5", "802.11ac"
        if re.search(r'\bHT\b|HT-MCS|\bMCS\b|802\.11n', bs, re.I):
            return "Wi-Fi 4", "802.11n"

        m = re.search(r'([\d.]+)\s*M[Bb]it/s', bs)
        if m:
            try:
                rate = float(m.group(1))
                if rate > 2400:
                    if (freq_mhz and freq_mhz >= 5925) or effective_band == "6GHz":
                        return "Wi-Fi 6E", "802.11ax"
                    return "Wi-Fi 6", "802.11ax"
                elif rate > 600:
                    if (freq_mhz and freq_mhz >= 5925) or effective_band == "6GHz":
                        return "Wi-Fi 6E", "802.11ax"
                    elif (freq_mhz and freq_mhz < 2500) or effective_band == "2.4GHz":
                        return "Wi-Fi 6", "802.11ax"
                    else:
                        return "Wi-Fi 5", "802.11ac"
                elif rate > 54:
                    if (freq_mhz and freq_mhz >= 5925) or effective_band == "6GHz":
                        return "Wi-Fi 6E", "802.11ax"
                    elif (freq_mhz and freq_mhz < 2500) or effective_band == "2.4GHz":
                        return "Wi-Fi 4", "802.11n"
                    else:
                        if abs(rate - 433.3) < 1.0 or abs(rate - 866.7) < 1.0:
                            return "Wi-Fi 5", "802.11ac"
                        return "Wi-Fi 4", "802.11n"
                elif rate <= 54:
                    if (freq_mhz and freq_mhz >= 4900) or effective_band == "5GHz":
                        return "Wi-Fi 2", "802.11a"
                    elif (freq_mhz and freq_mhz < 2500) or effective_band == "2.4GHz":
                        if rate <= 11.0:
                            return "Wi-Fi 1", "802.11b"
                        else:
                            return "Wi-Fi 3", "802.11g"
            except ValueError:
                pass

    # 3. Fallback based on band and frequency
    if (freq_mhz and freq_mhz >= 5925) or effective_band == "6GHz":
        return "Wi-Fi 6E", "802.11ax"
    if (freq_mhz and freq_mhz >= 4900) or effective_band == "5GHz":
        return "Wi-Fi 5", "802.11ac"
    if (freq_mhz and freq_mhz < 2500) or effective_band == "2.4GHz":
        return "Wi-Fi 4", "802.11n"

    return None, None


def _run_cmd(cmd, timeout=5):
    """Run command and return stdout string, or None on error."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and res.stdout:
            return res.stdout
    except Exception:
        pass
    return None


def get_link_bitrate_and_freq(iface: str):
    """Read bitrate string, frequency and BSSID from `iw dev <iface> link` or station dump."""
    if not shutil.which("iw"):
        return None, None, None

    out = _run_cmd(["iw", "dev", iface, "link"])
    if not out:
        out = _run_cmd(["sudo", "-n", "iw", "dev", iface, "link"])

    freq_mhz = None
    tx_bitrate = None
    rx_bitrate = None
    bssid = None

    if out:
        conn_m = re.search(r"Connected to ([0-9a-fA-F:]{17})", out, re.I)
        if conn_m:
            bssid = conn_m.group(1).lower()
        freq_m = re.search(r"^\s*freq:\s*([\d.]+)", out, re.M)
        if freq_m:
            try:
                freq_mhz = int(float(freq_m.group(1)))
            except ValueError:
                pass
        tx_m = re.search(r"^\s*tx bitrate:\s*(.+)$", out, re.M)
        if tx_m:
            tx_bitrate = tx_m.group(1).strip()
        rx_m = re.search(r"^\s*rx bitrate:\s*(.+)$", out, re.M)
        if rx_m:
            rx_bitrate = rx_m.group(1).strip()

    if not tx_bitrate and not rx_bitrate:
        sd_out = _run_cmd(["iw", "dev", iface, "station", "dump"])
        if not sd_out:
            sd_out = _run_cmd(["sudo", "-n", "iw", "dev", iface, "station", "dump"])
        if sd_out:
            if not bssid:
                b_m = re.search(r"Station\s+([0-9a-fA-F:]{17})", sd_out, re.I)
                if b_m:
                    bssid = b_m.group(1).lower()
            tx_m = re.search(r"^\s*tx bitrate:\s*(.+)$", sd_out, re.M)
            if tx_m:
                tx_bitrate = tx_m.group(1).strip()
            rx_m = re.search(r"^\s*rx bitrate:\s*(.+)$", sd_out, re.M)
            if rx_m:
                rx_bitrate = rx_m.group(1).strip()

    bitrate_str = tx_bitrate or rx_bitrate
    return bitrate_str, freq_mhz, bssid


def get_scan_dump_bssid_map(iface: str) -> dict:
    """Return map of BSSID -> bss block text from `iw dev <iface> scan dump`."""
    if not shutil.which("iw"):
        return {}
    out = _run_cmd(["iw", "dev", iface, "scan", "dump"])
    if not out:
        out = _run_cmd(["sudo", "-n", "iw", "dev", iface, "scan", "dump"])
    if not out:
        return {}
    bssid_map = {}
    for block in re.split(r"(?=BSS\s+[0-9a-fA-F:]{17})", out):
        m = re.match(r"BSS\s+([0-9a-fA-F:]{17})", block, re.I)
        if m:
            bssid_map[m.group(1).lower()] = block
    return bssid_map


def _get_active_wifi_generation(iface: str, freq_mhz=None, band=None) -> tuple:
    bitrate_str, link_freq, bssid = get_link_bitrate_and_freq(iface)
    effective_freq = link_freq or freq_mhz
    effective_band = band or (classify_band(effective_freq) if effective_freq is not None else None)

    if bitrate_str:
        gen, proto = classify_wifi_generation(
            freq_mhz=effective_freq,
            bitrate_str=bitrate_str,
            band=effective_band,
        )
        if gen:
            return gen, proto

    if bssid:
        bss_map = get_scan_dump_bssid_map(iface)
        bss_block = bss_map.get(bssid.lower())
        if bss_block:
            gen, proto = classify_wifi_generation(
                freq_mhz=effective_freq,
                raw_ies=bss_block,
                band=effective_band,
            )
            if gen:
                return gen, proto

    return classify_wifi_generation(freq_mhz=effective_freq, band=effective_band)


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
    even with --show-secrets. `--escape no` is deliberate: nmcli's terse-style output
    (which `-g` also uses) backslash-escapes ':' and '\' by default, and a secret is the
    one value we can't afford to get wrong trying to reverse that -- asking nmcli for
    the raw, unescaped value sidesteps the whole problem. Only the trailing newline is
    stripped, not the field generally, since a password can legitimately have leading
    or trailing whitespace of its own.

    Returns (password, error):
    - (psk, None) when a saved profile with a PSK was found.
    - (None, None) when the profile exists but has no PSK (Open network, 802.1x, etc) —
      not an error, just nothing to reuse.
    - (None, error) when no matching profile exists, or the lookup otherwise failed.
    """
    out, err = _run_nmcli(
        ["-s", "-e", "no", "-g", "802-11-wireless-security.psk", "connection", "show", name],
        timeout=NMCLI_TIMEOUT,
    )
    if err:
        if re.search(r"no such property|unknown property", err, re.IGNORECASE):
            return None, None
        return None, err

    password = (out or "").rstrip("\n")
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


@app.route("/api/saved-password")
def api_saved_password():
    """Look up a saved profile's PSK for `ssid`, so the connect dialog's password field
    can be auto-filled when this SSID is already saved (e.g. from another interface).
    `found=False` covers both "no matching saved profile" and "matched, but nothing
    stored" -- neither is an error, it's just nothing to fill in, and the operator can
    still type a password by hand.
    """
    ssid = (request.args.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"success": False, "error": "ssid is required."}), 400
    password, _ = _saved_psk(ssid)
    return jsonify({"success": True, "found": password is not None, "password": password})


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

    wifi_gen, proto = _get_active_wifi_generation(
        iface, freq_mhz=connected.get("freq_mhz"), band=connected.get("band")
    )

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
        "wifi_generation": wifi_gen,
        "protocol": proto,
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

    bss_map = get_scan_dump_bssid_map(iface)
    networks = []
    for line in (out or "").strip().splitlines():
        parts = _split_terse(line)
        if len(parts) < 7:
            continue
        in_use, ssid, signal, security, chan, freq = parts[:6]
        bssid = ":".join(parts[6:])
        if not ssid:
            continue
        freq_mhz = int(freq.split()[0]) if freq.split() and freq.split()[0].isdigit() else None
        band = classify_band(freq_mhz)
        raw_ies = bss_map.get(bssid.lower()) if bssid else None
        wifi_gen, proto = classify_wifi_generation(freq_mhz=freq_mhz, raw_ies=raw_ies, band=band)
        networks.append({
            "ssid": ssid,
            "bssid": bssid,
            "connected": in_use.strip() == "*",
            "signal": int(signal) if signal.isdigit() else 0,
            "security": classify_security(security),
            "channel": int(chan) if chan.isdigit() else None,
            "band": band,
            "wifi_generation": wifi_gen,
            "protocol": proto,
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

    results = []
    for iface in ifaces:
        ok, err = _connect_iface(ssid, iface, password)
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
