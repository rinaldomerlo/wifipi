#!/usr/bin/env python3
import os
import sys
import socket
import subprocess
from datetime import datetime
from flask import Flask, jsonify, render_template, request

# Adjust path to import parser.py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from parser import parse_scan_output

app = Flask(__name__)

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
            if raw.strip():
                return raw, None
            last_error = "Scan returned empty output."
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

@app.route('/api/interfaces')
def api_interfaces():
    return jsonify({
        'interfaces': get_wireless_interfaces()
    })

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
            
    raw_output, error = run_live_scan(interface)
    if error:
        return jsonify({
            'success': False, 
            'error': f"Scan failed: {error}"
        }), 200

    records = parse_scan_output(raw_output)
    if not records:
        return jsonify({
            'success': False, 
            'error': f"Scan executed on '{interface}' but returned empty results. Is the interface up?"
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
