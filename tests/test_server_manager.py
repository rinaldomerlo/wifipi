#!/usr/bin/env python3
import os
import socket
import sys
import unittest
from unittest.mock import patch

# Adjust sys.path to import app from iperf_server_manager
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'iperf_server_manager'))
from app import app, parse_port_from_args


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

    @patch('app.get_process_cmdline')
    def test_stop_server_non_iperf_pid(self, mock_cmdline):
        mock_cmdline.return_value = "python3 /some/other/script.py"
        response = self.client.post('/api/servers/stop', json={"pid": 99999})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn("not an iperf3 server instance", data['error'])


if __name__ == '__main__':
    unittest.main()
