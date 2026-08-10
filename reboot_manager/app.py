#!/usr/bin/env python3
"""
Flask app that reboots this Pi on request.

Deliberately tiny: one status read (hostname, uptime, whether a reboot
mechanism is even available) and one action (`sudo systemctl reboot`, falling
back to `sudo reboot`). Its systemd unit runs as root by default like
client_simulator and wifi_porcupine, so the `sudo` prefix is a harmless no-op
there -- see CLAUDE.md's Environment Split section.

Because this ends the process (and every other WiFiPi app on the same host)
mid-response, the actual reboot is fired from a short-lived background thread
after a small delay, so the HTTP response has time to flush before the host
goes down. The API also requires an explicit confirmation token in the POST
body -- not for the browser UI (which already makes you sit through a
countdown you can cancel), but so a stray or scripted POST can't take the
host down by accident.

Off-Linux, or without any reboot mechanism on PATH, /api/reboot refuses with
a clear JSON error instead of crashing -- this is what makes it degrade
gracefully on macOS in dev. See CLAUDE.md's Environment Split section.
"""

import platform
import shutil
import socket
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

REBOOT_CONFIRM_TOKEN = "REBOOT"
REBOOT_DELAY_SECONDS = 1.5  # lets the HTTP response flush before the host goes down

# Guards against a second /api/reboot call racing in during the delay window above.
reboot_lock = threading.Lock()
reboot_state = {"pending": False, "requested_at": None}


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def get_uptime_seconds():
    """Seconds since boot, via /proc/uptime. None where that doesn't exist (e.g. macOS)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.readline().split()[0])
    except Exception:
        return None


def format_uptime(seconds) -> str:
    """Render uptime seconds as e.g. '2d 4h 09m'; 'unknown' when unavailable."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes:02d}m" if (days or hours) else f"{minutes}m")
    return " ".join(parts)


def can_reboot():
    """Whether this host can actually be rebooted from here. Returns (ok, reason)."""
    if platform.system() != "Linux":
        return False, "not running on Linux"
    if not shutil.which("systemctl") and not shutil.which("reboot"):
        return False, "no reboot mechanism found (systemctl/reboot not on PATH)"
    return True, None


def _reboot_command():
    """Prefer `systemctl reboot` (cleaner shutdown of services); fall back to `reboot`."""
    if shutil.which("systemctl"):
        return ["sudo", "systemctl", "reboot"]
    return ["sudo", "reboot"]


def _do_reboot():
    """Background-thread target: wait for the response to flush, then reboot."""
    time.sleep(REBOOT_DELAY_SECONDS)
    try:
        subprocess.run(_reboot_command(), timeout=10)
    except Exception:
        # The host is either going down anyway or the command failed outright --
        # either way there is no request left to report the outcome to.
        pass


@app.route("/")
def index():
    """Render the reboot control page."""
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname")
def api_hostname():
    """Expose the hostname so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/api/status")
def api_status():
    """Report hostname, uptime, and whether a reboot is possible from here."""
    ok, reason = can_reboot()
    uptime = get_uptime_seconds()
    with reboot_lock:
        pending = reboot_state["pending"]
    return jsonify({
        "hostname": get_hostname(),
        "platform": platform.system(),
        "uptime_seconds": uptime,
        "uptime_text": format_uptime(uptime),
        "can_reboot": ok,
        "reason": reason,
        "reboot_pending": pending,
    })


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    """Reboot this host. Requires {"confirm": "REBOOT"} in the JSON body."""
    ok, reason = can_reboot()
    if not ok:
        return jsonify({"error": f"Cannot reboot: {reason}."}), 501

    data = request.get_json(silent=True) or {}
    if data.get("confirm") != REBOOT_CONFIRM_TOKEN:
        return jsonify({"error": "Missing or incorrect confirmation."}), 400

    with reboot_lock:
        if reboot_state["pending"]:
            return jsonify({"error": "A reboot has already been triggered."}), 409
        reboot_state["pending"] = True
        reboot_state["requested_at"] = time.time()

    threading.Thread(target=_do_reboot, daemon=True).start()
    return jsonify({
        "status": "rebooting",
        "message": "Reboot initiated. This host will be unreachable shortly.",
    }), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5011, debug=False)
