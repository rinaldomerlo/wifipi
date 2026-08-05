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

import wifi_porcupine.app as wp_app_module

wp_app = wp_app_module.app


class TestWifiPorcupine(unittest.TestCase):

    def setUp(self):
        wp_app.config['TESTING'] = True
        self.client = wp_app.test_client()
        wp_app_module.run_state.update({
            "running": False, "wifi_mode": None,
            "enlisted": [], "connected": set(), "config": None,
        })
        wp_app_module.stats.update({
            "reconnects": 0, "errors": 0, "active_interfaces": 0,
        })
        wp_app_module.active_ifaces.clear()
        wp_app_module.stop_event.clear()

    # -- basic routes --------------------------------------------------

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Porcupine', response.data)
        self.assertIn(b'Intensity', response.data)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_index_reattaches_to_running_run_on_load(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('function reattach()', html)
        self.assertIn('reattach();', html)

    def test_api_hostname_route(self):
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

    def test_status_route_defaults(self):
        data = self.client.get('/api/status').get_json()
        self.assertFalse(data['running'])
        self.assertEqual(data['reconnects'], 0)
        self.assertEqual(data['active_interfaces'], 0)

    def test_output_route_cursor_shape(self):
        data = self.client.get('/api/output?since=0').get_json()
        self.assertIn('lines', data)
        self.assertIn('next', data)
        self.assertIn('dropped', data)

    # -- intensity math --------------------------------------------------

    def test_compute_dwell_range_endpoints(self):
        self.assertEqual(wp_app_module.compute_dwell_range(1), (25.0, 45.0))
        self.assertEqual(wp_app_module.compute_dwell_range(10), (2.0, 5.0))

    def test_compute_dwell_range_monotonic(self):
        low1, high1 = wp_app_module.compute_dwell_range(2)
        low2, high2 = wp_app_module.compute_dwell_range(8)
        self.assertLess(low2, low1)
        self.assertLess(high2, high1)

    def test_compute_concurrency(self):
        self.assertEqual(wp_app_module.compute_concurrency(4, 10), 4)
        self.assertEqual(wp_app_module.compute_concurrency(4, 1), 1)
        self.assertEqual(wp_app_module.compute_concurrency(4, 5), 2)
        self.assertEqual(wp_app_module.compute_concurrency(0, 10), 0)

    # -- naming / profile args -------------------------------------------

    def test_profile_name(self):
        self.assertEqual(wp_app_module.profile_name('wlan0'), 'porcupine-wlan0')

    def test_build_profile_add_args_randomizes_mac(self):
        args = wp_app_module.build_profile_add_args('wlan0', 'MyNet', 'secret')
        self.assertIn('802-11-wireless.cloned-mac-address', args)
        i = args.index('802-11-wireless.cloned-mac-address')
        self.assertEqual(args[i + 1], 'random')
        self.assertIn('wifi-sec.psk', args)
        self.assertIn('secret', args)
        self.assertIn('MyNet', args)

    def test_build_profile_add_args_open_network_omits_psk(self):
        args = wp_app_module.build_profile_add_args('wlan1', 'OpenNet', '')
        self.assertNotIn('wifi-sec.psk', args)
        self.assertNotIn('wifi-sec.key-mgmt', args)

    # -- mode detection --------------------------------------------------

    def test_detect_wifi_mode_non_linux(self):
        with patch('wifi_porcupine.app.platform.system', return_value='Darwin'):
            mode, reason = wp_app_module.detect_wifi_mode()
        self.assertIsNone(mode)
        self.assertIn('Linux', reason)

    # -- interface enumeration (mocked subprocess boundary) --------------

    def test_get_wireless_interfaces_parses_iw_dev(self):
        iw_output = b"phy#0\n\tInterface wlan0\n\t\tifindex 3\nphy#1\n\tInterface wlan1\n"
        with patch('wifi_porcupine.app.subprocess.check_output', return_value=iw_output):
            ifaces = wp_app_module.get_wireless_interfaces()
        self.assertEqual(ifaces, ['wlan0', 'wlan1'])

    # -- /api/start validation ------------------------------------------

    def test_start_route_non_linux(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=(None, 'not running on Linux')):
            response = self.client.post('/api/start', json={"interfaces": ["wlan0"], "ssid": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Linux', response.get_json()['error'])

    def test_start_route_already_running(self):
        wp_app_module.run_state['running'] = True
        response = self.client.post('/api/start', json={"interfaces": ["wlan0"], "ssid": "x"})
        self.assertEqual(response.status_code, 409)

    def test_start_route_no_interfaces(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)):
            response = self.client.post('/api/start', json={"interfaces": [], "ssid": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('interface', response.get_json()['error'].lower())

    def test_start_route_no_ssid(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={"interfaces": ["wlan0"], "ssid": ""})
        self.assertEqual(response.status_code, 400)
        self.assertIn('SSID', response.get_json()['error'])

    def test_start_route_undetected_interface(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={"interfaces": ["wlan9"], "ssid": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('wlan9', response.get_json()['error'])

    @patch('wifi_porcupine.app.threading.Thread')
    def test_start_route_valid_spawns_background_thread(self, mock_thread):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0', 'wlan1']):
            response = self.client.post('/api/start', json={
                "interfaces": ["wlan0", "wlan1"],
                "ssid": "MyNet",
                "password": "secret",
                "intensity": 7,
                "duration_minutes": 5,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'starting')
        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args[1]
        self.assertEqual(kwargs['target'], wp_app_module.start_run)
        config = kwargs['args'][0]
        self.assertEqual(config['interfaces'], ['wlan0', 'wlan1'])
        self.assertEqual(config['intensity'], 7)
        self.assertTrue(wp_app_module.run_state['running'])

    def test_stop_route_when_not_running(self):
        response = self.client.post('/api/stop')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'no run in progress')


if __name__ == '__main__':
    unittest.main()
