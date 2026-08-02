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

import itertools
from collections import deque

import iperf_congestion_generator.app as gen_app_module
gen_app = gen_app_module.app


def reset_module_state():
    """Tests share one imported module, so the registry must not leak between them."""
    gen_app_module.tests.clear()
    gen_app_module._test_id_counter = itertools.count(1)


def make_test(**overrides):
    """Register a test record directly, without spawning iperf3."""
    config = {
        "server_ip": "192.168.1.100",
        "server_port": 5201,
        "bind_interface": "wlan0",
        "duration_minutes": 5,
        "bandwidth_mbps": "",
    }
    config.update(overrides)
    return gen_app_module._new_test(config)


class TestIPerfCongestionGenerator(unittest.TestCase):

    def setUp(self):
        gen_app.config['TESTING'] = True
        self.client = gen_app.test_client()
        reset_module_state()

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

    def test_api_hostname_route(self):
        # Consumed by the static landing page, which has no backend of its own
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

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
        # args[0] is now the test record; the connection details follow it.
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[1], "192.168.1.100")
        self.assertEqual(args[2], 5202)

        # Case 2: IP:Port string in server_ip
        mock_thread.reset_mock()
        response = self.client.post('/start', json={
            "server_ip": "192.168.1.100:5204",
            "duration_minutes": 10
        })
        self.assertEqual(response.status_code, 200)
        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]['args']
        self.assertEqual(args[1], "192.168.1.100")
        self.assertEqual(args[2], 5204)

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


class TestConcurrentTestRegistry(unittest.TestCase):
    """Several tests run at once, so state is a registry rather than one global."""

    def setUp(self):
        gen_app.config['TESTING'] = True
        self.client = gen_app.test_client()
        reset_module_state()

    @patch('iperf_congestion_generator.app.threading.Thread')
    def test_start_allows_concurrent_tests(self, mock_thread):
        """The old single-test 409 guard is gone -- concurrency is the point."""
        ids = []
        for port in (5201, 5202, 5203):
            res = self.client.post('/start', json={"server_ip": "192.168.1.100", "server_port": port})
            self.assertEqual(res.status_code, 200)
            ids.append(res.get_json()['test_id'])

        self.assertEqual(len(set(ids)), 3)
        listed = self.client.get('/tests').get_json()['tests']
        self.assertEqual(len(listed), 3)
        self.assertEqual([t['server_port'] for t in listed], [5201, 5202, 5203])

    @patch('iperf_congestion_generator.app.threading.Thread')
    def test_start_refuses_past_concurrency_cap(self, mock_thread):
        for _ in range(gen_app_module.MAX_CONCURRENT_TESTS):
            self.assertEqual(
                self.client.post('/start', json={"server_ip": "192.168.1.100"}).status_code, 200)

        res = self.client.post('/start', json={"server_ip": "192.168.1.100"})
        self.assertEqual(res.status_code, 409)
        self.assertIn('limit', res.get_json()['error'])

    def test_tests_route_reports_running_state(self):
        """A reloaded page rebuilds its tabs from here rather than assuming idle."""
        t = make_test(server_port=5202)
        data = self.client.get('/tests').get_json()
        self.assertEqual(data['max_concurrent'], gen_app_module.MAX_CONCURRENT_TESTS)
        self.assertEqual(len(data['tests']), 1)
        self.assertEqual(data['tests'][0]['id'], t['id'])
        self.assertEqual(data['tests'][0]['status'], 'running')
        self.assertEqual(data['tests'][0]['server_port'], 5202)

    def test_output_replays_from_cursor(self):
        t = make_test()
        for i in range(5):
            gen_app_module._emit(t, f"line {i}")

        first = self.client.get(f"/tests/{t['id']}/output?since=0").get_json()
        self.assertEqual(first['lines'], [f"line {i}" for i in range(5)])
        self.assertEqual(first['next'], 5)
        self.assertEqual(first['dropped'], 0)

        # A caught-up client gets nothing new, and the cursor holds steady.
        second = self.client.get(f"/tests/{t['id']}/output?since={first['next']}").get_json()
        self.assertEqual(second['lines'], [])
        self.assertEqual(second['next'], 5)

    def test_output_reports_dropped_lines_when_buffer_wraps(self):
        """total_lines keeps counting past what the ring buffer retains, so a
        client that falls behind is told rather than silently skipped."""
        t = make_test()
        t['lines'] = deque(maxlen=3)
        for i in range(10):
            gen_app_module._emit(t, f"line {i}")

        data = self.client.get(f"/tests/{t['id']}/output?since=0").get_json()
        self.assertEqual(data['lines'], ['line 7', 'line 8', 'line 9'])
        self.assertEqual(data['next'], 10)
        self.assertEqual(data['dropped'], 7)

    def test_output_unknown_test_404s(self):
        self.assertEqual(self.client.get('/tests/nope/output').status_code, 404)

    def test_stop_one_marks_only_that_test(self):
        a, b = make_test(server_port=5201), make_test(server_port=5202)
        a['process'] = MagicMock()

        res = self.client.post(f"/tests/{a['id']}/stop")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(a['stop_requested'])
        self.assertFalse(b['stop_requested'])
        a['process'].terminate.assert_called_once()

    def test_stop_all_marks_every_running_test(self):
        a, b = make_test(), make_test()
        res = self.client.post('/stop')
        self.assertEqual(res.get_json()['count'], 2)
        self.assertTrue(a['stop_requested'])
        self.assertTrue(b['stop_requested'])

    def test_forget_removes_finished_test_only(self):
        t = make_test()
        self.assertEqual(self.client.delete(f"/tests/{t['id']}").status_code, 409)

        t['status'] = 'finished'
        self.assertEqual(self.client.delete(f"/tests/{t['id']}").status_code, 200)
        self.assertNotIn(t['id'], gen_app_module.tests)

    def test_index_rebuilds_tabs_on_load(self):
        """The reattach: the page has no state of its own, it asks the server."""
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('refreshTests', html)
        self.assertIn('tab-title', html)
        self.assertNotIn('EventSource', html)


if __name__ == '__main__':
    unittest.main()
