"""Cross-platform SSH transport with POSIX ControlMaster multiplexing."""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import format_size, get_cache_dir, get_cfg


def is_aws_auth_error(stderr: str) -> bool:
    err_lower = stderr.lower()
    return "session has expired" in err_lower or "aws login" in err_lower or "expiredtoken" in err_lower


def get_control_dir() -> Path:
    return get_cache_dir() / "ssh"


def get_control_path() -> str:
    ctrl_dir = get_control_dir()
    ctrl_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(ctrl_dir, 0o700)
    return str(ctrl_dir / "%r@%h:%p")


def build_ssh_base_args() -> list[str]:
    args = [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
    ]
    if sys.platform != "win32":
        args.extend(
            [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={get_control_path()}",
                "-o",
                "ControlPersist=300",
            ]
        )
    return args


def close_ssh_control_socket(host: str) -> None:
    if sys.platform == "win32":
        return
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
    ssh_config = Path(ssh_config_path) if ssh_config_path else (Path.home() / ".ssh" / "config")
    if not ssh_config.is_file():
        return False
    try:
        lines = ssh_config.read_text(encoding="utf-8").splitlines(keepends=True)

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
            ssh_config.write_text("".join(out_lines), encoding="utf-8")
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
