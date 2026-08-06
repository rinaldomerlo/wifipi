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

import iperf_server_manager.app as sm_app_module

app = sm_app_module.app
parse_port_from_args = sm_app_module.parse_port_from_args
get_iperf3_run_as_ids = sm_app_module.get_iperf3_run_as_ids


class TestIPerfServerManager(unittest.TestCase):

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_parse_port_from_args(self):
        self.assertEqual(parse_port_from_args("/usr/bin/iperf3 --server --interval 0"), 5201)
        self.assertEqual(parse_port_from_args("/usr/bin/iperf3 --server -p 5202"), 5202)
        self.assertEqual(parse_port_from_args("/usr/bin/iperf3 --server --port 5203"), 5203)
        self.assertEqual(parse_port_from_args("/usr/bin/iperf3 -s --port=5204"), 5204)
        self.assertEqual(parse_port_from_args("iperf3 -s -p 5205 -i 1"), 5205)

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'iPerf3 Server Manager', response.data)

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

    def test_list_servers_route(self):
        response = self.client.get('/api/servers')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIn('servers', data)
        self.assertIn('services', data)
        self.assertIsInstance(data['servers'], list)

    def test_check_ports_route(self):
        response = self.client.get('/api/ports/check?start_port=5201&end_port=5205')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['ports']), 5)
        self.assertEqual(data['ports'][0]['port'], 5201)

    def test_start_server_invalid_port(self):
        response = self.client.post('/api/servers/start', json={"port": -1})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("between 1 and 65535", data['error'])

        response = self.client.post('/api/servers/start', json={"port": "invalid"})
        self.assertEqual(response.status_code, 400)

    def test_stop_server_missing_params(self):
        response = self.client.post('/api/servers/stop', json={})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("PID, Port, or Unit is required", data['error'])

    @patch('iperf_server_manager.app.get_process_cmdline')
    def test_stop_server_non_iperf_pid(self, mock_cmdline):
        mock_cmdline.return_value = "python3 /some/other/script.py"
        response = self.client.post('/api/servers/stop', json={"pid": 99999})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("not an iperf3 server instance", data['error'])

    @patch('iperf_server_manager.app.os.geteuid')
    def test_get_iperf3_run_as_ids_non_root_manager(self, mock_geteuid):
        # If the manager isn't running as root, it can't setuid to anyone -- inherit instead.
        mock_geteuid.return_value = 1000
        self.assertIsNone(get_iperf3_run_as_ids())

    @patch('iperf_server_manager.app.pwd')
    @patch('iperf_server_manager.app.os.geteuid')
    def test_get_iperf3_run_as_ids_no_iperf3_account(self, mock_geteuid, mock_pwd):
        # Root manager, but no iperf3 system user exists (e.g. undocumented step skipped) -- inherit.
        mock_geteuid.return_value = 0
        mock_pwd.getpwnam.side_effect = KeyError("iperf3")
        self.assertIsNone(get_iperf3_run_as_ids())

    @patch('iperf_server_manager.app.pwd')
    @patch('iperf_server_manager.app.os.geteuid')
    def test_get_iperf3_run_as_ids_drops_to_iperf3_user(self, mock_geteuid, mock_pwd):
        mock_geteuid.return_value = 0
        mock_pwd.getpwnam.return_value = MagicMock(pw_uid=999, pw_gid=999)
        self.assertEqual(get_iperf3_run_as_ids(), (999, 999))

    @patch('iperf_server_manager.app.get_iperf3_run_as_ids')
    @patch('iperf_server_manager.app.is_port_in_use')
    @patch('iperf_server_manager.app.get_running_iperf_servers')
    @patch('iperf_server_manager.app.subprocess.Popen')
    def test_start_server_drops_privileges_to_iperf3_user(
        self, mock_popen, mock_running, mock_port_in_use, mock_run_as
    ):
        mock_running.return_value = []
        mock_port_in_use.return_value = False
        mock_run_as.return_value = (999, 999)
        mock_popen.return_value = MagicMock(pid=12345)

        response = self.client.post('/api/servers/start', json={"port": 5299})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['run_as'], 'iperf3')

        _, kwargs = mock_popen.call_args
        self.assertEqual(kwargs.get('user'), 999)
        self.assertEqual(kwargs.get('group'), 999)

    @patch('iperf_server_manager.app.get_iperf3_run_as_ids')
    @patch('iperf_server_manager.app.is_port_in_use')
    @patch('iperf_server_manager.app.get_running_iperf_servers')
    @patch('iperf_server_manager.app.subprocess.Popen')
    def test_start_server_inherits_user_when_no_iperf3_account(
        self, mock_popen, mock_running, mock_port_in_use, mock_run_as
    ):
        mock_running.return_value = []
        mock_port_in_use.return_value = False
        mock_run_as.return_value = None
        mock_popen.return_value = MagicMock(pid=12345)

        response = self.client.post('/api/servers/start', json={"port": 5298})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIsNone(data['run_as'])

        _, kwargs = mock_popen.call_args
        self.assertNotIn('user', kwargs)
        self.assertNotIn('group', kwargs)


if __name__ == '__main__':
    unittest.main()
