#!/usr/bin/env python3
"""
Flask app that stresses a WiFi access point by rapidly and randomly
associating / disassociating several *physical* WiFi interfaces (the Pi's
built-in radio plus any USB adapters) against one target SSID, optionally
randomizing each interface's MAC on every reconnect so the AP sees a constant
stream of brand-new stations. That bloats the AP's association / DHCP-lease /
ARP tables far more than plain reconnect churn -- each interface is a
"spine" repeatedly poking the hub, hence "porcupine".

Every ticked interface churns at once, independently and out of sync with the
others (a random initial jitter, a constant per-interface speed bias, and a
long-tailed per-cycle duration keep them from lining up even when NetworkManager
stalls and releases them together) -- concurrency is just however many you
select. Three orthogonal knobs shape the churn: **Presence** (what fraction of the
time each interface is associated -- the duty cycle), **Churn rate** (how many
reconnects per minute), and **Variability** (how bursty vs. metronomic the per-cycle
timing is). Presence and rate set the connected (ON) and disconnected (OFF) durations
of every cycle (a low Presence + slow rate is a quiet household device that is mostly
disconnected; a high rate is an association storm); Variability sets how far each cycle
wanders from those means without moving Presence or the rate. See compute_durations()
and gamma_shape_from_variability().

Association/MAC churn goes through NetworkManager (`nmcli`, one connection
profile per interface; MAC randomization sets
`802-11-wireless.cloned-mac-address random` on that profile, toggle-able per
run). This requires Linux + privileged tooling, so the systemd unit runs this
app as root (see deploy/wifi-porcupine.service). Off-Linux -- e.g. macOS
during development -- every route degrades to a clear JSON error instead of
crashing. See CLAUDE.md's Environment Split section.
"""

import fcntl
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# --- Naming ---
PROFILE_PREFIX = "porcupine"   # NetworkManager connection profiles: porcupine-<iface>

# --- Churn model: two orthogonal knobs (templated into the UI so the bounds can't drift) ---
# Presence = the fraction of each cycle an interface stays associated (its duty cycle).
# Churn rate = reconnects per minute. Together they fix the connected (ON) and disconnected
# (OFF) durations of a cycle -- see compute_durations(). A low Presence + slow rate is a
# quiet household device (mostly disconnected); a high rate is an association storm.
PRESENCE_RANGE = (5, 95)               # percent of the time associated; slider bounds
PRESENCE_DEFAULT = 30                  # a typical household device: connected under 1/3 of the time
# The churn-rate slider is a fine-grained position mapped *geometrically* to a reconnect rate,
# so every step has about the same proportional effect (very slow settings stay as adjustable as
# very fast ones) -- the same reasoning the old intensity slider used. See churn_rate_from_pos().
CHURN_RANGE = (1, 100)                 # slider position; wide + fine-grained for smooth control
CHURN_DEFAULT = 12                     # ~0.1 reconnects/min -> ~1 reconnect / 10 min (household pace)
RATE_AT_MIN = 0.05                     # reconnects/min at slider min: very slow (~1 per 20 min)
RATE_AT_MAX = 20.0                     # reconnects/min at slider max: very high (storm; capped by reconnect cost)
# Variability = how much each cycle's ON/OFF wanders from its mean -- the per-cycle randomness that
# makes a real household read as bursty rather than metronomic. It sets the gamma *shape* of the
# draw (higher shape = tighter = more regular; lower shape = heavier tail = burstier). Mean is
# preserved regardless, so it does not move Presence or the rate. The default sits near the old
# fixed shape (~2), which is a natural household feel; the ends run from near-clockwork to very bursty.
VARIABILITY_RANGE = (0, 100)           # slider position: 0 = metronomic, 100 = very bursty
VARIABILITY_DEFAULT = 50               # ~gamma shape 2.2 -- the household-realistic middle
SHAPE_AT_MIN_VARIABILITY = 10.0        # near-regular timing (low spread) at slider min
SHAPE_AT_MAX_VARIABILITY = 0.5         # heavy-tailed, bursty timing (high spread) at slider max
GAP_MIN = 0.5                          # floor on the idle OFF gap between disassociate and reconnect, seconds
# Rough fixed cost of a reconnect that no setting can shrink: scan + DHCP, measured ~20s on a
# 5-interface Pi run. It is disconnected time too (so it counts against Presence), but bring_up()
# already spends it, so we don't sleep it -- we subtract it from the intended OFF. It also makes the
# UI's reconnects/min estimate honest: at high rate it dominates the cycle and caps the achievable rate.
OFFLINE_ESTIMATE_SECONDS = 20.0
DWELL_BIAS_RANGE = (0.4, 1.6)          # per-interface constant speed offset; see sample_period()
RETRY_BACKOFF_BASE = 2.0               # first failed connect retries within this many seconds
RETRY_BACKOFF_CAP = 60.0               # ceiling on the retry window; see compute_retry_delay()
RETRY_BACKOFF_MAX_DOUBLINGS = 20       # guard so a long outage can't compute 2**huge
MIN_DWELL = 1.0                        # floor on a sampled dwell, seconds
DWELL_TAIL_FACTOR = 3.0                # cap on a sampled dwell, as a multiple of its mean

NMCLI_TIMEOUT = 15
# Must not undercut nmcli's own default wait for `connection up` (90s). Killing nmcli does
# NOT cancel the activation NetworkManager is already running, so a shorter timeout here just
# produces a bogus "connect failed", an inflated error count, and an immediate second
# activation request stacked on the first -- visible in the journal as a stream of
# "disconnecting for new activation request" while the device is still in `config`.
# DHCP alone can hold an activation for 45s, so 30s was well inside the range that misfires.
CONNECT_TIMEOUT = 90
# Deliberately shorter: teardown is quick, and this one bounds how long a stop request waits
# on a wedged interface. It has no reason to inherit the connect timeout.
DISCONNECT_TIMEOUT = 30
MAX_OUTPUT_LINES = 2000

IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Cross-process guard (see acquire_run_lock()) so a stray second process -- a manual
# `python app.py` left running alongside the systemd service, a duplicate deploy, etc. --
# can't also churn the same physical interfaces. run_lock below only protects this one
# process's in-memory state; it can't see other processes.
LOCK_PATH = os.path.join(tempfile.gettempdir(), "wifi-porcupine.lock")
_lock_fd = None

# --- Shared state (guarded by run_lock; the log has its own lock) ---
run_lock = threading.Lock()
run_state = {
    "running": False,
    "wifi_mode": None,     # "live" while a run is active
    "enlisted": [],        # interfaces enlisted for this run
    "connected": set(),    # interfaces currently associated
    "config": None,
}
stats = {"reconnects": 0, "errors": 0, "active_interfaces": 0}
workers = []                # per-interface churn threads
duration_thread = None
stop_event = threading.Event()

log_lock = threading.Lock()
log_lines = deque(maxlen=MAX_OUTPUT_LINES)
log_total = 0


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def _run(cmd, timeout=NMCLI_TIMEOUT):
    """Run a command (already including sudo if needed) and return (ok, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return False, "", str(e)


def _nmcli(args, timeout=NMCLI_TIMEOUT):
    """Run `sudo nmcli <args>`; returns (ok, stdout, stderr). Never raises."""
    if not shutil.which("nmcli"):
        return False, "", "nmcli is not installed on this host (install network-manager)."
    return _run(["sudo", "nmcli"] + args, timeout=timeout)


def _split_terse(line: str) -> list:
    """Split a colon-delimited nmcli -t line, honoring backslash-escaped colons."""
    fields = re.split(r"(?<!\\):", line)
    return [f.replace("\\:", ":").replace("\\\\", "\\") for f in fields]


# ---------------------------------------------------------------------------
# Logging: a bounded ring buffer with a monotonic cursor, so a reloaded page or
# a second tab can replay from wherever it left off instead of losing lines the
# way a destructively-consumed queue would.
# ---------------------------------------------------------------------------
def _log(msg):
    global log_total
    stamp = time.strftime("%H:%M:%S")
    with log_lock:
        log_lines.append(f"[{stamp}] {msg}")
        log_total += 1


def read_output(since: int) -> dict:
    """Return buffered log lines from cursor `since` onward, reporting any drops."""
    with log_lock:
        total = log_total
        buffered = len(log_lines)
        first = total - buffered  # cursor value of log_lines[0]
        since = max(0, min(since, total))
        start = max(since, first)
        chunk = list(log_lines)[start - first:]
    return {"lines": chunk, "next": total, "dropped": max(0, first - since)}


def _interruptible_sleep(duration, extra=None):
    """Sleep up to `duration`s, waking immediately if stop_event (or `extra`) is set."""
    end = time.time() + duration
    while True:
        remaining = end - time.time()
        if remaining <= 0 or stop_event.is_set() or (extra is not None and extra.is_set()):
            return
        time.sleep(min(0.5, remaining))


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------
def get_wireless_interfaces() -> list:
    """List wireless interfaces via `iw dev`, falling back to /proc/net/wireless."""
    interfaces = []
    try:
        output = subprocess.check_output(
            ["iw", "dev"], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="replace")
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Interface"):
                parts = line.split()
                if len(parts) >= 2:
                    interfaces.append(parts[1])
    except Exception:
        pass

    if not interfaces and os.path.exists("/proc/net/wireless"):
        try:
            with open("/proc/net/wireless", "r") as f:
                for line in f.readlines()[2:]:
                    name = line.split(":")[0].strip()
                    if name:
                        interfaces.append(name)
        except Exception:
            pass

    return interfaces


def detect_wifi_mode():
    """Whether the physical-interface association/MAC churn can run. Returns (mode, reason)."""
    if platform.system() != "Linux":
        return None, "not running on Linux"
    if not shutil.which("nmcli"):
        return None, "nmcli is not installed (install network-manager)"
    if not shutil.which("iw"):
        return None, "the 'iw' command is not installed"
    return "live", None


def friendly(raw) -> str:
    """Condense a raw nmcli error to one short human-readable line."""
    raw = (raw or "").strip()
    if not raw:
        return "unknown error"
    low = raw.lower()
    if "secrets were required" in low or "no secrets" in low or "802-1x" in low:
        # NetworkManager asks for secrets both when the PSK is genuinely wrong and when a
        # 4-way handshake fails for any other reason -- an AP refusing or deauthing a station
        # under churn looks identical here. Blaming the password outright is misleading
        # mid-run, when the same PSK has already associated dozens of times.
        return "handshake rejected by the AP (or wrong password)"
    if "not found" in low or "no network" in low:
        return "target network not found in range"
    if "timeout" in low or "timed out" in low:
        return "association timed out"
    return raw.splitlines()[0][:200]


def classify_security(security_field: str) -> str:
    sec = (security_field or "").strip()
    if not sec or sec == "--":
        return "Open"
    return sec.split()[0] if " " not in sec else sec


def classify_band(freq_mhz):
    if freq_mhz is None:
        return None
    if freq_mhz < 2500:
        return "2.4GHz"
    if freq_mhz < 5900:
        return "5GHz"
    return "6GHz"


# ---------------------------------------------------------------------------
# Churn model math (pure helpers, unit-tested)
# ---------------------------------------------------------------------------
def churn_rate_from_pos(pos):
    """Reconnects/min for a churn-rate slider position, mapped geometrically.

    Perceived activity is roughly the rate itself, so a linear position->rate ramp would bunch
    all the perceptible change at the top of the slider and leave the low half nearly flat.
    Geometric spacing (each step multiplies the rate by a constant ratio) gives every step about
    the same proportional effect, so a very-slow household pace is as adjustable as a fast one.
    """
    lo, hi = CHURN_RANGE
    t = (pos - lo) / (hi - lo) if hi > lo else 0.0
    return RATE_AT_MIN * (RATE_AT_MAX / RATE_AT_MIN) ** t


def compute_durations(presence, churn_pos):
    """(on_mean, gap_mean) seconds for a Presence% and a churn-rate slider position.

    The two knobs are orthogonal and together fix the whole cycle:
        period    = 60 / rate                       (rate from churn_rate_from_pos)
        ON  (mean) = presence * period              -- time associated
        OFF (mean) = (1 - presence) * period        -- time disconnected

    The idle gap we actually sleep is that OFF minus the fixed scan+DHCP reconnect cost: that
    cost is disconnected time too (so it counts against Presence), but bring_up() already spends
    it, so sleeping the full OFF on top would double-count it. Both returned means are floored
    (ON at MIN_DWELL, gap at GAP_MIN). When the requested OFF is shorter than the reconnect cost
    -- the top of the churn slider -- the gap floors and the achievable rate falls below the
    requested one; achievable_rate() reports that honestly.
    """
    duty = presence / 100.0
    period = 60.0 / churn_rate_from_pos(churn_pos)
    on_mean = max(MIN_DWELL, duty * period)
    gap_mean = max(GAP_MIN, (1.0 - duty) * period - OFFLINE_ESTIMATE_SECONDS)
    return on_mean, gap_mean


def achievable_rate(presence, churn_pos):
    """Reconnects/min actually achievable, accounting for the reconnect cost and the ON/gap floors.

    Equals the requested rate until the churn slider is pushed past what the fixed scan+DHCP cost
    physically allows, after which it flattens out -- which is what keeps the UI estimate honest.
    """
    on_mean, gap_mean = compute_durations(presence, churn_pos)
    return 60.0 / (on_mean + gap_mean + OFFLINE_ESTIMATE_SECONDS)


def gamma_shape_from_variability(pos):
    """Gamma shape for a Variability slider position, mapped geometrically.

    Shape controls dispersion: the coefficient of variation of the draw is 1/sqrt(shape), so a
    high shape is near-regular timing and a low shape is heavy-tailed and bursty. Geometric
    spacing keeps every step's proportional effect on the spread about equal. The mean is
    unaffected (sample_period sets the scale to preserve it), so this knob never moves Presence
    or the reconnect rate -- it only changes how erratic the timing looks.
    """
    lo, hi = VARIABILITY_RANGE
    t = (pos - lo) / (hi - lo) if hi > lo else 0.0
    return SHAPE_AT_MIN_VARIABILITY * (SHAPE_AT_MAX_VARIABILITY / SHAPE_AT_MIN_VARIABILITY) ** t


def sample_period(mean, bias=1.0, floor=MIN_DWELL, shape=2.0, rng=random):
    """Draw one ON or OFF duration, in seconds, for a single churn cycle.

    Using the mean flat for every cycle is not enough to keep interfaces apart in practice.
    Reconnecting means scanning first, and something upstream (NM or wpa_supplicant) aligns *scan
    starts* across devices: in a NetworkManager journal from this app, each scan reliably took
    ~6.2s on its own device, yet pairs of devices that had gone disconnected 0.2s apart began
    scanning at the same instant to 20ms. That shared slot re-phases interfaces every cycle, and
    interfaces that also share one mean period just re-lock after each collapse, which is what
    makes the log read as batches.

    `bias` is what breaks that: a constant per-interface speed offset drawn once per run, so no
    two interfaces share a mean period and they drift across the slot boundary instead of marching
    back into step. Simulating five interfaces against that quantizer, the share of associations
    landing alone rather than in a clump rises from ~14% at a common period to ~24% across the
    width of DWELL_BIAS_RANGE (at a 20s slot; the gain holds at every slot size tried), which is
    why that range is deliberately wide. The same `bias` scales both the ON and the OFF draw, so
    an interface's duty cycle (its Presence) is preserved even as its overall pace drifts.

    The gamma tail rather than a flat value does *not* help the batching -- once `bias` is in play
    the same simulation puts it a fraction of a percent behind, consistently. It earns its place on
    the other half of the complaint: a single interface pacing a fixed value looks metronomic in
    the log, and this makes its own timing read as genuinely random. `shape` (from the Variability
    slider, via gamma_shape_from_variability) sets how heavy that tail is -- low shape is bursty,
    high shape is near-regular. If you are tempted to simplify, drop the tail, not the bias.

    The scale is set to `m / shape` so the mean stays `bias * mean` for any shape, and `bias` is
    symmetric about 1.0, so the reconnect-rate estimate stays honest. Clamped to
    [floor, DWELL_TAIL_FACTOR * bias * mean] so the tail can't strand an interface for minutes
    past its intended dwell.
    """
    m = bias * mean
    sample = rng.gammavariate(shape, m / shape)
    return max(floor, min(DWELL_TAIL_FACTOR * m, sample))


def compute_retry_delay(failures, bias=1.0, rng=random):
    """How long to wait before retrying a failed connect, in seconds.

    When an AP stops accepting -- its association table saturated by exactly the
    churn this app generates, or it starts rate-limiting -- every interface fails
    at once, and a fixed short retry gap makes that far worse. Each one then
    loops on the same near-constant period (NetworkManager's association timeout
    plus that gap), so they re-synchronize into a lockstep retry storm against an
    AP that is already struggling. The per-interface bias that keeps the success
    path desynchronized does not apply here, so failures batch even when normal
    churn does not.

    This is "full jitter" backoff: the window doubles per consecutive failure up
    to RETRY_BACKOFF_CAP, and the actual wait is drawn uniformly from *zero* to
    that window. Drawing from the whole window rather than jittering around its
    edge is what decorrelates retries -- two interfaces failing in the same
    instant get unrelated waits on the very first retry. The window is scaled by
    the interface's `bias` too, so even the ceilings differ once saturated.

    `failures` is the count of consecutive failures, so 1 on the first one.
    """
    doublings = min(max(int(failures), 1) - 1, RETRY_BACKOFF_MAX_DOUBLINGS)
    window = min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** doublings)) * bias
    return rng.uniform(0, window)


# ---------------------------------------------------------------------------
# nmcli profiles (one per interface, each with a random cloned MAC)
# ---------------------------------------------------------------------------
def profile_name(iface) -> str:
    return f"{PROFILE_PREFIX}-{iface}"


def build_profile_add_args(iface, ssid, password, randomize_mac=True):
    """Args for `nmcli connection add` creating this interface's porcupine profile.

    `802-11-wireless.cloned-mac-address random` makes NetworkManager pick a fresh
    random MAC on every activation, so each reconnect looks like a new station.
    Omitted when `randomize_mac` is False, so the interface churns under its own
    real (or globally-configured) MAC instead.
    """
    args = [
        "connection", "add", "type", "wifi",
        "con-name", profile_name(iface),
        "ifname", iface,
        "ssid", ssid,
        "connection.autoconnect", "no",
    ]
    if randomize_mac:
        args += ["802-11-wireless.cloned-mac-address", "random"]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    return args


def create_profile(iface, ssid, password, randomize_mac=True):
    """Recreate this interface's connection profile from scratch. Returns (ok, message)."""
    _nmcli(["connection", "delete", profile_name(iface)])  # clear any stale profile; ignore errors
    ok, out, err = _nmcli(build_profile_add_args(iface, ssid, password, randomize_mac))
    return ok, (err or out)


def delete_profile(iface):
    _nmcli(["connection", "delete", profile_name(iface)])


def bring_up(iface):
    return _nmcli(["connection", "up", profile_name(iface)], timeout=CONNECT_TIMEOUT)


def bring_down(iface):
    return _nmcli(["connection", "down", profile_name(iface)], timeout=DISCONNECT_TIMEOUT)


def read_iface_mac(iface):
    """Current (possibly cloned) MAC of an interface, read from sysfs. None if unavailable."""
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Churn engine
# ---------------------------------------------------------------------------
def churn_worker(iface, config):
    """One interface's association/MAC churn loop: connect (new MAC) -> ON dwell -> disconnect -> OFF gap.

    ON and OFF means come from compute_durations(Presence, churn rate); the OFF gap is the
    disconnected time on top of the scan+DHCP reconnect cost bring_up() already spends.

    Interfaces are started together in start_run(), so without a stagger they'd all connect in
    lockstep on cycle 1. Three things keep them apart, and all three are needed:

    * an initial jitter spread across a full cycle, so cycle 1 is already staggered;
    * a per-run `bias` giving this interface its own mean period (see sample_period()), so it
      drifts relative to its neighbours rather than sharing their rhythm -- this is the one
      that does the real work;
    * a long-tailed per-cycle duration, which keeps one interface's own timing from looking
      metronomic (it does not help the batching -- see sample_period()).

    The stagger alone is not enough, because reconnecting requires a scan and scan starts are
    aligned across devices upstream of us, so interfaces sharing one mean period re-synchronize
    on every such slot no matter how cycle 1 was staggered. That is exactly how the churn ends
    up looking batched.

    A failed connect takes none of the above -- it never reaches the dwell -- so it backs off
    separately via compute_retry_delay(), which is what keeps a saturated AP from collecting a
    lockstep retry storm from every interface at once.
    """
    on_mean, gap_mean = compute_durations(config["presence"], config["churn"])
    shape = gamma_shape_from_variability(config["variability"])
    bias = random.uniform(*DWELL_BIAS_RANGE)
    failures = 0
    _interruptible_sleep(random.uniform(0, on_mean + gap_mean + OFFLINE_ESTIMATE_SECONDS))
    while not stop_event.is_set():
        ok, _, err = bring_up(iface)
        if stop_event.is_set():
            break
        if not ok:
            failures += 1
            with run_lock:
                stats["errors"] += 1
            delay = compute_retry_delay(failures, bias)
            _log(f"[{iface}] connect failed: {friendly(err)} (retry in {delay:.0f}s)")
            _interruptible_sleep(delay)
            continue
        failures = 0

        mac = read_iface_mac(iface)
        with run_lock:
            stats["reconnects"] += 1
            run_state["connected"].add(iface)
            stats["active_interfaces"] = len(run_state["connected"])
        _log(f"[{iface}] associated as {mac or 'unknown MAC'} -> {config['ssid']}")

        _interruptible_sleep(sample_period(on_mean, bias, MIN_DWELL, shape))

        bring_down(iface)
        with run_lock:
            run_state["connected"].discard(iface)
            stats["active_interfaces"] = len(run_state["connected"])
        if not stop_event.is_set():
            _log(f"[{iface}] disassociated")
        _interruptible_sleep(sample_period(gap_mean, bias, GAP_MIN, shape))

    bring_down(iface)  # leave the radio idle on the way out
    with run_lock:
        run_state["connected"].discard(iface)
        stats["active_interfaces"] = len(run_state["connected"])


def acquire_run_lock(info: str = "") -> bool:
    """Grab a system-wide exclusive lock so at most one WiFi Porcupine process can
    have an active run at a time -- not just at most one run within this process.

    run_state["running"] alone only guards against double-starting within a single
    process; it's invisible to a second process (a stray manual `python app.py` left
    running next to the systemd service, a duplicate deploy, etc.). Two processes
    independently racing `nmcli connection up/down` against the same physical radios
    would corrupt NetworkManager state, not just waste resources.

    Uses flock() rather than a PID file: the OS drops it the moment the holding
    process exits for any reason, including a crash or SIGKILL, so there's no stale
    lock to detect or clean up on the next start. Returns False if another process
    (or another open of this file) already holds it. `info` (e.g. the target SSID)
    is stamped into the file alongside the PID purely so a rejected caller can report
    *what* is already running via read_run_lock_info(), not to coordinate anything.
    """
    global _lock_fd
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()} {info}".strip().encode())
    _lock_fd = fd
    return True


def read_run_lock_info() -> str:
    """Best-effort read of whatever acquire_run_lock() last stamped into the lock
    file, so a rejected /api/start can say what's already running and where.
    Returns '' if the file is missing or unreadable -- purely cosmetic, never
    used to make a decision.
    """
    try:
        with open(LOCK_PATH, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def release_run_lock():
    """Release the lock acquired by acquire_run_lock(). Safe to call when not held."""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None


def duration_timer(minutes):
    """Stop the run once its duration elapses (unless already stopped)."""
    if stop_event.wait(minutes * 60):
        return
    _log("Duration elapsed -- stopping run.")
    threading.Thread(target=stop_run, daemon=True).start()


def _fmt_duration(seconds):
    """Compact human duration for log lines: '45s' up to ~90s, then '3.2m'."""
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f}m"


def start_run(config):
    """Heavy setup for a run: profiles + churn threads. Runs in a thread.

    run_state['running'] has already been set True by the start route, so /status
    reports 'running' immediately even while this setup is still in progress.
    """
    global duration_thread

    on_mean, gap_mean = compute_durations(config["presence"], config["churn"])
    rate = achievable_rate(config["presence"], config["churn"])
    _log(
        f"Starting porcupine run: {len(config['interfaces'])} interface(s) -> "
        f"SSID '{config['ssid']}', presence {config['presence']}% "
        f"(~{_fmt_duration(on_mean)} on / ~{_fmt_duration(gap_mean)} off, ~{rate:.2g} reconnects/min)"
        + (", random MAC per reconnect" if config["randomize_mac"] else ", MAC unchanged")
    )

    for iface in config["interfaces"]:
        ok, msg = create_profile(iface, config["ssid"], config["password"], config["randomize_mac"])
        if not ok:
            _log(f"[{iface}] profile create failed: {friendly(msg)}")

    workers.clear()
    for iface in config["interfaces"]:
        t = threading.Thread(target=churn_worker, args=(iface, config), daemon=True)
        workers.append(t)
        t.start()

    duration_thread = threading.Thread(target=duration_timer, args=(config["duration_minutes"],), daemon=True)
    duration_thread.start()


def stop_run():
    """Stop a run: halt churn, disconnect + delete profiles. Idempotent."""
    with run_lock:
        if not run_state["running"]:
            return False
        run_state["running"] = False
        enlisted = list(run_state["enlisted"])
    stop_event.set()

    for t in workers:
        t.join(timeout=5)

    for iface in enlisted:
        bring_down(iface)
        delete_profile(iface)

    with run_lock:
        run_state["connected"] = set()
        run_state["wifi_mode"] = None
        stats["active_interfaces"] = 0
    release_run_lock()
    _log("Run stopped; interfaces disconnected and profiles removed.")
    stop_event.clear()
    return True


def _sweep_orphans():
    """Best-effort cleanup of leftover profiles from a previous run killed mid-flight (root/Linux only)."""
    if platform.system() != "Linux":
        return
    ok, out, _ = _run(["sudo", "-n", "nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=5)
    if ok:
        for line in out.splitlines():
            name = line.strip()
            if name.startswith(PROFILE_PREFIX + "-"):
                _run(["sudo", "-n", "nmcli", "connection", "delete", name])


# ---------------------------------------------------------------------------
# Scan + saved-password lookup: pure UI convenience so the SSID/password fields
# can be filled from a live scan instead of typed blind. Deliberately not a
# dependency on wifi_connection_manager -- this app stays fully self-contained
# (it may be the only app installed on a given Pi) and still creates its own
# disposable porcupine-<iface> profile regardless of where the password came from.
# ---------------------------------------------------------------------------
def find_saved_password(ssid: str):
    """Look up a saved NetworkManager wifi profile whose SSID matches `ssid` and
    reveal its stored PSK. Matches on the profile's actual 802-11-wireless.ssid
    property, not its name, since NetworkManager doesn't guarantee those match.

    Returns None if there's no matching saved profile, or if the match has no
    stored PSK (open network, enterprise/802.1x, or unreadable) -- all treated
    the same way by the caller: nothing to auto-fill, not an error.
    """
    ok, out, _ = _nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
    if not ok or not out:
        return None

    for line in out.strip().splitlines():
        parts = _split_terse(line)
        if len(parts) < 2 or parts[1] != "802-11-wireless":
            continue
        name = parts[0]

        ok2, ssid_out, _ = _nmcli(["-t", "-f", "802-11-wireless.ssid", "connection", "show", name])
        if not ok2 or not ssid_out:
            continue
        ssid_line = _split_terse(ssid_out.strip().splitlines()[0])
        if len(ssid_line) < 2 or ssid_line[1] != ssid:
            continue

        ok3, psk_out, _ = _nmcli(["-s", "-g", "802-11-wireless-security.psk", "connection", "show", name])
        if not ok3:
            return None  # matched profile has no PSK property (open/enterprise) or read failed
        lines = (psk_out or "").strip().splitlines()
        return _split_terse(lines[0])[0] if lines else None

    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        hostname=get_hostname(),
        presence_min=PRESENCE_RANGE[0],
        presence_max=PRESENCE_RANGE[1],
        presence_default=PRESENCE_DEFAULT,
        churn_min=CHURN_RANGE[0],
        churn_max=CHURN_RANGE[1],
        churn_default=CHURN_DEFAULT,
        variability_min=VARIABILITY_RANGE[0],
        variability_max=VARIABILITY_RANGE[1],
        variability_default=VARIABILITY_DEFAULT,
        rate_at_min=RATE_AT_MIN,
        rate_at_max=RATE_AT_MAX,
        offline_estimate=OFFLINE_ESTIMATE_SECONDS,
        min_dwell=MIN_DWELL,
        gap_min=GAP_MIN,
    )


@app.route("/api/hostname")
def api_hostname():
    return jsonify({"hostname": get_hostname()})


@app.route("/api/interfaces")
def api_interfaces():
    wifi_mode, wifi_reason = detect_wifi_mode()
    return jsonify({
        "interfaces": get_wireless_interfaces(),
        "wifi_supported": wifi_mode is not None,
        "wifi_reason": wifi_reason,
    })


@app.route("/api/scan")
def api_scan():
    """Scan for nearby WiFi networks on a chosen (or the first detected) interface,
    purely so the Target SSID field can be filled from a live list. `--rescan yes`
    is fine here (unlike wifi_connection_manager's status polling) because this is
    a single one-off request against one interface, not something polled on a timer.
    """
    interfaces = get_wireless_interfaces()
    requested_iface = request.args.get("interface")
    if requested_iface:
        if requested_iface not in interfaces:
            return jsonify({"success": False, "error": f"Unknown interface: {requested_iface}."}), 400
        iface = requested_iface
    else:
        iface = interfaces[0] if interfaces else ""
    if not iface:
        return jsonify({"success": False, "error": "No wireless interface detected on the system."}), 200

    ok, out, err = _nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ", "device", "wifi", "list",
         "ifname", iface, "--rescan", "yes"],
        timeout=NMCLI_TIMEOUT,
    )
    if not ok:
        return jsonify({"success": False, "error": f"Scan failed: {friendly(err)}"}), 200

    networks = {}
    for line in (out or "").strip().splitlines():
        parts = _split_terse(line)
        if len(parts) < 6:
            continue
        in_use, ssid, signal, security, chan, freq = parts[:6]
        if not ssid:
            continue
        freq_mhz = int(freq.split()[0]) if freq.split() and freq.split()[0].isdigit() else None
        net = {
            "ssid": ssid,
            "connected": in_use.strip() == "*",
            "signal": int(signal) if signal.isdigit() else 0,
            "security": classify_security(security),
            "channel": int(chan) if chan.isdigit() else None,
            "band": classify_band(freq_mhz),
        }
        # De-duplicate SSIDs seen on multiple BSSIDs (mesh/repeater setups); keep the strongest.
        existing = networks.get(ssid)
        if not existing or net["signal"] > existing["signal"]:
            networks[ssid] = net

    deduped = sorted(networks.values(), key=lambda n: n["signal"], reverse=True)
    return jsonify({"success": True, "interface": iface, "networks": deduped})


@app.route("/api/saved-password")
def api_saved_password():
    """Look up a saved NetworkManager profile's PSK for `ssid`, so the password
    field can be auto-filled when this Pi already has that network configured
    (e.g. via wifi_connection_manager). `found=False` covers both "no matching
    saved profile" and "matched, but nothing stored" -- neither is an error, it's
    just nothing to fill in, and the operator can still type a password by hand.
    """
    ssid = (request.args.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"success": False, "error": "ssid is required."}), 400
    password = find_saved_password(ssid)
    return jsonify({"success": True, "found": password is not None, "password": password})


@app.route("/api/status")
def api_status():
    with run_lock:
        payload = {
            "running": run_state["running"],
            "wifi_mode": run_state["wifi_mode"],
            "enlisted": list(run_state["enlisted"]),
        }
        payload.update(stats)
    return jsonify(payload)


@app.route("/api/output")
def api_output():
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    return jsonify(read_output(since))


@app.route("/api/start", methods=["POST"])
def api_start():
    global log_total
    data = request.get_json(silent=True) or {}

    with run_lock:
        if run_state["running"]:
            return jsonify({"error": "A run is already in progress."}), 409

    wifi_mode, wifi_reason = detect_wifi_mode()
    if wifi_mode is None:
        return jsonify({"error": f"Cannot start: {wifi_reason}."}), 400

    interfaces = data.get("interfaces") or []
    if not isinstance(interfaces, list) or not interfaces:
        return jsonify({"error": "Select at least one WiFi interface."}), 400

    detected = set(get_wireless_interfaces())
    clean = []
    for iface in interfaces:
        iface = str(iface).strip()
        if not IFACE_RE.match(iface):
            return jsonify({"error": f"Invalid interface name: {iface!r}."}), 400
        if detected and iface not in detected:
            return jsonify({"error": f"{iface} is not a detected WiFi interface."}), 400
        clean.append(iface)

    ssid = (data.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"error": "Target SSID is required."}), 400
    password = data.get("password") or ""

    try:
        presence = int(data.get("presence", PRESENCE_DEFAULT))
    except (TypeError, ValueError):
        return jsonify({"error": "Presence must be a number."}), 400
    if not (PRESENCE_RANGE[0] <= presence <= PRESENCE_RANGE[1]):
        return jsonify({"error": f"Presence must be between {PRESENCE_RANGE[0]} and {PRESENCE_RANGE[1]}%."}), 400

    try:
        churn = int(data.get("churn", CHURN_DEFAULT))
    except (TypeError, ValueError):
        return jsonify({"error": "Churn rate must be a number."}), 400
    if not (CHURN_RANGE[0] <= churn <= CHURN_RANGE[1]):
        return jsonify({"error": f"Churn rate must be between {CHURN_RANGE[0]} and {CHURN_RANGE[1]}."}), 400

    try:
        variability = int(data.get("variability", VARIABILITY_DEFAULT))
    except (TypeError, ValueError):
        return jsonify({"error": "Variability must be a number."}), 400
    if not (VARIABILITY_RANGE[0] <= variability <= VARIABILITY_RANGE[1]):
        return jsonify({"error": f"Variability must be between {VARIABILITY_RANGE[0]} and {VARIABILITY_RANGE[1]}."}), 400

    try:
        duration = int(data.get("duration_minutes", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "Duration must be a number."}), 400
    if duration < 1:
        return jsonify({"error": "Duration must be at least 1 minute."}), 400

    randomize_mac = bool(data.get("randomize_mac", False))

    config = {
        "interfaces": clean,
        "ssid": ssid,
        "password": password,
        "presence": presence,
        "churn": churn,
        "variability": variability,
        "duration_minutes": duration,
        "randomize_mac": randomize_mac,
    }

    with run_lock:
        if run_state["running"]:
            return jsonify({"error": "A run is already in progress."}), 409
        if not acquire_run_lock(f"ssid={ssid!r}"):
            holder = read_run_lock_info()
            detail = f" ({holder})" if holder else ""
            return jsonify({
                "error": f"Another WiFi Porcupine process already has a run in progress "
                         f"on this host{detail}. Stop it there first.",
            }), 409
        run_state["running"] = True
        run_state["wifi_mode"] = "live"
        run_state["enlisted"] = clean
        run_state["connected"] = set()
        run_state["config"] = config
        stats.update({"reconnects": 0, "errors": 0, "active_interfaces": 0})
    # Reset the log ring buffer so a new run's Live Activity starts clean rather
    # than replaying the previous run's lines (the client polls api/output?since=0).
    with log_lock:
        log_lines.clear()
        log_total = 0
    stop_event.clear()

    threading.Thread(target=start_run, args=(config,), daemon=True).start()
    return jsonify({"status": "starting"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stopped = stop_run()
    return jsonify({"status": "stopped" if stopped else "no run in progress"})


# Sweep any leftover profiles from a run that was killed mid-flight, so restarts are idempotent.
_sweep_orphans()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5010))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    app.run(host="0.0.0.0", port=port, debug=True)
