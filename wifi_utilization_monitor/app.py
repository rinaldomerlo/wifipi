#!/usr/bin/env python3
import os
import platform
import re
import shutil
import sys
import socket
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template, request

# Adjust path to import parser.py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from parser import parse_scan_output

app = Flask(__name__)

# Every dashboard tab drives its own client-side auto-refresh timer and calls
# /api/scan independently -- with several browsers open on the same host that
# means several near-simultaneous 'sudo iw scan' invocations against one radio,
# which the radio itself rejects with "device is busy" when they overlap.
# Coalesce concurrent/rapid requests per interface behind a lock + short-lived
# cache so only one real scan is ever in flight, and other callers within the
# cache window share its result instead of racing it. Requires the gunicorn
# service to run as a single worker (threads, not processes) -- see
# deploy/wifi-monitor.service -- since this state is in-process memory.
SCAN_CACHE_TTL_SECONDS = 4
_scan_cache = {}  # interface -> {'ts': float, 'raw': str, 'error': str|None}
_scan_locks = {}  # interface -> threading.Lock
_scan_locks_guard = threading.Lock()  # protects creation of entries in _scan_locks


def _get_scan_lock(interface):
    with _scan_locks_guard:
        lock = _scan_locks.get(interface)
        if lock is None:
            lock = threading.Lock()
            _scan_locks[interface] = lock
        return lock


def get_scan_result(interface):
    """Return a recent scan for `interface`, running a fresh one if needed.

    At most one 'iw scan' runs per interface at a time; concurrent callers
    block on that scan and then reuse its result rather than starting their
    own. Only successful scans (including legitimately-empty ones) are
    cached -- a transient failure shouldn't be pinned for other viewers or
    for the caller's own next retry.
    """
    conn = find_connected_wifi(interface)
    if conn and conn.get('connected'):
        ssid_label = f" '{conn['ssid']}'" if conn.get('ssid') else " (Hidden SSID)"
        bssid_label = f" [{conn['bssid']}]" if conn.get('bssid') else ""
        return None, (
            f"Wireless interface {conn['interface']} is currently connected to WiFi network{ssid_label}{bssid_label}. "
            f"The Pi must not be connected to any WiFi network while running WiFi Utilization Monitor. "
            f"Please disconnect from WiFi before scanning."
        )

    cached = _scan_cache.get(interface)
    if cached and (time.monotonic() - cached['ts']) < SCAN_CACHE_TTL_SECONDS:
        return cached['raw'], cached['error']

    with _get_scan_lock(interface):
        # Re-check: another thread may have just finished scanning while we
        # were waiting on the lock.
        cached = _scan_cache.get(interface)
        if cached and (time.monotonic() - cached['ts']) < SCAN_CACHE_TTL_SECONDS:
            return cached['raw'], cached['error']

        raw, error = run_live_scan(interface)
        if error is None:
            _scan_cache[interface] = {'ts': time.monotonic(), 'raw': raw, 'error': None}
        return raw, error


def _reset_scan_cache():
    """Test helper: clear cached/coalesced scan state between test cases."""
    _scan_cache.clear()
    _scan_locks.clear()

def get_hostname():
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return 'unknown-host'

def get_wireless_interfaces():
    """List wireless interfaces on Linux using 'iw dev' and /proc/net/wireless."""
    interfaces = []
    try:
        # Run 'iw dev' to list wireless devices
        output = subprocess.check_output(['iw', 'dev'], stderr=subprocess.DEVNULL).decode('utf-8', errors='replace')
        for line in output.splitlines():
            line = line.strip()
            if line.startswith('Interface'):
                parts = line.split()
                if len(parts) >= 2:
                    interfaces.append(parts[1])
    except Exception:
        pass
    
    # Check /proc/net/wireless as fallback interface discovery
    if not interfaces and os.path.exists('/proc/net/wireless'):
        try:
            with open('/proc/net/wireless', 'r') as f:
                lines = f.readlines()
                for line in lines[2:]:
                    parts = line.split(':')
                    if len(parts) >= 1:
                        interfaces.append(parts[0].strip())
        except Exception:
            pass

    return interfaces

def get_interface_connection(interface):
    """Check if `interface` is currently connected/associated to a WiFi network.
    
    Runs `iw dev <interface> link`.
    Returns a dict: {'connected': bool, 'ssid': str|None, 'bssid': str|None, 'interface': interface}.
    """
    res = {'connected': False, 'ssid': None, 'bssid': None, 'interface': interface}
    if not shutil.which('iw') or not interface:
        return res
    try:
        output = subprocess.check_output(
            ['iw', 'dev', interface, 'link'],
            stderr=subprocess.DEVNULL,
            timeout=3
        ).decode('utf-8', errors='replace')
    except Exception:
        return res

    match = re.search(r'Connected to ([0-9a-fA-F:]{17})', output, re.IGNORECASE)
    if match:
        res['connected'] = True
        res['bssid'] = match.group(1).lower()
        ssid_match = re.search(r'^\s*SSID:\s*(.+)$', output, re.MULTILINE)
        if ssid_match:
            res['ssid'] = ssid_match.group(1).strip()
    return res

def find_connected_wifi(target_interface=None):
    """Check if the target interface or any wireless interface is currently connected.
    
    Returns the connection dict if connected, or None if no interface is connected.
    """
    if target_interface:
        conn = get_interface_connection(target_interface)
        if conn.get('connected'):
            return conn

    for iface in get_wireless_interfaces():
        if iface != target_interface:
            conn = get_interface_connection(iface)
            if conn.get('connected'):
                return conn
    return None

def disconnect_wifi_interface(interface):
    """Disconnect a wireless interface from any active association.
    
    Tries NetworkManager `nmcli` first (if available) to release the connection,
    and `sudo iw dev <iface> disconnect`.
    Returns (success: bool, message_or_error: str).
    """
    if not interface:
        return False, "No interface specified."

    # In non-Linux environment (e.g. macOS dev) without wireless tools
    if platform.system() != "Linux" and not shutil.which('iw') and not shutil.which('nmcli'):
        _scan_cache.clear()
        return True, f"Simulated disconnect on {interface}."

    errors = []
    
    # 1. Try nmcli if available (managed connection disconnect)
    if shutil.which('nmcli'):
        try:
            cmd = ['nmcli', 'device', 'disconnect', interface]
            if shutil.which('sudo') and os.geteuid() != 0:
                cmd = ['sudo'] + cmd
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode != 0 and res.stderr:
                errors.append(f"nmcli: {res.stderr.strip()}")
        except Exception as e:
            errors.append(f"nmcli error: {e}")

    # 2. Try iw disconnect via sudo
    if shutil.which('iw'):
        try:
            cmd = ['iw', 'dev', interface, 'disconnect']
            if shutil.which('sudo') and os.geteuid() != 0:
                cmd = ['sudo'] + cmd
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode != 0 and res.stderr:
                errors.append(f"iw: {res.stderr.strip()}")
        except Exception as e:
            errors.append(f"iw error: {e}")

    # 3. Try wpa_cli if available as fallback
    if shutil.which('wpa_cli'):
        try:
            cmd = ['wpa_cli', '-i', interface, 'disconnect']
            if shutil.which('sudo') and os.geteuid() != 0:
                cmd = ['sudo'] + cmd
            subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:
            pass

    # Clear cached scan state
    _scan_cache.clear()

    # Verify if interface is still connected
    conn = get_interface_connection(interface)
    if not conn.get('connected'):
        return True, f"Disconnected interface {interface}."
    else:
        err_detail = "; ".join(errors) if errors else "Interface remained connected."
        return False, f"Could not disconnect {interface}: {err_detail}"

def run_live_scan(interface, max_retries=2):
    """Run `sudo iw dev <interface> scan` with retry support for transient timeouts."""
    cmd = ['sudo', 'iw', 'dev', interface, 'scan']
    last_error = None
    
    for attempt in range(max_retries):
        try:
            # Set a timeout of 12 seconds.
            # Run with sudo since iw scan requires root privileges.
            output = subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=12)
            raw = output.decode('utf-8', errors='replace')
            # A zero-exit scan is a completed scan; empty output is a valid result,
            # not an error -- in an isolated environment (e.g. an RF chamber) there
            # may simply be no APs in range. Only failures/timeouts (below) retry.
            return raw, None
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.decode('utf-8', errors='replace') if e.stderr else str(e)
            last_error = f"Command failed: {' '.join(cmd)}\nError: {err_msg}"
        except subprocess.TimeoutExpired:
            last_error = "Scan timed out (12s limit exceeded)"
        except Exception as e:
            last_error = f"Failed to execute scan: {str(e)}"
            
    return None, last_error

def resolve_vendor(bssid):
    """Resolves MAC address OUI prefixes to popular vendors for UI display."""
    oui = bssid.upper().replace(':', '')[:6]
    vendors = {
        '245A4C': 'Ubiquiti Inc.',
        '788A20': 'Ubiquiti Inc.',
        'FCECDA': 'Ubiquiti Inc.',
        'E828C1': 'Ubiquiti Inc.',
        '0418D6': 'Ubiquiti Inc.',
        'D8B377': 'Apple',
        '8CC781': 'Apple',
        'AC3B77': 'Apple',
        '002500': 'Apple',
        'F40F24': 'Apple',
        '00180A': 'Cisco',
        '002A6A': 'Cisco',
        '70695A': 'Cisco',
        'CC1AFA': 'Cisco',
        'C0C522': 'TP-Link',
        'A8A159': 'TP-Link',
        'B04E26': 'TP-Link',
        '50C7BF': 'TP-Link',
        'BC3AEA': 'TP-Link',
        'A42BB0': 'Netgear',
        '288088': 'Netgear',
        '8C3A31': 'Netgear',
        '30469A': 'Netgear',
        '349672': 'Linksys',
        '00226B': 'Linksys',
        'A00460': 'Linksys',
        '00E04C': 'Realtek',
        'D8ECB5': 'Samsung',
        'F8E903': 'Samsung',
        '001EC2': 'Samsung',
        '1008C1': 'Samsung',
        '408D5C': 'Samsung',
        'D0D003': 'Intel',
        '001500': 'Intel',
        'A434D9': 'Intel',
    }
    
    if oui in vendors:
        return vendors[oui]
        
    # Standard fallback hashes for variety if not in OUI dictionary
    hashed_vendor_id = int(oui, 16) % 6 if oui else 0
    generic_vendors = ['Intel', 'Broadcom', 'Qualcomm Atheros', 'TP-Link', 'Netgear', 'Realtek']
    return generic_vendors[hashed_vendor_id]

@app.route('/')
def index():
    return render_template('index.html', hostname=get_hostname())

@app.route('/api/hostname')
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({'hostname': get_hostname()})

@app.route('/api/interfaces')
def api_interfaces():
    return jsonify({
        'interfaces': get_wireless_interfaces()
    })

@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    """Disconnect active WiFi connection on the target or any connected wireless interface."""
    data = request.get_json(silent=True) or {}
    target_interface = data.get('interface')

    if not target_interface:
        conn = find_connected_wifi()
        if conn and conn.get('interface'):
            target_interface = conn['interface']
        else:
            ifaces = get_wireless_interfaces()
            if ifaces:
                target_interface = ifaces[0]

    if not target_interface:
        return jsonify({'success': False, 'error': 'No wireless interface found to disconnect.'}), 200

    ok, msg = disconnect_wifi_interface(target_interface)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 200

    # Also check if another interface is still connected
    other_conn = find_connected_wifi()
    if other_conn and other_conn.get('connected') and other_conn.get('interface') != target_interface:
        ok2, msg2 = disconnect_wifi_interface(other_conn['interface'])
        if not ok2:
            return jsonify({'success': False, 'error': msg2}), 200

    return jsonify({
        'success': True,
        'message': f"Successfully disconnected wireless interface {target_interface}."
    }), 200

@app.route('/api/scan')
def api_scan():
    interface = request.args.get('interface', '')
    
    # Auto-detect interface if none provided
    if not interface:
        ifaces = get_wireless_interfaces()
        if ifaces:
            interface = ifaces[0]
        else:
            return jsonify({
                'success': False, 
                'error': 'No wireless interface detected on the system.'
            }), 400
            
    raw_output, error = get_scan_result(interface)
    if error:
        conn = find_connected_wifi(interface)
        is_conn = bool(conn and conn.get('connected'))
        return jsonify({
            'success': False, 
            'connected': is_conn,
            'interface': conn['interface'] if is_conn else interface,
            'ssid': conn.get('ssid') if is_conn else None,
            'error': f"Scan failed: {error}" if not error.startswith("Wireless interface") and not error.startswith("Interface") else error
        }), 200

    # Empty results are valid (e.g. an RF chamber with no APs in range) -- fall
    # through to a normal success response reporting zero networks rather than
    # surfacing a scary error for what is a legitimately quiet environment.
    records = parse_scan_output(raw_output)

    # Secondary check: if any scanned record was flagged as associated
    associated_records = [r for r in records if r.get('associated')]
    if associated_records:
        assoc = associated_records[0]
        ssid_label = f" '{assoc['ssid']}'" if assoc.get('ssid') else " (Hidden SSID)"
        bssid_label = f" [{assoc['bssid']}]" if assoc.get('bssid') else ""
        return jsonify({
            'success': False,
            'connected': True,
            'error': (
                f"Wireless interface {interface} is associated to WiFi network{ssid_label}{bssid_label}. "
                f"The Pi must not be connected to any WiFi network while running WiFi Utilization Monitor. "
                f"Please disconnect from WiFi before scanning."
            )
        }), 200

    # Summarize stats for dashboard
    total_aps = len(records)
    
    chan_counts_24 = {}
    chan_counts_5 = {}
    chan_counts_6 = {}
    
    for r in records:
        chan = r.get('channel')
        band = r.get('band')
        
        if chan is not None:
            if band == '2.4GHz':
                chan_counts_24[chan] = chan_counts_24.get(chan, 0) + 1
            elif band == '5GHz':
                chan_counts_5[chan] = chan_counts_5.get(chan, 0) + 1
            elif band == '6GHz':
                chan_counts_6[chan] = chan_counts_6.get(chan, 0) + 1

    # Determine cleanest/congested channels
    non_overlap_24 = [1, 6, 11]
    cleanest_24 = min(non_overlap_24, key=lambda c: chan_counts_24.get(c, 0))
    congested_24 = max(chan_counts_24.keys(), key=lambda c: chan_counts_24[c]) if chan_counts_24 else 1
    
    cleanest_5 = 36
    if chan_counts_5:
        cleanest_5 = min(range(36, 165, 4), key=lambda c: chan_counts_5.get(c, 0))
        congested_5 = max(chan_counts_5.keys(), key=lambda c: chan_counts_5[c])
    else:
        congested_5 = None

    cleanest_6 = 1
    if chan_counts_6:
        cleanest_6 = min(chan_counts_6.keys(), key=lambda c: chan_counts_6[c])
        congested_6 = max(chan_counts_6.keys(), key=lambda c: chan_counts_6[c])
    else:
        congested_6 = None

    return jsonify({
        'success': True,
        'records': records,
        'meta': {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'data_source': f"Live scan on {interface}",
            'fallback': False,
            'total_aps': total_aps,
            'cleanest_channel_24': cleanest_24,
            'congested_channel_24': congested_24,
            'cleanest_channel_5': cleanest_5,
            'congested_channel_5': congested_5,
            'cleanest_channel_6': cleanest_6,
            'congested_channel_6': congested_6
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port argument: {sys.argv[1]}. Using default port {port}.", file=sys.stderr)

    app.run(host='0.0.0.0', port=port, debug=True)
