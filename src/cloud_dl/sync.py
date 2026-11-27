"""Resilient rsync and SSH byte-stream sync, filesystem tuning, cleanup, and checksum verification."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zlib
from pathlib import Path
from typing import IO

from .aws import get_aws_public_ip, get_aws_state, require_aws_auth, start_aws_instance, stop_aws_instance
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
    send_notification,
)
from .exceptions import CloudDlError, SyncError
from .rpc import resolve_cloud
from .ssh import build_ssh_base_args, close_ssh_control_socket, run_ssh, update_ssh_config_ip

TRANSIENT_RSYNC_CODES = {12, 30, 35, 255}
RSYNC_SSH_CMD = "ssh -c aes128-gcm@openssh.com -o ServerAliveInterval=15 -o ServerAliveCountMax=8 -o TCPKeepAlive=yes"
SSH_STREAM_CHUNK_BYTES = 262_144
SMALL_FILE_THRESHOLD_BYTES = 5 * 1024 * 1024
TAR_BATCH_MAX_FILES = 200
TAR_BATCH_MAX_BYTES = 64 * 1024 * 1024
_WIN_ILLEGAL_CHARS = re.compile(r'[:*?"<>|]')

_REMOTE_LIST_SCRIPT = (
    "import json,os,sys\n"
    "root,item=sys.argv[1],sys.argv[2]\n"
    "t=os.path.join(root,item) if item else root\n"
    "out=[]\n"
    "if os.path.isfile(t):\n"
    "    if not t.endswith('.aria2'):\n"
    "        out=[{'rel':os.path.basename(t),'size':os.path.getsize(t),'remote':t}]\n"
    "elif os.path.isdir(t):\n"
    "    base=os.path.dirname(t.rstrip('/')) if item else t.rstrip('/')\n"
    "    for dp,_,fns in os.walk(t,followlinks=True):\n"
    "        for fn in sorted(fns):\n"
    "            if fn.endswith('.aria2'):\n"
    "                continue\n"
    "            fp=os.path.join(dp,fn)\n"
    "            if os.path.isfile(fp):\n"
    "                out.append({'rel':os.path.relpath(fp,base).replace(os.sep,'/'),'size':os.path.getsize(fp),'remote':fp})\n"
    "print(json.dumps(out))\n"
)

_REMOTE_READ_SCRIPT = (
    "import shutil,sys\n"
    "p,off=sys.argv[1],int(sys.argv[2])\n"
    "with open(p,'rb') as f:\n"
    "    f.seek(off)\n"
    "    shutil.copyfileobj(f,sys.stdout.buffer,1048576)\n"
)

_REMOTE_TAR_SCRIPT = (
    "import json,sys,tarfile\n"
    "items=json.load(sys.stdin)\n"
    "with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as tf:\n"
    "    for entry in items:\n"
    "        tf.add(entry['remote'],arcname=entry['rel'],recursive=False)\n"
)


class _TransientStreamError(SyncError):
    """Raised when an SSH byte-stream transfer drops cleanly mid-file and can be retried."""


def _get_windows_filesystem_type(path: str) -> str:
    anchor = Path(path).resolve().anchor
    if not anchor:
        return ""
    fs_buf = ctypes.create_unicode_buffer(64)
    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return ""
    ok = kernel32.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(anchor),
        None,
        0,
        None,
        None,
        None,
        fs_buf,
        len(fs_buf),
    )
    return fs_buf.value.strip().lower() if ok else ""


def get_filesystem_type(path: str) -> str:
    """Detect destination filesystem type cleanly across Windows and POSIX."""
    if sys.platform == "win32":
        return _get_windows_filesystem_type(path)
    probe = path if os.path.exists(path) else os.path.dirname(path) or "/"
    res = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "-T", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return res.stdout.strip().lower() if res.returncode == 0 else ""


def _resolve_windows_dest(dest_override: str | None, preferred: str) -> str:
    downloads = str(Path.home() / "Downloads")
    target = dest_override or preferred or downloads
    drive = ntpath.splitdrive(target)[0]
    if drive and not os.path.exists(f"{drive}\\"):
        if dest_override:
            raise SyncError(f"Destination drive '{drive}' is not connected.")
        log_warn(f"Preferred drive '{drive}' is not available; falling back to '{downloads}'.")
        return downloads
    return target


def _try_posix_mount(preferred: str) -> None:
    dev_by_label = "/dev/disk/by-label/DATA"
    if not os.path.exists(dev_by_label):
        return
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


def _prompt_posix_fallback(preferred: str, dest_override: str | None) -> str:
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


def ensure_local_dest_mounted(dest_override: str | None) -> str:
    """Resolve destination path, mounting the preferred external volume on POSIX if needed."""
    cfg = get_cfg()
    preferred = cfg.get("preferred_local_dir", str(Path.home() / "Downloads"))
    if sys.platform == "win32":
        return _resolve_windows_dest(dest_override, preferred)

    wants_preferred = dest_override in (None, preferred)
    if wants_preferred and preferred and not os.path.ismount(preferred):
        _try_posix_mount(preferred)
        if not os.path.ismount(preferred):
            return _prompt_posix_fallback(preferred, dest_override)

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


def resolve_sync_backend(backend_arg: str) -> str:
    """Choose between rsync and pure-Python SSH stream backend cleanly."""
    if backend_arg == "ssh":
        return "ssh"
    has_rsync = shutil.which("rsync") is not None
    if backend_arg == "rsync":
        if not has_rsync:
            raise SyncError("Requested '--backend rsync', but 'rsync' is not installed on PATH. Use '--backend ssh'.")
        return "rsync"
    if sys.platform == "win32" or not has_rsync:
        return "ssh"
    return "rsync"


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


def _sanitize_rel_path(rel: str) -> str:
    clean = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in clean.split("/") if p and p != "."]
    if not parts or ".." in parts:
        raise SyncError(f"Unsafe relative path returned by remote host: '{rel}'")
    if sys.platform != "win32":
        return "/".join(parts)
    safe_parts = [(_WIN_ILLEGAL_CHARS.sub("_", p).rstrip(" .") or "_") for p in parts]
    return "/".join(safe_parts)


def _parse_remote_manifest(raw_json: str, dest_root: Path) -> list[tuple[Path, int, str, str]]:
    entries = json.loads(raw_json or "[]")
    if not isinstance(entries, list):
        raise SyncError("Remote file listing returned unexpected format.")
    resolved_root = dest_root.resolve()
    result: list[tuple[Path, int, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_rel = str(entry.get("rel", ""))
        remote_fp = str(entry.get("remote", ""))
        if raw_rel.endswith(".aria2") or not remote_fp:
            continue
        safe_rel = _sanitize_rel_path(raw_rel)
        local_target = (resolved_root / safe_rel).resolve()
        if not local_target.is_relative_to(resolved_root):
            raise SyncError(f"Path traversal rejected in remote manifest: '{raw_rel}'")
        result.append((local_target, int(entry.get("size", 0)), remote_fp, safe_rel))
    return result


def _list_remote_files_ssh(
    host: str, remote_dl_dir: str, item: str, dest_root: Path
) -> list[tuple[Path, int, str, str]]:
    cmd = f"python3 -c {shlex.quote(_REMOTE_LIST_SCRIPT)} {shlex.quote(remote_dl_dir)} {shlex.quote(item)}"
    code, stdout, stderr = run_ssh(host, cmd, timeout=30)
    if code != 0:
        raise _TransientStreamError(f"Failed to list remote files (exit {code}): {stderr.strip()}")
    return _parse_remote_manifest(stdout, dest_root)


def _build_ssh_stream_cmd(host: str, remote_cmd: str) -> list[str]:
    return [
        *build_ssh_base_args(),
        "-T",
        "-c",
        "aes128-gcm@openssh.com",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=6",
        host,
        remote_cmd,
    ]


def _render_stream_progress(label: str, done: int, total: int, elapsed: float) -> None:
    if not Terminal.IS_TTY:
        return
    pct = min(100, int((done / total) * 100)) if total > 0 else 100
    rate = done / elapsed if elapsed > 0 else 0
    eta = format_time(int((total - done) / rate)) if rate > 0 and done < total else "0s"
    line = (
        f"\r[SYNC] {label}: {format_size(done)} / {format_size(total)} ({pct}%) | {format_speed(rate)} | ETA {eta}   "
    )
    sys.stdout.write(line)
    sys.stdout.flush()


def _copy_stream_to_file(
    stdout_pipe: IO[bytes],
    dest_fh: IO[bytes],
    label: str,
    offset: int,
    remote_size: int,
) -> int:
    written = 0
    t_start = time.monotonic()
    last_print = 0.0
    while chunk := stdout_pipe.read(SSH_STREAM_CHUNK_BYTES):
        dest_fh.write(chunk)
        written += len(chunk)
        now = time.monotonic()
        if now - last_print >= 0.4:
            _render_stream_progress(label, offset + written, remote_size, now - t_start)
            last_print = now
    dest_fh.flush()
    if Terminal.IS_TTY:
        _render_stream_progress(label, offset + written, remote_size, max(0.01, time.monotonic() - t_start))
        sys.stdout.write("\n")
        sys.stdout.flush()
    return written


def _stream_single_file_ssh(host: str, remote_fp: str, local_path: Path, remote_size: int, rel_label: str) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_size = local_path.stat().st_size if local_path.is_file() else 0
    if local_size == remote_size:
        log_ok(f"Already synced: {rel_label} ({format_size(remote_size)})")
        return

    offset = local_size if 0 < local_size < remote_size else 0
    mode = "ab" if offset > 0 else "wb"
    if remote_size == 0:
        local_path.touch()
        log_ok(f"Created empty file: {rel_label}")
        return

    action = "Resuming" if offset > 0 else "Downloading"
    log_info(f"{action} {rel_label} from byte {format_size(offset)} / {format_size(remote_size)}...")
    remote_cmd = f"python3 -c {shlex.quote(_REMOTE_READ_SCRIPT)} {shlex.quote(remote_fp)} {offset}"
    ssh_cmd = _build_ssh_stream_cmd(host, remote_cmd)
    proc = subprocess.Popen(ssh_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.stdout is None or proc.stderr is None:
        raise SyncError("Failed to attach stdout/stderr pipes to SSH stream process.")
    with open(local_path, mode) as fh:
        _copy_stream_to_file(proc.stdout, fh, rel_label, offset, remote_size)
    err_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
    rc = proc.wait()
    final_size = local_path.stat().st_size if local_path.is_file() else 0
    if rc != 0 or final_size != remote_size:
        raise _TransientStreamError(
            f"Stream interrupted for '{rel_label}' ({final_size}/{remote_size} bytes, exit {rc}: {err_out})"
        )


def _feed_stdin_json(stdin_pipe: IO[bytes], payload: bytes) -> None:
    try:
        stdin_pipe.write(payload)
    except BrokenPipeError:
        pass
    finally:
        stdin_pipe.close()


def _extract_tar_member(tf: tarfile.TarFile, member: tarfile.TarInfo, resolved_root: Path) -> None:
    if not member.isfile():
        return
    safe_rel = _sanitize_rel_path(member.name)
    local_target = (resolved_root / safe_rel).resolve()
    if not local_target.is_relative_to(resolved_root):
        raise SyncError(f"Path traversal rejected in tar stream: '{member.name}'")
    local_target.parent.mkdir(parents=True, exist_ok=True)
    src_fh = tf.extractfile(member)
    if src_fh is None:
        return
    with src_fh, open(local_target, "wb") as out_fh:
        shutil.copyfileobj(src_fh, out_fh, SSH_STREAM_CHUNK_BYTES)


def _stream_tar_batch_ssh(host: str, batch: list[tuple[Path, int, str, str]], dest_root: Path) -> None:
    total_bytes = sum(sz for _p, sz, _rfp, _rel in batch)
    log_info(f"Batch-streaming {len(batch)} small file(s) ({format_size(total_bytes)}) over single SSH stream...")
    remote_cmd = f"python3 -c {shlex.quote(_REMOTE_TAR_SCRIPT)}"
    proc = subprocess.Popen(
        _build_ssh_stream_cmd(host, remote_cmd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise SyncError("Failed to attach pipes to SSH batch stream process.")

    payload = json.dumps([{"remote": rfp, "rel": rel} for _lp, _sz, rfp, rel in batch]).encode("utf-8")
    writer = threading.Thread(target=_feed_stdin_json, args=(proc.stdin, payload), daemon=True)
    writer.start()
    resolved_root = dest_root.resolve()
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for member in tf:
                _extract_tar_member(tf, member, resolved_root)
    except tarfile.TarError as exc:
        proc.wait()
        raise _TransientStreamError(f"Batch stream interrupted: {exc}") from exc
    writer.join(timeout=2)
    err_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
    rc = proc.wait()
    if rc != 0:
        raise _TransientStreamError(f"Batch stream failed (exit {rc}: {err_out})")
    for local_path, size, _rfp, rel in batch:
        if not local_path.is_file() or local_path.stat().st_size != size:
            raise _TransientStreamError(f"Incomplete batched file '{rel}' after tar stream.")


def _iter_tar_batches(items: list[tuple[Path, int, str, str]]) -> list[list[tuple[Path, int, str, str]]]:
    batches: list[list[tuple[Path, int, str, str]]] = []
    cur: list[tuple[Path, int, str, str]] = []
    cur_bytes = 0
    for item in items:
        if cur and (len(cur) >= TAR_BATCH_MAX_FILES or cur_bytes + item[1] > TAR_BATCH_MAX_BYTES):
            batches.append(cur)
            cur = []
            cur_bytes = 0
        cur.append(item)
        cur_bytes += item[1]
    if cur:
        batches.append(cur)
    return batches


def _partition_ssh_files(
    files: list[tuple[Path, int, str, str]],
) -> tuple[list[tuple[Path, int, str, str]], list[tuple[Path, int, str, str]]]:
    small_missing: list[tuple[Path, int, str, str]] = []
    per_file: list[tuple[Path, int, str, str]] = []
    for entry in files:
        local_path, size, _rfp, _rel = entry
        local_size = local_path.stat().st_size if local_path.is_file() else 0
        if local_size == size:
            per_file.append(entry)
        elif local_size == 0 and 0 < size < SMALL_FILE_THRESHOLD_BYTES:
            small_missing.append(entry)
        else:
            per_file.append(entry)
    if len(small_missing) < 2:
        per_file = small_missing + per_file
        small_missing = []
    return small_missing, per_file


def _refresh_aws_connection(cloud: str, host: str) -> None:
    if cloud == "aws" and get_aws_state() == "running":
        new_ip = get_aws_public_ip()
        if new_ip and update_ssh_config_ip(host, new_ip):
            close_ssh_control_socket(host)


def _run_ssh_stream_with_retries(
    host: str,
    cloud: str,
    remote_dl_dir: str,
    item: str,
    dest_dir: str,
    max_attempts: int,
) -> None:
    dest_root = Path(dest_dir)
    for attempt in range(1, max_attempts + 1):
        try:
            files = _list_remote_files_ssh(host, remote_dl_dir, item, dest_root)
            if not files:
                log_warn("No remote files matched target.")
                return
            small_missing, per_file = _partition_ssh_files(files)
            for batch in _iter_tar_batches(small_missing):
                _stream_tar_batch_ssh(host, batch, dest_root)
            for local_path, remote_size, remote_fp, safe_rel in per_file:
                _stream_single_file_ssh(host, remote_fp, local_path, remote_size, safe_rel)
            return
        except _TransientStreamError as exc:
            if attempt >= max_attempts:
                raise SyncError(str(exc)) from exc
            wait_s = min(60, 5 * attempt)
            log_warn(f"{exc} Retrying in {wait_s}s ({attempt}/{max_attempts})...")
            time.sleep(wait_s)
            _refresh_aws_connection(cloud, host)


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
        _refresh_aws_connection(cloud, host)
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
    os.makedirs(dest_dir, exist_ok=True)
    fstype = get_filesystem_type(dest_dir)
    backend = resolve_sync_backend(getattr(args, "backend", "auto"))
    remote_path = f"{cfg['remote_dl_dir'].rstrip('/')}/{item}" if item else f"{cfg['remote_dl_dir'].rstrip('/')}/"
    max_attempts = max(1, int(args.retries) + 1)

    log_info(f"Syncing from {cfg[cloud]['name']} to {dest_dir} (backend: {backend}, fs: {fstype or 'unknown'})...")
    sys.stdout.write(f"Source: {host}:{remote_path}\nTarget: {dest_dir}{os.sep}\n")

    if backend == "ssh":
        _run_ssh_stream_with_retries(host, cloud, cfg["remote_dl_dir"], item, dest_dir, max_attempts)
    else:
        is_fat = fstype in {"exfat", "vfat", "msdos", "fat32"}
        use_append = bool(getattr(args, "append", False))
        rsync_cmd = build_rsync_command(f"{host}:{remote_path}", dest_dir, is_fat=is_fat, append=use_append)
        ret_code = _run_rsync_with_retries(rsync_cmd, cloud, host, max_attempts)
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
