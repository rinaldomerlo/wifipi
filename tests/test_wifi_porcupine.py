#!/usr/bin/env python3
import fcntl
import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import wifi_porcupine.app as wp_app_module

wp_app = wp_app_module.app


class TestWifiPorcupine(unittest.TestCase):

    def setUp(self):
        wp_app.config['TESTING'] = True
        self.client = wp_app.test_client()
        wp_app_module.run_state.update({
            "running": False, "wifi_mode": None,
            "enlisted": [], "connected": set(), "config": None,
        })
        wp_app_module.stats.update({
            "reconnects": 0, "errors": 0, "active_interfaces": 0,
        })
        wp_app_module.stop_event.clear()
        wp_app_module.release_run_lock()  # defensive: don't let one test's lock leak into the next

    # -- basic routes --------------------------------------------------

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Porcupine', response.data)
        self.assertIn(b'Presence', response.data)
        self.assertIn(b'Churn rate', response.data)
        self.assertIn(b'Variability', response.data)

    def test_index_route_has_password_visibility_toggle(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('btn-toggle-password', html)
        self.assertIn('togglePasswordVisibility()', html)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_index_reattaches_to_running_run_on_load(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('function reattach()', html)
        self.assertIn('reattach();', html)

    def test_api_hostname_route(self):
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

    def test_status_route_defaults(self):
        data = self.client.get('/api/status').get_json()
        self.assertFalse(data['running'])
        self.assertEqual(data['reconnects'], 0)
        self.assertEqual(data['active_interfaces'], 0)

    def test_output_route_cursor_shape(self):
        data = self.client.get('/api/output?since=0').get_json()
        self.assertIn('lines', data)
        self.assertIn('next', data)
        self.assertIn('dropped', data)

    # -- churn model math: Presence x Churn rate x Variability -----------

    def test_churn_rate_from_pos_endpoints(self):
        lo, hi = wp_app_module.CHURN_RANGE
        self.assertAlmostEqual(wp_app_module.churn_rate_from_pos(lo), wp_app_module.RATE_AT_MIN)
        self.assertAlmostEqual(wp_app_module.churn_rate_from_pos(hi), wp_app_module.RATE_AT_MAX)

    def test_churn_rate_from_pos_monotonic_increasing(self):
        rates = [wp_app_module.churn_rate_from_pos(p) for p in range(1, 101)]
        self.assertTrue(all(b > a for a, b in zip(rates, rates[1:])))

    def test_churn_rate_from_pos_is_geometric(self):
        """Every slider step multiplies the rate by a constant ratio, so the midpoint is the
        geometric mean of the endpoints -- each step has ~the same proportional effect."""
        lo, hi = wp_app_module.CHURN_RANGE
        mid = (lo + hi) / 2
        r_lo = wp_app_module.churn_rate_from_pos(lo)
        r_hi = wp_app_module.churn_rate_from_pos(hi)
        self.assertAlmostEqual(wp_app_module.churn_rate_from_pos(mid), (r_lo * r_hi) ** 0.5, places=4)

    def test_churn_steps_have_even_proportional_effect(self):
        rates = [wp_app_module.churn_rate_from_pos(p) for p in range(1, 101)]
        ratios = [rates[i + 1] / rates[i] for i in range(len(rates) - 1)]
        self.assertLess(max(ratios) / min(ratios), 1.001)

    def test_compute_durations_household_default_is_mostly_off(self):
        """The whole point of the redesign: a quiet household setting spends most of the
        cycle disconnected, not connected."""
        on, gap = wp_app_module.compute_durations(
            wp_app_module.PRESENCE_DEFAULT, wp_app_module.CHURN_DEFAULT)
        self.assertGreater(gap, on)  # off longer than on
        self.assertGreater(on, 60.0)  # but still connected for minutes, not seconds

    def test_compute_durations_presence_raises_connected_share(self):
        low_on, low_gap = wp_app_module.compute_durations(20, 40)
        high_on, high_gap = wp_app_module.compute_durations(80, 40)
        self.assertGreater(high_on, low_on)
        self.assertLess(high_gap, low_gap)

    def test_compute_durations_rate_shortens_the_cycle(self):
        slow_on, slow_gap = wp_app_module.compute_durations(30, 5)
        fast_on, fast_gap = wp_app_module.compute_durations(30, 60)
        self.assertLess(fast_on + fast_gap, slow_on + slow_gap)

    def test_compute_durations_matches_requested_duty_when_unfloored(self):
        """Away from the floors, the connected share of a whole cycle == Presence%."""
        presence, churn = 40, 20
        on, gap = wp_app_module.compute_durations(presence, churn)
        period = on + gap + wp_app_module.OFFLINE_ESTIMATE_SECONDS
        self.assertAlmostEqual(on / period, presence / 100.0, places=2)

    def test_compute_durations_respects_floors_at_the_extreme(self):
        """Cranking churn to the top can't push ON below MIN_DWELL or the gap below GAP_MIN."""
        on, gap = wp_app_module.compute_durations(wp_app_module.PRESENCE_RANGE[0],
                                                  wp_app_module.CHURN_RANGE[1])
        self.assertGreaterEqual(on, wp_app_module.MIN_DWELL)
        self.assertGreaterEqual(gap, wp_app_module.GAP_MIN)

    def test_achievable_rate_matches_request_when_not_capped(self):
        """At a slow, reachable setting the achievable rate equals the requested rate."""
        presence, churn = 30, 12
        requested = wp_app_module.churn_rate_from_pos(churn)
        self.assertAlmostEqual(wp_app_module.achievable_rate(presence, churn), requested, places=3)

    def test_achievable_rate_capped_below_request_at_the_top(self):
        """Past what the fixed scan+DHCP cost allows, the real rate falls below the requested one."""
        churn = wp_app_module.CHURN_RANGE[1]
        requested = wp_app_module.churn_rate_from_pos(churn)
        self.assertLess(wp_app_module.achievable_rate(50, churn), requested)

    def test_gamma_shape_from_variability_endpoints(self):
        lo, hi = wp_app_module.VARIABILITY_RANGE
        self.assertAlmostEqual(wp_app_module.gamma_shape_from_variability(lo),
                               wp_app_module.SHAPE_AT_MIN_VARIABILITY)
        self.assertAlmostEqual(wp_app_module.gamma_shape_from_variability(hi),
                               wp_app_module.SHAPE_AT_MAX_VARIABILITY)

    def test_gamma_shape_from_variability_monotonic_decreasing(self):
        """More Variability => lower gamma shape => burstier timing."""
        shapes = [wp_app_module.gamma_shape_from_variability(p) for p in range(0, 101, 5)]
        self.assertTrue(all(b < a for a, b in zip(shapes, shapes[1:])))

    def test_sample_period_respects_clamps(self):
        for mean, floor in ((200.0, wp_app_module.MIN_DWELL), (2.0, wp_app_module.GAP_MIN)):
            for bias in (wp_app_module.DWELL_BIAS_RANGE[0], 1.0, wp_app_module.DWELL_BIAS_RANGE[1]):
                cap = wp_app_module.DWELL_TAIL_FACTOR * bias * mean
                for _ in range(2000):
                    d = wp_app_module.sample_period(mean, bias, floor)
                    self.assertGreaterEqual(d, floor)
                    self.assertLessEqual(d, max(cap, floor))

    def test_sample_period_preserves_mean(self):
        """The reconnects/min readout assumes the sampled mean == the requested mean."""
        mean = 120.0
        samples = [wp_app_module.sample_period(mean) for _ in range(20000)]
        self.assertAlmostEqual(sum(samples) / len(samples), mean, delta=mean * 0.1)

    def test_sample_period_bias_scales_the_mean(self):
        mean = 120.0
        slow = [wp_app_module.sample_period(mean, 1.4) for _ in range(20000)]
        fast = [wp_app_module.sample_period(mean, 0.6) for _ in range(20000)]
        self.assertGreater(sum(slow) / len(slow), sum(fast) / len(fast))

    def test_sample_period_variability_widens_the_spread(self):
        """Lower gamma shape (higher Variability) must produce a wider spread at the same mean."""
        mean = 120.0
        regular = wp_app_module.gamma_shape_from_variability(wp_app_module.VARIABILITY_RANGE[0])
        bursty = wp_app_module.gamma_shape_from_variability(wp_app_module.VARIABILITY_RANGE[1])

        def stdev(shape):
            xs = [wp_app_module.sample_period(mean, 1.0, wp_app_module.MIN_DWELL, shape)
                  for _ in range(20000)]
            m = sum(xs) / len(xs)
            return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5

        self.assertGreater(stdev(bursty), stdev(regular))

    def test_dwell_bias_range_is_symmetric_about_one(self):
        """Biases must average to 1.0 or the rate estimate drifts."""
        lo, hi = wp_app_module.DWELL_BIAS_RANGE
        self.assertAlmostEqual((lo + hi) / 2.0, 1.0)
        self.assertLess(lo, 1.0)

    # -- retry backoff ----------------------------------------------------

    def test_retry_delay_within_window(self):
        for failures in range(1, 12):
            for bias in (0.4, 1.0, 1.6):
                cap = wp_app_module.RETRY_BACKOFF_CAP * bias
                for _ in range(200):
                    d = wp_app_module.compute_retry_delay(failures, bias)
                    self.assertGreaterEqual(d, 0.0)
                    self.assertLessEqual(d, cap)

    def test_retry_delay_first_failure_is_short(self):
        for _ in range(500):
            d = wp_app_module.compute_retry_delay(1)
            self.assertLessEqual(d, wp_app_module.RETRY_BACKOFF_BASE)

    def test_retry_delay_grows_then_saturates(self):
        def mean(f):
            return sum(wp_app_module.compute_retry_delay(f) for _ in range(4000)) / 4000
        m1, m3, m5 = mean(1), mean(3), mean(5)
        self.assertLess(m1, m3)
        self.assertLess(m3, m5)
        self.assertAlmostEqual(mean(30), wp_app_module.RETRY_BACKOFF_CAP / 2,
                               delta=wp_app_module.RETRY_BACKOFF_CAP * 0.1)

    def test_retry_delay_survives_a_long_outage(self):
        """The doubling guard keeps 2**failures from blowing up after hours of failures."""
        d = wp_app_module.compute_retry_delay(100000)
        self.assertLessEqual(d, wp_app_module.RETRY_BACKOFF_CAP)

    def test_retry_delay_decorrelates_simultaneous_failures(self):
        """Full jitter, not jitter-around-the-window: two interfaces failing together must
        not land on near-identical retries, which is what caused the lockstep storm."""
        pairs = [(wp_app_module.compute_retry_delay(4, 1.0),
                  wp_app_module.compute_retry_delay(4, 1.0)) for _ in range(2000)]
        window = min(wp_app_module.RETRY_BACKOFF_CAP, wp_app_module.RETRY_BACKOFF_BASE * 8)
        close = sum(1 for a, b in pairs if abs(a - b) < window * 0.05)
        self.assertLess(close / len(pairs), 0.15)

    def test_retry_delay_treats_zero_as_first_failure(self):
        for _ in range(200):
            self.assertLessEqual(wp_app_module.compute_retry_delay(0),
                                 wp_app_module.RETRY_BACKOFF_BASE)

    def test_friendly_secrets_error_does_not_just_blame_the_password(self):
        msg = wp_app_module.friendly("Secrets were required, but not provided")
        self.assertIn("AP", msg)
        self.assertIn("password", msg)

    # -- naming / profile args -------------------------------------------

    def test_profile_name(self):
        self.assertEqual(wp_app_module.profile_name('wlan0'), 'porcupine-wlan0')

    def test_build_profile_add_args_randomizes_mac(self):
        args = wp_app_module.build_profile_add_args('wlan0', 'MyNet', 'secret')
        self.assertIn('802-11-wireless.cloned-mac-address', args)
        i = args.index('802-11-wireless.cloned-mac-address')
        self.assertEqual(args[i + 1], 'random')
        self.assertIn('wifi-sec.psk', args)
        self.assertIn('secret', args)
        self.assertIn('MyNet', args)

    def test_build_profile_add_args_open_network_omits_psk(self):
        args = wp_app_module.build_profile_add_args('wlan1', 'OpenNet', '')
        self.assertNotIn('wifi-sec.psk', args)
        self.assertNotIn('wifi-sec.key-mgmt', args)

    def test_build_profile_add_args_mac_randomization_off(self):
        args = wp_app_module.build_profile_add_args('wlan0', 'MyNet', '', randomize_mac=False)
        self.assertNotIn('802-11-wireless.cloned-mac-address', args)

    # -- scan classification helpers --------------------------------------

    def test_classify_security(self):
        self.assertEqual(wp_app_module.classify_security(''), 'Open')
        self.assertEqual(wp_app_module.classify_security('--'), 'Open')
        self.assertEqual(wp_app_module.classify_security('WPA2'), 'WPA2')
        self.assertEqual(wp_app_module.classify_security('WPA2 WPA3'), 'WPA2 WPA3')

    def test_classify_band(self):
        self.assertIsNone(wp_app_module.classify_band(None))
        self.assertEqual(wp_app_module.classify_band(2437), '2.4GHz')
        self.assertEqual(wp_app_module.classify_band(5220), '5GHz')
        self.assertEqual(wp_app_module.classify_band(6135), '6GHz')

    # -- /api/scan ---------------------------------------------------------

    @patch('wifi_porcupine.app.get_wireless_interfaces')
    @patch('wifi_porcupine.app._nmcli')
    def test_api_scan_dedupes_and_sorts_by_signal(self, mock_nmcli, mock_ifaces):
        mock_ifaces.return_value = ['wlan0']
        mock_nmcli.return_value = (True, (
            "*:HomeNetwork:60:WPA2:6:2437 MHz\n"
            ":HomeNetwork:80:WPA2:6:2437 MHz\n"  # same SSID, stronger BSSID -> should win
            ":GuestNet:40:--:44:5220 MHz\n"
        ), "")
        response = self.client.get('/api/scan')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['interface'], 'wlan0')
        self.assertEqual(len(data['networks']), 2)
        self.assertEqual(data['networks'][0]['ssid'], 'HomeNetwork')
        self.assertEqual(data['networks'][0]['signal'], 80)
        self.assertEqual(data['networks'][1]['ssid'], 'GuestNet')
        self.assertEqual(data['networks'][1]['security'], 'Open')

    @patch('wifi_porcupine.app.get_wireless_interfaces')
    def test_api_scan_unknown_interface_is_rejected(self, mock_ifaces):
        mock_ifaces.return_value = ['wlan0']
        response = self.client.get('/api/scan?interface=wlan9')
        self.assertEqual(response.status_code, 400)
        self.assertIn('wlan9', response.get_json()['error'])

    @patch('wifi_porcupine.app.get_wireless_interfaces')
    def test_api_scan_no_interfaces_detected(self, mock_ifaces):
        mock_ifaces.return_value = []
        response = self.client.get('/api/scan')
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn('No wireless interface', data['error'])

    @patch('wifi_porcupine.app.get_wireless_interfaces')
    @patch('wifi_porcupine.app._nmcli')
    def test_api_scan_uses_requested_interface(self, mock_nmcli, mock_ifaces):
        mock_ifaces.return_value = ['wlan0', 'wlan1']
        mock_nmcli.return_value = (True, "", "")
        self.client.get('/api/scan?interface=wlan1')
        args = mock_nmcli.call_args[0][0]
        self.assertIn('wlan1', args)
        self.assertIn('--rescan', args)
        self.assertEqual(args[args.index('--rescan') + 1], 'yes')

    # -- saved-password lookup ----------------------------------------------

    @patch('wifi_porcupine.app._nmcli')
    def test_find_saved_password_matches_by_ssid_property(self, mock_nmcli):
        def side_effect(args, timeout=None):
            if args[:3] == ["-t", "-f", "NAME,TYPE"]:
                return True, "OtherProfile:802-11-wireless\nHomeNetwork:802-11-wireless\n", ""
            if args[:2] == ["-t", "-f"] and "802-11-wireless.ssid" in args:
                name = args[-1]
                ssid = "SomethingElse" if name == "OtherProfile" else "HomeNetwork"
                return True, f"802-11-wireless.ssid:{ssid}\n", ""
            if "802-11-wireless-security.psk" in args:
                return True, "hunter2\n", ""
            return True, "", ""

        mock_nmcli.side_effect = side_effect
        self.assertEqual(wp_app_module.find_saved_password("HomeNetwork"), "hunter2")

    @patch('wifi_porcupine.app._nmcli')
    def test_find_saved_password_no_match_returns_none(self, mock_nmcli):
        def side_effect(args, timeout=None):
            if args[:3] == ["-t", "-f", "NAME,TYPE"]:
                return True, "OtherProfile:802-11-wireless\n", ""
            if "802-11-wireless.ssid" in args:
                return True, "802-11-wireless.ssid:SomethingElse\n", ""
            return True, "", ""

        mock_nmcli.side_effect = side_effect
        self.assertIsNone(wp_app_module.find_saved_password("HomeNetwork"))

    @patch('wifi_porcupine.app._nmcli')
    def test_find_saved_password_open_network_has_no_psk(self, mock_nmcli):
        def side_effect(args, timeout=None):
            if args[:3] == ["-t", "-f", "NAME,TYPE"]:
                return True, "GuestNet:802-11-wireless\n", ""
            if "802-11-wireless.ssid" in args:
                return True, "802-11-wireless.ssid:GuestNet\n", ""
            if "802-11-wireless-security.psk" in args:
                return False, "", "Error: no such property"
            return True, "", ""

        mock_nmcli.side_effect = side_effect
        self.assertIsNone(wp_app_module.find_saved_password("GuestNet"))

    def test_api_saved_password_requires_ssid(self):
        response = self.client.get('/api/saved-password')
        self.assertEqual(response.status_code, 400)

    @patch('wifi_porcupine.app.find_saved_password', return_value='hunter2')
    def test_api_saved_password_found(self, mock_find):
        response = self.client.get('/api/saved-password?ssid=HomeNetwork')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertTrue(data['found'])
        self.assertEqual(data['password'], 'hunter2')

    @patch('wifi_porcupine.app.find_saved_password', return_value=None)
    def test_api_saved_password_not_found(self, mock_find):
        response = self.client.get('/api/saved-password?ssid=UnknownNet')
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertFalse(data['found'])
        self.assertIsNone(data['password'])

    # -- mode detection --------------------------------------------------

    def test_detect_wifi_mode_non_linux(self):
        with patch('wifi_porcupine.app.platform.system', return_value='Darwin'):
            mode, reason = wp_app_module.detect_wifi_mode()
        self.assertIsNone(mode)
        self.assertIn('Linux', reason)

    # -- interface enumeration (mocked subprocess boundary) --------------

    def test_get_wireless_interfaces_parses_iw_dev(self):
        iw_output = b"phy#0\n\tInterface wlan0\n\t\tifindex 3\nphy#1\n\tInterface wlan1\n"
        with patch('wifi_porcupine.app.subprocess.check_output', return_value=iw_output):
            ifaces = wp_app_module.get_wireless_interfaces()
        self.assertEqual(ifaces, ['wlan0', 'wlan1'])

    # -- /api/start validation ------------------------------------------

    def test_start_route_non_linux(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=(None, 'not running on Linux')):
            response = self.client.post('/api/start', json={"interfaces": ["wlan0"], "ssid": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Linux', response.get_json()['error'])

    def test_start_route_already_running(self):
        wp_app_module.run_state['running'] = True
        response = self.client.post('/api/start', json={"interfaces": ["wlan0"], "ssid": "x"})
        self.assertEqual(response.status_code, 409)

    def test_start_route_no_interfaces(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)):
            response = self.client.post('/api/start', json={"interfaces": [], "ssid": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('interface', response.get_json()['error'].lower())

    def test_start_route_no_ssid(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={"interfaces": ["wlan0"], "ssid": ""})
        self.assertEqual(response.status_code, 400)
        self.assertIn('SSID', response.get_json()['error'])

    def test_start_route_undetected_interface(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={"interfaces": ["wlan9"], "ssid": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('wlan9', response.get_json()['error'])

    def test_start_route_rejects_out_of_range_presence(self):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={
                "interfaces": ["wlan0"], "ssid": "MyNet", "presence": 250,
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn('Presence', response.get_json()['error'])

    def test_start_route_accepts_zero_variability(self):
        """Variability 0 is a valid (metronomic) setting, not a missing value."""
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']), \
             patch('wifi_porcupine.app.threading.Thread') as mock_thread:
            response = self.client.post('/api/start', json={
                "interfaces": ["wlan0"], "ssid": "MyNet", "variability": 0,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_thread.call_args[1]['args'][0]['variability'], 0)

    @patch('wifi_porcupine.app.threading.Thread')
    def test_start_route_valid_spawns_background_thread(self, mock_thread):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0', 'wlan1']):
            response = self.client.post('/api/start', json={
                "interfaces": ["wlan0", "wlan1"],
                "ssid": "MyNet",
                "password": "secret",
                "presence": 40,
                "churn": 25,
                "variability": 70,
                "randomize_mac": True,
                "duration_minutes": 5,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'starting')
        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args[1]
        self.assertEqual(kwargs['target'], wp_app_module.start_run)
        config = kwargs['args'][0]
        self.assertEqual(config['interfaces'], ['wlan0', 'wlan1'])
        self.assertEqual(config['presence'], 40)
        self.assertEqual(config['churn'], 25)
        self.assertEqual(config['variability'], 70)
        self.assertTrue(config['randomize_mac'])

    @patch('wifi_porcupine.app.threading.Thread')
    def test_start_route_randomize_mac_defaults_off(self, mock_thread):
        """Household emulation is the default posture: a real device keeps its own MAC, so
        MAC randomization must be off unless the run explicitly asks for it."""
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={
                "interfaces": ["wlan0"], "ssid": "MyNet",
            })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(mock_thread.call_args[1]['args'][0]['randomize_mac'])
        self.assertTrue(wp_app_module.run_state['running'])

    @patch('wifi_porcupine.app.threading.Thread')
    def test_start_route_randomize_mac_can_be_disabled(self, mock_thread):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={
                "interfaces": ["wlan0"], "ssid": "MyNet", "randomize_mac": False,
            })
        self.assertEqual(response.status_code, 200)
        config = mock_thread.call_args[1]['args'][0]
        self.assertFalse(config['randomize_mac'])

    def test_stop_route_when_not_running(self):
        response = self.client.post('/api/stop')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'no run in progress')

    # -- cross-process run lock ------------------------------------------
    #
    # acquire_run_lock()/release_run_lock() guard against a second OS process
    # (not just a second request to this one) starting a competing run. flock()
    # locks are per *open file description*, not per process, so opening the
    # same path a second time from right here in the test and locking it is a
    # faithful stand-in for "another process already holds it".

    def test_acquire_and_release_run_lock_real_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, 'test.lock')
            with patch('wifi_porcupine.app.LOCK_PATH', lock_path):
                self.assertTrue(wp_app_module.acquire_run_lock('ssid=Foo'))

                # A second, independent open of the same path -- simulating another
                # process -- must not also be able to lock it.
                other_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    with self.assertRaises(OSError):
                        fcntl.flock(other_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(other_fd)

                self.assertIn('Foo', wp_app_module.read_run_lock_info())

                wp_app_module.release_run_lock()

                # Freed after release.
                other_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(other_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(other_fd, fcntl.LOCK_UN)
                finally:
                    os.close(other_fd)

    def test_release_run_lock_when_not_held_is_a_noop(self):
        wp_app_module.release_run_lock()  # must not raise

    @patch('wifi_porcupine.app.threading.Thread')
    @patch('wifi_porcupine.app.acquire_run_lock', return_value=False)
    @patch('wifi_porcupine.app.read_run_lock_info', return_value='pid=999 ssid=OtherNet')
    def test_start_route_rejected_when_another_process_holds_the_lock(
        self, mock_info, mock_acquire, mock_thread
    ):
        with patch('wifi_porcupine.app.detect_wifi_mode', return_value=('live', None)), \
             patch('wifi_porcupine.app.get_wireless_interfaces', return_value=['wlan0']):
            response = self.client.post('/api/start', json={"interfaces": ["wlan0"], "ssid": "MyNet"})
        self.assertEqual(response.status_code, 409)
        error = response.get_json()['error']
        self.assertIn('Another WiFi Porcupine process', error)
        self.assertIn('pid=999 ssid=OtherNet', error)
        mock_thread.assert_not_called()
        self.assertFalse(wp_app_module.run_state['running'])


if __name__ == '__main__':
    unittest.main()
