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

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    def test_api_status_no_interface(self, mock_ifaces):
        mock_ifaces.return_value = []
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("No wireless interface", data['error'])

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_status_connected(self, mock_run, mock_ifaces):
        mock_ifaces.return_value = ["wlan0"]

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
        self.assertEqual(len(data['interfaces']), 1)
        ifc = data['interfaces'][0]
        self.assertTrue(ifc['connected'])
        self.assertEqual(ifc['ssid'], 'HomeNetwork')
        self.assertEqual(ifc['ip_address'], '192.168.1.42')
        self.assertEqual(ifc['band'], '5GHz')

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_status_multiple_interfaces(self, mock_run, mock_ifaces):
        # Two wifi radios: wlan0 associated, wlan1 idle. /api/status must report both,
        # each scoped to its own `device wifi list ifname <iface>` query.
        mock_ifaces.return_value = ["wlan0", "wlan1"]

        def side_effect(args, timeout=None):
            if args[:3] == ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ"]:
                if "wlan0" in args:
                    return "*:HomeNetwork:80:WPA2:44:5220 MHz\n", None
                return "", None  # wlan1: nothing in-use
            if args[:3] == ["-t", "-f", "IP4.ADDRESS"]:
                return "IP4.ADDRESS[1]:192.168.1.42/24\n", None
            if args[:3] == ["-t", "-f", "GENERAL.CONNECTION"]:
                return "GENERAL.CONNECTION:HomeNetwork\n", None
            return "", None

        mock_run.side_effect = side_effect
        response = self.client.get('/api/status')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['interfaces']), 2)
        by_iface = {ifc['interface']: ifc for ifc in data['interfaces']}
        self.assertTrue(by_iface['wlan0']['connected'])
        self.assertEqual(by_iface['wlan0']['ssid'], 'HomeNetwork')
        self.assertFalse(by_iface['wlan1']['connected'])

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_scan_dedupes_and_sorts_by_signal(self, mock_run, mock_ifaces):
        mock_ifaces.return_value = ["wlan0"]
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

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_scan_keeps_connected_bssid_even_with_weaker_signal(self, mock_run, mock_ifaces):
        # Mesh/repeater setups can broadcast one SSID from multiple BSSIDs. The
        # connected one must win the de-dup even if a sibling AP reports a
        # stronger signal, or the UI loses track of which network is active.
        mock_ifaces.return_value = ["wlan0"]
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

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_scan_scopes_to_requested_interface(self, mock_run, mock_ifaces):
        mock_ifaces.return_value = ["wlan0", "wlan1"]
        mock_run.return_value = ("", None)
        response = self.client.get('/api/scan?interface=wlan1')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['interface'], 'wlan1')
        args = mock_run.call_args[0][0]
        self.assertIn('ifname', args)
        self.assertIn('wlan1', args)

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    def test_api_scan_unknown_interface_is_rejected(self, mock_ifaces):
        mock_ifaces.return_value = ["wlan0"]
        response = self.client.get('/api/scan?interface=wlan9')
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown interface", response.get_json()['error'])

    def test_api_connect_requires_ssid(self):
        response = self.client.post('/api/connect', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("SSID is required", response.get_json()['error'])

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_connect_bad_password_is_friendly(self, mock_run, mock_ifaces):
        mock_ifaces.return_value = ["wlan0"]
        mock_run.return_value = (None, "Secrets were required, but not provided.")
        response = self.client.post('/api/connect', json={"ssid": "HomeNetwork", "password": "wrong"})
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertEqual(data['error'], "Incorrect password.")

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    def test_api_connect_unknown_interface_is_rejected(self, mock_ifaces):
        mock_ifaces.return_value = ["wlan0"]
        response = self.client.post('/api/connect', json={"ssid": "HomeNetwork", "interface": "wlan9"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown interface", response.get_json()['error'])

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_connect_threads_requested_interface(self, mock_run, mock_ifaces):
        mock_ifaces.return_value = ["wlan0", "wlan1"]
        mock_run.return_value = ("", None)
        response = self.client.post('/api/connect', json={"ssid": "HomeNetwork", "interface": "wlan1"})
        data = response.get_json()
        self.assertTrue(data['success'])
        args = mock_run.call_args[0][0]
        self.assertIn('ifname', args)
        self.assertIn('wlan1', args)

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_disconnect_success(self, mock_run, mock_ifaces):
        mock_ifaces.return_value = ["wlan0"]
        mock_run.return_value = ("", None)
        response = self.client.post('/api/disconnect')
        data = response.get_json()
        self.assertTrue(data['success'])

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_disconnect_targets_requested_interface(self, mock_run, mock_ifaces):
        mock_ifaces.return_value = ["wlan0", "wlan1"]
        mock_run.return_value = ("", None)
        response = self.client.post('/api/disconnect', json={"interface": "wlan1"})
        data = response.get_json()
        self.assertTrue(data['success'])
        mock_run.assert_called_once_with(["device", "disconnect", "wlan1"], timeout=conn_app_module.CONNECT_TIMEOUT)

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    def test_api_interfaces_lists_wifi_devices(self, mock_ifaces):
        mock_ifaces.return_value = ["wlan0", "wlan1"]
        response = self.client.get('/api/interfaces')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['interfaces'], ["wlan0", "wlan1"])

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_get_wireless_interfaces_parses_device_status(self, mock_run):
        mock_run.return_value = ("wlan0:wifi\nwlan1:wifi\neth0:ethernet\n", None)
        self.assertEqual(conn_app_module.get_wireless_interfaces(), ["wlan0", "wlan1"])

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_connect_all_only_idle_reports_mixed_results(self, mock_run, mock_ifaces):
        # wlan0 is already connected (excluded by only_idle); wlan1 and wlan2 are idle,
        # one succeeds and one fails — the response must reflect both outcomes.
        mock_ifaces.return_value = ["wlan0", "wlan1", "wlan2"]

        def side_effect(args, timeout=None):
            if args[:3] == ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ"]:
                if "wlan0" in args:
                    return "*:HomeNetwork:80:WPA2:44:5220 MHz\n", None
                return "", None
            if args[:3] == ["-t", "-f", "IP4.ADDRESS"]:
                return "", None
            if args[:3] == ["-t", "-f", "GENERAL.CONNECTION"]:
                return "", None
            if args[:3] == ["device", "wifi", "connect"]:
                if "wlan1" in args:
                    return "", None
                if "wlan2" in args:
                    return None, "Secrets were required, but not provided."
            return "", None

        mock_run.side_effect = side_effect
        response = self.client.post('/api/connect-all', json={"ssid": "HomeNetwork"})
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['connected'], 1)
        self.assertEqual(data['failed'], 1)
        results_by_iface = {r['interface']: r for r in data['results']}
        self.assertEqual(set(results_by_iface.keys()), {"wlan1", "wlan2"})
        self.assertTrue(results_by_iface['wlan1']['ok'])
        self.assertFalse(results_by_iface['wlan2']['ok'])
        self.assertEqual(results_by_iface['wlan2']['error'], "Incorrect password.")

    def test_api_connect_all_requires_ssid(self):
        response = self.client.post('/api/connect-all', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("SSID is required", response.get_json()['error'])

    @patch('wifi_connection_manager.app.get_wireless_interfaces')
    def test_api_connect_all_no_interface(self, mock_ifaces):
        mock_ifaces.return_value = []
        response = self.client.post('/api/connect-all', json={"ssid": "HomeNetwork"})
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("No wireless interface", data['error'])

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

    def test_api_autoconnect_requires_name(self):
        response = self.client.post('/api/autoconnect', json={"enabled": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Connection name is required", response.get_json()['error'])

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_autoconnect_enable(self, mock_run):
        mock_run.return_value = ("", None)
        response = self.client.post('/api/autoconnect', json={"name": "HomeNetwork", "enabled": True})
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIn("enabled", data['message'])
        mock_run.assert_called_once_with(["connection", "modify", "HomeNetwork", "autoconnect", "yes"], timeout=conn_app_module.NMCLI_TIMEOUT)

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_autoconnect_disable(self, mock_run):
        mock_run.return_value = ("", None)
        response = self.client.post('/api/autoconnect', json={"name": "HomeNetwork", "enabled": False})
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIn("disabled", data['message'])
        mock_run.assert_called_once_with(["connection", "modify", "HomeNetwork", "autoconnect", "no"], timeout=conn_app_module.NMCLI_TIMEOUT)

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_autoconnect_nmcli_error(self, mock_run):
        mock_run.return_value = (None, "unknown connection 'Ghost'")
        response = self.client.post('/api/autoconnect', json={"name": "Ghost", "enabled": True})
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("Failed to update auto-connect", data['error'])

    def test_api_reveal_requires_name(self):
        response = self.client.post('/api/reveal', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Connection name is required", response.get_json()['error'])

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_reveal_returns_password(self, mock_run):
        mock_run.return_value = ("correcthorsebatterystaple\n", None)
        response = self.client.post('/api/reveal', json={"name": "HomeNetwork"})
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['password'], 'correcthorsebatterystaple')
        mock_run.assert_called_once_with(
            ["-s", "-g", "802-11-wireless-security.psk", "connection", "show", "HomeNetwork"],
            timeout=conn_app_module.NMCLI_TIMEOUT,
        )

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_reveal_unescapes_colons(self, mock_run):
        mock_run.return_value = (r"pass\:with\:colons" + "\n", None)
        response = self.client.post('/api/reveal', json={"name": "HomeNetwork"})
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['password'], 'pass:with:colons')

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_reveal_open_network_returns_null_password(self, mock_run):
        mock_run.return_value = ("", None)
        response = self.client.post('/api/reveal', json={"name": "Airport_Free_WiFi"})
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIsNone(data['password'])

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_reveal_no_such_property_treated_as_no_password(self, mock_run):
        mock_run.return_value = (None, "Error: 802-11-wireless-security.psk: no such property.")
        response = self.client.post('/api/reveal', json={"name": "Office-Lab-VLAN12"})
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIsNone(data['password'])

    @patch('wifi_connection_manager.app._run_nmcli')
    def test_api_reveal_nmcli_error(self, mock_run):
        mock_run.return_value = (None, "Error: Ghost - no such connection profile.")
        response = self.client.post('/api/reveal', json={"name": "Ghost"})
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("Failed to reveal password", data['error'])


if __name__ == '__main__':
    unittest.main()
