# Changelog

All notable changes to this project will be documented in this file.

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
