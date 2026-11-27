"""Configuration management, terminal styling, formatters, and `init` wizard."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .exceptions import ConfigError


def enable_windows_ansi() -> None:
    """Enable VT100 escape processing in classic Windows conhost consoles."""
    if sys.platform != "win32":
        return
    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return
    for handle_id in (-11, -12):
        with contextlib.suppress(OSError, AttributeError):
            handle = kernel32.kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_ulong()
            if handle and kernel32.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def resolve_bin(name: str) -> str:
    """Resolve Windows .cmd/.bat shims cleanly while keeping standard command names."""
    if sys.platform == "win32":
        found = shutil.which(name)
        if found and found.lower().endswith((".cmd", ".bat")):
            return found
    return name


def get_config_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "cloud-dl"
    return Path.home() / ".config" / "cloud-dl"


def get_cache_dir() -> Path:
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "cloud-dl"
    return Path.home() / ".cache" / "cloud-dl"


def get_config_path() -> str:
    env_override = os.environ.get("CLOUD_DL_CONFIG")
    if env_override:
        return os.path.expanduser(env_override)
    return str(get_config_dir() / "config.json")


def _default_local_download_dir() -> tuple[str, str]:
    downloads = str(Path.home() / "Downloads")
    if sys.platform == "win32":
        return downloads, downloads
    preferred = "/mnt/DATA"
    default_local = preferred if os.path.ismount(preferred) else downloads
    return preferred, default_local


def build_default_config() -> dict[str, Any]:
    """Build baseline default configuration without import-time filesystem checks."""
    preferred_dir, default_local = _default_local_download_dir()
    ariang_default = str(
        (Path(os.environ.get("LOCALAPPDATA", "")) / "cloud-dl" / "index.html")
        if (sys.platform == "win32" and os.environ.get("LOCALAPPDATA"))
        else (Path.home() / ".local" / "share" / "cloud-dl" / "index.html")
    )
    return {
        "aws": {
            "name": "AWS Sydney",
            "host": "aws-vps",
            "instance_id": "",
            "region": "ap-southeast-2",
            "specs": "t4g.small (2 vCPU, 2GB RAM, up to 5 Gbps burst)",
            "disk_size_gb": 80,
            "compute_per_hr": 0.0211,
            "storage_per_gb_mo": 0.088,
            "credit_balance_usd": 180.0,
        },
        "oci": {
            "name": "OCI Hyderabad",
            "host": "oci-vps",
            "region": "ap-hyderabad-1",
            "specs": "VM.Standard.E2.1.Micro (Always Free, 10 TB egress)",
            "disk_size_gb": 45,
        },
        "rpc_secret": "",
        "rpc_port": 6800,
        "remote_dl_dir": "/home/ubuntu/downloads",
        "preferred_local_dir": preferred_dir,
        "default_local_dir": default_local,
        "watchdog_threshold_s": 1800,
        "proxy": {"socks_port": 1080, "http_port": 8888},
        "ariang_path": ariang_default,
    }


class Terminal:
    IS_TTY = sys.stdout.isatty()
    BOLD = "\033[1m" if IS_TTY else ""
    DIM = "\033[2m" if IS_TTY else ""
    UNDERLINE = "\033[4m" if IS_TTY else ""
    RED = "\033[31m" if IS_TTY else ""
    GREEN = "\033[32m" if IS_TTY else ""
    YELLOW = "\033[33m" if IS_TTY else ""
    BLUE = "\033[34m" if IS_TTY else ""
    CYAN = "\033[36m" if IS_TTY else ""
    RESET = "\033[0m" if IS_TTY else ""
    CLEAR_SCREEN = "\033[2J\033[H" if IS_TTY else ""


def log_info(msg: str) -> None:
    sys.stderr.write(f"{Terminal.CYAN}[INFO]{Terminal.RESET} {msg}\n")


def log_ok(msg: str) -> None:
    sys.stderr.write(f"{Terminal.GREEN}[OK]{Terminal.RESET} {msg}\n")


def log_warn(msg: str) -> None:
    sys.stderr.write(f"{Terminal.YELLOW}[WARN]{Terminal.RESET} {msg}\n")


def log_err(msg: str) -> None:
    sys.stderr.write(f"{Terminal.RED}[ERROR]{Terminal.RESET} {msg}\n")


def format_size(bytes_val: Any) -> str:
    try:
        val = float(bytes_val)
    except (ValueError, TypeError):
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if val < 1024.0:
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} PB"


def format_speed(bps: Any) -> str:
    return f"{format_size(bps)}/s"


def format_time(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (ValueError, TypeError):
        return "N/A"
    if s < 0 or s > 86400 * 30:
        return "N/A"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m {sec}s"
    if m > 0:
        return f"{m}m {sec}s"
    return f"{sec}s"


def send_notification(title: str, message: str) -> None:
    notify_bin = shutil.which("notify-send")
    if notify_bin:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [notify_bin, "-a", "cloud-dl", title, message],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override dictionary into a copy of base."""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | None = None) -> dict[str, Any]:
    defaults = build_default_config()
    path = config_path or get_config_path()
    merged = deep_merge(defaults, {})
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                log_warn(f"Ignoring non-object config in {path}")
            else:
                merged = deep_merge(defaults, cfg)
        except (OSError, json.JSONDecodeError) as exc:
            log_warn(f"Failed to load {path}: {exc}")

    env_secret = os.environ.get("CLOUD_DL_RPC_SECRET")
    if env_secret:
        merged["rpc_secret"] = env_secret
    return merged


CFG: dict[str, Any] = {}


def get_cfg() -> dict[str, Any]:
    """Return active configuration, loading lazily on first access."""
    if not CFG:
        CFG.update(load_config())
    return CFG


def reload_config(config_path: str | None = None) -> dict[str, Any]:
    fresh = load_config(config_path=config_path)
    CFG.clear()
    CFG.update(fresh)
    return CFG


def _prompt_with_default(label: str, current_val: Any) -> str:
    raw = input(f"{label} [{current_val}]: ").strip()
    return raw if raw else str(current_val)


def detect_default_ssh_key() -> str | None:
    ssh_dir = Path.home() / ".ssh"
    for name in ("oci_vps", "id_ed25519", "id_rsa"):
        candidate = ssh_dir / name
        if candidate.is_file():
            return str(candidate)
    return None


def _build_ssh_host_block(alias: str, host_ip: str, key_path: str | None) -> str:
    lines = [
        f"Host {alias}",
        f"  HostName {host_ip or f'<{alias.upper()}_IP>'}",
        "  User ubuntu",
    ]
    if key_path:
        lines.append(f"  IdentityFile {key_path}")
    if alias == "aws-vps":
        lines.extend(["  Ciphers aes128-gcm@openssh.com,chacha20-poly1305@openssh.com"])
    lines.extend(["  ServerAliveInterval 15", "  ServerAliveCountMax 8", "  StrictHostKeyChecking accept-new"])
    return "\n".join(lines) + "\n"


def _ssh_config_has_host(lines: list[str], alias: str) -> bool:
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("host ") and alias in stripped.split()[1:]:
            return True
    return False


def _replace_ssh_host_ip(lines: list[str], alias: str, new_ip: str) -> list[str]:
    in_block = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("host "):
            in_block = alias in stripped.split()[1:]
            out.append(line)
            continue
        if in_block and lower.startswith("hostname "):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}HostName {new_ip}\n")
            continue
        out.append(line)
    return out


def setup_ssh_config_aliases(
    *,
    oci_ip: str | None = None,
    aws_ip: str | None = None,
    key_path: str | None = None,
    ssh_config_path: Path | None = None,
) -> tuple[Path, list[str]]:
    """Create or update oci-vps/aws-vps host blocks in ~/.ssh/config without clobbering existing settings."""
    target = ssh_config_path or (Path.home() / ".ssh" / "config")
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(target.parent, 0o700)

    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    lines = existing.splitlines(keepends=True)
    detected_key = key_path or detect_default_ssh_key()
    updated_hosts: list[str] = []

    for alias, ip_val in (("oci-vps", oci_ip), ("aws-vps", aws_ip)):
        if _ssh_config_has_host(lines, alias):
            if ip_val:
                lines = _replace_ssh_host_ip(lines, alias, ip_val)
                updated_hosts.append(alias)
        elif ip_val or not existing:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            if lines:
                lines.append("\n")
            lines.append(_build_ssh_host_block(alias, ip_val or "", detected_key))
            updated_hosts.append(alias)

    target.write_text("".join(lines), encoding="utf-8")
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
    return target, updated_hosts


def _prompt_init_values(
    current: dict[str, Any], cfg_path: Path
) -> tuple[str | None, str | None, str | None, bool, bool]:
    sys.stdout.write(f"\n{Terminal.BOLD}cloud-dl Configuration Setup ({cfg_path}){Terminal.RESET}\n")
    sys.stdout.write("-" * 60 + "\n")
    current["aws"]["instance_id"] = _prompt_with_default(
        "AWS EC2 Instance ID (blank for OCI-only mode)", current["aws"]["instance_id"]
    )
    if current["aws"]["instance_id"]:
        current["aws"]["region"] = _prompt_with_default("AWS Region", current["aws"]["region"])
        current["aws"]["compute_per_hr"] = float(
            _prompt_with_default("EC2 compute USD/hour", current["aws"]["compute_per_hr"])
        )
        current["aws"]["credit_balance_usd"] = float(
            _prompt_with_default("Credit balance USD", current["aws"]["credit_balance_usd"])
        )
    current["preferred_local_dir"] = _prompt_with_default("Preferred local sync folder", current["preferred_local_dir"])
    shared_secret = input("Shared Aria2 RPC secret (leave blank to auto-generate/keep): ").strip()
    if shared_secret:
        current["rpc_secret"] = shared_secret
    oci_ip = input("OCI VPS public IP for ~/.ssh/config (leave blank to skip): ").strip() or None
    aws_ip = input("AWS VPS public IP for ~/.ssh/config (leave blank to skip): ").strip() or None
    default_key = detect_default_ssh_key() or ""
    key_input = input(f"SSH private key path [{default_key}]: ").strip()
    key_path = key_input or (default_key or None)
    write_ssh = bool(oci_ip or aws_ip) or not (Path.home() / ".ssh" / "config").is_file()
    return oci_ip, aws_ip, key_path, write_ssh, bool(shared_secret)


def _apply_init_flags(current: dict[str, Any], args: argparse.Namespace) -> bool:
    if getattr(args, "instance_id", None) is not None:
        current["aws"]["instance_id"] = args.instance_id.strip()
    if getattr(args, "region", None):
        current["aws"]["region"] = args.region.strip()
    if getattr(args, "local_dir", None):
        current["preferred_local_dir"] = args.local_dir.strip()
    explicit_secret = getattr(args, "rpc_secret", None)
    if explicit_secret:
        current["rpc_secret"] = explicit_secret.strip()
        return True
    return False


def cmd_init(args: argparse.Namespace) -> None:
    """Create or update user config.json with shared or generated RPC credentials and SSH aliases."""
    cfg_path = Path(get_config_path())
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    current = deep_merge(build_default_config(), get_cfg())
    secret_supplied = _apply_init_flags(current, args)

    oci_ip = getattr(args, "oci_ip", None)
    aws_ip = getattr(args, "aws_ip", None)
    key_path = getattr(args, "ssh_key", None)
    write_ssh = bool(getattr(args, "write_ssh_config", False) or oci_ip or aws_ip)

    if not getattr(args, "non_interactive", False):
        try:
            oci_ip, aws_ip, key_path, prompt_ssh, prompt_secret = _prompt_init_values(current, cfg_path)
            write_ssh = write_ssh or prompt_ssh
            secret_supplied = secret_supplied or prompt_secret
        except (KeyboardInterrupt, EOFError) as exc:
            raise ConfigError("Initialization canceled.") from exc
        except ValueError as exc:
            raise ConfigError(f"Invalid numeric value provided: {exc}") from exc

    rotated = False
    if not secret_supplied and (not getattr(args, "keep_secret", False) or not current.get("rpc_secret")):
        current["rpc_secret"] = secrets.token_hex(16)
        rotated = True

    cfg_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(cfg_path, 0o600)
    reload_config(str(cfg_path))
    perm_msg = "permissions 0600" if os.name != "nt" else "user profile scope"
    log_ok(f"Saved configuration to {cfg_path} ({perm_msg}).")
    if write_ssh:
        ssh_path, aliases = setup_ssh_config_aliases(oci_ip=oci_ip, aws_ip=aws_ip, key_path=key_path)
        if aliases:
            log_ok(f"Configured SSH aliases ({', '.join(aliases)}) in {ssh_path}.")
    if rotated:
        log_info("Generated a new 32-char RPC secret in config.json.")
        log_info("Sync `rpc-secret` in ~/.aria2/aria2.conf on your VPS nodes and restart aria2c.")
