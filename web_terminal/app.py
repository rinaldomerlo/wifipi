#!/usr/bin/env python3
"""
Flask wrapper that presents a browser terminal inside the WiFiPi UI.

This app deliberately owns no terminal logic at all. The PTY, the VT/ANSI
emulation, resize, reconnect and flow control are all handled by `ttyd`
(https://github.com/tsl0922/ttyd), a separate daemon bound to loopback that
embeds xterm.js. All this app contributes is the shared header, hostname badge
and Home button around an iframe pointing at ttyd, plus a health check so a
stopped ttyd produces an explanatory panel rather than a blank frame.

Consequently this module needs no privileges and spawns no subprocesses -- it is
the one app in the suite that shells out to nothing.
"""

import socket

from flask import Flask, render_template, jsonify

app = Flask(__name__)

# ttyd listens here, bound to loopback only (see deploy/ttyd.service). Nginx is
# the sole route in, via the /terminal/tty/ location.
TTYD_PORT = 5009
TTYD_HOST = "127.0.0.1"


def get_hostname() -> str:
    """Return the hostname of the machine serving this app (shown in the GUI header)."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def ttyd_available() -> bool:
    """
    Report whether the ttyd backend is accepting connections.

    A plain TCP connect is enough -- we only need to know the daemon is up, and
    this keeps the check dependency-free and fast. Any OSError (refused, no
    route, timeout) means unavailable, which is also the normal answer on a macOS
    dev machine where ttyd isn't installed at all.
    """
    try:
        with socket.create_connection((TTYD_HOST, TTYD_PORT), timeout=0.5):
            return True
    except OSError:
        return False


@app.route("/")
def index():
    """Render the terminal page."""
    return render_template("index.html", hostname=get_hostname())


@app.route("/api/hostname")
def api_hostname():
    """Expose the hostname so the static landing page can display it too."""
    return jsonify({"hostname": get_hostname()})


@app.route("/api/status")
def api_status():
    """Report whether the terminal backend is reachable, for the page to act on."""
    available = ttyd_available()
    return jsonify({
        "available": available,
        "port": TTYD_PORT,
        "reason": "" if available else (
            "The ttyd backend is not accepting connections on "
            f"{TTYD_HOST}:{TTYD_PORT}."
        ),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5008, debug=False)
