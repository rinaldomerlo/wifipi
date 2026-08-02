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

import client_simulator.app as cs_app_module

cs_app = cs_app_module.app


class TestClientSimulator(unittest.TestCase):

    def setUp(self):
        cs_app.config['TESTING'] = True
        self.client = cs_app.test_client()
        cs_app_module.sim_running = False
        cs_app_module.sim_clients.clear()
        cs_app_module.sim_stats.update({"requests": 0, "errors": 0, "churn_events": 0})
        cs_app_module.sim_context.update({"mode": None, "bind_interface": None})
        cs_app_module.stop_event.clear()
        while not cs_app_module.output_queue.empty():
            cs_app_module.output_queue.get_nowait()

    # -- basic routes --------------------------------------------------

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Client', response.data)
        self.assertIn(b'Target URL', response.data)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_index_reattaches_to_running_simulation_on_load(self):
        """
        The simulation outlives the page, so a reload must probe /status and
        restore the running UI rather than showing an idle screen while clients
        are still generating traffic.
        """
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('function reattach()', html)
        self.assertIn('reattach();', html)

    def test_api_hostname_route(self):
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

    def test_status_route_defaults(self):
        response = self.client.get('/status')
        data = response.get_json()
        self.assertFalse(data['running'])
        self.assertEqual(data['active_clients'], 0)
        self.assertEqual(data['requests'], 0)

    # -- mode detection --------------------------------------------------

    def test_detect_mode_non_linux(self):
        with patch('client_simulator.app.platform.system', return_value='Darwin'):
            mode, reason = cs_app_module.detect_mode('wlan0')
        self.assertEqual(mode, 'simulated')
        self.assertIn('Linux', reason)

    def test_detect_mode_no_ip_binary(self):
        with patch('client_simulator.app.platform.system', return_value='Linux'), \
             patch('client_simulator.app.shutil.which', return_value=None):
            mode, reason = cs_app_module.detect_mode('wlan0')
        self.assertEqual(mode, 'simulated')
        self.assertIn('ip', reason)

    def test_detect_mode_no_passwordless_sudo(self):
        with patch('client_simulator.app.platform.system', return_value='Linux'), \
             patch('client_simulator.app.shutil.which', return_value='/usr/sbin/ip'), \
             patch('client_simulator.app._run', return_value=(False, '', 'sudo: a password is required')):
            mode, reason = cs_app_module.detect_mode('wlan0')
        self.assertEqual(mode, 'simulated')
        self.assertIn('sudo', reason)

    def test_detect_mode_netns_available(self):
        with patch('client_simulator.app.platform.system', return_value='Linux'), \
             patch('client_simulator.app.shutil.which', return_value='/usr/sbin/ip'), \
             patch('client_simulator.app._run', return_value=(True, '', '')):
            mode, reason = cs_app_module.detect_mode('wlan0')
        self.assertEqual(mode, 'netns')
        self.assertIsNone(reason)

    # -- pure helpers --------------------------------------------------

    def test_client_ip_allocation(self):
        self.assertEqual(cs_app_module.client_ip(1), '10.200.0.2')
        self.assertEqual(cs_app_module.client_ip(254), '10.200.0.255')
        self.assertEqual(cs_app_module.client_ip(255), '10.200.1.0')

    def test_veth_names_within_ifnamsiz_limit(self):
        host, peer = cs_app_module.veth_names(500)
        self.assertLessEqual(len(host), 15)
        self.assertLessEqual(len(peer), 15)
        self.assertNotEqual(host, peer)

    def test_compute_churn_count(self):
        self.assertEqual(cs_app_module.compute_churn_count(20, 10), 2)
        self.assertEqual(cs_app_module.compute_churn_count(3, 10), 0)
        self.assertEqual(cs_app_module.compute_churn_count(0, 50), 0)

    # -- namespace command sequences (mocked subprocess boundary) ------

    def test_create_client_namespace_command_sequence(self):
        with patch('client_simulator.app._run', return_value=(True, '', '')) as mock_run:
            ok, ip_addr = cs_app_module.create_client_namespace(1, 'wlan0')
        self.assertTrue(ok)
        self.assertEqual(ip_addr, '10.200.0.2')
        first_cmd = mock_run.call_args_list[0][0][0]
        self.assertEqual(first_cmd[:4], ['sudo', 'ip', 'netns', 'add'])
        self.assertIn('wfsim-1', first_cmd)

    def test_create_client_namespace_failure_cleans_up(self):
        with patch('client_simulator.app._run', side_effect=[(True, '', ''), (False, '', 'boom')]), \
             patch('client_simulator.app.delete_client_namespace') as mock_delete:
            ok, err = cs_app_module.create_client_namespace(1, 'wlan0')
        self.assertFalse(ok)
        self.assertEqual(err, 'boom')
        mock_delete.assert_called_once_with(1)

    def test_delete_client_namespace_command(self):
        with patch('client_simulator.app._run', return_value=(True, '', '')) as mock_run:
            cs_app_module.delete_client_namespace(3)
        mock_run.assert_called_once_with(['sudo', 'ip', 'netns', 'delete', 'wfsim-3'])

    def test_setup_bridge_failure_reports_error(self):
        with patch('client_simulator.app._run', side_effect=[(True, '', '')] * 2 + [(False, '', 'link exists')]):
            ok, err = cs_app_module.setup_bridge('wlan0')
        self.assertFalse(ok)
        self.assertEqual(err, 'link exists')

    # -- traffic helpers --------------------------------------------------

    def test_curl_in_namespace_raises_on_failure(self):
        with patch('client_simulator.app._run', return_value=(False, '', 'curl: (7) Failed to connect')):
            with self.assertRaises(RuntimeError):
                cs_app_module._curl_in_namespace('wfsim-1', 'http://192.168.1.1/')

    def test_curl_in_namespace_returns_size(self):
        with patch('client_simulator.app._run', return_value=(True, '1234', '')):
            size = cs_app_module._curl_in_namespace('wfsim-1', 'http://192.168.1.1/')
        self.assertEqual(size, '1234')

    def test_dig_in_namespace(self):
        with patch('client_simulator.app._run', return_value=(True, '93.184.216.34', '')) as mock_run:
            ok = cs_app_module._dig_in_namespace('wfsim-1', '192.168.1.1', 'example.com')
        self.assertTrue(ok)
        cmd = mock_run.call_args[0][0]
        self.assertIn('@192.168.1.1', cmd)
        self.assertIn('example.com', cmd)

    def test_resolve_dns_failure(self):
        with patch('client_simulator.app.socket.gethostbyname', side_effect=OSError('not found')):
            self.assertFalse(cs_app_module._resolve_dns('nope.invalid'))

    # -- client lifecycle --------------------------------------------------

    def test_spawn_client_netns_mode(self):
        config = {"mode": "netns", "bind_interface": "wlan0", "target_url": "", "dns_targets": [], "dns_server": ""}
        with patch('client_simulator.app.create_client_namespace', return_value=(True, '10.200.0.5')), \
             patch('client_simulator.app.threading.Thread') as mock_thread:
            client_id = cs_app_module.spawn_client(config)
        self.assertIsNotNone(client_id)
        self.assertEqual(cs_app_module.sim_clients[client_id]['ip'], '10.200.0.5')
        mock_thread.assert_called_once()

    def test_spawn_client_netns_failure_returns_none(self):
        config = {"mode": "netns", "bind_interface": "wlan0", "target_url": "", "dns_targets": [], "dns_server": ""}
        with patch('client_simulator.app.create_client_namespace', return_value=(False, 'boom')):
            client_id = cs_app_module.spawn_client(config)
        self.assertIsNone(client_id)
        self.assertEqual(len(cs_app_module.sim_clients), 0)

    def test_spawn_client_simulated_mode_skips_namespace_creation(self):
        config = {"mode": "simulated", "bind_interface": "wlan0", "target_url": "", "dns_targets": [], "dns_server": ""}
        with patch('client_simulator.app.create_client_namespace') as mock_create, \
             patch('client_simulator.app.threading.Thread') as mock_thread:
            client_id = cs_app_module.spawn_client(config)
        mock_create.assert_not_called()
        self.assertEqual(cs_app_module.sim_clients[client_id]['ip'], '(simulated)')
        mock_thread.assert_called_once()

    def test_retire_client_netns_mode_deletes_namespace(self):
        fake_stop_event = MagicMock()
        fake_thread = MagicMock()
        cs_app_module.sim_clients[42] = {"stop_event": fake_stop_event, "thread": fake_thread, "ip": "x"}
        with patch('client_simulator.app.delete_client_namespace') as mock_delete:
            cs_app_module.retire_client(42, 'netns')
        fake_stop_event.set.assert_called_once()
        fake_thread.join.assert_called_once()
        mock_delete.assert_called_once_with(42)
        self.assertNotIn(42, cs_app_module.sim_clients)

    def test_retire_client_simulated_mode_skips_namespace_delete(self):
        fake_stop_event = MagicMock()
        fake_thread = MagicMock()
        cs_app_module.sim_clients[7] = {"stop_event": fake_stop_event, "thread": fake_thread, "ip": "(simulated)"}
        with patch('client_simulator.app.delete_client_namespace') as mock_delete:
            cs_app_module.retire_client(7, 'simulated')
        mock_delete.assert_not_called()
        self.assertNotIn(7, cs_app_module.sim_clients)

    # -- /start validation --------------------------------------------------

    def test_start_route_already_running(self):
        cs_app_module.sim_running = True
        response = self.client.post('/start', json={"target_url": "http://192.168.1.1/"})
        self.assertEqual(response.status_code, 409)

    def test_start_route_invalid_client_count(self):
        response = self.client.post('/start', json={"client_count": 501, "target_url": "http://x/"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Client count must be between", response.get_json()['error'])

    def test_start_route_invalid_churn_rate(self):
        response = self.client.post('/start', json={"churn_rate_percent": 99, "target_url": "http://x/"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Churn rate must be between", response.get_json()['error'])

    def test_start_route_invalid_interface(self):
        response = self.client.post('/start', json={"bind_interface": "eth9", "target_url": "http://x/"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Interface must be wlan0 or eth0", response.get_json()['error'])

    def test_start_route_no_target(self):
        response = self.client.post('/start', json={"target_url": "", "dns_targets": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Provide a target URL", response.get_json()['error'])

    def test_start_route_invalid_url_scheme(self):
        response = self.client.post('/start', json={"target_url": "ftp://192.168.1.1/"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("must start with http", response.get_json()['error'])

    @patch('client_simulator.app.threading.Thread')
    def test_start_route_valid_spawns_background_thread(self, mock_thread):
        response = self.client.post('/start', json={
            "client_count": 5,
            "churn_rate_percent": 20,
            "duration_minutes": 5,
            "bind_interface": "wlan0",
            "target_url": "http://192.168.1.1/",
            "dns_targets": "example.com, cloudflare.com",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'starting')
        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args[1]
        self.assertEqual(kwargs['target'], cs_app_module.start_simulation)
        config = kwargs['args'][0]
        self.assertEqual(config['client_count'], 5)
        self.assertEqual(config['churn_rate_percent'], 20)
        self.assertEqual(config['target_url'], 'http://192.168.1.1/')
        self.assertEqual(config['dns_targets'], ['example.com', 'cloudflare.com'])

    def test_stop_route_when_not_running(self):
        response = self.client.post('/stop')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'no simulation running')


if __name__ == '__main__':
    unittest.main()
