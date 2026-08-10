#!/usr/bin/env python3
import os
import socket
import sys
import unittest
from unittest.mock import patch

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import reboot_manager.app as rm_app_module

rm_app = rm_app_module.app


class TestRebootManagerRoutes(unittest.TestCase):

    def setUp(self):
        rm_app.config['TESTING'] = True
        self.client = rm_app.test_client()
        # Tests run on macOS/off-Linux, but reset any lingering pending flag anyway.
        with rm_app_module.reboot_lock:
            rm_app_module.reboot_state['pending'] = False
            rm_app_module.reboot_state['requested_at'] = None

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Reboot', response.data)

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

    def test_get_hostname_falls_back(self):
        with patch('reboot_manager.app.socket.gethostname', side_effect=OSError('boom')):
            self.assertEqual(rm_app_module.get_hostname(), 'unknown-host')


class TestUptimeFormatting(unittest.TestCase):

    def test_format_uptime_none(self):
        self.assertEqual(rm_app_module.format_uptime(None), 'unknown')

    def test_format_uptime_minutes_only(self):
        self.assertEqual(rm_app_module.format_uptime(125), '2m')

    def test_format_uptime_hours_and_minutes(self):
        self.assertEqual(rm_app_module.format_uptime(3661), '1h 01m')

    def test_format_uptime_days_hours_minutes(self):
        self.assertEqual(rm_app_module.format_uptime(90000 + 3600 + 60), '1d 2h 01m')

    def test_get_uptime_seconds_missing_proc(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertIsNone(rm_app_module.get_uptime_seconds())


class TestCanReboot(unittest.TestCase):

    def test_non_linux_refused(self):
        with patch('reboot_manager.app.platform.system', return_value='Darwin'):
            ok, reason = rm_app_module.can_reboot()
            self.assertFalse(ok)
            self.assertIn('Linux', reason)

    def test_linux_without_tools_refused(self):
        with patch('reboot_manager.app.platform.system', return_value='Linux'), \
             patch('reboot_manager.app.shutil.which', return_value=None):
            ok, reason = rm_app_module.can_reboot()
            self.assertFalse(ok)
            self.assertIn('reboot mechanism', reason)

    def test_linux_with_systemctl_allowed(self):
        with patch('reboot_manager.app.platform.system', return_value='Linux'), \
             patch('reboot_manager.app.shutil.which', side_effect=lambda c: '/usr/bin/systemctl' if c == 'systemctl' else None):
            ok, reason = rm_app_module.can_reboot()
            self.assertTrue(ok)
            self.assertIsNone(reason)


class TestApiStatus(unittest.TestCase):

    def setUp(self):
        rm_app.config['TESTING'] = True
        self.client = rm_app.test_client()

    def test_status_reports_capability(self):
        with patch('reboot_manager.app.platform.system', return_value='Darwin'):
            response = self.client.get('/api/status')
            data = response.get_json()
            self.assertFalse(data['can_reboot'])
            self.assertIn('reason', data)
            self.assertIn('hostname', data)
            self.assertIn('uptime_text', data)


class TestApiReboot(unittest.TestCase):

    def setUp(self):
        rm_app.config['TESTING'] = True
        self.client = rm_app.test_client()
        with rm_app_module.reboot_lock:
            rm_app_module.reboot_state['pending'] = False
            rm_app_module.reboot_state['requested_at'] = None

    def test_reboot_refused_off_linux(self):
        with patch('reboot_manager.app.platform.system', return_value='Darwin'):
            response = self.client.post('/api/reboot', json={'confirm': 'REBOOT'})
            self.assertEqual(response.status_code, 501)
            self.assertIn('error', response.get_json())

    def test_reboot_refused_without_confirmation(self):
        with patch('reboot_manager.app.can_reboot', return_value=(True, None)):
            response = self.client.post('/api/reboot', json={})
            self.assertEqual(response.status_code, 400)

    def test_reboot_refused_with_wrong_confirmation(self):
        with patch('reboot_manager.app.can_reboot', return_value=(True, None)):
            response = self.client.post('/api/reboot', json={'confirm': 'yes please'})
            self.assertEqual(response.status_code, 400)

    def test_reboot_triggers_background_thread(self):
        with patch('reboot_manager.app.can_reboot', return_value=(True, None)), \
             patch('reboot_manager.app.threading.Thread') as mock_thread:
            response = self.client.post('/api/reboot', json={'confirm': 'REBOOT'})
            self.assertEqual(response.status_code, 202)
            mock_thread.assert_called_once()
            mock_thread.return_value.start.assert_called_once()

    def test_reboot_rejects_concurrent_trigger(self):
        with patch('reboot_manager.app.can_reboot', return_value=(True, None)), \
             patch('reboot_manager.app.threading.Thread'):
            first = self.client.post('/api/reboot', json={'confirm': 'REBOOT'})
            self.assertEqual(first.status_code, 202)
            second = self.client.post('/api/reboot', json={'confirm': 'REBOOT'})
            self.assertEqual(second.status_code, 409)

    def test_do_reboot_runs_expected_command(self):
        with patch('reboot_manager.app.time.sleep'), \
             patch('reboot_manager.app.shutil.which', return_value='/usr/bin/systemctl'), \
             patch('reboot_manager.app.subprocess.run') as mock_run:
            rm_app_module._do_reboot()
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd, ['sudo', 'systemctl', 'reboot'])

    def test_do_reboot_falls_back_without_systemctl(self):
        with patch('reboot_manager.app.time.sleep'), \
             patch('reboot_manager.app.shutil.which', return_value=None), \
             patch('reboot_manager.app.subprocess.run') as mock_run:
            rm_app_module._do_reboot()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd, ['sudo', 'reboot'])

    def test_do_reboot_swallows_errors(self):
        with patch('reboot_manager.app.time.sleep'), \
             patch('reboot_manager.app.shutil.which', return_value='/usr/bin/systemctl'), \
             patch('reboot_manager.app.subprocess.run', side_effect=OSError('boom')):
            rm_app_module._do_reboot()  # must not raise


if __name__ == '__main__':
    unittest.main()
