"""PID-tracked UI tunnels, OCI browsing launcher, watchdog inspection, and health checks."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .aws import get_aws_state, require_aws_auth, start_aws_instance
from .config import Terminal, format_size, get_cfg, get_config_path, log_err, log_info, log_ok, log_warn
from .exceptions import CloudDlError, ConfigError
from .rpc import build_rpc_payload, query_status_payload, resolve_cloud
from .ssh import run_ssh

ARIANG_URL = "https://github.com/mayswind/AriaNg/releases/latest/download/AriaNg-AllInOne.zip"


def get_tunnel_state_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLOUD_DL_TUNNEL_STATE", "~/.cache/cloud-dl/ui-tunnel.json")))


def load_tunnel_state() -> dict[str, Any] | None:
    path = get_tunnel_state_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def stop_tracked_tunnel() -> bool:
    """Terminate the exact SSH tunnel PID recorded in ui-tunnel.json and clean state."""
    path = get_tunnel_state_path()
    state = load_tunnel_state()
    if not state:
        return False
    pid = state.get("pid")
    stopped = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except ProcessLookupError:
            stopped = True
        except OSError as exc:
            log_warn(f"Could not stop tunnel PID {pid}: {exc}")
    with contextlib.suppress(OSError):
        path.unlink()
    return stopped


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_local_port(port: int, proc: subprocess.Popen[bytes], timeout_s: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.15)
    return proc.poll() is None


def _ensure_ariang_bundle(ariang_path: Path) -> None:
    if ariang_path.is_file():
        return
    ariang_path.parent.mkdir(parents=True, exist_ok=True)
    log_info("AriaNg HTML bundle not found locally; downloading latest release...")
    zip_tmp = ariang_path.parent / "ariang.zip"
    try:
        urllib.request.urlretrieve(ARIANG_URL, str(zip_tmp))
        subprocess.run(
            ["unzip", "-o", str(zip_tmp), "index.html", "-d", str(ariang_path.parent)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        log_ok(f"Installed AriaNg UI bundle at {ariang_path}")
    except (OSError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        log_warn(f"Could not auto-download AriaNg bundle ({exc}); use remote URL or install index.html manually.")
    finally:
        with contextlib.suppress(OSError):
            zip_tmp.unlink()


def _open_ui_tunnel(cloud: str, host: str, remote_port: int) -> None:
    tracked = load_tunnel_state()
    if tracked:
        pid = tracked.get("pid")
        if tracked.get("cloud") == cloud and isinstance(pid, int) and is_pid_alive(pid):
            return
        log_info("Closing previous UI tunnel before reconnecting...")
        stop_tracked_tunnel()

    log_info(f"Opening SSH tunnel to {get_cfg()[cloud]['name']} (Port 6800)...")
    tunnel_cmd = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-L",
        f"6800:127.0.0.1:{int(remote_port)}",
        host,
    ]
    proc = subprocess.Popen(
        tunnel_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if not _wait_local_port(6800, proc):
        raise CloudDlError("Failed to initialize SSH port forward on localhost:6800.")

    state_path = get_tunnel_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"pid": proc.pid, "cloud": cloud, "host": host, "port": 6800}, indent=2) + "\n",
        encoding="utf-8",
    )


def cmd_browse(args: argparse.Namespace) -> None:
    target = [args.target] if args.target else []
    sub = [args.browser] if args.browser else []
    subprocess.run(["oci-browse", *sub, *target], check=False)


def cmd_ui(args: argparse.Namespace) -> None:
    if args.stop:
        log_info("Terminating tracked AriaNg SSH tunnel...")
        if stop_tracked_tunnel():
            log_ok("Tunnel terminated.")
        else:
            log_info("No active tracked tunnel found.")
        return

    cfg = get_cfg()
    secret = cfg.get("rpc_secret", "")
    if not secret:
        raise ConfigError("RPC secret is not configured. Run 'cloud-dl init' first.")

    auto_login = not getattr(args, "no_auto_login", False)
    cloud = resolve_cloud(args.cloud, interactive=True, auto_login=auto_login)
    host = cfg[cloud]["host"]

    if cloud == "aws":
        require_aws_auth(auto_login=auto_login)
        if get_aws_state() != "running" and not start_aws_instance(auto_login=auto_login):
            raise CloudDlError("Failed to start AWS instance.")

    _open_ui_tunnel(cloud, host, int(cfg["rpc_port"]))
    _ensure_ariang_bundle(Path(cfg["ariang_path"]))

    secret_b64 = base64.b64encode(secret.encode("utf-8")).decode("utf-8")
    ui_url = f"file://{cfg['ariang_path']}#!/settings/rpc/set/http/127.0.0.1/6800/jsonrpc/{secret_b64}"

    log_ok("AriaNg web interface ready.")
    sys.stdout.write(f"Connected to: {cfg[cloud]['name']} via localhost:6800\n")
    sys.stdout.write(f"Dashboard URL: {ui_url}\n")
    sys.stdout.write("Close tunnel when done: 'cloud-dl ui --stop'\n")

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["xdg-open", ui_url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
        )


def cmd_watchdog(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    cloud = (args.cloud or "aws").lower()
    host = cfg[cloud]["host"]
    threshold_s = int(cfg.get("watchdog_threshold_s", 1800))

    sys.stdout.write(f"\n{Terminal.BOLD}Watchdog Daemon Status: {cfg[cloud]['name']}{Terminal.RESET}\n")
    payload = build_rpc_payload("aria2.tellActive", req_id="1")
    remote_cmd = (
        f"echo -n 'Active RPC jobs: '; curl -s http://127.0.0.1:{int(cfg['rpc_port'])}/jsonrpc "
        "-H 'Content-Type: application/json' -d @- | jq '.result | length' 2>/dev/null || echo '0'; "
        "echo -n 'Active SSH sessions: '; ss -Htn state established '( sport = :22 )' | wc -l; "
        "if [ -f /tmp/idle_since ]; then "
        "  IDLE=$(cat /tmp/idle_since); NOW=$(date +%s); DIFF=$((NOW - IDLE)); "
        f"  echo \"Idle since: $(date -d @$IDLE '+%H:%M:%S') (${{DIFF}}s elapsed / {threshold_s}s shutdown threshold)\"; "
        "else "
        "  echo 'System state: ACTIVE (idle countdown disarmed)'; "
        "fi"
    )
    _code, stdout, stderr = run_ssh(host, remote_cmd, stdin_data=payload)
    if stdout.strip():
        sys.stdout.write(stdout + "\n")
    elif stderr.strip():
        log_err(f"Watchdog probe failed: {stderr.strip()}")


def _check_config_health(cfg: dict[str, Any]) -> int:
    issues = 0
    cfg_path = Path(get_config_path())
    if cfg_path.is_file():
        mode = cfg_path.stat().st_mode & 0o777
        if mode & 0o077:
            log_warn(f"Config permissions are too open ({oct(mode)}); run 'chmod 600 {cfg_path}'.")
            issues += 1
        else:
            log_ok(f"Config loaded ({cfg_path}, mode {oct(mode)}).")
    else:
        log_warn(f"Config file missing at {cfg_path}; run 'cloud-dl init'.")
        issues += 1

    if cfg["aws"].get("instance_id") and cfg.get("rpc_secret"):
        log_ok("AWS instance ID and RPC secret configured.")
    else:
        log_warn("Missing AWS instance_id or rpc_secret; run 'cloud-dl init'.")
        issues += 1
    return issues


def _check_aws_and_storage_health(cfg: dict[str, Any]) -> tuple[int, str]:
    issues = 0
    st = get_aws_state()
    if st == "auth_expired":
        log_warn("AWS CLI session expired (run 'aws login' or any interactive AWS cloud-dl command).")
        issues += 1
    elif st == "unconfigured":
        log_warn("AWS instance ID not configured.")
        issues += 1
    else:
        log_ok(f"AWS session active ({cfg['aws']['instance_id']}: {st}).")

    pref_dir = cfg.get("preferred_local_dir", "/mnt/DATA")
    if os.path.ismount(pref_dir):
        usage = shutil.disk_usage(pref_dir)
        log_ok(
            f"External drive mounted at {pref_dir} ({format_size(usage.free)} free / {format_size(usage.total)} total)."
        )
    else:
        log_warn(f"Preferred drive '{pref_dir}' is not mounted.")
        issues += 1
    return issues, st


def _check_proxy_and_rpc_health(cfg: dict[str, Any], aws_state: str) -> int:
    issues = 0
    try:
        socks_active = subprocess.run(
            ["systemctl", "--user", "is-active", "oci-socks5.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
        if socks_active.returncode == 0 and socks_active.stdout.strip() == "active":
            log_ok("OCI SOCKS5/HTTP tunnel service (oci-socks5.service) is active.")
        else:
            log_warn("oci-socks5.service is inactive (start with 'oci-browse status').")
            issues += 1
    except (OSError, subprocess.SubprocessError):
        log_warn("Could not query oci-socks5.service status.")
        issues += 1

    for c_key in ["oci", "aws"]:
        if c_key == "aws" and aws_state != "running":
            log_info(f"Skipping RPC check on {cfg[c_key]['name']} (state: {aws_state}).")
            continue
        _act, _wai, _stp, _disk, err = query_status_payload(cfg[c_key]["host"])
        if err:
            log_warn(f"{cfg[c_key]['name']} Aria2 JSON-RPC unreachable ({err}).")
            issues += 1
        else:
            log_ok(f"{cfg[c_key]['name']} Aria2 JSON-RPC reachable.")
    return issues


def cmd_doctor(_args: argparse.Namespace) -> None:
    """Run read-only diagnostics across config, AWS auth, storage, proxy, and RPC."""
    cfg = get_cfg()
    sys.stdout.write(f"\n{Terminal.BOLD}cloud-dl System Health Check{Terminal.RESET}\n")
    sys.stdout.write("=" * 68 + "\n")
    issues = _check_config_health(cfg)
    storage_issues, st = _check_aws_and_storage_health(cfg)
    issues += storage_issues
    issues += _check_proxy_and_rpc_health(cfg, st)
    sys.stdout.write("=" * 68 + "\n")
    if issues == 0:
        log_ok("All subsystems healthy.")
    else:
        log_warn(f"Detected {issues} warning(s) above.")
