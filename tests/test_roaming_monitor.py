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

import roaming_monitor.app as rm_app_module

rm_app = rm_app_module.app

# Representative `iw event -t` output, as emitted on a station during a roam.
LINE_CONNECTED = "1000.000000: wlan0 (phy #0): connected to aa:bb:cc:dd:ee:01"
LINE_AUTH = "1000.100000: wlan0 (phy #0): auth aa:bb:cc:dd:ee:02 -> 11:22:33:44:55:66 status: 0: Successful"
LINE_ASSOC = "1000.200000: wlan0 (phy #0): assoc aa:bb:cc:dd:ee:02 -> 11:22:33:44:55:66 status: 0: Successful"
LINE_DEAUTH = ("1000.300000: wlan0 (phy #0): deauth aa:bb:cc:dd:ee:01 -> ff:ff:ff:ff:ff:ff "
               "reason 3: Deauthenticated because sending STA is leaving")
LINE_DISCONNECT = "1000.400000: wlan0 (phy #0): disconnected (by AP) reason: 15: 4-Way Handshake timeout"
LINE_SCAN_START = "1000.500000: wlan0 (phy #0): scan started"
LINE_CQM = "1000.600000: wlan0 (phy #0): RSSI went below CQM threshold"


def reset_module_state():
    rm_app_module.events.clear()
    rm_app_module._transition_start = None
    rm_app_module._boot_offset = None
    rm_app_module._roam_durations.clear()
    rm_app_module.state.update({
        "running": False, "interface": None, "connected": False,
        "current_bssid": None, "ssid": None, "started_at": None, "error": None,
    })
    rm_app_module.stats.update({
        "roams": 0, "reconnects": 0, "disconnects": 0,
        "last_roam_ms": None, "avg_roam_ms": None,
    })
    while not rm_app_module.output_queue.empty():
        rm_app_module.output_queue.get_nowait()


class TestRoamingMonitorRoutes(unittest.TestCase):

    def setUp(self):
        rm_app.config['TESTING'] = True
        self.client = rm_app.test_client()
        reset_module_state()

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Roaming', response.data)
        self.assertIn(b'Wireless Interface', response.data)

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

    @patch('roaming_monitor.app.get_wireless_interfaces')
    def test_api_interfaces_route(self, mock_ifaces):
        mock_ifaces.return_value = ['wlan0', 'wlan1']
        response = self.client.get('/api/interfaces')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['interfaces'], ['wlan0', 'wlan1'])

    def test_api_status_shape_before_start(self):
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['running'])
        self.assertEqual(data['roams'], 0)
        self.assertIsNone(data['current_bssid'])

    def test_api_events_empty_before_start(self):
        response = self.client.get('/api/events')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['events'], [])

    def test_stop_when_not_running(self):
        response = self.client.post('/api/stop')
        self.assertEqual(response.get_json()['status'], 'not running')

    @patch('roaming_monitor.app.platform.system')
    @patch('roaming_monitor.app.get_wireless_interfaces')
    def test_start_rejects_non_linux(self, mock_ifaces, mock_system):
        mock_ifaces.return_value = ['wlan0']
        mock_system.return_value = 'Darwin'
        response = self.client.post('/api/start', json={'interface': 'wlan0'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Linux', response.get_json()['error'])

    @patch('roaming_monitor.app.shutil.which')
    @patch('roaming_monitor.app.platform.system')
    def test_start_reports_missing_iw(self, mock_system, mock_which):
        mock_system.return_value = 'Linux'
        mock_which.return_value = None
        response = self.client.post('/api/start', json={'interface': 'wlan0'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('iw is not installed', response.get_json()['error'])

    @patch('roaming_monitor.app.get_wireless_interfaces')
    def test_start_rejects_bogus_interface_name(self, mock_ifaces):
        mock_ifaces.return_value = ['wlan0']
        response = self.client.post('/api/start', json={'interface': 'wlan0; rm -rf /'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid interface', response.get_json()['error'])

    @patch('roaming_monitor.app.threading.Thread')
    @patch('roaming_monitor.app.shutil.which')
    @patch('roaming_monitor.app.platform.system')
    def test_start_launches_monitor_thread(self, mock_system, mock_which, mock_thread):
        mock_system.return_value = 'Linux'
        mock_which.return_value = '/usr/sbin/iw'
        response = self.client.post('/api/start', json={'interface': 'wlan0'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['interface'], 'wlan0')
        mock_thread.assert_called_once()
        self.assertEqual(mock_thread.call_args[1]['args'], ('wlan0',))


IW_LINK_CONNECTED = """Connected to aa:bb:cc:dd:ee:01 (on wlan0)
\tSSID: ChamberAP
\tfreq: 5180
\tsignal: -47 dBm
\ttx bitrate: 2882.9 MBit/s
"""


class TestCurrentLinkSeeding(unittest.TestCase):
    """
    `iw event` only reports transitions, so a session started while already associated
    must seed the BSSID from `iw dev <iface> link` -- otherwise the first roam is
    misread as an initial connect and goes uncounted.
    """

    def setUp(self):
        rm_app.config['TESTING'] = True
        self.client = rm_app.test_client()
        reset_module_state()

    @patch('roaming_monitor.app.subprocess.check_output')
    def test_parses_current_association(self, mock_check):
        mock_check.return_value = IW_LINK_CONNECTED.encode()
        link = rm_app_module.get_current_link('wlan0')
        self.assertTrue(link['connected'])
        self.assertEqual(link['bssid'], 'aa:bb:cc:dd:ee:01')
        self.assertEqual(link['ssid'], 'ChamberAP')

    @patch('roaming_monitor.app.subprocess.check_output')
    def test_handles_not_connected(self, mock_check):
        mock_check.return_value = b"Not connected.\n"
        link = rm_app_module.get_current_link('wlan0')
        self.assertFalse(link['connected'])
        self.assertIsNone(link['bssid'])

    @patch('roaming_monitor.app.subprocess.check_output')
    def test_handles_iw_failure_gracefully(self, mock_check):
        mock_check.side_effect = OSError("boom")
        link = rm_app_module.get_current_link('wlan0')
        self.assertFalse(link['connected'])
        self.assertIsNone(link['bssid'])

    @patch('roaming_monitor.app.threading.Thread')
    @patch('roaming_monitor.app.shutil.which')
    @patch('roaming_monitor.app.platform.system')
    @patch('roaming_monitor.app.get_current_link')
    def test_start_seeds_state_and_emits_baseline(self, mock_link, mock_system,
                                                  mock_which, mock_thread):
        mock_system.return_value = 'Linux'
        mock_which.return_value = '/usr/sbin/iw'
        mock_link.return_value = {
            "connected": True, "bssid": "aa:bb:cc:dd:ee:01", "ssid": "ChamberAP",
        }

        self.client.post('/api/start', json={'interface': 'wlan0'})

        self.assertEqual(rm_app_module.state['current_bssid'], 'aa:bb:cc:dd:ee:01')
        self.assertTrue(rm_app_module.state['connected'])
        self.assertEqual(rm_app_module.state['ssid'], 'ChamberAP')

        baseline = list(rm_app_module.events)[0]
        self.assertEqual(baseline['type'], 'baseline')
        self.assertIn('aa:bb:cc:dd:ee:01', baseline['detail'])

    def test_first_roam_after_seeding_is_counted(self):
        # Regression: without a seeded BSSID this connect classified as "initial",
        # so the first roam of every session was lost from the statistics.
        rm_app_module.state['current_bssid'] = 'aa:bb:cc:dd:ee:01'
        rm_app_module.state['connected'] = True

        rm_app_module.process_event(rm_app_module.parse_iw_event(
            "9000.000000: wlan0 (phy #0): deauth aa:bb:cc:dd:ee:01 -> ff:ff:ff:ff:ff:ff reason 3: leaving"
        ))
        ev = rm_app_module.process_event(rm_app_module.parse_iw_event(
            "9000.500000: wlan0 (phy #0): connected to aa:bb:cc:dd:ee:02"
        ))

        self.assertEqual(ev['transition'], 'roam')
        self.assertEqual(ev['from_bssid'], 'aa:bb:cc:dd:ee:01')
        self.assertAlmostEqual(ev['duration_ms'], 500.0, places=1)
        self.assertEqual(rm_app_module.stats['roams'], 1)


class TestIwEventParsing(unittest.TestCase):

    def setUp(self):
        reset_module_state()

    def test_parses_connected(self):
        ev = rm_app_module.parse_iw_event(LINE_CONNECTED)
        self.assertEqual(ev['type'], 'connected')
        self.assertEqual(ev['bssid'], 'aa:bb:cc:dd:ee:01')
        self.assertEqual(ev['interface'], 'wlan0')
        self.assertAlmostEqual(ev['ts'], 1000.0)

    def test_parses_auth_and_keeps_both_macs(self):
        ev = rm_app_module.parse_iw_event(LINE_AUTH)
        self.assertEqual(ev['type'], 'auth')
        self.assertEqual(ev['macs'], ['aa:bb:cc:dd:ee:02', '11:22:33:44:55:66'])
        self.assertEqual(ev['status_code'], 0)

    def test_parses_assoc(self):
        self.assertEqual(rm_app_module.parse_iw_event(LINE_ASSOC)['type'], 'assoc')

    def test_parses_deauth_with_decoded_reason(self):
        ev = rm_app_module.parse_iw_event(LINE_DEAUTH)
        self.assertEqual(ev['type'], 'deauth')
        self.assertEqual(ev['reason_code'], 3)
        self.assertIn('leaving', ev['reason_text'])

    def test_parses_disconnect_with_decoded_reason(self):
        ev = rm_app_module.parse_iw_event(LINE_DISCONNECT)
        self.assertEqual(ev['type'], 'disconnected')
        self.assertEqual(ev['reason_code'], 15)
        self.assertEqual(ev['reason_text'], '4-Way Handshake timeout')

    def test_parses_scan_and_cqm(self):
        self.assertEqual(rm_app_module.parse_iw_event(LINE_SCAN_START)['type'], 'scan')
        self.assertEqual(rm_app_module.parse_iw_event(LINE_CQM)['type'], 'cqm')

    def test_unknown_reason_code_is_labelled_not_dropped(self):
        ev = rm_app_module.parse_iw_event(
            "1.0: wlan0 (phy #0): disconnected (by AP) reason: 99: Whatever"
        )
        self.assertEqual(ev['reason_code'], 99)
        self.assertEqual(ev['reason_text'], 'Unknown reason code')

    def test_blank_line_returns_none(self):
        self.assertIsNone(rm_app_module.parse_iw_event('   '))

    def test_unrecognised_line_is_kept_as_other(self):
        ev = rm_app_module.parse_iw_event('1.0: wlan0 (phy #0): something new here')
        self.assertEqual(ev['type'], 'other')
        self.assertEqual(ev['detail'], 'something new here')


class TestRoamTiming(unittest.TestCase):
    """The roam clock: started by the first sign of a transition, stopped by 'connected'."""

    def setUp(self):
        reset_module_state()

    @staticmethod
    def _feed(line):
        return rm_app_module.process_event(rm_app_module.parse_iw_event(line))

    def test_initial_connect_is_not_a_roam(self):
        ev = self._feed(LINE_CONNECTED)
        self.assertEqual(ev['transition'], 'initial')
        self.assertEqual(rm_app_module.stats['roams'], 0)
        self.assertEqual(rm_app_module.state['current_bssid'], 'aa:bb:cc:dd:ee:01')

    def test_deauth_then_connect_elsewhere_is_a_timed_roam(self):
        self._feed(LINE_CONNECTED)
        self._feed("2000.000000: wlan0 (phy #0): deauth aa:bb:cc:dd:ee:01 -> ff:ff:ff:ff:ff:ff reason 3: leaving")
        ev = self._feed("2000.694000: wlan0 (phy #0): connected to aa:bb:cc:dd:ee:02")

        self.assertEqual(ev['transition'], 'roam')
        self.assertEqual(ev['from_bssid'], 'aa:bb:cc:dd:ee:01')
        self.assertAlmostEqual(ev['duration_ms'], 694.0, places=1)
        self.assertEqual(rm_app_module.stats['roams'], 1)
        self.assertAlmostEqual(rm_app_module.stats['last_roam_ms'], 694.0, places=1)

    def test_fast_transition_without_disconnect_still_timed(self):
        # 802.11r: no deauth/disconnect at all, auth straight to the new AP.
        self._feed(LINE_CONNECTED)
        self._feed("3000.000000: wlan0 (phy #0): auth aa:bb:cc:dd:ee:09 -> 11:22:33:44:55:66 status: 0: Successful")
        ev = self._feed("3000.050000: wlan0 (phy #0): connected to aa:bb:cc:dd:ee:09")

        self.assertEqual(ev['transition'], 'roam')
        self.assertAlmostEqual(ev['duration_ms'], 50.0, places=1)

    def test_auth_to_current_ap_does_not_start_the_clock(self):
        self._feed(LINE_CONNECTED)
        # Re-auth involving the AP we're already on: not a transition.
        self._feed("3000.000000: wlan0 (phy #0): auth aa:bb:cc:dd:ee:01 -> 11:22:33:44:55:66 status: 0: Successful")
        self.assertIsNone(rm_app_module._transition_start)

    def test_reconnect_to_same_bssid_is_not_counted_as_roam(self):
        self._feed(LINE_CONNECTED)
        self._feed("4000.000000: wlan0 (phy #0): disconnected (by AP) reason: 4: inactivity")
        ev = self._feed("4000.300000: wlan0 (phy #0): connected to aa:bb:cc:dd:ee:01")

        self.assertEqual(ev['transition'], 'reconnect')
        self.assertEqual(rm_app_module.stats['roams'], 0)
        self.assertEqual(rm_app_module.stats['reconnects'], 1)
        self.assertEqual(rm_app_module.stats['disconnects'], 1)

    def test_average_roam_tracks_multiple_roams(self):
        self._feed(LINE_CONNECTED)
        self._feed("5000.000000: wlan0 (phy #0): deauth aa:bb:cc:dd:ee:01 -> ff:ff:ff:ff:ff:ff reason 3: leaving")
        self._feed("5000.600000: wlan0 (phy #0): connected to aa:bb:cc:dd:ee:02")
        self._feed("6000.000000: wlan0 (phy #0): deauth aa:bb:cc:dd:ee:02 -> ff:ff:ff:ff:ff:ff reason 3: leaving")
        self._feed("6000.800000: wlan0 (phy #0): connected to aa:bb:cc:dd:ee:01")

        self.assertEqual(rm_app_module.stats['roams'], 2)
        self.assertAlmostEqual(rm_app_module.stats['last_roam_ms'], 800.0, places=1)
        self.assertAlmostEqual(rm_app_module.stats['avg_roam_ms'], 700.0, places=1)

    def test_events_carry_interface_so_other_radios_can_be_filtered(self):
        # `iw event` reports every wireless interface; monitor_loop drops the ones that
        # don't belong to the interface being watched, so roam timing isn't interleaved.
        other = rm_app_module.parse_iw_event(
            "8000.000000: wlan1 (phy #1): connected to bb:bb:bb:bb:bb:bb"
        )
        self.assertEqual(other['interface'], 'wlan1')
        mine = rm_app_module.parse_iw_event(LINE_CONNECTED)
        self.assertEqual(mine['interface'], 'wlan0')

    def test_disconnect_marks_link_down(self):
        self._feed(LINE_CONNECTED)
        self.assertTrue(rm_app_module.state['connected'])
        self._feed("7000.000000: wlan0 (phy #0): disconnected (local request)")
        self.assertFalse(rm_app_module.state['connected'])


if __name__ == '__main__':
    unittest.main()
