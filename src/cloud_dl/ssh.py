"""SSH transport with ControlMaster multiplexing, disk parsing, and config IP updates."""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .config import format_size, get_cfg

CONTROL_DIR = Path(os.path.expanduser("~/.cache/cloud-dl/ssh"))


def is_aws_auth_error(stderr: str) -> bool:
    err_lower = stderr.lower()
    return "session has expired" in err_lower or "aws login" in err_lower or "expiredtoken" in err_lower


def get_control_path() -> str:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(CONTROL_DIR, 0o700)
    return str(CONTROL_DIR / "%r@%h:%p")


def build_ssh_base_args() -> list[str]:
    return [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "BatchMode=yes",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={get_control_path()}",
        "-o",
        "ControlPersist=300",
    ]


def close_ssh_control_socket(host: str) -> None:
    cmd = [
        "ssh",
        "-o",
        f"ControlPath={get_control_path()}",
        "-O",
        "exit",
        host,
    ]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)


def update_ssh_config_ip(host_alias: str, new_ip: str, ssh_config_path: str | None = None) -> bool:
    """Update HostName only within the matching Host block of ~/.ssh/config."""
    ssh_config = ssh_config_path or os.path.expanduser("~/.ssh/config")
    if not os.path.isfile(ssh_config):
        return False
    try:
        with open(ssh_config, encoding="utf-8") as fh:
            lines = fh.readlines()

        in_target_block = False
        updated = False
        out_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("host "):
                aliases = stripped.split()[1:]
                in_target_block = host_alias in aliases
                out_lines.append(line)
                continue

            if in_target_block and lower.startswith("hostname "):
                indent = line[: len(line) - len(line.lstrip())]
                current_ip = stripped.split(None, 1)[1].strip()
                if current_ip != new_ip:
                    out_lines.append(f"{indent}HostName {new_ip}\n")
                    updated = True
                else:
                    out_lines.append(line)
                continue

            out_lines.append(line)

        if updated:
            with open(ssh_config, "w", encoding="utf-8") as fh:
                fh.writelines(out_lines)
        return updated
    except OSError:
        return False


def parse_df_bytes_line(line: str) -> dict[str, Any] | None:
    """Parse a single `df -B1` output line into human-readable and byte fields."""
    parts = line.strip().split()
    if len(parts) < 5:
        return None
    try:
        size_bytes = int(parts[1])
        used_bytes = int(parts[2])
        avail_bytes = int(parts[3])
    except ValueError:
        return None
    return {
        "size": format_size(size_bytes),
        "used": format_size(used_bytes),
        "avail": format_size(avail_bytes),
        "size_bytes": size_bytes,
        "used_bytes": used_bytes,
        "avail_bytes": avail_bytes,
        "use_pct": parts[4],
    }


def run_ssh(
    host: str,
    command: str,
    timeout: int = 15,
    stdin_data: str | None = None,
) -> tuple[int, str, str]:
    from .aws import get_aws_public_ip, get_aws_state

    cfg = get_cfg()
    cmd = [*build_ssh_base_args(), host, command]
    try:
        res = subprocess.run(
            cmd,
            check=False,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode != 0 and host == cfg["aws"]["host"] and get_aws_state() == "running":
            new_ip = get_aws_public_ip()
            if new_ip and update_ssh_config_ip(host, new_ip):
                close_ssh_control_socket(host)
                res = subprocess.run(
                    cmd,
                    check=False,
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Connection timed out"
    except OSError as exc:
        return -1, "", str(exc)


def get_disk_free(host: str) -> dict[str, Any] | None:
    q_dir = shlex.quote(get_cfg()["remote_dl_dir"])
    code, stdout, _ = run_ssh(host, f"df -B1 {q_dir} 2>/dev/null || df -B1 $HOME | tail -1", timeout=6)
    if code == 0 and stdout.strip():
        return parse_df_bytes_line(stdout.strip().splitlines()[-1])
    return None
