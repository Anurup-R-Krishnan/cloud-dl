"""Cross-platform UI tunnels, SOCKS5 browser launcher, watchdog checks, and diagnostics."""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
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
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

from .aws import get_aws_state, require_aws_auth, start_aws_instance
from .config import (
    Terminal,
    format_size,
    get_cache_dir,
    get_cfg,
    get_config_path,
    log_err,
    log_info,
    log_ok,
    log_warn,
)
from .exceptions import CloudDlError, ConfigError
from .rpc import build_rpc_payload, query_status_payload, resolve_cloud
from .ssh import run_ssh

ARIANG_URL = "https://github.com/mayswind/AriaNg/releases/latest/download/AriaNg-AllInOne.zip"

_BROWSER_BINARIES: dict[str, list[str]] = {
    "chrome": [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        "microsoft-edge",
        "msedge",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "zen": [
        "zen",
        "zen-browser",
        r"C:\Program Files\Zen Browser\zen.exe",
    ],
    "firefox": [
        "firefox",
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
}


def get_tunnel_state_path(kind: str = "ui") -> Path:
    if kind == "ui":
        override = os.environ.get("CLOUD_DL_TUNNEL_STATE")
        if override:
            return Path(os.path.expanduser(override))
    return get_cache_dir() / f"{kind}-tunnel.json"


def load_tunnel_state(kind: str = "ui") -> dict[str, Any] | None:
    path = get_tunnel_state_path(kind)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _is_windows_pid_alive(pid: int) -> bool:
    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return False
    handle = kernel32.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == 259
        return False
    finally:
        kernel32.kernel32.CloseHandle(handle)


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> bool:
    if sys.platform == "win32":
        res = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return res.returncode in {0, 128}
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except OSError as exc:
        log_warn(f"Could not stop tunnel PID {pid}: {exc}")
        return False


def stop_tracked_tunnel(kind: str = "ui") -> bool:
    """Terminate the exact SSH tunnel PID recorded in state JSON and clean state."""
    path = get_tunnel_state_path(kind)
    state = load_tunnel_state(kind)
    if not state:
        return False
    pid = state.get("pid")
    stopped = _terminate_pid(pid) if isinstance(pid, int) and pid > 0 else False
    with contextlib.suppress(OSError):
        path.unlink()
    return stopped


def _is_local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_local_port(port: int, proc: subprocess.Popen[bytes], timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if _is_local_port_open(port):
            return True
        time.sleep(0.15)
    return proc.poll() is None


def _popen_detached(cmd: list[str]) -> subprocess.Popen[bytes]:
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _ensure_ariang_bundle(ariang_path: Path) -> None:
    if ariang_path.is_file():
        return
    ariang_path.parent.mkdir(parents=True, exist_ok=True)
    log_info("AriaNg HTML bundle not found locally; downloading latest release...")
    zip_tmp = ariang_path.parent / "ariang.zip"
    try:
        urllib.request.urlretrieve(ARIANG_URL, str(zip_tmp))
        with zipfile.ZipFile(zip_tmp, "r") as zf:
            if "index.html" not in zf.namelist():
                raise CloudDlError("Downloaded AriaNg archive did not contain index.html.")
            ariang_path.write_bytes(zf.read("index.html"))
        log_ok(f"Installed AriaNg UI bundle at {ariang_path}")
    except (OSError, zipfile.BadZipFile, urllib.error.URLError, CloudDlError) as exc:
        log_warn(f"Could not auto-download AriaNg bundle ({exc}); use remote URL or install index.html manually.")
    finally:
        with contextlib.suppress(OSError):
            zip_tmp.unlink()


def _save_tunnel_state(kind: str, payload: dict[str, Any]) -> None:
    state_path = get_tunnel_state_path(kind)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _open_ui_tunnel(cloud: str, host: str, remote_port: int) -> None:
    tracked = load_tunnel_state("ui")
    if tracked:
        pid = tracked.get("pid")
        if tracked.get("cloud") == cloud and isinstance(pid, int) and is_pid_alive(pid):
            return
        log_info("Closing previous UI tunnel before reconnecting...")
        stop_tracked_tunnel("ui")

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
    proc = _popen_detached(tunnel_cmd)
    if not _wait_local_port(6800, proc):
        raise CloudDlError("Failed to initialize SSH port forward on localhost:6800.")
    _save_tunnel_state("ui", {"pid": proc.pid, "cloud": cloud, "host": host, "port": 6800})


def _ensure_socks_tunnel(host: str, socks_port: int) -> None:
    if _is_local_port_open(socks_port):
        return
    tracked = load_tunnel_state("socks")
    if tracked:
        stop_tracked_tunnel("socks")

    log_info(f"Starting OCI SOCKS5 tunnel via {host} on 127.0.0.1:{socks_port}...")
    tunnel_cmd = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-D",
        f"127.0.0.1:{int(socks_port)}",
        host,
    ]
    proc = _popen_detached(tunnel_cmd)
    if not _wait_local_port(socks_port, proc):
        raise CloudDlError(f"Failed to start SOCKS5 SSH proxy on 127.0.0.1:{socks_port}.")
    _save_tunnel_state("socks", {"pid": proc.pid, "host": host, "port": socks_port})


def _find_browser_binary(browser: str) -> str | None:
    candidates = list(_BROWSER_BINARIES.get(browser, []))
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app and browser == "chrome":
            candidates.append(str(Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        elif local_app and browser == "zen":
            candidates.append(str(Path(local_app) / "Programs" / "Zen Browser" / "zen.exe"))
    for candidate in candidates:
        if os.path.isabs(candidate) and Path(candidate).is_file():
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _write_firefox_socks_prefs(profile_dir: Path, socks_port: int) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    prefs = (
        'user_pref("network.proxy.type", 1);\n'
        'user_pref("network.proxy.socks", "127.0.0.1");\n'
        f'user_pref("network.proxy.socks_port", {int(socks_port)});\n'
        'user_pref("network.proxy.socks_version", 5);\n'
        'user_pref("network.proxy.socks_remote_dns", true);\n'
    )
    (profile_dir / "user.js").write_text(prefs, encoding="utf-8")


def _print_browse_status(socks_port: int, http_port: int) -> None:
    socks_up = _is_local_port_open(socks_port)
    http_up = _is_local_port_open(http_port)
    tracked = load_tunnel_state("socks")
    pid = tracked.get("pid") if tracked else None
    pid_note = f" (PID {pid})" if isinstance(pid, int) and is_pid_alive(pid) else ""
    if socks_up:
        log_ok(f"SOCKS5 proxy active on 127.0.0.1:{socks_port}{pid_note}.")
    else:
        log_warn(f"SOCKS5 proxy is inactive on 127.0.0.1:{socks_port}. Run 'cloud-dl browse' to start it.")
    if http_up:
        log_ok(f"HTTP proxy active on 127.0.0.1:{http_port}.")


def _print_browse_env(socks_port: int, http_port: int) -> None:
    if sys.platform == "win32":
        sys.stdout.write(
            f'$env:ALL_PROXY = "socks5h://127.0.0.1:{socks_port}"\n'
            f'$env:HTTPS_PROXY = "socks5h://127.0.0.1:{socks_port}"\n'
            f'$env:HTTP_PROXY = "socks5h://127.0.0.1:{socks_port}"\n'
        )
        return
    proxy_http = (
        f"http://127.0.0.1:{http_port}" if _is_local_port_open(http_port) else f"socks5h://127.0.0.1:{socks_port}"
    )
    sys.stdout.write(
        f'export ALL_PROXY="socks5h://127.0.0.1:{socks_port}"\n'
        f'export HTTPS_PROXY="{proxy_http}"\n'
        f'export HTTP_PROXY="{proxy_http}"\n'
    )


def _launch_proxied_browser(browser: str, socks_port: int, target: str | None) -> None:
    binary = _find_browser_binary(browser)
    if not binary:
        raise CloudDlError(
            f"Browser '{browser}' executable not found on this machine. "
            "Install Chrome, Edge, Zen, or Firefox, or run 'cloud-dl browse env'."
        )
    profile_dir = get_cache_dir() / f"browser-{browser}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    if browser in {"chrome", "edge"}:
        cmd = [
            binary,
            f"--proxy-server=socks5://127.0.0.1:{int(socks_port)}",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
        ]
    else:
        _write_firefox_socks_prefs(profile_dir, socks_port)
        cmd = [binary, "--profile", str(profile_dir), "--no-remote"]
    if target:
        cmd.append(target)
    _popen_detached(cmd)
    log_ok(f"Launched {browser} routed through OCI SOCKS5 proxy (127.0.0.1:{socks_port}).")


def cmd_browse(args: argparse.Namespace) -> None:
    """Manage the OCI SOCKS5 proxy tunnel and launch an isolated proxied browser."""
    cfg = get_cfg()
    proxy_cfg = cfg.get("proxy", {})
    socks_port = int(proxy_cfg.get("socks_port", 1080))
    http_port = int(proxy_cfg.get("http_port", 8888))
    action = (args.browser or "chrome").lower()

    if action == "status":
        _print_browse_status(socks_port, http_port)
        return
    if action == "stop":
        if stop_tracked_tunnel("socks"):
            log_ok("Stopped OCI SOCKS5 tunnel.")
        else:
            log_info("No tracked SOCKS5 tunnel found.")
        return
    if action == "env":
        _ensure_socks_tunnel(cfg["oci"]["host"], socks_port)
        _print_browse_env(socks_port, http_port)
        return

    _ensure_socks_tunnel(cfg["oci"]["host"], socks_port)
    _launch_proxied_browser(action, socks_port, args.target)


def cmd_ui(args: argparse.Namespace) -> None:
    if args.stop:
        log_info("Terminating tracked AriaNg SSH tunnel...")
        if stop_tracked_tunnel("ui"):
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
    ariang_file = Path(cfg["ariang_path"]).expanduser()
    _ensure_ariang_bundle(ariang_file)

    secret_b64 = base64.b64encode(secret.encode("utf-8")).decode("utf-8")
    base_uri = ariang_file.resolve().as_uri()
    ui_url = f"{base_uri}#!/settings/rpc/set/http/127.0.0.1/6800/jsonrpc/{secret_b64}"

    log_ok("AriaNg web interface ready.")
    sys.stdout.write(f"Connected to: {cfg[cloud]['name']} via localhost:6800\n")
    sys.stdout.write(f"Dashboard URL: {ui_url}\n")
    sys.stdout.write("Close tunnel when done: 'cloud-dl ui --stop'\n")
    webbrowser.open(ui_url)


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
        if os.name == "nt":
            log_ok(f"Config loaded ({cfg_path}).")
        else:
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

    for tool_name in ["ssh", "aws"]:
        if shutil.which(tool_name):
            log_ok(f"Required tool '{tool_name}' found on PATH.")
        else:
            log_warn(f"Required tool '{tool_name}' not found on PATH.")
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

    pref_dir = cfg.get("preferred_local_dir", str(Path.home() / "Downloads"))
    storage_ok = os.path.exists(pref_dir) if sys.platform == "win32" else os.path.ismount(pref_dir)
    if storage_ok:
        usage = shutil.disk_usage(pref_dir)
        log_ok(
            f"Local storage ready at {pref_dir} ({format_size(usage.free)} free / {format_size(usage.total)} total)."
        )
    else:
        log_warn(f"Preferred download location '{pref_dir}' is unavailable.")
        issues += 1
    return issues, st


def _check_proxy_and_rpc_health(cfg: dict[str, Any], aws_state: str) -> int:
    issues = 0
    socks_port = int(cfg.get("proxy", {}).get("socks_port", 1080))
    if _is_local_port_open(socks_port):
        log_ok(f"OCI SOCKS5 proxy listening on 127.0.0.1:{socks_port}.")
    else:
        log_warn(f"OCI SOCKS5 proxy inactive on 127.0.0.1:{socks_port} (start with 'cloud-dl browse').")
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
    """Run read-only diagnostics across config, tools, AWS auth, storage, proxy, and RPC."""
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
