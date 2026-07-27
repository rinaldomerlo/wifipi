#!/usr/bin/env python3
import re
import json
from datetime import datetime, timezone

BSS_HEADER_RE = re.compile(
    r'^BSS (?P<bssid>[0-9a-fA-F:]{17})\(on (?P<interface>\S+?)\)'
)

RAW_IE_SECTIONS = (
    'HT capabilities', 'HT operation', 'VHT capabilities', 'VHT operation',
    'HE capabilities', 'HE operation', 'RSN', 'WPA', 'WPS', 'WMM',
    'Extended capabilities', 'RM enabled capabilities',
    'Overlapping BSS scan params', 'Supported operating classes',
    'Extended supported rates', 'Supported rates', 'Country',
    'AP Channel Report', 'Transmit Power Envelope', 'TPC report',
    'Power constraint', 'MESH Configuration', 'MESH ID',
)


def freq_to_channel(freq_mhz):
    """Derive (channel, band) from a center frequency in MHz."""
    freq = int(round(freq_mhz))
    if freq == 2484:
        return 14, '2.4GHz'
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5, '2.4GHz'
    if 5955 <= freq <= 7115:
        return (freq - 5950) // 5, '6GHz'
    if 4900 <= freq <= 5895:
        return (freq - 5000) // 5, '5GHz'
    return None, None


def split_bss_blocks(text):
    """Yield (header_line, [body_lines]) for each BSS entry in the scan dump."""
    lines = text.splitlines()
    block = None
    for line in lines:
        if line.startswith('BSS '):
            if block is not None:
                yield block
            block = (line, [])
        elif block is not None:
            block[1].append(line)
    if block is not None:
        yield block


def top_level_sections(body_lines):
    """
    Split a BSS block's body into top-level sections (single-tab-indented
    headers) mapped to their raw text (header + nested lines), preserving
    order of first occurrence. Sections that repeat keep only the first.
    """
    sections = {}
    current_name = None
    current_lines = []

    def flush():
        if current_name is not None and current_name not in sections:
            sections[current_name] = '\n'.join(current_lines)

    for line in body_lines:
        if line.startswith('\t') and not line.startswith('\t\t') and not line.startswith('\t '):
            # New top-level section, e.g. "\tHT capabilities:" or "\tRSN:\t * Version: 1"
            flush()
            name = line[1:].split(':', 1)[0].strip()
            current_name = name
            current_lines = [line]
        elif current_name is not None:
            current_lines.append(line)
    flush()
    return sections


def parse_channel_width(sections):
    """Look for an explicit channel width in VHT/HE operation, else HT operation, else None."""
    for key in ('VHT operation', 'HE operation'):
        text = sections.get(key)
        if text:
            m = re.search(r'channel width:.*\((\d+)\s*MHz\)', text)
            if m:
                return int(m.group(1))
    ht_op = sections.get('HT operation')
    if ht_op:
        m = re.search(r'STA channel width:\s*(\d+)\s*MHz', ht_op)
        if m:
            return int(m.group(1))
    return None


def parse_security(sections):
    """Build a short human-readable security summary from RSN/WPA sections."""
    rsn = sections.get('RSN')
    wpa = sections.get('WPA')
    if not rsn and not wpa:
        return 'OPEN'

    suites = set()
    for text in (rsn, wpa):
        if not text:
            continue
        m = re.search(r'Authentication suites:\s*(.+)', text)
        if m:
            for s in m.group(1).split():
                if 'SAE' in s:
                    suites.add('SAE')
                elif 'PSK' in s:
                    suites.add('PSK')
                elif '802.1X' in s:
                    suites.add('802.1X')

    labels = []
    if rsn and wpa:
        labels.append('WPA/WPA2')
    elif rsn:
        labels.append('WPA3' if 'SAE' in suites and 'PSK' not in suites else 'WPA2')
        if 'SAE' in suites and 'PSK' in suites:
            labels[-1] = 'WPA2/WPA3'
    elif wpa:
        labels.append('WPA')

    if suites:
        labels.append('/'.join(sorted(suites)))
    return '-'.join(labels) if labels else 'ENCRYPTED'


def parse_bss_block(header_line, body_lines):
    m = BSS_HEADER_RE.match(header_line)
    if not m:
        return None
    record = {
        'bssid': m.group('bssid').lower(),
        'interface': m.group('interface'),
    }

    body_text = '\n'.join(body_lines)

    freq_m = re.search(r'^\tfreq:\s*([\d.]+)', body_text, re.MULTILINE)
    record['freq_mhz'] = float(freq_m.group(1)) if freq_m else None

    signal_m = re.search(r'^\tsignal:\s*(-?[\d.]+)\s*dBm', body_text, re.MULTILINE)
    record['signal_dbm'] = float(signal_m.group(1)) if signal_m else None

    beacon_m = re.search(r'^\tbeacon interval:\s*(\d+)\s*TUs', body_text, re.MULTILINE)
    record['beacon_interval_tu'] = int(beacon_m.group(1)) if beacon_m else None

    cap_m = re.search(r'^\tcapability:\s*(.+)', body_text, re.MULTILINE)
    record['capability'] = cap_m.group(1).strip() if cap_m else None

    ssid_m = re.search(r'^\tSSID:\s*(.*)', body_text, re.MULTILINE)
    ssid = ssid_m.group(1).strip() if ssid_m else ''
    invalid_prefixes = (
        'supported rates', 'extended supported rates', 'ds parameter set',
        'country', 'rsn', 'wpa', 'wps', 'wmm', 'ht capabilities',
        'vht capabilities', 'he capabilities', 'capability', 'signal',
        'freq', 'beacon interval', 'last seen'
    )
    if not ssid or any(ssid.lower().startswith(p) for p in invalid_prefixes):
        ssid = None
    record['ssid'] = ssid

    last_seen_m = re.search(r'^\tlast seen:\s*(\d+)\s*ms ago', body_text, re.MULTILINE)
    record['last_seen_ms_ago'] = int(last_seen_m.group(1)) if last_seen_m else None

    sections = top_level_sections(body_lines)

    # BSS Load IE
    bss_load = sections.get('BSS Load')
    record['station_count'] = None
    record['channel_utilisation'] = None
    record['admission_capacity'] = None
    if bss_load:
        sc_m = re.search(r'station count:\s*(\d+)', bss_load)
        if sc_m:
            record['station_count'] = int(sc_m.group(1))
        cu_m = re.search(r'channel utilisation:\s*(\d+)/255', bss_load)
        if cu_m:
            record['channel_utilisation'] = int(cu_m.group(1))
        ac_m = re.search(r'available admission capacity:\s*(\d+)', bss_load)
        if ac_m:
            record['admission_capacity'] = int(ac_m.group(1))

    record['channel_width_mhz'] = parse_channel_width(sections) or 20
    record['security'] = parse_security(sections)

    channel = None
    if record['freq_mhz'] is not None:
        channel, band = freq_to_channel(record['freq_mhz'])
    else:
        band = None

    # Fall back to the DS Parameter set (2.4GHz only) if frequency-derived lookup failed.
    if channel is None:
        ds_m = re.search(r'^\tDS Parameter set:\s*channel\s*(\d+)', body_text, re.MULTILINE)
        if ds_m:
            channel = int(ds_m.group(1))
    record['channel'] = channel
    record['band'] = band

    raw_ies = {name: sections[name] for name in RAW_IE_SECTIONS if name in sections}
    record['raw_ies'] = raw_ies

    return record


def parse_scan_output(text):
    records = []
    for header_line, body_lines in split_bss_blocks(text):
        rec = parse_bss_block(header_line, body_lines)
        if rec is not None:
            records.append(rec)
    return records
