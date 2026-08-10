#!/usr/bin/env python3
import json
import os
import socket
import subprocess
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

    def test_status_route_reports_idle(self):
        wb_app_module.active_sessions = 0
        response = self.client.get('/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['running'])
        self.assertEqual(data['active_sessions'], 0)

    def test_status_route_reports_running(self):
        """active_sessions is the same value /start and /stop gate on."""
        wb_app_module.active_sessions = 3
        try:
            response = self.client.get('/status')
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data['running'])
            self.assertEqual(data['active_sessions'], 3)
        finally:
            wb_app_module.active_sessions = 0

    def test_index_reattaches_to_running_simulation_on_load(self):
        """
        The simulation outlives the page, so a reload must probe /status and
        restore the running UI rather than showing an idle screen.
        """
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('function reattach()', html)
        self.assertIn('reattach();', html)

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
        # Case 1: separate IP and port, explicit intensity of 1 session
        response = self.client.post('/start', json={
            "target_ip": "192.168.1.100",
            "target_port": 80,
            "duration_minutes": 5,
            "intensity": 1,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'started')
        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[0], 1)  # session_id
        self.assertEqual(args[1], "192.168.1.100")
        self.assertEqual(args[2], 80)
        self.assertEqual(args[3], 5)

        # Case 2: IP:Port string in target_ip
        mock_thread.reset_mock()
        response = self.client.post('/start', json={
            "target_ip": "192.168.1.100:5004",
            "duration_minutes": 10,
            "intensity": 1,
        })
        self.assertEqual(response.status_code, 200)
        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[1], "192.168.1.100")
        self.assertEqual(args[2], 5004)

    @patch('web_browsing_simulator.app.threading.Thread')
    def test_start_route_intensity_spawns_one_thread_per_session(self, mock_thread):
        response = self.client.post('/start', json={
            "target_ip": "192.168.1.100",
            "duration_minutes": 5,
            "intensity": 4,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['sessions'], 4)
        self.assertEqual(mock_thread.call_count, 4)
        session_ids = sorted(call[1]['args'][0] for call in mock_thread.call_args_list)
        self.assertEqual(session_ids, [1, 2, 3, 4])

    def test_start_route_invalid_intensity(self):
        response = self.client.post('/start', json={"target_ip": "192.168.1.100", "intensity": 11})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Intensity must be between 1 and 10", response.get_json()['error'])

    def test_stop_route(self):
        response = self.client.post('/stop')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'no test running')

    @patch('web_browsing_simulator.app.shutil.which')
    @patch('web_browsing_simulator.app.subprocess.run')
    def test_scan_for_servers(self, mock_sub_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'

        ip_addr_stdout = "inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        # `ip -o addr show` (used by get_bindable_interfaces) is one address per line
        # with a fixed "<idx>: <ifname> ..." prefix, unlike the plain fixture above.
        ip_addr_oneline_stdout = "2: wlan0    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        nmap_stdout = (
            "# Nmap scan report\n"
            "Host: 192.168.1.100 ()\tPorts: 5004/open/tcp//unknown///\n"
            "Host: 192.168.1.50 ()\tPorts: 5004/open/tcp//unknown///\n"
            "Host: 192.168.1.105 ()\tPorts: 5004/open/tcp//unknown///\n"
        )

        def side_effect(cmd, **kwargs):
            m = MagicMock()
            if cmd[0] == "ip":
                m.stdout = ip_addr_oneline_stdout if "-o" in cmd else ip_addr_stdout
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

    def _scan_side_effect(self, nmap_result):
        """subprocess.run stub for scan_for_servers: real-ish `ip` output, caller's nmap result."""
        ip_addr_stdout = "inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        ip_addr_oneline_stdout = "2: wlan0    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"

        def side_effect(cmd, **kwargs):
            if cmd[0] == "ip":
                return MagicMock(stdout=ip_addr_oneline_stdout if "-o" in cmd else ip_addr_stdout)
            if isinstance(nmap_result, Exception):
                raise nmap_result
            return nmap_result

        return side_effect

    @patch('web_browsing_simulator.app.shutil.which')
    @patch('web_browsing_simulator.app.subprocess.run')
    def test_scan_for_servers_reports_nmap_failure(self, mock_sub_run, mock_which):
        """A failed nmap must raise with its stderr, not be mistaken for an empty result."""
        mock_which.return_value = '/usr/bin/nmap'
        mock_sub_run.side_effect = self._scan_side_effect(
            MagicMock(returncode=1, stdout="", stderr="WARNING: No targets were specified\n")
        )

        with self.assertRaises(RuntimeError) as ctx:
            wb_app_module.scan_for_servers(bind_interface="wlan0")
        self.assertIn("No targets were specified", str(ctx.exception))

    @patch('web_browsing_simulator.app.shutil.which')
    @patch('web_browsing_simulator.app.subprocess.run')
    def test_scan_for_servers_reports_timeout(self, mock_sub_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'
        mock_sub_run.side_effect = self._scan_side_effect(
            subprocess.TimeoutExpired(cmd="nmap", timeout=60)
        )

        with self.assertRaises(RuntimeError) as ctx:
            wb_app_module.scan_for_servers(bind_interface="wlan0")
        self.assertIn("timed out", str(ctx.exception))

    @patch('web_browsing_simulator.app.shutil.which')
    @patch('web_browsing_simulator.app.subprocess.run')
    def test_scan_for_servers_records_scanned_subnet(self, mock_sub_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'
        mock_sub_run.side_effect = self._scan_side_effect(
            MagicMock(returncode=0, stdout="", stderr="")
        )

        meta = {}
        self.assertEqual(wb_app_module.scan_for_servers(bind_interface="wlan0", meta=meta), [])
        self.assertEqual(meta['interface'], 'wlan0')
        self.assertEqual(meta['subnet'], '192.168.1.0/24')
        self.assertEqual(meta['port'], 5004)

    @patch('web_browsing_simulator.app.scan_for_servers')
    def test_scan_route_returns_scan_context(self, mock_scan):
        """An empty result still tells the UI what was actually scanned."""
        def fill(bind_interface, meta=None):
            meta.update({"interface": "eth0", "subnet": "10.0.0.0/24", "port": 5004})
            return []
        mock_scan.side_effect = fill

        response = self.client.post('/scan', json={'bind_interface': 'wlan0'})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['servers'], [])
        self.assertEqual(data['subnet'], '10.0.0.0/24')
        self.assertEqual(data['interface'], 'eth0')

    @patch('web_browsing_simulator.app.scan_for_servers')
    def test_scan_route_returns_json_for_unexpected_error(self, mock_scan):
        """Non-RuntimeError failures must stay JSON so the browser can display them."""
        mock_scan.side_effect = OSError("nmap vanished")

        response = self.client.post('/scan', json={'bind_interface': 'wlan0'})
        self.assertEqual(response.status_code, 500)
        self.assertIn("nmap vanished", response.get_json()['error'])

    @patch('web_browsing_simulator.app.shutil.which')
    @patch('web_browsing_simulator.app.subprocess.run')
    def test_get_bindable_interfaces_parses_oneline_output(self, mock_sub_run, mock_which):
        mock_which.return_value = '/usr/sbin/ip'
        mock_sub_run.return_value = MagicMock(stdout=(
            "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0\n"
            "3: wlan0    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        ))
        self.assertEqual(wb_app_module.get_bindable_interfaces(), ['eth0', 'wlan0'])

    @patch('web_browsing_simulator.app.shutil.which', return_value=None)
    def test_get_bindable_interfaces_without_ip_binary(self, mock_which):
        self.assertEqual(wb_app_module.get_bindable_interfaces(), [])

    def test_interfaces_route(self):
        response = self.client.get('/interfaces')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIn('interfaces', data)


if __name__ == '__main__':
    unittest.main()
