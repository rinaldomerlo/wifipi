#!/usr/bin/env python3
import os
import socket
import sys
import unittest
from unittest.mock import patch, MagicMock

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import network_device_scanner.app as nds_app_module

nds_app = nds_app_module.app

IP_ADDR_STDOUT = "inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
# `ip -o addr show` (used by get_bindable_interfaces) puts one address per line with a
# fixed "<idx>: <ifname> ..." prefix, unlike the plain `ip addr show` fixture above.
IP_ADDR_ONELINE_STDOUT = "2: wlan0    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"

NMAP_XML_STDOUT = """<?xml version="1.0"?>
<nmaprun>
<host><status state="up" reason="arp-response"/>
<address addr="192.168.1.50" addrtype="ipv4"/>
</host>
<host><status state="up" reason="arp-response"/>
<address addr="192.168.1.77" addrtype="ipv4"/>
<address addr="AA:BB:CC:DD:EE:FF" addrtype="mac" vendor="Raspberry Pi Foundation"/>
<hostnames><hostname name="wifipi-2.lan" type="PTR"/></hostnames>
</host>
<host><status state="down"/>
<address addr="192.168.1.99" addrtype="ipv4"/>
</host>
</nmaprun>
"""


def subprocess_side_effect(cmd, **kwargs):
    m = MagicMock()
    m.returncode = 0
    if cmd[0] == "ip":
        m.stdout = IP_ADDR_ONELINE_STDOUT if "-o" in cmd else IP_ADDR_STDOUT
    elif cmd[0] == "sudo":
        m.stdout = NMAP_XML_STDOUT
        m.stderr = ""
    return m


class TestNetworkDeviceScanner(unittest.TestCase):

    def setUp(self):
        nds_app.config['TESTING'] = True
        self.client = nds_app.test_client()
        nds_app_module.last_scan = {
            "devices": [], "count": 0, "cidr": None,
            "bind_interface": None, "timestamp": None, "error": None,
        }

    # -- basic routes --------------------------------------------------

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Device Scanner', response.data)
        self.assertIn(b'Bind Interface', response.data)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_api_hostname_route(self):
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

    # -- XML parsing -----------------------------------------------------

    def test_parse_nmap_hosts_only_returns_up_hosts_with_full_details(self):
        devices = nds_app_module.parse_nmap_hosts(NMAP_XML_STDOUT)
        self.assertEqual(len(devices), 2)

        no_mac = next(d for d in devices if d["ip"] == "192.168.1.50")
        self.assertEqual(no_mac["mac"], "")
        self.assertEqual(no_mac["vendor"], "")
        self.assertEqual(no_mac["hostname"], "")

        with_mac = next(d for d in devices if d["ip"] == "192.168.1.77")
        self.assertEqual(with_mac["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(with_mac["vendor"], "Raspberry Pi Foundation")
        self.assertEqual(with_mac["hostname"], "wifipi-2.lan")

        self.assertNotIn("192.168.1.99", [d["ip"] for d in devices])

    def test_parse_nmap_hosts_handles_malformed_xml(self):
        self.assertEqual(nds_app_module.parse_nmap_hosts("not xml"), [])

    # -- scan_lan ----------------------------------------------------------

    @patch('network_device_scanner.app.shutil.which')
    @patch('network_device_scanner.app.subprocess.run')
    def test_scan_lan_success_flags_self_and_sorts_by_ip(self, mock_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'
        mock_run.side_effect = subprocess_side_effect

        result = nds_app_module.scan_lan(bind_interface="wlan0")

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["cidr"], "192.168.1.0/24")
        self.assertEqual([d["ip"] for d in result["devices"]], ["192.168.1.50", "192.168.1.77"])
        self.assertTrue(result["devices"][0]["is_self"])
        self.assertFalse(result["devices"][1]["is_self"])

        nmap_calls = [c for c in mock_run.call_args_list if c[0][0][0] == 'sudo']
        self.assertEqual(len(nmap_calls), 1)
        self.assertEqual(
            nmap_calls[0][0][0],
            ['sudo', '-n', 'nmap', '-e', 'wlan0', '-sn', '-T4', '-oX', '-', '192.168.1.0/24'],
        )

    @patch('network_device_scanner.app.shutil.which')
    def test_scan_lan_raises_when_nmap_missing(self, mock_which):
        mock_which.return_value = None
        with self.assertRaises(RuntimeError):
            nds_app_module.scan_lan(bind_interface="wlan0")

    @patch('network_device_scanner.app.shutil.which')
    @patch('network_device_scanner.app.subprocess.run')
    def test_scan_lan_invalid_interface_falls_back_to_wlan0(self, mock_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'
        mock_run.side_effect = subprocess_side_effect

        result = nds_app_module.scan_lan(bind_interface="not-a-real-iface")
        self.assertEqual(result["bind_interface"], "wlan0")

    # -- routes --------------------------------------------------------

    @patch('network_device_scanner.app.shutil.which')
    @patch('network_device_scanner.app.subprocess.run')
    def test_api_scan_success(self, mock_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'
        mock_run.side_effect = subprocess_side_effect

        response = self.client.post('/api/scan', json={"bind_interface": "wlan0"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], 2)

    @patch('network_device_scanner.app.shutil.which')
    def test_api_scan_error_when_nmap_missing(self, mock_which):
        mock_which.return_value = None

        response = self.client.post('/api/scan', json={"bind_interface": "wlan0"})
        self.assertEqual(response.status_code, 500)
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn('nmap', data['error'])

    def test_api_devices_before_any_scan(self):
        response = self.client.get('/api/devices')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['devices'], [])
        self.assertIsNone(data['timestamp'])

    @patch('network_device_scanner.app.shutil.which')
    @patch('network_device_scanner.app.subprocess.run')
    def test_api_devices_returns_cached_result_after_scan(self, mock_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'
        mock_run.side_effect = subprocess_side_effect

        self.client.post('/api/scan', json={"bind_interface": "wlan0"})
        response = self.client.get('/api/devices')
        data = response.get_json()
        self.assertEqual(data['count'], 2)
        self.assertIsNotNone(data['timestamp'])

    @patch('network_device_scanner.app.shutil.which')
    @patch('network_device_scanner.app.subprocess.run')
    def test_get_bindable_interfaces_parses_oneline_output(self, mock_run, mock_which):
        mock_which.return_value = '/usr/sbin/ip'
        mock_run.return_value = MagicMock(stdout=(
            "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0\n"
            "3: wlan0    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        ))
        self.assertEqual(nds_app_module.get_bindable_interfaces(), ['eth0', 'wlan0'])

    @patch('network_device_scanner.app.shutil.which', return_value=None)
    def test_get_bindable_interfaces_without_ip_binary(self, mock_which):
        self.assertEqual(nds_app_module.get_bindable_interfaces(), [])

    def test_api_interfaces_route(self):
        response = self.client.get('/api/interfaces')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIn('interfaces', data)


if __name__ == '__main__':
    unittest.main()
