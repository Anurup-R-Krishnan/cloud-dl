"""Aria2 JSON-RPC queue management, provider selection, and status display."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import sys
import time
from typing import Any

from .aws import ensure_aws_auth, get_aws_state, require_aws_auth, start_aws_instance
from .config import (
    Terminal,
    format_size,
    format_speed,
    format_time,
    get_cfg,
    log_err,
    log_info,
    log_ok,
    log_warn,
)
from .exceptions import CloudDlError, ConfigError, RpcError, UsageError
from .ssh import get_disk_free, parse_df_bytes_line, run_ssh

STATUS_BADGES = {
    "ACTIVE": f"{Terminal.GREEN}[ACTIVE]{Terminal.RESET}",
    "WAITING": f"{Terminal.YELLOW}[QUEUED]{Terminal.RESET}",
    "PAUSED": f"{Terminal.YELLOW}[PAUSED]{Terminal.RESET}",
    "COMPLETE": f"{Terminal.CYAN}[COMPLETE]{Terminal.RESET}",
}


def build_rpc_payload(method: str, params: list[Any] | None = None, req_id: str | None = None) -> str:
    secret = get_cfg().get("rpc_secret", "")
    if not secret:
        raise ConfigError("RPC secret is not configured. Run 'cloud-dl init' or set CLOUD_DL_RPC_SECRET.")
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": req_id or f"cloud-dl-{int(time.time() * 1000)}",
            "method": method,
            "params": [f"token:{secret}"] + (params or []),
        }
    )


def resolve_cloud(arg_cloud: str | None, interactive: bool = True, auto_login: bool = True) -> str:
    if arg_cloud:
        c = arg_cloud.lower()
        if c in ["aws", "1"]:
            return "aws"
        if c in ["oci", "2"]:
            return "oci"
        raise UsageError(f"Unknown cloud '{arg_cloud}'. Valid choices: 'aws', 'oci'.")
    if interactive:
        return select_cloud(auto_login=auto_login)
    return "aws" if get_aws_state() == "running" else "oci"


def select_cloud(arg_cloud: str | None = None, auto_login: bool = True) -> str:
    if arg_cloud:
        return resolve_cloud(arg_cloud, interactive=False)

    cfg = get_cfg()
    ensure_aws_auth(auto_login=auto_login)
    sys.stdout.write(f"\n{Terminal.BOLD}Target Cloud Provider Selection{Terminal.RESET}\n")
    sys.stdout.write("-" * 60 + "\n")

    aws_disk_gb = int(cfg["aws"].get("disk_size_gb", 80))
    oci_disk_gb = int(cfg["oci"].get("disk_size_gb", 45))

    aws_st = get_aws_state()
    aws_disk = get_disk_free(cfg["aws"]["host"]) if aws_st == "running" else None
    aws_disk_str = (
        f"Free: {aws_disk['avail']} / {aws_disk['size']}" if aws_disk else f"{aws_disk_gb} GB volume (idle/offline)"
    )

    oci_disk = get_disk_free(cfg["oci"]["host"])
    oci_disk_str = f"Free: {oci_disk['avail']} / {oci_disk['size']}" if oci_disk else f"{oci_disk_gb} GB volume"

    sys.stdout.write(f" [1] {cfg['aws']['name']} ({cfg['aws']['specs']})\n")
    sys.stdout.write(f"     Status: {aws_st} | {aws_disk_str}\n\n")

    sys.stdout.write(f" [2] {cfg['oci']['name']} ({cfg['oci']['specs']})\n")
    sys.stdout.write(f"     Status: always-on | {oci_disk_str}\n")
    sys.stdout.write("-" * 60 + "\n")

    while True:
        try:
            choice = input("Select provider [1=AWS / 2=OCI] (default: 1): ").strip().lower()
        except (KeyboardInterrupt, EOFError) as exc:
            raise CloudDlError("Operation aborted.") from exc
        if choice in ["", "1", "aws"]:
            return "aws"
        if choice in ["2", "oci"]:
            return "oci"
        sys.stdout.write("Invalid selection. Enter 1 or 2.\n")


def rpc_call(host: str, method: str, params: list[Any] | None = None) -> Any:
    cfg = get_cfg()
    json_str = build_rpc_payload(method, params)
    remote_cmd = (
        f"curl -s -m 7 http://127.0.0.1:{int(cfg['rpc_port'])}/jsonrpc -H 'Content-Type: application/json' -d @-"
    )
    code, stdout, stderr = run_ssh(host, remote_cmd, timeout=12, stdin_data=json_str)
    if code != 0 or not stdout.strip():
        raise RpcError(f"RPC call failed on {host}: {stderr.strip() or 'Daemon unreachable'}")
    try:
        resp = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise RpcError(f"Invalid JSON response from {host}: {stdout[:200]}") from exc

    if "error" in resp:
        err = resp["error"]
        raise RpcError(f"Aria2 Error ({err.get('code')}): {err.get('message')}")
    return resp.get("result")


def queue_single_target(host: str, target: str, dest_dir: str) -> str:
    options = {"dir": dest_dir}
    if target.startswith(("magnet:?", "http://", "https://", "ftp://")) or "xt=urn:btih:" in target:
        return str(rpc_call(host, "aria2.addUri", [[target], options]))
    if os.path.isfile(target):
        with open(target, "rb") as fh:
            b64_content = base64.b64encode(fh.read()).decode("utf-8")
        return str(rpc_call(host, "aria2.addTorrent", [b64_content, [], options]))
    raise ValueError(f"Unrecognized target: {target}")


def query_status_payload(
    host: str,
) -> tuple[list[Any] | None, list[Any] | None, list[Any] | None, str, str | None]:
    cfg = get_cfg()
    secret = cfg.get("rpc_secret", "")
    if not secret:
        return None, None, None, "", "RPC secret is not configured. Run 'cloud-dl init' first."
    token = f"token:{secret}"
    batch_payload = [
        {
            "jsonrpc": "2.0",
            "id": "act",
            "method": "aria2.tellActive",
            "params": [
                token,
                [
                    "gid",
                    "status",
                    "totalLength",
                    "completedLength",
                    "downloadSpeed",
                    "uploadSpeed",
                    "bittorrent",
                    "files",
                    "numSeeders",
                    "connections",
                ],
            ],
        },
        {
            "jsonrpc": "2.0",
            "id": "wai",
            "method": "aria2.tellWaiting",
            "params": [token, 0, 20, ["gid", "status", "totalLength", "completedLength", "files"]],
        },
        {
            "jsonrpc": "2.0",
            "id": "stp",
            "method": "aria2.tellStopped",
            "params": [token, 0, 10, ["gid", "status", "totalLength", "completedLength", "files", "errorMessage"]],
        },
    ]
    batch_json = json.dumps(batch_payload)
    q_dl_dir = shlex.quote(cfg["remote_dl_dir"])
    remote_script = (
        f"RES=$(curl -s -m 5 http://127.0.0.1:{int(cfg['rpc_port'])}/jsonrpc "
        "-H 'Content-Type: application/json' -d @-); "
        "echo '===JSON_START==='; "
        'echo "$RES"; '
        "echo '===DISK_START==='; "
        f"(df -B1 {q_dl_dir} 2>/dev/null || df -B1 $HOME) | tail -1"
    )
    code, stdout, stderr = run_ssh(host, remote_script, timeout=12, stdin_data=batch_json)
    if code != 0 or not stdout:
        return None, None, None, "", stderr.strip() or "Connection failed"

    active: list[Any] = []
    waiting: list[Any] = []
    stopped: list[Any] = []
    disk_line = ""

    if "===JSON_START===" in stdout and "===DISK_START===" in stdout:
        json_part = stdout.split("===JSON_START===", 1)[1].split("===DISK_START===", 1)[0].strip()
        disk_line = stdout.split("===DISK_START===", 1)[1].strip()
        try:
            parsed = json.loads(json_part)
            if isinstance(parsed, list):
                buckets = {"act": active, "wai": waiting, "stp": stopped}
                for resp in parsed:
                    target_bucket = buckets.get(resp.get("id"))
                    if target_bucket is not None and isinstance(resp.get("result"), list):
                        target_bucket.extend(resp["result"])
        except json.JSONDecodeError:
            pass

    return active, waiting, stopped, disk_line, None


def print_status_table(
    cloud: str,
    host: str,
    active: list[Any],
    waiting: list[Any],
    stopped: list[Any],
    disk_line: str,
) -> None:
    cfg = get_cfg()
    sys.stdout.write(f"\n{Terminal.BOLD}Download Status: {cfg[cloud]['name']}{Terminal.RESET}\n")
    disk_parsed = parse_df_bytes_line(disk_line) if disk_line else None
    disk_info = (
        f"Disk: {disk_parsed['avail']} free / {disk_parsed['size']} total ({disk_parsed['use_pct']} used)"
        if disk_parsed
        else ""
    )
    sys.stdout.write(f"Host: {host} | {disk_info}\n")
    sys.stdout.write("-" * 74 + "\n")

    all_jobs = active + waiting + stopped
    if not all_jobs:
        sys.stdout.write("Queue is empty. Use 'cloud-dl add <target>' to start a transfer.\n\n")
        return

    for job in all_jobs:
        gid = job.get("gid", "unknown")
        st = job.get("status", "").upper()
        total = int(job.get("totalLength", 0))
        done = int(job.get("completedLength", 0))
        pct = (done / total * 100) if total > 0 else 0.0
        dspeed = int(job.get("downloadSpeed", 0))
        uspeed = int(job.get("uploadSpeed", 0))

        name = "Unknown"
        bt = job.get("bittorrent", {})
        if bt and bt.get("info", {}).get("name"):
            name = bt["info"]["name"]
        elif job.get("files"):
            first_path = job["files"][0].get("path", "")
            if first_path:
                name = os.path.basename(first_path)

        badge = STATUS_BADGES.get(st, f"{Terminal.RED}[{st}]{Terminal.RESET}")
        eta_str = f" | ETA: {format_time((total - done) / dspeed)}" if dspeed > 0 and total > done else ""
        peers_str = (
            f" | Seeders: {job.get('numSeeders', 0)} ({job.get('connections', 0)} conn)" if "numSeeders" in job else ""
        )

        bar_len = 20
        filled = round(bar_len * (pct / 100))
        bar = "=" * filled + "-" * (bar_len - filled)

        sys.stdout.write(f"* {badge} {Terminal.BOLD}{name}{Terminal.RESET}\n")
        sys.stdout.write(f"  GID: {gid} | [{bar}] {pct:5.1f}% | {format_size(done)} / {format_size(total)}\n")
        if st == "ACTIVE":
            sys.stdout.write(f"  Speed: down {format_speed(dspeed)} | up {format_speed(uspeed)}{eta_str}{peers_str}\n")
        if job.get("errorMessage"):
            sys.stdout.write(f"  {Terminal.RED}Error: {job['errorMessage']}{Terminal.RESET}\n")
        sys.stdout.write("\n")


def _load_add_targets(args: argparse.Namespace) -> list[str]:
    if args.file:
        if not os.path.isfile(args.file):
            raise CloudDlError(f"File not found: '{args.file}'")
        with open(args.file, encoding="utf-8") as fh:
            targets = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
        log_info(f"Loaded {len(targets)} target(s) from {args.file}")
        return targets
    if args.target:
        return [args.target.strip()]
    raise UsageError("Specify a target URL/magnet or pass a file with '--file <path>'.")


def _maybe_switch_from_low_oci_disk(cloud: str, host: str, auto_login: bool) -> tuple[str, str]:
    cfg = get_cfg()
    disk = get_disk_free(host)
    if not disk:
        return cloud, host
    log_info(f"Remote available disk: {disk['avail']} (total {disk['size']})")
    avail_gb = disk["avail_bytes"] / (1024**3)
    if cloud != "oci" or avail_gb >= 35.0:
        return cloud, host

    log_warn("OCI has less than 35 GB available storage.")
    log_warn("Large packages (>35 GB) may exhaust remote storage.")
    try:
        confirm = input("Continue on OCI? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError) as exc:
        raise CloudDlError("Aborted.") from exc
    if confirm in ["y", "yes"]:
        return cloud, host

    log_info("Switching to AWS...")
    aws_host = cfg["aws"]["host"]
    if get_aws_state() != "running" and not start_aws_instance(auto_login=auto_login):
        raise CloudDlError("Failed to start AWS instance.")
    return "aws", aws_host


def cmd_add(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    auto_login = not getattr(args, "no_auto_login", False)
    cloud = resolve_cloud(args.cloud, interactive=True, auto_login=auto_login)
    host = cfg[cloud]["host"]

    if cloud == "aws":
        require_aws_auth(auto_login=auto_login)
        if get_aws_state() != "running" and not start_aws_instance(auto_login=auto_login):
            raise CloudDlError("Failed to start AWS instance.")

    dest_dir = args.dest if args.dest else cfg["remote_dl_dir"]
    targets = _load_add_targets(args)
    cloud, host = _maybe_switch_from_low_oci_disk(cloud, host, auto_login)

    log_info(f"Submitting {len(targets)} item(s) to {cfg[cloud]['name']}...")
    gids: list[str] = []
    for tgt in targets:
        try:
            gid = queue_single_target(host, tgt, dest_dir)
            gids.append(gid)
            sys.stdout.write(f"  -> Task queued (GID: {gid})\n")
        except (CloudDlError, ValueError, OSError) as exc:
            log_err(f"Failed to queue '{tgt[:60]}': {exc}")

    log_ok(f"Queued {len(gids)}/{len(targets)} task(s) successfully.")
    sys.stdout.write(f"Destination: {dest_dir}\n")
    sys.stdout.write("Use 'cloud-dl status' or 'cloud-dl top' to monitor transfer.\n")


def cmd_status(args: argparse.Namespace) -> None:
    cloud = resolve_cloud(args.cloud, interactive=False)
    host = get_cfg()[cloud]["host"]
    active, waiting, stopped, disk_line, err = query_status_payload(host)
    if err:
        log_err(f"Query failed for {cloud}: {err}")
        if cloud == "aws":
            st = get_aws_state()
            if st == "auth_expired":
                sys.stdout.write("AWS session status: AUTH EXPIRED (run 'aws login')\n")
            else:
                sys.stdout.write(f"AWS instance status: {st}\n")
                if st != "running":
                    sys.stdout.write("Start instance with: 'cloud-dl start --cloud aws'\n")
        raise RpcError(f"Unable to query status for {cloud}.")

    print_status_table(cloud, host, active or [], waiting or [], stopped or [], disk_line)


def cmd_top(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    cloud = resolve_cloud(args.cloud, interactive=False)
    host = cfg[cloud]["host"]

    sys.stdout.write(f"Starting terminal monitor on {cfg[cloud]['name']} (Press Ctrl+C to exit)...\n")
    try:
        while True:
            active, waiting, stopped, disk_line, err = query_status_payload(host)
            sys.stdout.write(Terminal.CLEAR_SCREEN)
            sys.stdout.write(f"{Terminal.BOLD}CLOUD-DL MONITOR | {time.strftime('%H:%M:%S')}{Terminal.RESET}\n")
            if err:
                log_err(f"Connection lost: {err}")
            else:
                print_status_table(cloud, host, active or [], waiting or [], stopped or [], disk_line)
            time.sleep(2)
    except KeyboardInterrupt:
        sys.stdout.write("\nMonitor closed.\n")


def cmd_limit(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    cloud = resolve_cloud(args.cloud, interactive=True)
    host = cfg[cloud]["host"]

    opts: dict[str, str] = {}
    if args.down:
        opts["max-overall-download-limit"] = args.down
    if args.up:
        opts["max-overall-upload-limit"] = args.up

    if not opts:
        cur = rpc_call(host, "aria2.getGlobalOption")
        down_lim = str(cur.get("max-overall-download-limit", "0"))
        up_lim = str(cur.get("max-overall-upload-limit", "0"))
        down_str = format_speed(int(down_lim)) if down_lim.isdigit() and down_lim != "0" else "Unlimited"
        up_str = format_speed(int(up_lim)) if up_lim.isdigit() and up_lim != "0" else "Unlimited"
        sys.stdout.write(f"Current limits on {cfg[cloud]['name']}:\n")
        sys.stdout.write(f"  Download limit: {down_str}\n")
        sys.stdout.write(f"  Upload limit:   {up_str}\n")
        return

    rpc_call(host, "aria2.changeGlobalOption", [opts])
    log_ok(f"Updated speed limits on {cfg[cloud]['name']}:")
    for k, v in opts.items():
        sys.stdout.write(f"  {k} = {v}\n")


def cmd_pause(args: argparse.Namespace) -> None:
    cloud = resolve_cloud(args.cloud, interactive=True)
    rpc_call(get_cfg()[cloud]["host"], "aria2.pause", [args.gid])
    log_ok(f"Paused task {args.gid}.")


def cmd_resume(args: argparse.Namespace) -> None:
    cloud = resolve_cloud(args.cloud, interactive=True)
    rpc_call(get_cfg()[cloud]["host"], "aria2.unpause", [args.gid])
    log_ok(f"Resumed task {args.gid}.")


def cmd_cancel(args: argparse.Namespace) -> None:
    cloud = resolve_cloud(args.cloud, interactive=True)
    rpc_call(get_cfg()[cloud]["host"], "aria2.forceRemove", [args.gid])
    log_ok(f"Canceled task {args.gid}.")
