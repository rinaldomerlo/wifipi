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

import iperf_congestion_generator.app as gen_app_module
gen_app = gen_app_module.app


class TestIPerfCongestionGenerator(unittest.TestCase):

    def setUp(self):
        gen_app.config['TESTING'] = True
        self.client = gen_app.test_client()

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'IPERF', response.data)
        self.assertIn(b'Network Congestion Generator', response.data)
        self.assertIn(b'Server IP Address', response.data)
        self.assertIn(b'Port', response.data)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        # Every GUI screen must display the host it is served from
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_start_route_invalid_ip(self):
        response = self.client.post('/start', json={"server_ip": "invalid-ip"})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertIn("Invalid server IP address", data['error'])

    def test_start_route_invalid_port(self):
        response = self.client.post('/start', json={"server_ip": "192.168.1.100", "server_port": 999999})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertIn("Server port must be between 1 and 65535", data['error'])

    @patch('iperf_congestion_generator.app.threading.Thread')
    def test_start_route_valid_and_ip_port_parsing(self, mock_thread):
        # Case 1: Separate IP and port
        response = self.client.post('/start', json={
            "server_ip": "192.168.1.100",
            "server_port": 5202,
            "duration_minutes": 5,
            "bind_interface": "wlan0",
            "bandwidth_mbps": "100"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'started')
        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[0], "192.168.1.100")
        self.assertEqual(args[1], 5202)

        # Case 2: IP:Port string in server_ip
        mock_thread.reset_mock()
        response = self.client.post('/start', json={
            "server_ip": "192.168.1.100:5204",
            "duration_minutes": 10
        })
        self.assertEqual(response.status_code, 200)
        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[0], "192.168.1.100")
        self.assertEqual(args[1], 5204)

    @patch('iperf_congestion_generator.app.shutil.which')
    @patch('iperf_congestion_generator.app.subprocess.run')
    def test_scan_for_servers_multi_port(self, mock_sub_run, mock_which):
        mock_which.return_value = '/usr/bin/nmap'

        # Mock ip addr response then nmap response
        ip_addr_stdout = "inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        nmap_stdout = (
            "# Nmap scan report\n"
            "Host: 192.168.1.100 ()\tPorts: 5201/open/tcp//iperf3///, 5202/open/tcp//iperf3///, 5204/open/tcp//iperf3///\n"
            "Host: 192.168.1.50 ()\tPorts: 5201/open/tcp//iperf3///\n"
            "Host: 192.168.1.105 ()\tPorts: 5203/open/tcp//iperf3///\n"
        )

        def side_effect(cmd, **kwargs):
            m = MagicMock()
            if cmd[0] == "ip":
                m.stdout = ip_addr_stdout
            elif cmd[0] == "nmap":
                m.stdout = nmap_stdout
            return m

        mock_sub_run.side_effect = side_effect

        servers = gen_app_module.scan_for_servers(bind_interface="wlan0")
        self.assertEqual(len(servers), 4)
        self.assertNotIn({"ip": "192.168.1.50", "port": 5201}, servers)
        self.assertIn({"ip": "192.168.1.100", "port": 5201}, servers)
        self.assertIn({"ip": "192.168.1.100", "port": 5202}, servers)
        self.assertIn({"ip": "192.168.1.100", "port": 5204}, servers)
        self.assertIn({"ip": "192.168.1.105", "port": 5203}, servers)

        # Verify nmap command contains -e wlan0
        nmap_call_args = [call for call in mock_sub_run.call_args_list if call[0][0][0] == 'nmap']
        self.assertTrue(len(nmap_call_args) > 0)
        self.assertIn('-e', nmap_call_args[0][0][0])
        self.assertIn('wlan0', nmap_call_args[0][0][0])

    def test_stop_route(self):
        response = self.client.post('/stop')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'no test running')


if __name__ == '__main__':
    unittest.main()
