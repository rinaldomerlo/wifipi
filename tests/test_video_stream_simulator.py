#!/usr/bin/env python3
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

# Evict any existing 'app' module to avoid import collision with other Flask apps
if 'app' in sys.modules:
    del sys.modules['app']
if 'media_gen' in sys.modules:
    del sys.modules['media_gen']

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, 'video_stream_simulator'))

# Importing the app generates the HLS ladder as a side effect. Point it at a temp
# dir so the test run doesn't write a corpus into the working tree, and shrink the
# segment count first -- the module object is shared with the app's own
# `from media_gen import ...`, so this applies to the generation it triggers.
import media_gen as media_gen_module  # noqa: E402

media_gen_module.SEGMENT_COUNT = 2

_TEST_CONTENT_DIR = tempfile.mkdtemp(prefix='wifipi-videostream-test-')
os.environ['VIDEOSTREAM_CONTENT_DIR'] = _TEST_CONTENT_DIR

import video_stream_simulator.app as vs_app_module  # noqa: E402

vs_app = vs_app_module.app


def tearDownModule():
    shutil.rmtree(_TEST_CONTENT_DIR, ignore_errors=True)
    os.environ.pop('VIDEOSTREAM_CONTENT_DIR', None)


MASTER_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=426x240
240p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080p/index.m3u8
"""

MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:4.000,
seg-0.ts
#EXTINF:4.000,
seg-1.ts
#EXTINF:2.500,
seg-2.ts
#EXT-X-ENDLIST
"""


class TestVideoStreamSimulatorRoutes(unittest.TestCase):

    def setUp(self):
        vs_app.config['TESTING'] = True
        self.client = vs_app.test_client()
        vs_app_module.active_viewers = 0
        vs_app_module.viewer_stats.clear()

    def test_index_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Video Stream', response.data)
        self.assertIn(b'Target IP Address', response.data)

    def test_index_route_displays_hostname(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('host-badge', html)
        self.assertIn(socket.gethostname(), html)

    def test_hostname_endpoint(self):
        response = self.client.get('/api/hostname')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['hostname'], socket.gethostname())

    def test_index_renders_ladder_options(self):
        """The Fixed Rendition picker and the JS demand estimate both come from the ladder."""
        html = self.client.get('/').get_data(as_text=True)
        for rung in media_gen_module.LADDER:
            self.assertIn(rung['name'], html)

    def test_status_route_reports_idle(self):
        response = self.client.get('/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['running'])
        self.assertEqual(data['active_viewers'], 0)

    def test_status_route_reports_running(self):
        vs_app_module.active_viewers = 3
        try:
            data = self.client.get('/status').get_json()
            self.assertTrue(data['running'])
            self.assertEqual(data['active_viewers'], 3)
        finally:
            vs_app_module.active_viewers = 0

    def test_interfaces_route(self):
        response = self.client.get('/interfaces')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertIsInstance(data['interfaces'], list)

    def test_stop_when_idle(self):
        response = self.client.post('/stop')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'no test running')


class TestMetricsRoute(unittest.TestCase):

    def setUp(self):
        vs_app.config['TESTING'] = True
        self.client = vs_app.test_client()
        vs_app_module.active_viewers = 0
        vs_app_module.viewer_stats.clear()

    def tearDown(self):
        vs_app_module.viewer_stats.clear()
        vs_app_module.active_viewers = 0

    def test_metrics_empty(self):
        data = self.client.get('/metrics').get_json()
        self.assertFalse(data['running'])
        self.assertEqual(data['viewers'], [])
        self.assertEqual(data['aggregate']['viewer_count'], 0)
        self.assertIsNone(data['aggregate']['avg_startup_s'])

    def test_metrics_aggregates_across_viewers(self):
        vs_app_module.viewer_stats[1] = {
            "viewer_id": 1, "rendition": "720p", "bitrate_kbps": 2800,
            "throughput_kbps": 3500.0, "buffer_s": 20.0, "stalls": 1,
            "stall_seconds": 2.5, "switches": 3, "startup_s": 1.2,
            "segments": 10, "bytes": 14_000_000, "state": "playing",
        }
        vs_app_module.viewer_stats[2] = {
            "viewer_id": 2, "rendition": "480p", "bitrate_kbps": 1400,
            "throughput_kbps": 1800.0, "buffer_s": 12.0, "stalls": 2,
            "stall_seconds": 4.5, "switches": 1, "startup_s": 2.8,
            "segments": 8, "bytes": 6_000_000, "state": "playing",
        }
        vs_app_module.active_viewers = 2

        data = self.client.get('/metrics').get_json()
        agg = data['aggregate']
        self.assertTrue(data['running'])
        self.assertEqual(agg['viewer_count'], 2)
        self.assertEqual(agg['stalls'], 3)
        self.assertAlmostEqual(agg['stall_seconds'], 7.0)
        self.assertEqual(agg['total_mb'], 20.0)
        self.assertEqual(agg['avg_bitrate_kbps'], 2100)  # (2800 + 1400) / 2
        self.assertAlmostEqual(agg['avg_startup_s'], 2.0)
        self.assertEqual([v['viewer_id'] for v in data['viewers']], [1, 2])

    def test_metrics_falls_back_to_finished_viewers(self):
        """Once every viewer has finished, the averages must describe the run that ran
        rather than collapsing to zero because nothing is 'playing' any more."""
        vs_app_module.viewer_stats[1] = {
            "viewer_id": 1, "rendition": "1080p", "bitrate_kbps": 5000,
            "throughput_kbps": 6000.0, "buffer_s": 0.0, "stalls": 0,
            "stall_seconds": 0.0, "switches": 2, "startup_s": 0.9,
            "segments": 30, "bytes": 60_000_000, "state": "finished",
        }
        agg = self.client.get('/metrics').get_json()['aggregate']
        self.assertEqual(agg['avg_bitrate_kbps'], 5000)


class TestStartValidation(unittest.TestCase):

    def setUp(self):
        vs_app.config['TESTING'] = True
        self.client = vs_app.test_client()
        vs_app_module.active_viewers = 0

    def _post(self, **kwargs):
        body = {"target_ip": "192.168.1.50", "duration_minutes": 5, "intensity": 2}
        body.update(kwargs)
        return self.client.post('/start', json=body)

    def test_rejects_invalid_ip(self):
        response = self._post(target_ip="not-an-ip")
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid target IP', response.get_json()['error'])

    def test_rejects_missing_ip(self):
        response = self._post(target_ip="")
        self.assertEqual(response.status_code, 400)

    def test_rejects_bad_duration(self):
        response = self._post(duration_minutes=0)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Duration', response.get_json()['error'])

    def test_rejects_out_of_range_intensity(self):
        response = self._post(intensity=99)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Intensity', response.get_json()['error'])

    def test_rejects_bad_port(self):
        response = self._post(target_port=70000)
        self.assertEqual(response.status_code, 400)
        self.assertIn('port', response.get_json()['error'])

    def test_refuses_when_already_running(self):
        vs_app_module.active_viewers = 1
        try:
            response = self._post()
            self.assertEqual(response.status_code, 409)
        finally:
            vs_app_module.active_viewers = 0


class TestPlaylistParsing(unittest.TestCase):

    def test_parse_master_sorts_by_bitrate(self):
        renditions = vs_app_module.parse_master_playlist(MASTER_PLAYLIST)
        self.assertEqual([r['bitrate_kbps'] for r in renditions], [400, 800, 5000])
        self.assertEqual(renditions[0]['playlist'], '240p/index.m3u8')
        self.assertEqual(renditions[2]['resolution'], '1920x1080')

    def test_parse_master_ignores_junk(self):
        self.assertEqual(vs_app_module.parse_master_playlist("#EXTM3U\n"), [])

    def test_parse_media_playlist(self):
        segments = vs_app_module.parse_media_playlist(MEDIA_PLAYLIST)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0]['uri'], 'seg-0.ts')
        self.assertAlmostEqual(segments[2]['duration'], 2.5)

    def test_parse_media_playlist_defaults_duration(self):
        """A segment line with no preceding EXTINF still has to advance the buffer."""
        segments = vs_app_module.parse_media_playlist("#EXTM3U\nseg-0.ts\n")
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0]['duration'], 4.0)

    def test_roundtrip_against_generated_playlists(self):
        """The generator and the client parser have to agree — they are the two halves
        of the same contract, and only a real generated corpus proves it."""
        with open(os.path.join(_TEST_CONTENT_DIR, 'master.m3u8')) as f:
            renditions = vs_app_module.parse_master_playlist(f.read())
        self.assertEqual(len(renditions), len(media_gen_module.LADDER))

        first = renditions[0]
        with open(os.path.join(_TEST_CONTENT_DIR, first['playlist'])) as f:
            segments = vs_app_module.parse_media_playlist(f.read())
        self.assertEqual(len(segments), media_gen_module.SEGMENT_COUNT)


class TestAbrSelection(unittest.TestCase):

    LADDER = [
        {"name": "240p", "bitrate_kbps": 400},
        {"name": "360p", "bitrate_kbps": 800},
        {"name": "480p", "bitrate_kbps": 1400},
        {"name": "720p", "bitrate_kbps": 2800},
        {"name": "1080p", "bitrate_kbps": 5000},
    ]

    def test_picks_bottom_rung_on_a_terrible_link(self):
        self.assertEqual(vs_app_module.select_rendition(self.LADDER, 100, 0), 0)

    def test_steps_up_one_rung_at_a_time(self):
        """A single fast segment must not fling the session to the top of the ladder."""
        self.assertEqual(vs_app_module.select_rendition(self.LADDER, 50_000, 0), 1)
        self.assertEqual(vs_app_module.select_rendition(self.LADDER, 50_000, 1), 2)

    def test_drops_without_restriction(self):
        """Dropping is unrestricted: a collapsed link needs the player out of the way now."""
        self.assertEqual(vs_app_module.select_rendition(self.LADDER, 450, 4), 0)

    def test_applies_safety_factor(self):
        """1750 kbps measured * 0.8 = 1400, so 480p (1400) fits but nothing above it."""
        self.assertEqual(vs_app_module.select_rendition(self.LADDER, 1750, 2), 2)
        # Just under, and the same estimate no longer affords 480p.
        self.assertEqual(vs_app_module.select_rendition(self.LADDER, 1730, 2), 1)


class TestBufferModel(unittest.TestCase):

    def test_drain_reduces_buffer(self):
        buffer_s, stall_s = vs_app_module._drain(20.0, 3.0, playing=True)
        self.assertAlmostEqual(buffer_s, 17.0)
        self.assertEqual(stall_s, 0.0)

    def test_drain_reports_stall_when_buffer_runs_dry(self):
        buffer_s, stall_s = vs_app_module._drain(2.0, 5.0, playing=True)
        self.assertEqual(buffer_s, 0.0)
        self.assertAlmostEqual(stall_s, 3.0)

    def test_no_drain_before_playback_starts(self):
        """Startup buffering is not a stall — nothing is playing yet to interrupt."""
        buffer_s, stall_s = vs_app_module._drain(1.0, 8.0, playing=False)
        self.assertAlmostEqual(buffer_s, 1.0)
        self.assertEqual(stall_s, 0.0)


class TestContentUrl(unittest.TestCase):

    def test_flask_port_uses_direct_content_path(self):
        url = vs_app_module.content_url('192.168.1.10', 5012, 'master.m3u8')
        self.assertEqual(url, 'http://192.168.1.10:5012/content/master.m3u8')

    def test_other_ports_use_the_nginx_alias(self):
        url = vs_app_module.content_url('192.168.1.10', 80, '720p/seg-0.ts')
        self.assertEqual(url, 'http://192.168.1.10:80/videostream/content/720p/seg-0.ts')


class _CountingHandler(BaseHTTPRequestHandler):
    """Serves a fixed body and counts how many TCP connections were opened.

    One handler instance is constructed per connection, so counting instances counts
    connections -- which is exactly what the keep-alive behaviour needs to be measured on.
    """

    protocol_version = 'HTTP/1.1'   # required, or the server closes after every response
    connections = 0
    body = b'x' * 4096

    def __init__(self, *args, **kwargs):
        type(self).connections += 1
        super().__init__(*args, **kwargs)

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class TestSessionTransport(unittest.TestCase):
    """The transport is the layer that was measuring itself instead of the link."""

    def setUp(self):
        _CountingHandler.connections = 0
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _CountingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_one_session_reuses_a_single_connection(self):
        # The whole point of the dependency: five segments must not cost five handshakes
        # and five slow starts, which is what capped throughput on a high-RTT link.
        session = vs_app_module.make_session(0)
        try:
            for i in range(5):
                vs_app_module.fetch(session, f'{self.base}/seg-{i}.ts')
        finally:
            session.close()
        self.assertEqual(_CountingHandler.connections, 1)

    def test_a_session_per_request_costs_a_connection_each(self):
        # Guards the comparison above: the counter really does track connections.
        for i in range(5):
            session = vs_app_module.make_session(0)
            try:
                vs_app_module.fetch(session, f'{self.base}/seg-{i}.ts')
            finally:
                session.close()
        self.assertEqual(_CountingHandler.connections, 5)

    def test_fetch_counts_the_bytes_it_read(self):
        session = vs_app_module.make_session(0)
        try:
            self.assertEqual(vs_app_module.fetch(session, f'{self.base}/seg-0.ts'),
                             len(_CountingHandler.body))
        finally:
            session.close()

    def test_requests_identity_encoding(self):
        # A gzip layer would decouple bytes-counted from bytes-on-the-wire and skew the
        # throughput estimate the ABR runs on.
        session = vs_app_module.make_session(0)
        self.addCleanup(session.close)
        self.assertEqual(session.headers['Accept-Encoding'], 'identity')

    def test_segments_get_no_transport_retry(self):
        # A failed segment must reach the ABR logic immediately so it can drop a rung.
        self.assertEqual(vs_app_module.SEGMENT_ATTEMPTS, 0)
        session = vs_app_module.make_session(vs_app_module.SEGMENT_ATTEMPTS)
        self.addCleanup(session.close)
        self.assertEqual(session.get_adapter('http://h/').max_retries.total, 0)

    def test_playlists_get_a_retry_budget(self):
        session = vs_app_module.make_session(vs_app_module.PLAYLIST_ATTEMPTS)
        self.addCleanup(session.close)
        retry = session.get_adapter('http://h/').max_retries
        self.assertEqual(retry.total, vs_app_module.PLAYLIST_ATTEMPTS)
        self.assertEqual(retry.backoff_factor, vs_app_module.PLAYLIST_RETRY_BACKOFF_S)


class _SlowLinkHandler(BaseHTTPRequestHandler):
    """Serves playlists instantly but segments slowly enough to sit under the bottom rung."""

    protocol_version = 'HTTP/1.1'
    master = (b'#EXTM3U\n'
              b'#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=426x240\n240p/index.m3u8\n'
              b'#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n360p/index.m3u8\n')
    media = (b'#EXTM3U\n#EXT-X-TARGETDURATION:4\n'
             b'#EXTINF:4.000,\nseg-0.ts\n#EXTINF:4.000,\nseg-1.ts\n#EXT-X-ENDLIST\n')
    # 4 KB taking ~0.2s is ~160 kbps -- under the 400 kbps floor, and quick to test.
    segment = b'y' * 4096
    delay = 0.2

    def do_GET(self):
        if self.path.endswith('master.m3u8'):
            body = self.master
        elif self.path.endswith('index.m3u8'):
            body = self.media
        else:
            time.sleep(self.delay)
            body = self.segment
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestLinkLimitedDetection(unittest.TestCase):
    """A link too slow for the bottom rung must be reported as a link problem, in words.

    The app previously emitted correct numbers that read exactly like a malfunctioning
    player, which cost a full debugging session before anyone checked the link itself.
    """

    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _SlowLinkHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        vs_app_module.stop_event.clear()
        vs_app_module.viewer_stats.clear()
        vs_app_module.active_viewers = 0
        while not vs_app_module.output_queue.empty():
            vs_app_module.output_queue.get()

    def tearDown(self):
        vs_app_module.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        vs_app_module.stop_event.clear()
        vs_app_module.viewer_stats.clear()
        vs_app_module.active_viewers = 0

    def _run_viewer(self, seconds=6.0):
        thread = threading.Thread(
            target=vs_app_module.run_viewer_loop,
            args=(1, '127.0.0.1', self.port, 1, True, ''), daemon=True)
        thread.start()
        deadline = time.time() + seconds
        # Captured while the viewer is live: joining ends the session, which overwrites
        # the state with "finished".
        self.observed_state = None
        while time.time() < deadline:
            state = vs_app_module.viewer_stats.get(1, {}).get('state')
            if state == 'link-limited':
                self.observed_state = state
                break
            time.sleep(0.1)
        vs_app_module.stop_event.set()
        thread.join(20)
        lines = []
        while not vs_app_module.output_queue.empty():
            line = vs_app_module.output_queue.get()
            if line:
                lines.append(line)
        return ''.join(lines)

    def test_reports_link_limited_in_plain_words(self):
        log = self._run_viewer()
        self.assertIn('LINK-LIMITED', log)
        self.assertIn('not a fault in this app', log)

    def test_prints_the_verification_commands_prefilled(self):
        # An operator should not have to compose the oracle command under pressure.
        log = self._run_viewer()
        self.assertIn('curl -s -o /dev/null', log)
        self.assertIn(f'http://127.0.0.1:{self.port}/videostream/content/240p/seg-', log)
        self.assertIn('iperf3 -c 127.0.0.1', log)

    def test_state_is_distinct_from_buffering(self):
        self._run_viewer()
        self.assertEqual(self.observed_state, 'link-limited')

    def test_link_limited_viewers_still_count_as_live_in_metrics(self):
        # Dropping them would make the aggregate describe only the viewers doing well.
        self._run_viewer()
        vs_app.config['TESTING'] = True
        vs_app_module.viewer_stats[1]['state'] = 'link-limited'   # as it was mid-run
        agg = vs_app.test_client().get('/metrics').get_json()['aggregate']
        self.assertEqual(agg['viewer_count'], 1)
        self.assertGreater(agg['avg_bitrate_kbps'], 0)

    def test_needs_several_consecutive_segments(self):
        # One slow segment is ordinary variance, not a verdict on the link.
        self.assertGreaterEqual(vs_app_module.LINK_LIMITED_SEGMENTS, 2)


class TestStopBehaviour(unittest.TestCase):

    def setUp(self):
        vs_app.config['TESTING'] = True
        self.client = vs_app.test_client()
        vs_app_module.stop_event.clear()
        vs_app_module.active_viewers = 0
        while not vs_app_module.output_queue.empty():
            vs_app_module.output_queue.get()

    def tearDown(self):
        vs_app_module.stop_event.clear()
        vs_app_module.active_viewers = 0

    def _log(self):
        lines = []
        while not vs_app_module.output_queue.empty():
            line = vs_app_module.output_queue.get()
            if line:
                lines.append(line)
        return ''.join(lines)

    def test_sets_the_event_even_before_viewers_register(self):
        # Threads increment active_viewers themselves, so a Stop in the gap after /start
        # used to report "no test running" and set nothing, leaving the run going.
        self.assertEqual(vs_app_module.active_viewers, 0)
        self.client.post('/stop')
        self.assertTrue(vs_app_module.stop_event.is_set())

    def test_acknowledges_in_the_live_log(self):
        # The operator is watching the Live Segment Stream, not the HTTP response.
        vs_app_module.active_viewers = 3
        response = self.client.post('/stop')
        self.assertEqual(response.get_json()['viewers'], 3)
        log = self._log()
        self.assertIn('Stop requested', log)
        self.assertIn('3 viewer(s)', log)

    def test_transfer_aborts_mid_segment(self):
        # Without this, a Stop lands only after the current read finishes -- up to
        # SEGMENT_TIMEOUT_S of silence on a bad link.
        session = vs_app_module.make_session(0)
        self.addCleanup(session.close)
        chunks = [b'x' * 65536 for _ in range(50)]

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def iter_content(self, size): return iter(chunks)
            def __enter__(self): return self
            def __exit__(self, *a): return False

        vs_app_module.stop_event.set()
        with patch.object(session, 'get', return_value=_Resp()):
            with self.assertRaises(vs_app_module.StopRequested):
                vs_app_module.fetch(session, 'http://h/240p/seg-0.ts')


class TestPlaylistFetchError(unittest.TestCase):

    def test_error_names_the_failing_url(self):
        # The whole point of the type: the log line has to say what could not be fetched.
        url = 'http://192.168.0.180:80/videostream/content/240p/index.m3u8'
        session = vs_app_module.make_session(0)
        self.addCleanup(session.close)
        with patch.object(session, 'get',
                          side_effect=requests.exceptions.ReadTimeout('timed out')):
            with self.assertRaises(vs_app_module.PlaylistFetchError) as ctx:
                vs_app_module.fetch_playlist(session, url)
        self.assertEqual(ctx.exception.url, url)
        self.assertIn('240p/index.m3u8', str(ctx.exception))
        self.assertIn('timed out', str(ctx.exception))

    def test_uses_split_connect_and_read_timeouts(self):
        session = vs_app_module.make_session(0)
        self.addCleanup(session.close)
        with patch.object(session, 'get') as mock:
            vs_app_module.fetch_playlist(session, 'http://h/master.m3u8')
        self.assertEqual(mock.call_args.kwargs['timeout'],
                         (vs_app_module.CONNECT_TIMEOUT_S, vs_app_module.PLAYLIST_TIMEOUT_S))

    def test_http_error_status_is_reported_not_parsed(self):
        # A 404 used to sail through as an empty playlist with no renditions.
        session = vs_app_module.make_session(0)
        self.addCleanup(session.close)
        response = requests.Response()
        response.status_code = 404
        response.url = 'http://h/master.m3u8'
        response.raw = io.BytesIO(b'')   # the context manager closes it on the way out
        with patch.object(session, 'get', return_value=response):
            with self.assertRaises(vs_app_module.PlaylistFetchError) as ctx:
                vs_app_module.fetch_playlist(session, 'http://h/master.m3u8')
        self.assertIn('404', str(ctx.exception))


class TestMediaGeneration(unittest.TestCase):

    def test_nominal_segment_bytes(self):
        # 5000 kbps for 4s = 20 Mbit = 2.5 MB
        self.assertEqual(media_gen_module.nominal_segment_bytes(5000, 4), 2_500_000)

    def test_generated_corpus_is_complete(self):
        manifest_path = os.path.join(_TEST_CONTENT_DIR, 'manifest.json')
        self.assertTrue(os.path.exists(manifest_path))
        with open(manifest_path) as f:
            manifest = json.load(f)

        self.assertEqual(len(manifest['renditions']), len(media_gen_module.LADDER))
        for rendition in manifest['renditions']:
            playlist = os.path.join(_TEST_CONTENT_DIR, rendition['playlist'])
            self.assertTrue(os.path.exists(playlist), playlist)
            for i in range(rendition['segment_count']):
                segment = os.path.join(_TEST_CONTENT_DIR, rendition['name'], f'seg-{i}.ts')
                self.assertTrue(os.path.exists(segment), segment)

    def test_segment_sizes_track_the_ladder_bitrate(self):
        """Segment size is the whole mechanism — a rung that doesn't cost what it claims
        would make every throughput and ABR number meaningless."""
        with open(os.path.join(_TEST_CONTENT_DIR, 'manifest.json')) as f:
            manifest = json.load(f)

        for rendition in manifest['renditions']:
            expected = media_gen_module.nominal_segment_bytes(rendition['bitrate_kbps'])
            actual = rendition['total_bytes'] / rendition['segment_count']
            # Jittered +/- SIZE_JITTER per segment, so allow a little more than that.
            self.assertLess(abs(actual - expected) / expected, media_gen_module.SIZE_JITTER + 0.05)

    def test_ensure_is_idempotent(self):
        """Unlike the browsing corpus this is not regenerated per start: a stable corpus
        is what makes two runs against the same target comparable."""
        before = os.path.getmtime(os.path.join(_TEST_CONTENT_DIR, 'manifest.json'))
        media_gen_module.ensure_media_ladder(_TEST_CONTENT_DIR)
        after = os.path.getmtime(os.path.join(_TEST_CONTENT_DIR, 'manifest.json'))
        self.assertEqual(before, after)


class TestScan(unittest.TestCase):

    def test_scan_without_nmap_raises(self):
        with patch('video_stream_simulator.app.shutil.which', return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                vs_app_module.scan_for_servers('wlan0')
        self.assertIn('nmap', str(ctx.exception))

    def test_scan_route_returns_json_error(self):
        """Off-Linux dev must get a JSON error, not Flask's HTML 500 page."""
        vs_app.config['TESTING'] = True
        client = vs_app.test_client()
        with patch('video_stream_simulator.app.scan_for_servers', side_effect=RuntimeError('boom')):
            response = client.post('/scan', json={'bind_interface': 'wlan0'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['error'], 'boom')


if __name__ == '__main__':
    unittest.main()
