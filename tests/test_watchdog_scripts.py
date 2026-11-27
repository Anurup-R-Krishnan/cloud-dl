"""Offline tests for Watchdog v2 shell script using mocked PATH stubs."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_SCRIPT = REPO_ROOT / "scripts" / "auto-shutdown.sh"


def _write_stub(bin_dir: Path, name: str, body: str) -> Path:
    target = bin_dir / name
    target.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return target


def test_watchdog_resets_idle_when_rpc_job_active(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    idle_file = tmp_path / "idle_since"
    idle_file.write_text("1000\n", encoding="utf-8")
    shutdown_marker = tmp_path / "shutdown_called"

    _write_stub(bin_dir, "curl", 'echo \'{"result":[{"gid":"1"}]}\'')
    _write_stub(bin_dir, "ss", "exit 0")
    _write_stub(bin_dir, "pgrep", "exit 1")
    _write_stub(bin_dir, "logger", "exit 0")
    shutdown_stub = _write_stub(bin_dir, "shutdown-stub", f'touch "{shutdown_marker}"')

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "WATCHDOG_ENV_FILE": str(tmp_path / "missing.env"),
            "IDLE_FILE": str(idle_file),
            "SHUTDOWN_CMD": str(shutdown_stub),
        }
    )
    res = subprocess.run(["bash", str(WATCHDOG_SCRIPT)], check=False, env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert not idle_file.exists()
    assert not shutdown_marker.exists()


def test_watchdog_arms_and_triggers_shutdown_after_threshold(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    idle_file = tmp_path / "idle_since"
    shutdown_marker = tmp_path / "shutdown_called"

    _write_stub(bin_dir, "curl", "echo '{\"result\":[]}'")
    _write_stub(bin_dir, "ss", "exit 0")
    _write_stub(bin_dir, "pgrep", "exit 1")
    _write_stub(bin_dir, "logger", "exit 0")
    shutdown_stub = _write_stub(bin_dir, "shutdown-stub", f'touch "{shutdown_marker}"')

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "WATCHDOG_ENV_FILE": str(tmp_path / "missing.env"),
            "IDLE_FILE": str(idle_file),
            "THRESHOLD_SECONDS": "60",
            "SHUTDOWN_CMD": str(shutdown_stub),
        }
    )

    # First idle pass: creates idle timestamp file without shutting down
    res1 = subprocess.run(["bash", str(WATCHDOG_SCRIPT)], check=False, env=env, capture_output=True, text=True)
    assert res1.returncode == 0
    assert idle_file.exists()
    assert not shutdown_marker.exists()

    # Fast-forward idle timestamp past threshold
    idle_file.write_text(f"{int(time.time()) - 120}\n", encoding="utf-8")
    res2 = subprocess.run(["bash", str(WATCHDOG_SCRIPT)], check=False, env=env, capture_output=True, text=True)
    assert res2.returncode == 0
    assert shutdown_marker.exists()
