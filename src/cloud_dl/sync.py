"""Resilient rsync sync, exFAT tuning, remote cleanup, and checksum verification."""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
import time
import zlib
from pathlib import Path

from .aws import get_aws_public_ip, get_aws_state, require_aws_auth, start_aws_instance, stop_aws_instance
from .config import Terminal, get_cfg, log_err, log_info, log_ok, log_warn, send_notification
from .exceptions import CloudDlError, SyncError
from .rpc import resolve_cloud
from .ssh import close_ssh_control_socket, run_ssh, update_ssh_config_ip

TRANSIENT_RSYNC_CODES = {12, 30, 35, 255}
RSYNC_SSH_CMD = "ssh -c aes128-gcm@openssh.com -o ServerAliveInterval=15 -o ServerAliveCountMax=8 -o TCPKeepAlive=yes"


def get_filesystem_type(path: str) -> str:
    """Detect destination filesystem type cleanly via findmnt."""
    probe = path if os.path.exists(path) else os.path.dirname(path) or "/"
    res = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "-T", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return res.stdout.strip().lower() if res.returncode == 0 else ""


def ensure_local_dest_mounted(dest_override: str | None) -> str:
    """Resolve destination path, mounting the preferred external volume if needed."""
    cfg = get_cfg()
    preferred = cfg.get("preferred_local_dir", "/mnt/DATA")
    wants_preferred = dest_override in (None, preferred)

    if wants_preferred and preferred and not os.path.ismount(preferred):
        dev_by_label = "/dev/disk/by-label/DATA"
        if os.path.exists(dev_by_label):
            log_info(f"'{preferred}' is not mounted. Attempting automatic mount...")
            res = subprocess.run(
                ["udisksctl", "mount", "-b", os.path.realpath(dev_by_label), "--no-user-interaction"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode != 0 or not os.path.ismount(preferred):
                subprocess.run(["sudo", "-n", "mount", preferred], check=False, capture_output=True, timeout=15)
            if os.path.ismount(preferred):
                log_ok(f"Mounted external volume at '{preferred}'.")

        if not os.path.ismount(preferred):
            if dest_override == preferred:
                raise SyncError(f"Destination '{preferred}' is not mounted. Mount the drive first.")
            fallback = os.path.expanduser("~/Downloads")
            log_warn(f"Preferred drive '{preferred}' is not mounted; fallback is '{fallback}'.")
            if sys.stdin.isatty():
                try:
                    ans = input(f"Proceed writing to '{fallback}' instead? [y/N]: ").strip().lower()
                except (KeyboardInterrupt, EOFError) as exc:
                    raise CloudDlError("Operation aborted.") from exc
                if ans not in ["y", "yes"]:
                    raise SyncError("Sync aborted because preferred drive is not mounted.")
            return fallback

    return dest_override or preferred or os.path.expanduser("~/Downloads")


def _hash_file_md5(target: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with open(target, "rb") as fh:
        while chunk := fh.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _hash_file_crc32(target: Path) -> str:
    crc = 0
    with open(target, "rb") as fh:
        while chunk := fh.read(4 * 1024 * 1024):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08x}"


def _verify_single_md5_manifest(manifest: Path, sync_root: Path, resolved_root: Path) -> tuple[int, list[str]]:
    log_info(f"Verifying MD5 manifest: {manifest.relative_to(sync_root)}")
    checked = 0
    failures: list[str] = []
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith((";", "#")) or len(line) < 35:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 32:
            continue
        rel_str = parts[1].lstrip("*").replace("\\", "/")
        target = (manifest.parent / rel_str).resolve()
        if not target.is_relative_to(resolved_root) or not target.is_file():
            log_err(f"Missing or unsafe path in MD5 manifest: {rel_str}")
            failures.append(rel_str)
            continue
        checked += 1
        if _hash_file_md5(target) != parts[0].lower():
            rel_lbl = str(target.relative_to(sync_root))
            log_err(f"MD5 mismatch: {rel_lbl}")
            failures.append(rel_lbl)
    return checked, failures


def _verify_single_sfv_manifest(manifest: Path, sync_root: Path, resolved_root: Path) -> tuple[int, list[str]]:
    log_info(f"Verifying SFV manifest: {manifest.relative_to(sync_root)}")
    checked = 0
    failures: list[str] = []
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2 or len(parts[1]) != 8:
            continue
        rel_str = parts[0].replace("\\", "/")
        target = (manifest.parent / rel_str).resolve()
        if not target.is_relative_to(resolved_root) or not target.is_file():
            log_err(f"Missing or unsafe path in SFV manifest: {rel_str}")
            failures.append(rel_str)
            continue
        checked += 1
        if _hash_file_crc32(target) != parts[1].lower():
            rel_lbl = str(target.relative_to(sync_root))
            log_err(f"CRC32 mismatch: {rel_lbl}")
            failures.append(rel_lbl)
    return checked, failures


def verify_synced_checksums(sync_root: Path) -> bool:
    """Verify .md5 and .sfv manifests under sync_root safely inside root boundary."""
    if not sync_root.exists():
        log_err(f"Verification path does not exist: '{sync_root}'")
        return False

    resolved_root = sync_root.resolve()
    md5_files = sorted(sync_root.rglob("*.md5"))
    sfv_files = sorted(sync_root.rglob("*.sfv"))
    if not md5_files and not sfv_files:
        log_info("No .md5 or .sfv manifest files found; skipping checksum pass.")
        return True

    total_checked = 0
    failed_files: list[str] = []
    for manifest in md5_files:
        cnt, errs = _verify_single_md5_manifest(manifest, sync_root, resolved_root)
        total_checked += cnt
        failed_files.extend(errs)
    for manifest in sfv_files:
        cnt, errs = _verify_single_sfv_manifest(manifest, sync_root, resolved_root)
        total_checked += cnt
        failed_files.extend(errs)

    if failed_files:
        log_err(f"Integrity verification failed ({len(failed_files)}/{total_checked} failed).")
        return False
    log_ok(f"Integrity verification passed ({total_checked} file(s) verified).")
    return True


def _select_remote_sync_item(host: str, remote_dl_dir: str) -> str | None:
    _code, stdout, _ = run_ssh(host, f"ls -1 {shlex.quote(remote_dl_dir)}")
    items = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not items:
        log_warn(f"No completed items found in {remote_dl_dir}.")
        return None
    sys.stdout.write("\nAvailable items to synchronize:\n")
    for idx, it in enumerate(items, 1):
        sys.stdout.write(f"  [{idx}] {it}\n")
    sys.stdout.write("  [A] All items\n")

    try:
        sel = input("Select item [1..N or A] (default: 1): ").strip()
    except (KeyboardInterrupt, EOFError) as exc:
        raise CloudDlError("Operation aborted.") from exc
    if sel.upper() == "A":
        return ""
    try:
        choice_idx = int(sel) - 1 if sel else 0
        return items[choice_idx]
    except (ValueError, IndexError) as exc:
        raise SyncError("Invalid selection index.") from exc


def build_rsync_command(
    src: str,
    dest_dir: str,
    *,
    is_fat: bool = False,
    append: bool = False,
) -> list[str]:
    """Build rsync flags tuned for POSIX or exFAT/FAT32 filesystems."""
    base_flags = ["-rltvh", "--modify-window=1"] if is_fat else ["-avh"]
    resume_flags = ["--partial", "--append", "--info=progress2"] if append else ["--partial", "--info=progress2"]
    return [
        "rsync",
        *base_flags,
        *resume_flags,
        "-s",
        "-e",
        RSYNC_SSH_CMD,
        "--timeout=60",
        src,
        dest_dir.rstrip("/") + "/",
    ]


def _run_rsync_with_retries(rsync_cmd: list[str], cloud: str, host: str, max_attempts: int) -> int:
    ret_code = 1
    for attempt in range(1, max_attempts + 1):
        res = subprocess.run(rsync_cmd, check=False)
        ret_code = res.returncode
        if ret_code == 0:
            return 0
        if ret_code not in TRANSIENT_RSYNC_CODES or attempt >= max_attempts:
            break
        wait_s = min(60, 5 * attempt)
        log_warn(
            f"rsync interrupted (code {ret_code}, attempt {attempt}/{max_attempts}). "
            f"Refreshing connection and retrying in {wait_s}s..."
        )
        time.sleep(wait_s)
        if cloud == "aws" and get_aws_state() == "running":
            new_ip = get_aws_public_ip()
            if new_ip and update_ssh_config_ip(host, new_ip):
                close_ssh_control_socket(host)
    return ret_code


def _maybe_clean_remote(host: str, cloud_name: str, remote_dl_dir: str, item: str, args: argparse.Namespace) -> None:
    if args.clean:
        do_clean = True
    elif args.no_clean:
        do_clean = False
    else:
        try:
            ans = input(f"Delete '{item or 'all'}' from {cloud_name} to reclaim storage? [y/N]: ").strip().lower()
            do_clean = ans in ["y", "yes"]
        except (KeyboardInterrupt, EOFError):
            do_clean = False

    if not do_clean:
        log_info("Source files preserved on remote VPS.")
        return

    rm_cmd = (
        f"rm -rf {shlex.quote(f'{remote_dl_dir.rstrip("/")}/{item}')}"
        if item
        else f"rm -rf {shlex.quote(remote_dl_dir.rstrip('/'))}/*"
    )
    log_info(f"Removing remote files from {cloud_name}...")
    run_ssh(host, rm_cmd)
    log_ok("Remote files removed. Storage reclaimed.")


def cmd_list(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    cloud = resolve_cloud(args.cloud, interactive=True)
    host = cfg[cloud]["host"]

    sys.stdout.write(
        f"\n{Terminal.BOLD}Directory Listing: {cfg['remote_dl_dir']} ({cfg[cloud]['name']}){Terminal.RESET}\n"
    )
    remote_cmd = f"ls -lh {shlex.quote(cfg['remote_dl_dir'])} 2>/dev/null"
    _code, stdout, _ = run_ssh(host, remote_cmd)
    sys.stdout.write((stdout + "\n") if stdout.strip() else "Directory is empty.\n")


def cmd_sync(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    auto_login = not getattr(args, "no_auto_login", False)
    cloud = resolve_cloud(args.cloud, interactive=True, auto_login=auto_login)
    host = cfg[cloud]["host"]

    if cloud == "aws":
        require_aws_auth(auto_login=auto_login)
        if get_aws_state() != "running" and not start_aws_instance(auto_login=auto_login):
            raise CloudDlError("Failed to start AWS instance.")

    item = args.item
    if not item:
        item = _select_remote_sync_item(host, cfg["remote_dl_dir"])
        if item is None:
            return

    dest_dir = ensure_local_dest_mounted(args.dest)
    if not os.path.exists(dest_dir):
        log_info(f"Creating local directory '{dest_dir}'...")
        os.makedirs(dest_dir, exist_ok=True)

    fstype = get_filesystem_type(dest_dir)
    is_fat = fstype in {"exfat", "vfat", "msdos"}
    use_append = bool(getattr(args, "append", False))

    remote_path = f"{cfg['remote_dl_dir'].rstrip('/')}/{item}" if item else f"{cfg['remote_dl_dir'].rstrip('/')}/"
    src = f"{host}:{remote_path}"
    log_info(f"Syncing from {cfg[cloud]['name']} to {dest_dir} (fs: {fstype or 'unknown'})...")
    sys.stdout.write(f"Source: {src}\nTarget: {dest_dir}/\n")

    rsync_cmd = build_rsync_command(src, dest_dir, is_fat=is_fat, append=use_append)
    ret_code = _run_rsync_with_retries(rsync_cmd, cloud, host, max(1, int(args.retries) + 1))
    if ret_code != 0:
        raise SyncError(f"rsync failed with exit code {ret_code}. Remote files were preserved.")

    log_ok("Synchronization completed successfully.")
    if getattr(args, "verify", False):
        verify_root = Path(dest_dir) / item if item else Path(dest_dir)
        if not verify_synced_checksums(verify_root):
            raise SyncError("Checksum verification failed. Remote files preserved and auto-stop skipped.")

    send_notification("cloud-dl", f"Transfer finished: {item or 'downloads'} -> {dest_dir}")
    _maybe_clean_remote(host, cfg[cloud]["name"], cfg["remote_dl_dir"], item, args)
    if getattr(args, "stop_after", False) and cloud == "aws":
        stop_aws_instance(auto_login=auto_login)


def cmd_clean(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    cloud = resolve_cloud(args.cloud, interactive=True)
    host = cfg[cloud]["host"]

    item = args.item
    if not item:
        _code, stdout, _ = run_ssh(host, f"ls -1 {shlex.quote(cfg['remote_dl_dir'])}")
        items = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not items:
            log_warn("No items found to remove.")
            return
        sys.stdout.write(f"\nItems stored on {cfg[cloud]['name']}:\n")
        for idx, it in enumerate(items, 1):
            sys.stdout.write(f"  [{idx}] {it}\n")
        try:
            sel = input("Enter index to delete: ").strip()
            item = items[int(sel) - 1]
        except (ValueError, IndexError, KeyboardInterrupt, EOFError):
            log_info("Aborted.")
            return

    if args.yes:
        confirm = "y"
    else:
        try:
            confirm = input(f"Permanently delete '{item}' from {cfg[cloud]['name']}? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            confirm = "n"

    if confirm in ["y", "yes"]:
        target_path = f"{cfg['remote_dl_dir'].rstrip('/')}/{item}"
        run_ssh(host, f"rm -rf {shlex.quote(target_path)}")
        log_ok(f"Deleted '{item}' from {cfg[cloud]['name']}.")
    else:
        log_info("Canceled.")


def cmd_extract(args: argparse.Namespace) -> None:
    cfg = get_cfg()
    cloud = resolve_cloud(args.cloud, interactive=True)
    host = cfg[cloud]["host"]

    item = args.item
    q_dir = shlex.quote(cfg["remote_dl_dir"])
    q_item = shlex.quote(item)
    log_info(f"Extracting '{item}' on {cfg[cloud]['name']}...")
    remote_cmd = (
        f"cd {q_dir} && "
        f"case {q_item} in "
        f"*.tar.gz|*.tgz|*.tar.xz|*.tar.bz2|*.tar) tar -xvf {q_item} ;; "
        f"*.zip) unzip -o {q_item} ;; "
        f"*.7z|*.rar) 7z x -y {q_item} ;; "
        f"*) 7z x -y {q_item} || tar -xvf {q_item} ;; "
        f"esac"
    )
    code, _, stderr = run_ssh(host, remote_cmd, timeout=600)
    if code != 0:
        raise SyncError(f"Extraction failed: {stderr.strip()}")
    log_ok("Extraction completed successfully.")
