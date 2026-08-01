#!/usr/bin/env python3
import os
import socket
import sys
import unittest
from unittest.mock import MagicMock, patch

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import web_terminal.app as wt_app_module

wt_app = wt_app_module.app


class TestWebTerminalRoutes(unittest.TestCase):

    def setUp(self):
        wt_app.config['TESTING'] = True
        self.client = wt_app.test_client()

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Terminal', response.data)

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

    def test_index_embeds_relative_iframe_src(self):
        """
        The iframe src must stay relative. An absolute '/tty/' would break the
        app under the /terminal/ nginx prefix, the same way an absolute
        EventSource URL would break the streaming apps.
        """
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('src="tty/"', html)
        self.assertNotIn('src="/tty/"', html)

    def test_get_hostname_falls_back(self):
        with patch('web_terminal.app.socket.gethostname', side_effect=OSError('boom')):
            self.assertEqual(wt_app_module.get_hostname(), 'unknown-host')


class TestBackendStatus(unittest.TestCase):

    def setUp(self):
        wt_app.config['TESTING'] = True
        self.client = wt_app.test_client()

    def test_status_available(self):
        with patch('web_terminal.app.socket.create_connection') as mock_conn:
            mock_conn.return_value = MagicMock()
            response = self.client.get('/api/status')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['available'])
        self.assertEqual(data['reason'], '')
        self.assertEqual(data['port'], wt_app_module.TTYD_PORT)

    def test_status_unavailable(self):
        with patch('web_terminal.app.socket.create_connection',
                   side_effect=ConnectionRefusedError('refused')):
            response = self.client.get('/api/status')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['available'])
        self.assertTrue(data['reason'])

    def test_status_unavailable_on_timeout(self):
        """socket.timeout is an OSError subclass and must not escape as a 500."""
        with patch('web_terminal.app.socket.create_connection',
                   side_effect=socket.timeout('timed out')):
            response = self.client.get('/api/status')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['available'])

    def test_ttyd_available_probes_loopback(self):
        with patch('web_terminal.app.socket.create_connection') as mock_conn:
            mock_conn.return_value = MagicMock()
            self.assertTrue(wt_app_module.ttyd_available())

        mock_conn.assert_called_once()
        address = mock_conn.call_args[0][0]
        self.assertEqual(address, (wt_app_module.TTYD_HOST, wt_app_module.TTYD_PORT))
        self.assertEqual(wt_app_module.TTYD_HOST, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
