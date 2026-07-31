#!/usr/bin/env python3
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']
if 'content_gen' in sys.modules:
    del sys.modules['content_gen']

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import web_browsing_simulator.app as wb_app_module
import content_gen as content_gen_module  # same module app.py imports (its dir is on sys.path)

wb_app = wb_app_module.app


class TestWebBrowsingSimulator(unittest.TestCase):

    def setUp(self):
        wb_app.config['TESTING'] = True
        self.client = wb_app.test_client()

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Web Browsing', response.data)
        self.assertIn(b'Target IP Address', response.data)

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

    def test_generate_corpus_matches_files_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = content_gen_module.generate_corpus(tmpdir)
            pages = manifest['pages']

            self.assertGreaterEqual(len(pages), content_gen_module.PAGE_COUNT_RANGE[0])
            self.assertLessEqual(len(pages), content_gen_module.PAGE_COUNT_RANGE[1])

            for page_id, page in pages.items():
                html_path = os.path.join(tmpdir, page['html_path'])
                self.assertEqual(os.path.getsize(html_path), page['html_size'])

                asset_count = len(page['assets'])
                self.assertGreaterEqual(asset_count, content_gen_module.ASSET_COUNT_RANGE[0])
                self.assertLessEqual(asset_count, content_gen_module.ASSET_COUNT_RANGE[1])

                for asset in page['assets']:
                    asset_path = os.path.join(tmpdir, asset['path'])
                    self.assertEqual(os.path.getsize(asset_path), asset['size'])
                    ext = asset_path.rsplit('.', 1)[-1]
                    expected_ext = {
                        'text/css': 'css',
                        'application/javascript': 'js',
                        'image/jpeg': 'jpg',
                        'image/png': 'png',
                    }[asset['content_type']]
                    self.assertEqual(ext, expected_ext)

    def test_ensure_content_corpus_reuses_existing_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = content_gen_module.ensure_content_corpus(tmpdir)
            second = content_gen_module.ensure_content_corpus(tmpdir)
            self.assertEqual(first, second)

    def test_content_manifest_route(self):
        response = self.client.get('/content/manifest.json')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn('pages', data)
        self.assertGreater(len(data['pages']), 0)

    def test_content_page_and_asset_routes(self):
        manifest = self.client.get('/content/manifest.json').get_json()
        page_id, page = next(iter(manifest['pages'].items()))

        html_response = self.client.get('/' + page['html_path'].replace('pages/', 'content/pages/', 1))
        self.assertEqual(html_response.status_code, 200)
        self.assertEqual(len(html_response.data), page['html_size'])

        if page['assets']:
            asset = page['assets'][0]
            asset_response = self.client.get('/' + asset['path'].replace('assets/', 'content/assets/', 1))
            self.assertEqual(asset_response.status_code, 200)
            self.assertEqual(len(asset_response.data), asset['size'])
            self.assertEqual(asset_response.content_type, asset['content_type'])

    def test_content_routes_404_for_unknown_files(self):
        self.assertEqual(self.client.get('/content/pages/does-not-exist.html').status_code, 404)
        self.assertEqual(self.client.get('/content/assets/nope/asset-0.png').status_code, 404)

    def test_start_route_invalid_ip(self):
        response = self.client.post('/start', json={"target_ip": "invalid-ip"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid target IP address", response.get_json()['error'])

    def test_start_route_invalid_port(self):
        response = self.client.post('/start', json={"target_ip": "192.168.1.100", "target_port": 999999})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Target port must be between 1 and 65535", response.get_json()['error'])

    @patch('web_browsing_simulator.app.threading.Thread')
    def test_start_route_valid_and_ip_port_parsing(self, mock_thread):
        # Case 1: separate IP and port
        response = self.client.post('/start', json={
            "target_ip": "192.168.1.100",
            "target_port": 80,
            "duration_minutes": 5,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'started')
        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[0], "192.168.1.100")
        self.assertEqual(args[1], 80)
        self.assertEqual(args[2], 5)

        # Case 2: IP:Port string in target_ip
        mock_thread.reset_mock()
        response = self.client.post('/start', json={
            "target_ip": "192.168.1.100:5004",
            "duration_minutes": 10
        })
        self.assertEqual(response.status_code, 200)
        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[0], "192.168.1.100")
        self.assertEqual(args[1], 5004)

    def test_stop_route(self):
        response = self.client.post('/stop')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'no test running')

    @patch('web_browsing_simulator.app.shutil.which')
    @patch('web_browsing_simulator.app.subprocess.run')
    def test_scan_for_servers(self, mock_sub_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'

        ip_addr_stdout = "inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        nmap_stdout = (
            "# Nmap scan report\n"
            "Host: 192.168.1.100 ()\tPorts: 5004/open/tcp//unknown///\n"
            "Host: 192.168.1.50 ()\tPorts: 5004/open/tcp//unknown///\n"
            "Host: 192.168.1.105 ()\tPorts: 5004/open/tcp//unknown///\n"
        )

        def side_effect(cmd, **kwargs):
            m = MagicMock()
            if cmd[0] == "ip":
                m.stdout = ip_addr_stdout
            elif cmd[0] == "nmap":
                m.stdout = nmap_stdout
            return m

        mock_sub_run.side_effect = side_effect

        servers = wb_app_module.scan_for_servers(bind_interface="wlan0")
        self.assertEqual(len(servers), 2)
        self.assertIn({"ip": "192.168.1.100", "port": 5004}, servers)
        self.assertIn({"ip": "192.168.1.105", "port": 5004}, servers)
        self.assertNotIn({"ip": "192.168.1.50", "port": 5004}, servers)

        nmap_call_args = [call for call in mock_sub_run.call_args_list if call[0][0][0] == 'nmap']
        self.assertTrue(len(nmap_call_args) > 0)
        self.assertIn('-e', nmap_call_args[0][0][0])
        self.assertIn('wlan0', nmap_call_args[0][0][0])
        self.assertIn('5004', nmap_call_args[0][0][0])


if __name__ == '__main__':
    unittest.main()
