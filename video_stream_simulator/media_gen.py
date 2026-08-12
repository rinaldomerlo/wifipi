#!/usr/bin/env python3
"""
Generates an HLS ABR ladder on disk so the video streaming simulator has a
realistic, reproducible corpus to stream instead of a real movie file.

The playlists are genuine HLS -- a master playlist pointing at one media
playlist per rendition, each listing fixed-duration segments -- so the client
walks the same request chain a real player does. The segment *bytes* are
random and undecodable, which is deliberate: this exercises the WiFi link, not
a video decoder, and what matters is that each segment is the size a real
encode at that bitrate would have produced. Synthetic bytes also mean the
corpus generates in under a second on a Pi instead of tying up the CPU for an
hour re-encoding, and they make the bitrate exact rather than whatever an
encoder happened to emit.

Set VIDEOSTREAM_USE_FFMPEG=1 to encode real (decodable) segments from ffmpeg's
`testsrc2` pattern instead -- useful if you want to point an actual player at
the corpus. It needs ffmpeg on PATH and is dramatically slower.

Per-segment sizes are jittered around the nominal bitrate because real VBR
encodes vary shot to shot; a perfectly uniform segment size would make the
client's bandwidth estimate unrealistically easy.
"""

import json
import os
import random
import shutil
import subprocess

# The ABR ladder. Roughly the rungs a real streaming service ships, trimmed at
# the top so a full corpus stays SD-card-friendly (see CORPUS_SECONDS below).
LADDER = [
    {"name": "240p", "width": 426, "height": 240, "bitrate_kbps": 400},
    {"name": "360p", "width": 640, "height": 360, "bitrate_kbps": 800},
    {"name": "480p", "width": 854, "height": 480, "bitrate_kbps": 1400},
    {"name": "720p", "width": 1280, "height": 720, "bitrate_kbps": 2800},
    {"name": "1080p", "width": 1920, "height": 1080, "bitrate_kbps": 5000},
]

SEGMENT_DURATION_S = 4
SEGMENT_COUNT = 12  # 48s of media per rendition; the client loops the playlist
SIZE_JITTER = 0.15  # +/- 15% around the nominal segment size, mimicking VBR

# Total corpus size is sum(bitrates) * SEGMENT_DURATION_S * SEGMENT_COUNT / 8,
# which for the ladder above is about 62 MB.
CORPUS_SECONDS = SEGMENT_DURATION_S * SEGMENT_COUNT


def nominal_segment_bytes(bitrate_kbps: int, duration_s: float = SEGMENT_DURATION_S) -> int:
    """Bytes a segment of the given duration occupies at the given bitrate."""
    return int(bitrate_kbps * 1000 * duration_s / 8)


def _write_random_segment(path: str, size: int) -> int:
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    return size


def _encode_real_segments(rendition: dict, rendition_dir: str) -> list[int]:
    """Encode decodable segments with ffmpeg's testsrc2 pattern. Slow; opt-in only.

    Constrained VBV (minrate == maxrate) so the delivered bitrate matches the
    ladder rung rather than drifting with scene complexity -- the whole point of
    a test corpus is that a throughput change means a *network* change.
    """
    bitrate = f"{rendition['bitrate_kbps']}k"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"testsrc2=size={rendition['width']}x{rendition['height']}:rate=30",
        "-t", str(CORPUS_SECONDS),
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", bitrate, "-minrate", bitrate, "-maxrate", bitrate,
        "-bufsize", f"{rendition['bitrate_kbps'] * 2}k",
        "-g", str(SEGMENT_DURATION_S * 30), "-keyint_min", str(SEGMENT_DURATION_S * 30),
        "-sc_threshold", "0",
        "-f", "hls",
        "-hls_time", str(SEGMENT_DURATION_S),
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", os.path.join(rendition_dir, "seg-%d.ts"),
        os.path.join(rendition_dir, "index.m3u8"),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)

    sizes = []
    for i in range(SEGMENT_COUNT):
        path = os.path.join(rendition_dir, f"seg-{i}.ts")
        sizes.append(os.path.getsize(path) if os.path.exists(path) else 0)
    return sizes


def _write_media_playlist(rendition_dir: str, segment_count: int) -> None:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION_S}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for i in range(segment_count):
        lines.append(f"#EXTINF:{SEGMENT_DURATION_S:.3f},")
        lines.append(f"seg-{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    with open(os.path.join(rendition_dir, "index.m3u8"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _write_master_playlist(content_dir: str) -> None:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for r in LADDER:
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={r['bitrate_kbps'] * 1000},"
            f"RESOLUTION={r['width']}x{r['height']}"
        )
        lines.append(f"{r['name']}/index.m3u8")
    with open(os.path.join(content_dir, "master.m3u8"), "w") as f:
        f.write("\n".join(lines) + "\n")


def generate_ladder(content_dir: str, use_ffmpeg: bool = False) -> dict:
    """Generate a fresh HLS ladder into content_dir, overwriting any manifest there."""
    os.makedirs(content_dir, exist_ok=True)
    real_encode = use_ffmpeg and bool(shutil.which("ffmpeg"))

    renditions = []
    for r in LADDER:
        rendition_dir = os.path.join(content_dir, r["name"])
        os.makedirs(rendition_dir, exist_ok=True)

        if real_encode:
            try:
                sizes = _encode_real_segments(r, rendition_dir)
            except Exception:
                # An ffmpeg that is present but unusable (missing libx264, killed by
                # a timeout) must not leave the app with no corpus at all -- fall
                # back to synthetic bytes for this rendition and carry on.
                real_encode = False
                sizes = []
        else:
            sizes = []

        if not sizes:
            nominal = nominal_segment_bytes(r["bitrate_kbps"])
            sizes = []
            for i in range(SEGMENT_COUNT):
                size = int(nominal * random.uniform(1 - SIZE_JITTER, 1 + SIZE_JITTER))
                _write_random_segment(os.path.join(rendition_dir, f"seg-{i}.ts"), size)
                sizes.append(size)
            _write_media_playlist(rendition_dir, SEGMENT_COUNT)

        renditions.append({
            "name": r["name"],
            "width": r["width"],
            "height": r["height"],
            "bitrate_kbps": r["bitrate_kbps"],
            "playlist": f"{r['name']}/index.m3u8",
            "segment_count": len(sizes),
            "total_bytes": sum(sizes),
        })

    _write_master_playlist(content_dir)

    manifest = {
        "master": "master.m3u8",
        "segment_duration_s": SEGMENT_DURATION_S,
        "duration_s": CORPUS_SECONDS,
        "encoded": real_encode,
        "renditions": renditions,
    }
    with open(os.path.join(content_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    return manifest


def ensure_media_ladder(content_dir: str, use_ffmpeg: bool = False) -> dict:
    """Generate the ladder if it isn't already on disk, and return the manifest either way.

    Unlike the browsing simulator's corpus this is *not* regenerated per process
    start: it is ~62 MB and, more importantly, a stable corpus is what lets two
    runs against the same target be compared to each other.
    """
    manifest_path = os.path.join(content_dir, "manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path) as f:
                return json.load(f)
        except Exception:
            pass  # truncated/corrupt manifest -- regenerate below
    return generate_ladder(content_dir, use_ffmpeg=use_ffmpeg)
