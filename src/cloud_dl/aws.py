"""AWS EC2 lifecycle management and automatic browser authentication."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

from .config import Terminal, get_cfg, log_err, log_info, log_ok, log_warn, resolve_bin
from .exceptions import AuthError, CloudDlError, ConfigError
from .ssh import close_ssh_control_socket, is_aws_auth_error, run_ssh, update_ssh_config_ip


def get_aws_state() -> str:
    cfg = get_cfg()
    instance_id = cfg["aws"].get("instance_id", "").strip()
    if not instance_id:
        return "unconfigured"
    try:
        cmd = [
            resolve_bin("aws"),
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance_id,
            "--region",
            cfg["aws"]["region"],
            "--query",
            "Reservations[0].Instances[0].State.Name",
            "--output",
            "text",
        ]
        res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=8)
        if res.returncode == 0:
            return res.stdout.strip()
        if is_aws_auth_error(res.stderr):
            return "auth_expired"
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def get_aws_public_ip() -> str | None:
    cfg = get_cfg()
    instance_id = cfg["aws"].get("instance_id", "").strip()
    if not instance_id:
        return None
    try:
        cmd = [
            resolve_bin("aws"),
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance_id,
            "--region",
            cfg["aws"]["region"],
            "--query",
            "Reservations[0].Instances[0].PublicIpAddress",
            "--output",
            "text",
        ]
        res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=8)
        ip = res.stdout.strip()
        if res.returncode == 0 and ip and ip != "None":
            return ip
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def ensure_aws_auth(auto_login: bool = True) -> bool:
    """Verify AWS credentials are active; optionally launch interactive `aws login`."""
    st = get_aws_state()
    if st != "auth_expired":
        return True

    if not auto_login or not sys.stdin.isatty():
        log_err("AWS session expired. Run 'aws login' first.")
        return False

    log_info("AWS session expired. Opening browser for 'aws login'...")
    login_res = subprocess.run([resolve_bin("aws"), "login"], check=False, timeout=300)
    if login_res.returncode != 0:
        log_err("AWS login did not complete successfully.")
        return False

    verify_res = subprocess.run(
        [resolve_bin("aws"), "sts", "get-caller-identity"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if verify_res.returncode != 0:
        log_err(f"AWS authentication verification failed: {verify_res.stderr.strip()}")
        return False

    log_ok("AWS session authenticated.")
    return True


def require_aws_auth(auto_login: bool = True) -> None:
    if not ensure_aws_auth(auto_login=auto_login):
        raise AuthError("AWS authentication required.")


def stop_aws_instance(auto_login: bool = True) -> bool:
    cfg = get_cfg()
    if not ensure_aws_auth(auto_login=auto_login):
        return False
    instance_id = cfg["aws"].get("instance_id", "").strip()
    if not instance_id:
        log_err("AWS instance_id is not configured. Run 'cloud-dl init' first.")
        return False
    log_info(f"Stopping {cfg['aws']['name']} instance ({instance_id})...")
    res = subprocess.run(
        [
            resolve_bin("aws"),
            "ec2",
            "stop-instances",
            "--instance-ids",
            instance_id,
            "--region",
            cfg["aws"]["region"],
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if res.returncode != 0:
        if is_aws_auth_error(res.stderr):
            log_err("AWS session expired. Run 'aws login' first.")
        else:
            log_err(f"Stop command failed: {res.stderr.strip()}")
        return False
    close_ssh_control_socket(cfg["aws"]["host"])
    log_ok("Stop command issued. Compute billing paused.")
    return True


def _boot_ec2_and_wait(instance_id: str, region: str, name: str) -> bool:
    log_info(f"Starting {name} EC2 instance ({instance_id})...")
    cmd = [resolve_bin("aws"), "ec2", "start-instances", "--instance-ids", instance_id, "--region", region]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        msg = (
            "AWS session expired. Run 'aws login' first."
            if is_aws_auth_error(res.stderr)
            else f"Failed to start AWS instance: {res.stderr.strip() or 'unknown error'}"
        )
        log_err(msg)
        return False

    sys.stdout.write("Waiting for instance to enter 'running' state...")
    sys.stdout.flush()
    for _ in range(30):
        time.sleep(2)
        sys.stdout.write(".")
        sys.stdout.flush()
        if get_aws_state() == "running":
            sys.stdout.write(f" {Terminal.GREEN}done{Terminal.RESET}\n")
            return True
    sys.stdout.write("\n")
    log_warn("Instance did not report 'running' within 60 seconds.")
    return True


def _wait_for_ssh_ready(host: str) -> bool:
    sys.stdout.write("Waiting for SSH service to become ready...")
    sys.stdout.flush()
    last_err = "timed out"
    for _ in range(25):
        code, _, stderr = run_ssh(host, "uptime", timeout=12)
        if code == 0:
            sys.stdout.write(f" {Terminal.GREEN}ready{Terminal.RESET}\n")
            return True
        if stderr.strip():
            last_err = stderr.strip()
        time.sleep(2)
        sys.stdout.write(".")
        sys.stdout.flush()

    sys.stdout.write("\n")
    log_err(f"Instance is running, but SSH did not become ready ({last_err}).")
    return False


def start_aws_instance(auto_login: bool = True) -> bool:
    cfg = get_cfg()
    if not ensure_aws_auth(auto_login=auto_login):
        return False

    instance_id = cfg["aws"].get("instance_id", "").strip()
    if not instance_id:
        log_err("AWS instance_id is not configured. Run 'cloud-dl init' first.")
        return False

    st = get_aws_state()
    if st != "running":
        if not _boot_ec2_and_wait(instance_id, cfg["aws"]["region"], cfg["aws"]["name"]):
            return False
    else:
        log_info(f"{cfg['aws']['name']} EC2 instance is already running.")

    new_ip = get_aws_public_ip()
    if new_ip and update_ssh_config_ip(cfg["aws"]["host"], new_ip):
        close_ssh_control_socket(cfg["aws"]["host"])
        log_info(f"Updated ~/.ssh/config for {cfg['aws']['host']} with public IP: {new_ip}")

    return _wait_for_ssh_ready(cfg["aws"]["host"])


def cmd_start(args: argparse.Namespace) -> None:
    auto_login = not getattr(args, "no_auto_login", False)
    cloud = (args.cloud or "aws").lower()
    if cloud == "aws":
        if not start_aws_instance(auto_login=auto_login):
            raise CloudDlError("Failed to start AWS instance.")
    else:
        log_info("OCI instance is an Always-Free node and remains online.")


def cmd_stop(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    auto_login = not getattr(args, "no_auto_login", False)
    cloud = (args.cloud or "aws").lower()
    if cloud == "aws":
        if not stop_aws_instance(auto_login=auto_login):
            raise CloudDlError("Failed to stop AWS instance.")
    else:
        log_info("Stopping OCI VPS...")
        code, _, stderr = run_ssh(cfg["oci"]["host"], "sudo shutdown -h now")
        if code != 0 and stderr.strip():
            raise ConfigError(f"OCI shutdown failed: {stderr.strip()}")
