#!/usr/bin/env python3
"""
Flask web app that simulates adaptive-bitrate video streaming between two Pis,
as a complement to the browsing simulator's bursty page loads and iperf3's
sustained stream. Every instance serves a generated HLS ABR ladder (see
media_gen.py) and can also drive viewers against another instance's ladder.

What makes this a *streaming* load rather than a bulk download is the playback
clock: each simulated viewer keeps a media buffer, fetches segments only while
that buffer is below target, and then idles -- so the link sees the on/off
sawtooth a real player produces, not a flat-out transfer. Each viewer also runs
its own ABR logic, estimating throughput from recent segment downloads and
switching rendition to match, which turns link quality into the metrics that
actually matter for video: startup delay, rebuffer (stall) count and ratio, and
how far down the ladder the session was forced.

An "intensity" slider (1-10) runs that many independent viewers concurrently.
"""

import ipaddress
import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import requests
import requests.adapters
from urllib3.util.retry import Retry

from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_from_directory

# Adjust path to import media_gen.py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from media_gen import ensure_media_ladder

app = Flask(__name__)

# VIDEOSTREAM_CONTENT_DIR relocates the generated ladder. Mainly for the test suite,
# which points it at a temp dir so importing this module doesn't write ~62 MB into the
# working tree; also useful in production to put the corpus on a USB disk rather than
# the SD card.
CONTENT_DIR = os.environ.get("VIDEOSTREAM_CONTENT_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "content"
)
USE_FFMPEG = os.environ.get("VIDEOSTREAM_USE_FFMPEG", "").strip() in ("1", "true", "yes")
MANIFEST = ensure_media_ladder(CONTENT_DIR, use_ffmpeg=USE_FFMPEG)

CONTENT_PORT = 5012

# Player tuning. BUFFER_TARGET_S is how much media a viewer tries to keep ahead of
# the playhead -- once it's reached, fetching pauses until playback drains it back
# down, which is what produces the on/off request pattern. STARTUP_BUFFER_S is how
# much must land before playback begins, i.e. what startup delay measures.
BUFFER_TARGET_S = 24.0
STARTUP_BUFFER_S = 4.0
SEGMENT_TIMEOUT_S = 30

# ABR tuning. The safety factor keeps the chosen rendition below the measured
# throughput (a rung sized exactly at the estimate stalls the moment the link
# dips), and the EWMA weight smooths single-segment noise.
ABR_SAFETY_FACTOR = 0.8
ABR_EWMA_WEIGHT = 0.3

# Playlist fetching. Playlists are a few hundred bytes, but they cross the same link this
# app exists to congest -- so a slow one is a symptom to ride out, not a reason to abandon
# the viewer. Retry with backoff on a longer ceiling than fetch_text's bare default, which
# was tight enough that one hiccup on a 400-byte request killed a whole session.
PLAYLIST_TIMEOUT_S = 15
PLAYLIST_ATTEMPTS = 3
PLAYLIST_RETRY_BACKOFF_S = 1.0
# Split from the read timeouts: refusing to connect is a fast, unambiguous answer, so
# there's no reason to spend a segment's full patience budget on it.
CONNECT_TIMEOUT_S = 5

# How many consecutive segments must measure below the ladder's bottom rung before the
# run is called link-limited. More than one because a single slow segment is ordinary
# variance; three in a row on a link that cannot even carry the lowest rendition is a
# statement about the network, and the app should say so in those words rather than
# leaving an operator to infer it from a wall of REBUFFER lines.
LINK_LIMITED_SEGMENTS = 3

# Segments deliberately get *no* transport retry. A failed segment is a real streaming
# event the ABR logic is written to react to -- dropping a rung immediately, as a player
# would -- and silently retrying underneath would both delay that reaction and fold the
# retry time into the throughput estimate driving it.
SEGMENT_ATTEMPTS = 0

INTENSITY_RANGE = (1, 10)
DEFAULT_INTENSITY = 2

# Global state for the running simulation. active_viewers is only ever mutated inside
# run_viewer_loop itself (mirroring the browsing simulator) so it accurately reflects
# how many viewer threads' bodies are actually running.
stop_event = threading.Event()
active_viewers = 0
test_lock = threading.Lock()
output_queue = queue.Queue()
viewer_stats: dict[int, dict] = {}


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def is_valid_ip(ip: str) -> bool:
    pattern = r"^\d{1,3}(\.\d{1,3}){3}$"
    return bool(re.match(pattern, ip))


def get_bindable_interfaces() -> list:
    """Real network interfaces with a live IPv4 address, i.e. usable as a scan-bind
    target -- excludes loopback and anything with no address. Read-only (`ip addr show`),
    so no privilege is needed. Returns [] off-Linux or without iproute2 installed;
    callers treat that as "can't verify" rather than "none exist".
    """
    if not shutil.which("ip"):
        return []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []

    interfaces = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] not in interfaces:
            interfaces.append(parts[1])
    return interfaces


def scan_for_servers(bind_interface: str = "wlan0", meta: dict | None = None) -> list[dict]:
    """Scan the LAN for other Pis running this app (port 5012 open), excluding local host IPs.

    `meta`, if given, is filled in with the interface and subnet the scan actually used
    (which may differ from `bind_interface` -- see the fallback below). The caller reports
    those back to the UI so an empty result can be told apart from a scan that quietly ran
    against the wrong subnet.
    """
    if not shutil.which("nmap"):
        raise RuntimeError("nmap is not installed. Run: sudo apt-get install nmap")

    detected = get_bindable_interfaces()
    if detected and bind_interface not in detected:
        bind_interface = detected[0]

    local_ips = {"127.0.0.1"}

    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        ip_str = str(ipaddress.ip_interface(parts[1]).ip)
                        if not ip_str.startswith("127."):
                            local_ips.add(ip_str)
                    except Exception:
                        pass
    except Exception:
        pass

    cidr = None
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", bind_interface],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        iface_obj = ipaddress.ip_interface(parts[1])
                        ip_str = str(iface_obj.ip)
                        if not ip_str.startswith("127."):
                            local_ips.add(ip_str)
                            cidr = str(iface_obj.network)
                            break
                    except Exception:
                        pass
    except Exception:
        pass

    if not cidr:
        raise RuntimeError(f"No active IPv4 address found on interface {bind_interface}")

    if meta is not None:
        meta.update({"interface": bind_interface, "subnet": cidr, "port": CONTENT_PORT})

    cmd = ["nmap", "-e", bind_interface, "-Pn", "-p", str(CONTENT_PORT), "--open", "-n", "-T4", "-oG", "-", cidr]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"LAN scan of {cidr} on {bind_interface} timed out after 60s. "
            "A congested link can push a /24 sweep past the limit -- retry, or set the target IP manually."
        )
    except Exception as e:
        raise RuntimeError(f"Failed to run nmap: {e}")

    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(
            result.stderr.strip() or f"nmap scan of {cidr} on {bind_interface} failed (exit {result.returncode})."
        )

    found = []
    for line in result.stdout.splitlines():
        if line.startswith("Host:") and "/open" in line:
            parts = line.split()
            if len(parts) >= 2:
                ip = parts[1]
                if ip not in local_ips:
                    found.append({"ip": ip, "port": CONTENT_PORT})

    def ip_key(item):
        try:
            return (0, socket.inet_aton(item["ip"]))
        except Exception:
            return (1, item["ip"])

    found.sort(key=ip_key)
    return found


def content_url(target_ip: str, target_port: int, relative_path: str) -> str:
    """
    Build the URL for a piece of the generated ladder on the target.

    Port 5012 is this app's own Flask port, reachable directly in local dev
    (no nginx in front) at /content/... . Any other port is assumed to be the
    target's nginx (typically 80), where /videostream/content/ is aliased
    straight to the generated ladder directory -- the realistic path, and the
    only one that can actually keep up with several 1080p viewers, since it
    serves segments from nginx rather than through Python.
    """
    prefix = "/content/" if target_port == CONTENT_PORT else "/videostream/content/"
    return f"http://{target_ip}:{target_port}{prefix}{relative_path}"


def make_session(retries: int) -> requests.Session:
    """Build a viewer's HTTP session.

    The point is the connection pool underneath: reusing one warm connection across a
    viewer's segments skips a TCP handshake *and* keeps the congestion window, whereas a
    fresh connection per segment restarts slow start every time. On a high-RTT link that
    difference dominates the measurement -- at 200 ms RTT a ~200 KB segment needs roughly
    4.5-5.5 round trips cold (handshake and first byte, then doublings from an initial
    window of ~14 KB), capping apparent throughput somewhere near 1.3-1.8 Mbps no matter
    how fast the link really is. That is at or below the 1.0 Mbps the ABR needs to justify
    even the 360p rung, so a cold-connecting client can report a fast link as 240p-only.

    One caveat on how much credit this deserves: Linux's tcp_slow_start_after_idle (on by
    default) resets the congestion window after an idle period, and viewers idle by design
    once the buffer reaches BUFFER_TARGET_S. So on a *healthy* link the kernel re-slow-starts
    each buffer cycle regardless of this pooling, and the win is concentrated in the
    continuously-fetching case -- a link too poor to ever fill the buffer, which is exactly
    when the measurement matters most. Setting net.ipv4.tcp_slow_start_after_idle=0 on both
    ends would extend the benefit to the healthy case, but that is a host tuning decision
    and not something this app should impose.

    Sessions are not thread-safe, so each viewer thread builds its own.
    """
    session = requests.Session()
    # Never let the server compress: these are incompressible random bytes anyway, but a
    # gzip layer would decouple bytes-on-the-wire from bytes-counted and quietly corrupt
    # the throughput estimate the ABR runs on.
    session.headers["Accept-Encoding"] = "identity"
    adapter = requests.adapters.HTTPAdapter(
        max_retries=Retry(total=retries, backoff_factor=PLAYLIST_RETRY_BACKOFF_S,
                          status_forcelist=[502, 503, 504], allowed_methods=["GET"])
    )
    session.mount("http://", adapter)
    return session


def fetch(session: requests.Session, url: str, timeout: int = SEGMENT_TIMEOUT_S) -> int:
    """GET a URL and return the number of bytes read, discarding the body.

    Streamed and counted chunk by chunk rather than buffered whole: a 1080p segment is
    2.5 MB and ten viewers holding one each is memory this app has no reason to spend.
    """
    with session.get(url, stream=True, timeout=(CONNECT_TIMEOUT_S, timeout)) as resp:
        resp.raise_for_status()
        return sum(len(chunk) for chunk in resp.iter_content(65536))


def fetch_text(session: requests.Session, url: str, timeout: int = PLAYLIST_TIMEOUT_S) -> str:
    with session.get(url, timeout=(CONNECT_TIMEOUT_S, timeout)) as resp:
        resp.raise_for_status()
        return resp.text


class PlaylistFetchError(Exception):
    """A playlist that would not load, carrying the URL for the log line.

    Worth its own type purely so the viewer loop can tell "this playlist would not load"
    apart from a bug, and so the operator is told *which* URL died rather than a bare
    "timed out" with no indication of what was being fetched.
    """

    def __init__(self, url: str, cause: Exception):
        super().__init__(f"{url}: {cause}")
        self.url = url
        self.cause = cause


def fetch_playlist(session: requests.Session, url: str) -> str:
    """GET a playlist, translating any transport failure into a URL-bearing error.

    The retrying happens a layer down, in the session's Retry policy -- playlists are
    tiny but cross the same link this app exists to congest, so a slow one is a symptom
    to ride out rather than a reason to abandon the viewer.
    """
    try:
        return fetch_text(session, url)
    except requests.RequestException as e:
        raise PlaylistFetchError(url, e) from e


def parse_master_playlist(text: str) -> list[dict]:
    """Parse an HLS master playlist into a bitrate-sorted list of renditions.

    The client walks real playlists rather than the convenience manifest.json so it
    issues the same request chain a real player does (master -> media -> segments),
    and so it still works against a corpus ffmpeg produced.
    """
    renditions = []
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = line.split(":", 1)[1]
        bandwidth = 0
        resolution = ""
        match = re.search(r"BANDWIDTH=(\d+)", attrs)
        if match:
            bandwidth = int(match.group(1))
        match = re.search(r"RESOLUTION=(\d+x\d+)", attrs)
        if match:
            resolution = match.group(1)
        uri = ""
        for candidate in lines[i + 1:]:
            if candidate and not candidate.startswith("#"):
                uri = candidate
                break
        if uri and bandwidth:
            renditions.append({
                "bitrate_kbps": bandwidth // 1000,
                "resolution": resolution,
                "name": resolution.split("x")[-1] + "p" if resolution else f"{bandwidth // 1000}k",
                "playlist": uri,
            })
    renditions.sort(key=lambda r: r["bitrate_kbps"])
    return renditions


def parse_media_playlist(text: str) -> list[dict]:
    """Parse an HLS media playlist into an ordered list of {uri, duration} segments."""
    segments = []
    duration = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",")[0])
            except (ValueError, IndexError):
                duration = None
        elif line and not line.startswith("#"):
            segments.append({"uri": line, "duration": duration or 4.0})
            duration = None
    return segments


def select_rendition(renditions: list[dict], bandwidth_kbps: float, current_index: int) -> int:
    """Pick a ladder index for the measured bandwidth.

    Two behaviours copied from real players: a safety factor, so the chosen rung sits
    below the estimate rather than exactly at it; and a one-rung-at-a-time cap when
    stepping *up*, which stops a single fast segment from flinging the session to 1080p
    and straight back down. Dropping is unrestricted -- when the link goes bad the
    player needs to get out of the way immediately or it stalls.
    """
    budget = bandwidth_kbps * ABR_SAFETY_FACTOR
    target = 0
    for i, r in enumerate(renditions):
        if r["bitrate_kbps"] <= budget:
            target = i
    if target > current_index:
        return min(current_index + 1, target)
    return target


def _drain(buffer_s: float, elapsed_s: float, playing: bool) -> tuple[float, float]:
    """Advance the playback clock over elapsed_s. Returns (new_buffer_s, stall_s).

    A stall is simply the buffer going empty before the fetch that was meant to
    refill it returned -- exactly the condition a viewer experiences as a spinner.
    """
    if not playing:
        return buffer_s, 0.0
    if elapsed_s <= buffer_s:
        return buffer_s - elapsed_s, 0.0
    return 0.0, elapsed_s - buffer_s


def run_viewer_loop(viewer_id: int, target_ip: str, target_port: int,
                    duration_minutes: int, abr_enabled: bool, pinned_rendition: str) -> None:
    """
    Run one simulated ABR viewer in a background thread, streaming per-segment
    summaries to output_queue and keeping live QoE numbers in viewer_stats.

    Multiple viewers run concurrently (one per intensity level), each independently
    randomized -- a startup jitter so they don't all request in lockstep, and their
    own bandwidth estimate and ladder position -- so raising intensity scales load
    without collapsing the viewers into a single synchronized request pattern.
    """
    global active_viewers

    with test_lock:
        active_viewers += 1

    end_time = time.time() + duration_minutes * 60
    tag = f"[v{viewer_id}] "

    stats = {
        "viewer_id": viewer_id,
        "rendition": "-",
        "bitrate_kbps": 0,
        "throughput_kbps": 0.0,
        "buffer_s": 0.0,
        "stalls": 0,
        "stall_seconds": 0.0,
        "switches": 0,
        "startup_s": None,
        "segments": 0,
        "bytes": 0,
        "state": "starting",
    }
    with test_lock:
        viewer_stats[viewer_id] = stats

    # Two sessions because they want opposite retry policies (see SEGMENT_ATTEMPTS), and
    # one set per thread because requests sessions are not thread-safe. Playlists are
    # fetched once per rendition and then cached, so the second connection costs little.
    playlist_session = make_session(PLAYLIST_ATTEMPTS)
    segment_session = make_session(SEGMENT_ATTEMPTS)

    try:
        # Stagger viewers so a high intensity doesn't fire every first request in the
        # same millisecond -- real viewers don't press play simultaneously, and the
        # thundering herd would distort the startup-delay numbers.
        time.sleep(random.uniform(0, 2.0))
        if stop_event.is_set():
            return

        master_url = content_url(target_ip, target_port, "master.m3u8")
        output_queue.put(f"{tag}Fetching master playlist: {master_url}\n")
        try:
            renditions = parse_master_playlist(fetch_playlist(playlist_session, master_url))
        except PlaylistFetchError as e:
            # Fatal, unlike the in-loop failures below: without a ladder there is nothing
            # to stream. Name the URL so it's clear whether the target is serving at all.
            output_queue.put(f"{tag}Could not fetch master playlist -- {e}\n")
            stats["state"] = "error"
            return
        if not renditions:
            output_queue.put(f"{tag}Target master playlist has no renditions.\n")
            return

        # Media playlists are fetched once per rendition and cached: this is VOD, so
        # they never change, and re-fetching them would add request overhead a real
        # VOD player doesn't have either.
        playlists: dict[str, list[dict]] = {}

        def segments_for(index: int) -> list[dict]:
            r = renditions[index]
            if r["playlist"] not in playlists:
                playlists[r["playlist"]] = parse_media_playlist(
                    fetch_playlist(playlist_session,
                                   content_url(target_ip, target_port, r["playlist"]))
                )
            return playlists[r["playlist"]]

        if abr_enabled:
            current = 0  # real players start low and climb once they've measured the link
        else:
            names = [r["name"] for r in renditions]
            current = names.index(pinned_rendition) if pinned_rendition in names else len(renditions) - 1

        ladder_desc = ", ".join(f"{r['name']}@{r['bitrate_kbps']}k" for r in renditions)
        output_queue.put(f"{tag}Ladder: {ladder_desc}\n")
        output_queue.put(
            f"{tag}Mode: {'ABR (adaptive)' if abr_enabled else 'fixed ' + renditions[current]['name']}, "
            f"starting at {renditions[current]['name']}.\n"
        )

        buffer_s = 0.0
        playing = False
        bw_est = None
        below_floor = 0
        link_limited = False
        seg_index = 0
        session_start = time.time()
        # A per-viewer bias on the buffer target: identical targets would make every
        # viewer pause and resume fetching together, which is not how a room full of
        # devices behaves and would produce artificially synchronized load spikes.
        buffer_target = BUFFER_TARGET_S * random.uniform(0.85, 1.15)

        while time.time() < end_time and not stop_event.is_set():
            t0 = time.time()
            try:
                segments = segments_for(current)
            except PlaylistFetchError as e:
                # Same reasoning as a failed segment below: a playlist that won't load is a
                # link symptom, and this app exists to provoke exactly that. Drop a rung and
                # keep going -- and let the buffer drain as it really would -- rather than
                # tearing the viewer down over one unlucky control request.
                buffer_s, stall_s = _drain(buffer_s, time.time() - t0, playing)
                if stall_s > 0:
                    stats["stalls"] += 1
                    stats["stall_seconds"] += stall_s
                    output_queue.put(f"{tag}REBUFFER: buffer ran dry for {stall_s:.1f}s\n")
                output_queue.put(f"{tag}playlist unavailable -- {e}\n")
                if abr_enabled and current > 0:
                    current -= 1
                    stats["switches"] += 1
                time.sleep(1.0)
                continue
            if not segments:
                output_queue.put(f"{tag}Rendition {renditions[current]['name']} has no segments.\n")
                return

            # Loop the VOD corpus rather than stopping at ENDLIST: the run length is set
            # by the duration slider, and 48s of media would otherwise end it early.
            segment = segments[seg_index % len(segments)]
            rendition = renditions[current]
            seg_url = content_url(
                target_ip, target_port,
                urllib.parse.urljoin(rendition["playlist"], segment["uri"])
            )

            t0 = time.time()
            try:
                nbytes = fetch(segment_session, seg_url)
            except Exception as e:
                # A failed segment is a real streaming event, not a reason to tear the
                # viewer down: drop a rung (the usual player response to a bad fetch)
                # and keep going, letting the buffer drain as it really would.
                elapsed = time.time() - t0
                buffer_s, stall_s = _drain(buffer_s, elapsed, playing)
                if stall_s > 0:
                    stats["stalls"] += 1
                    stats["stall_seconds"] += stall_s
                output_queue.put(f"{tag}segment failed -- {seg_url}: {e}; dropping rendition.\n")
                if abr_enabled and current > 0:
                    current -= 1
                    stats["switches"] += 1
                time.sleep(1.0)
                continue

            elapsed = max(time.time() - t0, 1e-6)
            throughput_kbps = (nbytes * 8 / 1000) / elapsed

            buffer_s, stall_s = _drain(buffer_s, elapsed, playing)
            if stall_s > 0:
                stats["stalls"] += 1
                stats["stall_seconds"] += stall_s
                output_queue.put(f"{tag}REBUFFER: buffer ran dry for {stall_s:.1f}s\n")

            buffer_s += segment["duration"]
            seg_index += 1

            if not playing and buffer_s >= STARTUP_BUFFER_S:
                playing = True
                stats["startup_s"] = round(time.time() - session_start, 2)
                output_queue.put(f"{tag}playback started after {stats['startup_s']:.2f}s\n")

            bw_est = throughput_kbps if bw_est is None else (
                (1 - ABR_EWMA_WEIGHT) * bw_est + ABR_EWMA_WEIGHT * throughput_kbps
            )

            # Below the bottom rung there is no rendition left to drop to, so the run has
            # stopped measuring video quality and started measuring a broken link. Say
            # that outright: the numbers alone read like a malfunctioning player, and an
            # operator should not have to work out that the tool is reporting faithfully.
            # The verdict is computed here (the state below depends on it) but announced
            # after this segment's own log line, so the line that triggered it reads first.
            floor_kbps = renditions[0]["bitrate_kbps"]
            verdict = None
            if throughput_kbps < floor_kbps:
                below_floor += 1
            else:
                if link_limited:
                    verdict = (f"{tag}link recovered ({throughput_kbps:.0f} kbps, above the "
                               f"{floor_kbps} kbps floor).\n")
                below_floor = 0
                link_limited = False
            if below_floor >= LINK_LIMITED_SEGMENTS and not link_limited:
                link_limited = True
                verdict = (
                    f"{tag}LINK-LIMITED: {below_floor} consecutive segments below the "
                    f"{floor_kbps} kbps needed for the lowest rendition "
                    f"({renditions[0]['name']}), measured {throughput_kbps:.0f} kbps. "
                    f"ABR has no lower rung to drop to, so this is a network limit, not a "
                    f"fault in this app -- it is reporting what the link delivered. "
                    f"Confirm independently with:\n"
                    f"{tag}    curl -s -o /dev/null -w '%{{time_total}}s %{{speed_download}} B/s\\n' {seg_url}\n"
                    f"{tag}    iperf3 -c {target_ip} -t 10\n"
                )

            stats.update({
                "rendition": rendition["name"],
                "bitrate_kbps": rendition["bitrate_kbps"],
                "throughput_kbps": round(bw_est, 1),
                "buffer_s": round(buffer_s, 1),
                "segments": stats["segments"] + 1,
                "bytes": stats["bytes"] + nbytes,
                "state": "link-limited" if link_limited else ("playing" if playing else "buffering"),
            })

            output_queue.put(
                f"{tag}{rendition['name']} seg{seg_index - 1}: {nbytes / 1024:.0f} KB, "
                f"{elapsed * 1000:.0f} ms, {throughput_kbps:.0f} kbps, buffer {buffer_s:.1f}s\n"
            )
            if verdict:
                output_queue.put(verdict)

            if abr_enabled:
                chosen = select_rendition(renditions, bw_est, current)
                if chosen != current:
                    output_queue.put(
                        f"{tag}switch {renditions[current]['name']} -> {renditions[chosen]['name']} "
                        f"(est {bw_est:.0f} kbps)\n"
                    )
                    current = chosen
                    stats["switches"] += 1

            # Buffer is full: idle instead of fetching. This is the step that makes the
            # load a stream rather than a download, so the drain is walked in small
            # increments to stay responsive to Stop.
            while (buffer_s > buffer_target - segment["duration"]
                   and not stop_event.is_set() and time.time() < end_time):
                time.sleep(0.25)
                buffer_s -= 0.25
                stats["buffer_s"] = round(buffer_s, 1)

        watched = max(time.time() - session_start, 1e-6)
        rebuffer_ratio = stats["stall_seconds"] / watched * 100
        output_queue.put(
            f"{tag}session finished: {stats['segments']} segments, "
            f"{stats['bytes'] / 1_000_000:.1f} MB, {stats['stalls']} stall(s) "
            f"totalling {stats['stall_seconds']:.1f}s ({rebuffer_ratio:.2f}% rebuffer ratio), "
            f"{stats['switches']} switch(es).\n"
        )
        if link_limited:
            output_queue.put(
                f"{tag}session ended link-limited: the path to {target_ip} could not carry "
                f"the {renditions[0]['bitrate_kbps']} kbps bottom rung. Fix the link before "
                f"reading anything into the QoE numbers above.\n"
            )
        stats["state"] = "finished"
    except requests.RequestException as e:
        output_queue.put(f"{tag}Error reaching target: {e}\n")
        stats["state"] = "error"
    except Exception as e:
        output_queue.put(f"{tag}Error: {e}\n")
        stats["state"] = "error"
    finally:
        playlist_session.close()
        segment_session.close()
        with test_lock:
            active_viewers -= 1
            all_done = active_viewers == 0
        if all_done:
            output_queue.put("\nAll viewers completed.\n")
            output_queue.put(None)  # sentinel


@app.route("/")
def index():
    return render_template(
        "index.html",
        hostname=get_hostname(),
        renditions=MANIFEST.get("renditions", []),
        encoded=MANIFEST.get("encoded", False),
    )


@app.route("/api/hostname", methods=["GET"])
def api_hostname():
    """Expose the host name so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/interfaces")
def list_interfaces():
    """Real interfaces with a live IPv4 address, for the Bind Interface dropdown."""
    return jsonify({"success": True, "interfaces": get_bindable_interfaces()})


@app.route("/content/manifest.json")
def content_manifest():
    return send_from_directory(CONTENT_DIR, "manifest.json")


@app.route("/content/<path:filename>")
def content_file(filename):
    """Serve the master playlist, media playlists and segments.

    In production nginx aliases /videostream/content/ straight to this directory and
    this route is never hit -- serving several concurrent 1080p viewers' segments
    through Flask would make the app itself the bottleneck rather than the link.
    """
    return send_from_directory(CONTENT_DIR, filename)


@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json(silent=True) or {}
    bind_interface = data.get("bind_interface") or "wlan0"
    meta = {}
    try:
        servers = scan_for_servers(bind_interface=bind_interface, meta=meta)
    except Exception as e:
        # Catch broadly, not just RuntimeError: anything escaping here would otherwise
        # become Flask's HTML 500 page, which the browser's res.json() can only report
        # as an opaque parse error instead of the actual failure.
        return jsonify({"error": str(e)}), 500
    return jsonify({"servers": servers, **meta})


@app.route("/start", methods=["POST"])
def start():
    with test_lock:
        if active_viewers > 0:
            return jsonify({"error": "A test is already running"}), 409

    data = request.json or {}
    raw_target_ip = (data.get("target_ip") or "").strip()
    raw_target_port = data.get("target_port") or 80

    if ":" in raw_target_ip:
        parts = raw_target_ip.split(":", 1)
        target_ip = parts[0].strip()
        try:
            target_port = int(parts[1].strip())
        except ValueError:
            target_port = 80
    else:
        target_ip = raw_target_ip
        try:
            target_port = int(raw_target_port)
        except (ValueError, TypeError):
            target_port = 80

    # Default on absent/blank only, not on any falsy value: `x or default` would turn an
    # explicit 0 into the default and make the range checks below unreachable for it.
    raw_duration = data.get("duration_minutes")
    try:
        duration_minutes = 10 if raw_duration in (None, "") else int(raw_duration)
    except (ValueError, TypeError):
        return jsonify({"error": "Duration must be an integer"}), 400

    raw_intensity = data.get("intensity")
    try:
        intensity = DEFAULT_INTENSITY if raw_intensity in (None, "") else int(raw_intensity)
    except (ValueError, TypeError):
        return jsonify({"error": "Intensity must be an integer"}), 400

    abr_enabled = bool(data.get("abr", True))
    pinned_rendition = (data.get("rendition") or "").strip()

    if not target_ip or not is_valid_ip(target_ip):
        return jsonify({"error": "Invalid target IP address"}), 400
    if not (1 <= target_port <= 65535):
        return jsonify({"error": "Target port must be between 1 and 65535"}), 400
    if duration_minutes < 1:
        return jsonify({"error": "Duration must be at least 1 minute"}), 400
    if not (INTENSITY_RANGE[0] <= intensity <= INTENSITY_RANGE[1]):
        return jsonify({"error": f"Intensity must be between {INTENSITY_RANGE[0]} and {INTENSITY_RANGE[1]}"}), 400

    while not output_queue.empty():
        try:
            output_queue.get_nowait()
        except queue.Empty:
            break

    with test_lock:
        viewer_stats.clear()

    stop_event.clear()
    for viewer_id in range(1, intensity + 1):
        thread = threading.Thread(
            target=run_viewer_loop,
            args=(viewer_id, target_ip, target_port, duration_minutes, abr_enabled, pinned_rendition),
            daemon=True
        )
        thread.start()

    return jsonify({"status": "started", "viewers": intensity})


@app.route("/status")
def status():
    """
    Report whether a simulation is running and how many viewers are live.

    The simulation lives in this process, not in the browser page, so a reloaded
    or reopened page needs to ask rather than assume it is idle.
    """
    with test_lock:
        return jsonify({"running": active_viewers > 0, "active_viewers": active_viewers})


@app.route("/metrics")
def metrics():
    """Live per-viewer QoE numbers, plus the aggregate the UI's stat tiles show.

    Polled rather than pushed down the SSE stream: these are current values, not
    events, so a page that reattaches mid-run should see the real state immediately
    instead of waiting for the next segment to be logged.
    """
    with test_lock:
        viewers = [dict(s) for s in viewer_stats.values()]
        running = active_viewers > 0

    viewers.sort(key=lambda v: v["viewer_id"])
    total_bytes = sum(v["bytes"] for v in viewers)
    stalls = sum(v["stalls"] for v in viewers)
    stall_seconds = sum(v["stall_seconds"] for v in viewers)
    # "link-limited" counts as live: those viewers are still fetching, and dropping them
    # would make the aggregate describe only the viewers that happen to be doing well.
    live = [v for v in viewers if v["state"] in ("playing", "buffering", "link-limited")] or viewers
    avg_bitrate = sum(v["bitrate_kbps"] for v in live) / len(live) if live else 0
    startups = [v["startup_s"] for v in viewers if v["startup_s"] is not None]

    return jsonify({
        "running": running,
        "viewers": viewers,
        "aggregate": {
            "viewer_count": len(viewers),
            "total_mb": round(total_bytes / 1_000_000, 1),
            "avg_bitrate_kbps": round(avg_bitrate),
            "stalls": stalls,
            "stall_seconds": round(stall_seconds, 1),
            "avg_startup_s": round(sum(startups) / len(startups), 2) if startups else None,
        },
    })


@app.route("/stop", methods=["POST"])
def stop():
    with test_lock:
        if active_viewers > 0:
            stop_event.set()
            return jsonify({"status": "stopped"})
    return jsonify({"status": "no test running"})


@app.route("/stream")
def stream():
    def generate():
        while True:
            try:
                line = output_queue.get(timeout=30)
                if line is None:
                    break
                yield "data: " + line.rstrip("\n").replace("\n", "\\n") + "\n\n"
            except queue.Empty:
                yield "data: \n\n"  # keepalive

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5012, debug=False)
