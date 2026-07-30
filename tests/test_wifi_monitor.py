import importlib
import os
import socket
import sys
import unittest

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

if __name__ == '__main__':
    unittest.main()
