# Changelog

All notable changes to this project will be documented in this file.

## [2.6.0] - 2026-11-27

### Added
- Trailing 1 MiB SHA-256 tail integrity verification before byte-offset SSH stream resume (`--backend ssh`), automatically restarting cleanly from offset `0` if tail corruption is detected.
- Advisory `sync.lock` concurrency guard (`fcntl.flock` / `msvcrt.locking`) preventing overlapping `cloud-dl sync` runs on the same machine.
- Extended-length Windows path (`\\?\`) support in `cloud-dl sync` (`_as_os_path`) for deeply nested archives exceeding `240` characters.
- External egress IP display in `cloud-dl browse status` and strict `mypy --strict` enforcement in CI alongside Node 24–native GitHub Actions (`@v7`).

### Changed
- Converted `scripts/oci-browse` into a thin shim forwarding to `cloud-dl browse` cleanly.
- Updated `regression_tests/regression_test_clouddl.py` to import `is_aws_auth_error` from `cloud_dl.ssh` and resolve `ariang_path` dynamically from user config.

## [2.5.1] - 2026-11-27

### Added
- Friend onboarding in `cloud-dl init`: `--rpc-secret` (paste a shared token instead of regenerating), `--oci-ip`, `--aws-ip`, `--ssh-key`, and `--write-ssh-config` to scaffold `~/.ssh/config` aliases automatically, plus OCI-only mode when AWS EC2 is left blank.
- Small-file tar batching over a single SSH stream (`--backend ssh`) for files under 5 MiB, avoiding per-file SSH handshake overhead across hundreds of small files.
- Automatic filtering of `.aria2` sidecar files in SSH stream sync manifests and Windows-illegal character sanitization (`: * ? " < > |`) on `win32`.
- Classic Windows console VT100 escape processing (`enable_windows_ansi()`) and `.cmd`/`.bat` AWS CLI shim fallback (`resolve_bin()`).

### Changed
- `build_ssh_base_args()` now enables `StrictHostKeyChecking=accept-new` so dynamic EC2 IP rotations and first-time friend setups succeed seamlessly on Windows OpenSSH.
- `cloud-dl doctor` supports OCI-only setups cleanly and treats idle on-demand SOCKS5 ports as `[INFO]`.

## [2.5.0] - 2026-11-27

### Added
- Native Windows 10/11 support without WSL across all `cloud-dl` commands.
- Pure-Python resumable SSH byte-stream sync backend (`--backend auto|rsync|ssh`) with exact byte-offset resume, ETA progress bar, and AWS IP refresh on retries (`rsync` on Linux, native SSH stream on Windows or when `rsync` is absent).
- Cross-platform `cloud-dl browse` SOCKS5 tunnel launcher supporting Chrome, Microsoft Edge, Zen Browser, and Firefox with isolated profiles and PowerShell `$env:` output (`cloud-dl browse env` / `cloud-dl browse stop`).
- Native Windows `ctypes` volume filesystem detection (`GetVolumeInformationW`), Windows process tracking (`taskkill` / `OpenProcess`), stdlib `zipfile` AriaNg extraction, `webbrowser.open` UI launcher, and `%APPDATA%` / `%LOCALAPPDATA%` path resolution.
- Windows GitHub Actions CI matrix coverage (`windows-latest` + `ubuntu-latest` across Python 3.12, 3.13, and 3.14).

## [2.4.0] - 2026-11-27

### Added
- Modular `src/cloud_dl/` Python package with `pyproject.toml`, `cloud-dl` console script entrypoint, and `python -m cloud_dl` support (`__main__.py`).
- Automatic interactive `aws login` re-authentication and command resume when AWS CLI credentials expire (`--no-auto-login` escape hatch).
- `cloud-dl init` configuration wizard generating `0600` `~/.config/cloud-dl/config.json` with a randomized 32-character RPC token.
- `cloud-dl doctor` system health check validating config permissions, AWS session status, external volume mounts, proxy systemd status, and Aria2 JSON-RPC connectivity.
- `cloud-dl sync` QoL flags: `--append` (sequential fast resume), `--verify` (path-traversal-guarded `.md5` and `.sfv` verification), `--stop-after` (stop EC2 immediately on completion), and automatic exFAT/FAT flag tuning (`-rltvh --modify-window=1 --info=progress2`).
- SSH `ControlMaster` connection multiplexing (`~/.cache/cloud-dl/ssh`) and PID-tracked `ui` tunnel management (`ui-tunnel.json`).
- Offline `pytest` test suites (`tests/`), `ruff` strict linting configuration, pre-commit hooks, and GitHub Actions CI/Release workflows.

### Changed
- Watchdog v2 (`scripts/auto-shutdown.sh` and `scripts/user-data.sh`) queries Aria2 JSON-RPC (`aria2.tellActive` / `aria2.tellWaiting`) and active `rsync`/`scp` processes before starting idle countdown.
- Replaced hardcoded legacy shell wrappers (`aws-vps-*`, `aws-torrent-*`) with thin shims delegating to `cloud-dl`.
- `start_aws_instance()` SSH readiness probe timeout increased from `4s` to `12s` to eliminate false timeout stalls against `ConnectTimeout=5`.
