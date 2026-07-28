#!/usr/bin/env python3
"""
iPerf3 Server Manager - Flask Web Application
Provides a web interface to view, start, stop, restart, and monitor iperf3 server daemons and systemd services.
"""

import os
import sys
import re
import signal
import socket
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)


def parse_port_from_args(cmdline_str: str) -> int:
    """Extract port number from iperf3 command line or service file string. Default is 5201."""
    match = re.search(r'(?:-p|--port)\s+([0-9]+)', cmdline_str)
    if not match:
        match = re.search(r'--port=([0-9]+)', cmdline_str)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return 5201


def get_running_iperf_servers() -> list:
    """
    Find all running iperf3 server processes.
    Returns a list of dicts containing process metadata.
    """
    servers = []
    try:
        # ps -eo pid,user,etime,args
        output = subprocess.check_output(
            ["ps", "-eo", "pid,user,etime,args"],
            text=True,
            stderr=subprocess.DEVNULL
        )
        for line in output.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            
            parts = line.split(maxsplit=3)
            if len(parts) < 4:
                continue
            
            pid_str, user, etime, cmd = parts[0], parts[1], parts[2], parts[3]

            if "iperf3" in cmd and ("--server" in cmd or " -s " in cmd or cmd.endswith(" -s")):
                if "grep" in cmd or "python" in cmd:
                    continue
                try:
                    pid = int(pid_str)
                except ValueError:
                    continue
                
                port = parse_port_from_args(cmd)
                servers.append({
                    "pid": pid,
                    "user": user,
                    "elapsed": etime,
                    "command": cmd,
                    "port": port
                })
    except Exception as e:
        app.logger.error(f"Error querying process list: {e}")

    servers.sort(key=lambda s: s["port"])
    return servers


def get_systemd_iperf_services() -> list:
    """
    Discover systemd service units for iperf3 (e.g. iperf3.service, iperf3-5202.service).
    Returns list of dicts with unit_name, port, active_state, sub_state, path.
    """
    services = []
    unit_files = set()

    # Search /etc/systemd/system for iperf3*.service
    sys_dir = "/etc/systemd/system"
    if os.path.exists(sys_dir):
        try:
            for fname in os.listdir(sys_dir):
                if fname.startswith("iperf3") and fname.endswith(".service"):
                    unit_files.add(fname)
        except Exception:
            pass

    # Fallback/supplement with systemctl list-unit-files if systemctl is available
    try:
        out = subprocess.check_output(
            ["systemctl", "list-unit-files", "iperf3*.service"],
            text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].startswith("iperf3") and parts[0].endswith(".service"):
                unit_files.add(parts[0])
    except Exception:
        pass

    # If no files found, add default candidates if they exist in ps or systemctl status
    if not unit_files:
        for default_unit in ["iperf3.service", "iperf3-5202.service", "iperf3-5203.service", "iperf3-5204.service"]:
            unit_files.add(default_unit)

    for unit in sorted(unit_files):
        port = 5201
        # Extract port from unit name e.g. iperf3-5202.service -> 5202
        p_match = re.search(r'iperf3-([0-9]+)\.service', unit)
        if p_match:
            port = int(p_match.group(1))
        else:
            # Check unit file contents for ExecStart
            unit_path = os.path.join(sys_dir, unit)
            if os.path.exists(unit_path):
                try:
                    with open(unit_path, 'r') as f:
                        content = f.read()
                        port = parse_port_from_args(content)
                except Exception:
                    pass

        # Query systemctl status
        active_state = "unknown"
        sub_state = "unknown"
        pid = None
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", unit],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            active_state = out
        except subprocess.CalledProcessError as cpe:
            active_state = cpe.output.strip() if cpe.output else "inactive"
        except Exception:
            active_state = "inactive"

        # Match running process to unit
        running_servers = get_running_iperf_servers()
        for s in running_servers:
            if s["port"] == port:
                pid = s["pid"]
                break

        services.append({
            "unit": unit,
            "port": port,
            "active_state": active_state,
            "pid": pid
        })

    services.sort(key=lambda x: x["port"])
    return services


def run_systemctl_command(action: str, unit: str) -> tuple[bool, str]:
    """Execute systemctl start/stop/restart command for a given unit."""
    if action not in ("start", "stop", "restart"):
        return False, "Invalid action"

    # Try standard systemctl first, then sudo systemctl
    cmds = [
        ["systemctl", action, unit],
        ["sudo", "systemctl", action, unit]
    ]

    last_err = ""
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                return True, f"Successfully executed systemctl {action} {unit}"
            last_err = res.stderr.strip() or res.stdout.strip() or f"Exit code {res.returncode}"
        except Exception as e:
            last_err = str(e)

    return False, last_err or f"Failed to execute systemctl {action} {unit}"


def is_port_in_use(port: int) -> bool:
    """Check if a TCP port is currently open or listening on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def get_process_cmdline(pid: int) -> str:
    """Retrieve command line for a given PID from /proc or ps."""
    try:
        proc_cmdline = f"/proc/{pid}/cmdline"
        if os.path.exists(proc_cmdline):
            with open(proc_cmdline, 'r') as f:
                content = f.read().replace('\x00', ' ').strip()
                if content:
                    return content
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        return out
    except Exception:
        return ""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/servers", methods=["GET"])
def list_servers():
    """API endpoint to list all running iperf3 server daemons and systemd services."""
    servers = get_running_iperf_servers()
    services = get_systemd_iperf_services()
    return jsonify({
        "success": True,
        "servers": servers,
        "services": services,
        "count": len(servers),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


@app.route("/api/servers/start", methods=["POST"])
def start_server():
    """
    API endpoint to launch a new iperf3 server daemon or systemd service.
    """
    data = request.get_json(silent=True) or {}
    unit = data.get("unit")

    # If unit specified, attempt systemctl start
    if unit:
        ok, msg = run_systemctl_command("start", unit)
        if ok:
            return jsonify({"success": True, "message": msg})
        else:
            return jsonify({"success": False, "error": msg}), 500

    try:
        port = int(data.get("port") or 5201)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid port number"}), 400

    if not (1 <= port <= 65535):
        return jsonify({"success": False, "error": "Port number must be between 1 and 65535"}), 400

    try:
        interval = int(data.get("interval") if data.get("interval") is not None else 0)
    except (ValueError, TypeError):
        interval = 0

    bind_address = (data.get("bind_address") or "").strip()
    one_off = bool(data.get("one_off", False))

    existing_servers = get_running_iperf_servers()
    for s in existing_servers:
        if s["port"] == port:
            return jsonify({
                "success": False,
                "error": f"An iperf3 server is already running on port {port} (PID {s['pid']})"
            }), 409

    if is_port_in_use(port):
        return jsonify({
            "success": False,
            "error": f"Port {port} is already in use by another process"
        }), 409

    cmd = ["iperf3", "--server", "--port", str(port), "--interval", str(interval)]
    if bind_address:
        cmd.extend(["--bind", bind_address])
    if one_off:
        cmd.append("--one-off")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return jsonify({
            "success": True,
            "message": f"iperf3 server started successfully on port {port}",
            "pid": proc.pid,
            "port": port,
            "command": " ".join(cmd)
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to start iperf3 server: {str(e)}"
        }), 500


@app.route("/api/servers/stop", methods=["POST"])
def stop_server():
    """
    API endpoint to stop a running iperf3 server daemon by PID, port, or unit name.
    """
    data = request.get_json(silent=True) or {}
    unit = data.get("unit")
    pid = data.get("pid")
    port = data.get("port")

    if unit:
        ok, msg = run_systemctl_command("stop", unit)
        if ok:
            return jsonify({"success": True, "message": msg})
        # If systemctl stop failed, fallback to PID kill if process is found

    if not pid and port:
        try:
            port = int(port)
            servers = get_running_iperf_servers()
            for s in servers:
                if s["port"] == port:
                    pid = s["pid"]
                    break
        except (ValueError, TypeError):
            pass

    if not pid:
        return jsonify({"success": False, "error": "PID, Port, or Unit is required"}), 400

    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid PID"}), 400

    cmdline = get_process_cmdline(pid)
    if "iperf3" not in cmdline:
        return jsonify({
            "success": False,
            "error": f"Process PID {pid} is not an iperf3 server instance"
        }), 400

    try:
        os.kill(pid, signal.SIGTERM)
        return jsonify({
            "success": True,
            "message": f"Successfully stopped iperf3 server (PID {pid})"
        })
    except ProcessLookupError:
        return jsonify({
            "success": False,
            "error": f"No process found with PID {pid}"
        }), 404
    except PermissionError:
        return jsonify({
            "success": False,
            "error": f"Permission denied when trying to stop PID {pid}"
        }), 403
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to stop process {pid}: {str(e)}"
        }), 500


@app.route("/api/servers/restart", methods=["POST"])
def restart_server():
    """
    API endpoint to restart an iperf3 server unit or process.
    """
    data = request.get_json(silent=True) or {}
    unit = data.get("unit")
    port = data.get("port")
    pid = data.get("pid")

    if unit:
        ok, msg = run_systemctl_command("restart", unit)
        if ok:
            return jsonify({"success": True, "message": msg})
        else:
            return jsonify({"success": False, "error": msg}), 500

    if pid or port:
        # Stop existing process first
        stop_res = stop_server()
        if stop_res[1] if isinstance(stop_res, tuple) else None:
            pass
        # Wait briefly for process cleanup
        import time
        time.sleep(0.5)
        # Start new process on same port
        return start_server()

    return jsonify({"success": False, "error": "Unit, PID, or Port is required for restart"}), 400


@app.route("/api/ports/check", methods=["GET"])
def check_ports():
    """
    API endpoint to check availability of a range of ports (default 5201-5210).
    """
    try:
        start_port = int(request.args.get("start_port", 5201))
        end_port = int(request.args.get("end_port", 5210))
    except (ValueError, TypeError):
        start_port, end_port = 5201, 5210

    start_port = max(1, min(start_port, 65535))
    end_port = max(start_port, min(end_port, 65535))

    running_servers = {s["port"]: s for s in get_running_iperf_servers()}
    results = []

    for port in range(start_port, end_port + 1):
        if port in running_servers:
            results.append({
                "port": port,
                "status": "active",
                "pid": running_servers[port]["pid"],
                "user": running_servers[port]["user"]
            })
        elif is_port_in_use(port):
            results.append({
                "port": port,
                "status": "occupied_other",
                "pid": None,
                "user": None
            })
        else:
            results.append({
                "port": port,
                "status": "available",
                "pid": None,
                "user": None
            })

    return jsonify({
        "success": True,
        "ports": results
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    app.run(host="0.0.0.0", port=port, debug=True)
