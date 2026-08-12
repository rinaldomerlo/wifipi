import importlib
import os
import socket
import sys
import threading
import unittest
from unittest.mock import patch

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']

import wifi_utilization_monitor.app as wifi_app_module
wifi_app = wifi_app_module.app

class TestWifiUtilizationMonitor(unittest.TestCase):
    def setUp(self):
        wifi_app.config['TESTING'] = True
        self.client = wifi_app.test_client()
        # Scan coalescing state is module-level (shared across requests by
        # design); clear it so tests don't leak cached scans into each other.
        wifi_app_module._reset_scan_cache()

    def test_index_route_renders_embedded_css(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        # Verify index html contains embedded CSS styles for resilient rendering
        self.assertIn('<style>', html)
        self.assertIn('--bg-dark:', html)
        self.assertIn('--glass-bg:', html)
        self.assertIn('WIFIMON', html)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        # Every GUI screen must display the host it is served from
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_index_route_has_reboot_link(self):
        # wifimon has no reboot privileges of its own (sudoers only grants
        # `iw`) -- the header button must link to the dedicated reboot_manager
        # app rather than reimplementing any reboot logic here.
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('fa-power-off', html)
        self.assertIn('../reboot/', html)

    def test_api_hostname_route(self):
        # Consumed by the static landing page, which has no backend of its own
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

    def test_api_interfaces_route(self):
        response = self.client.get('/api/interfaces')
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertIn('interfaces', json_data)
        self.assertIsInstance(json_data['interfaces'], list)

    def test_api_scan_route_structure(self):
        response = self.client.get('/api/scan?interface=wlan0')
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertIn('success', json_data)

    def test_api_scan_empty_is_success_not_error(self):
        # In an isolated environment (e.g. an RF chamber) a scan can legitimately
        # find no APs. That is a successful, zero-network result -- not an error.
        with patch('wifi_utilization_monitor.app.run_live_scan', return_value=('', None)):
            response = self.client.get('/api/scan?interface=wlan0')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['records'], [])
        self.assertEqual(data['meta']['total_aps'], 0)

    def test_api_scan_command_failure_still_errors(self):
        # A genuine scan failure (interface down, permission denied, timeout) must
        # still surface as an error -- only *empty* results are treated as valid.
        with patch('wifi_utilization_monitor.app.run_live_scan',
                   return_value=(None, 'Command failed: iw dev wlan0 scan')):
            response = self.client.get('/api/scan?interface=wlan0')
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn('Scan failed', data['error'])

    def test_concurrent_scans_coalesce_to_one_iw_call(self):
        # Several dashboard viewers polling at once must never race each other
        # into 'device is busy' -- only one real scan should run.
        call_count = {'n': 0}

        def slow_scan(interface, max_retries=2):
            call_count['n'] += 1
            threading.Event().wait(0.15)  # simulate a real scan taking a moment
            return ('', None)

        with patch('wifi_utilization_monitor.app.run_live_scan', side_effect=slow_scan):
            threads = [
                threading.Thread(target=lambda: self.client.get('/api/scan?interface=wlan0'))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(call_count['n'], 1)

    def test_scan_cache_expires_after_ttl(self):
        # A stale cache shouldn't be served forever -- once it ages out, the
        # next request must trigger a fresh scan.
        with patch('wifi_utilization_monitor.app.run_live_scan', return_value=('', None)) as mock_scan:
            self.client.get('/api/scan?interface=wlan0')
            self.assertEqual(mock_scan.call_count, 1)

            self.client.get('/api/scan?interface=wlan0')
            self.assertEqual(mock_scan.call_count, 1, "second request within the TTL should reuse the cache")

            wifi_app_module._scan_cache['wlan0']['ts'] -= wifi_app_module.SCAN_CACHE_TTL_SECONDS + 1
            self.client.get('/api/scan?interface=wlan0')
            self.assertEqual(mock_scan.call_count, 2, "request after the TTL should re-scan")

    def test_failed_scans_are_not_cached(self):
        # A transient failure shouldn't be pinned in the cache for other
        # viewers, or block the caller's own next retry.
        with patch('wifi_utilization_monitor.app.run_live_scan',
                   return_value=(None, 'Command failed: iw dev wlan0 scan')) as mock_scan:
            self.client.get('/api/scan?interface=wlan0')
            self.client.get('/api/scan?interface=wlan0')

        self.assertEqual(mock_scan.call_count, 2)

    def test_different_interfaces_scan_independently(self):
        with patch('wifi_utilization_monitor.app.run_live_scan', return_value=('', None)) as mock_scan:
            self.client.get('/api/scan?interface=wlan0')
            self.client.get('/api/scan?interface=wlan1')

        self.assertEqual(mock_scan.call_count, 2)


if __name__ == '__main__':
    unittest.main()
