#!/usr/bin/env python3
import os
import socket
import sys
import unittest
from unittest.mock import patch

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import wifi_connection_manager.app as conn_app_module
conn_app = conn_app_module.app


class TestWifiConnectionManager(unittest.TestCase):

    def setUp(self):
        conn_app.config['TESTING'] = True
        self.client = conn_app.test_client()

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'WIFI', response.data)
        self.assertIn(b'CONNECT', response.data)
        self.assertIn(b'Network Connection Manager', response.data)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        # Every GUI screen must display the host it is served from
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_api_hostname_route(self):
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

    def test_split_terse_handles_escaped_colons(self):
        parts = conn_app_module._split_terse(r"in-use:SSID\:With\:Colons:90:WPA2")
        self.assertEqual(parts, ['in-use', 'SSID:With:Colons', '90', 'WPA2'])

    def test_classify_security(self):
        self.assertEqual(conn_app_module.classify_security(''), 'Open')
        self.assertEqual(conn_app_module.classify_security('--'), 'Open')
        self.assertEqual(conn_app_module.classify_security('WPA2'), 'WPA2')

    def test_classify_band(self):
        self.assertEqual(conn_app_module.classify_band(2437), '2.4GHz')
        self.assertEqual(conn_app_module.classify_band(5180), '5GHz')
        self.assertEqual(conn_app_module.classify_band(6135), '6GHz')
        self.assertIsNone(conn_app_module.classify_band(None))

    def test_friendly_connect_error_maps_bad_password(self):
        msg = conn_app_module.friendly_connect_error(
            "Error: Connection activation failed: Secrets were required, but not provided."
        )
        self.assertEqual(msg, "Incorrect password.")

    def test_friendly_connect_error_falls_back_to_raw(self):
        msg = conn_app_module.friendly_connect_error("some unexpected nmcli error")
        self.assertIn("some unexpected nmcli error", msg)

    @patch('wifi_connection_manager.app.shutil.which')
    def test_run_nmcli_missing_binary(self, mock_which):
        mock_which.return_value = None
        out, err = conn_app_module._run_nmcli(["device", "status"])
        self.assertIsNone(out)
        self.assertIn("not installed", err)

    @patch('wifi_connection_manager.app.get_wireless_interface')
    def test_api_status_no_interface(self, mock_iface):
        mock_iface.return_value = ""
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("No wireless interface", data['error'])

    @patch('wifi_connection_manager.app.get_wireless_interface')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_status_connected(self, mock_run, mock_iface):
        mock_iface.return_value = "wlan0"

        def side_effect(args, timeout=None):
            if args[:3] == ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ"]:
                return "*:HomeNetwork:80:WPA2:44:5220 MHz\n", None
            if args[:3] == ["-t", "-f", "IP4.ADDRESS"]:
                return "IP4.ADDRESS[1]:192.168.1.42/24\n", None
            if args[:3] == ["-t", "-f", "GENERAL.CONNECTION"]:
                return "GENERAL.CONNECTION:HomeNetwork\n", None
            return "", None

        mock_run.side_effect = side_effect
        response = self.client.get('/api/status')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertTrue(data['connected'])
        self.assertEqual(data['ssid'], 'HomeNetwork')
        self.assertEqual(data['ip_address'], '192.168.1.42')
        self.assertEqual(data['band'], '5GHz')

    @patch('wifi_connection_manager.app.get_wireless_interface')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_scan_dedupes_and_sorts_by_signal(self, mock_run, mock_iface):
        mock_iface.return_value = "wlan0"
        mock_run.return_value = (
            " :Weak:20:WPA2:1:2412 MHz:AA:BB:CC:DD:EE:01\n"
            " :Strong:90:--:11:2462 MHz:AA:BB:CC:DD:EE:02\n"
            " :Strong:60:--:11:2462 MHz:AA:BB:CC:DD:EE:03\n",
            None,
        )
        response = self.client.get('/api/scan')
        data = response.get_json()
        self.assertTrue(data['success'])
        ssids = [n['ssid'] for n in data['networks']]
        self.assertEqual(ssids, ['Strong', 'Weak'])
        strong = next(n for n in data['networks'] if n['ssid'] == 'Strong')
        self.assertEqual(strong['signal'], 90)
        self.assertEqual(strong['security'], 'Open')

    @patch('wifi_connection_manager.app.get_wireless_interface')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_scan_keeps_connected_bssid_even_with_weaker_signal(self, mock_run, mock_iface):
        # Mesh/repeater setups can broadcast one SSID from multiple BSSIDs. The
        # connected one must win the de-dup even if a sibling AP reports a
        # stronger signal, or the UI loses track of which network is active.
        mock_iface.return_value = "wlan0"
        mock_run.return_value = (
            "*:MeshNet:40:WPA2:1:2412 MHz:AA:BB:CC:DD:EE:01\n"
            " :MeshNet:90:WPA2:11:2462 MHz:AA:BB:CC:DD:EE:02\n",
            None,
        )
        response = self.client.get('/api/scan')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['networks']), 1)
        mesh = data['networks'][0]
        self.assertTrue(mesh['connected'])
        self.assertEqual(mesh['signal'], 40)

    def test_api_connect_requires_ssid(self):
        response = self.client.post('/api/connect', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("SSID is required", response.get_json()['error'])

    @patch('wifi_connection_manager.app.get_wireless_interface')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_connect_bad_password_is_friendly(self, mock_run, mock_iface):
        mock_iface.return_value = "wlan0"
        mock_run.return_value = (None, "Secrets were required, but not provided.")
        response = self.client.post('/api/connect', json={"ssid": "HomeNetwork", "password": "wrong"})
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertEqual(data['error'], "Incorrect password.")

    @patch('wifi_connection_manager.app.get_wireless_interface')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_disconnect_success(self, mock_run, mock_iface):
        mock_iface.return_value = "wlan0"
        mock_run.return_value = ("", None)
        response = self.client.post('/api/disconnect')
        data = response.get_json()
        self.assertTrue(data['success'])

    def test_api_forget_requires_name(self):
        response = self.client.post('/api/forget', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Connection name is required", response.get_json()['error'])

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_saved_filters_wifi_connections(self, mock_run):
        mock_run.return_value = (
            "HomeNetwork:802-11-wireless:yes\n"
            "eth0-static:802-3-ethernet:yes\n",
            None,
        )
        response = self.client.get('/api/saved')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['saved']), 1)
        self.assertEqual(data['saved'][0]['name'], 'HomeNetwork')


if __name__ == '__main__':
    unittest.main()
