"""Offline unit and integration tests for modular cloud_dl package."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from cloud_dl import aws, cli, config, cost, doctor, exceptions, ssh, sync


def reset_test_cfg(config_path: Path | None = None) -> None:
    if config_path is not None:
        os.environ["CLOUD_DL_CONFIG"] = str(config_path)
    else:
        os.environ["CLOUD_DL_CONFIG"] = "/tmp/cloud-dl-nonexistent-test.json"
    config.reload_config()


def test_deep_merge_preserves_nested_defaults() -> None:
    base = {"aws": {"host": "aws-vps", "region": "ap-southeast-2"}, "rpc_port": 6800}
    merged = config.deep_merge(base, {"aws": {"host": "custom-aws"}})
    assert merged["aws"]["host"] == "custom-aws"
    assert merged["aws"]["region"] == "ap-southeast-2"
    assert merged["rpc_port"] == 6800


def test_parse_df_bytes_line_valid_and_invalid() -> None:
    parsed = ssh.parse_df_bytes_line("/dev/nvme0n1p1 85899345920 21474836480 64424509440 25% /")
    assert parsed is not None
    assert parsed["avail_bytes"] == 64424509440
    assert parsed["use_pct"] == "25%"
    assert ssh.parse_df_bytes_line("short line") is None
    assert ssh.parse_df_bytes_line("/dev/nvme0n1p1 bad bad bad 25% /") is None


def test_update_ssh_config_ip_scoped(tmp_path: Path) -> None:
    cfg = tmp_path / "ssh_config"
    cfg.write_text(
        "Host oci-vps\n  HostName 10.0.0.1\n\nHost aws-vps\n  HostName 10.0.0.2\n  User ubuntu\n",
        encoding="utf-8",
    )
    assert ssh.update_ssh_config_ip("aws-vps", "203.0.113.5", ssh_config_path=str(cfg)) is True
    updated = cfg.read_text(encoding="utf-8")
    assert "HostName 10.0.0.1" in updated
    assert "HostName 203.0.113.5" in updated
    assert "10.0.0.2" not in updated


def test_cmd_cost_negative_zero_and_auth_expired(capsys: pytest.CaptureFixture[str]) -> None:
    reset_test_cfg()
    config.CFG["aws"]["instance_id"] = "i-test0001"
    config.CFG["aws"]["region"] = "eu-central-1"
    config.CFG["aws"]["credit_balance_usd"] = 100.0
    ce_resp = subprocess.CompletedProcess(
        args=["aws"],
        returncode=0,
        stdout=json.dumps({"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "-0.0001"}}}]}),
        stderr="",
    )
    ec2_expired = subprocess.CompletedProcess(
        args=["aws"],
        returncode=255,
        stdout="",
        stderr="aws: [ERROR]: Your session has expired. Please reauthenticate using 'aws login'.",
    )

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["aws", "ce", "get-cost-and-usage"]:
            return ce_resp
        return ec2_expired

    with (
        patch.object(cost.subprocess, "run", side_effect=fake_run),
        patch.object(aws.subprocess, "run", side_effect=fake_run),
    ):
        cost.cmd_cost(None)

    out = capsys.readouterr().out
    assert "AWS Project (eu-central-1):" in out
    assert "Cost Explorer (MTD):       $0.00 USD" in out
    assert "AUTH EXPIRED (run 'aws login')" in out
    assert "$100 covers" in out


def test_cmd_sync_transient_retry_then_succeeds(tmp_path: Path) -> None:
    reset_test_cfg()
    config.CFG["aws"]["instance_id"] = "i-test0001"
    args = cli.build_parser().parse_args(
        [
            "sync",
            "sample-pkg",
            "--cloud",
            "aws",
            "--backend",
            "rsync",
            "--dest",
            str(tmp_path),
            "--retries",
            "2",
            "--no-clean",
        ]
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "rsync":
            rc = 12 if len([c for c in calls if c[0] == "rsync"]) == 1 else 0
            return subprocess.CompletedProcess(cmd, rc, "", "")
        return subprocess.CompletedProcess(cmd, 0, "running\n", "")

    with (
        patch.object(sync.shutil, "which", return_value="/usr/bin/rsync"),
        patch.object(sync, "require_aws_auth"),
        patch.object(sync, "get_aws_state", return_value="running"),
        patch.object(sync, "get_aws_public_ip", return_value="203.0.113.9"),
        patch.object(sync, "update_ssh_config_ip", return_value=False),
        patch.object(sync, "send_notification"),
        patch.object(sync.time, "sleep"),
        patch.object(sync.subprocess, "run", side_effect=fake_run),
    ):
        sync.cmd_sync(args)

    rsync_calls = [c for c in calls if c[0] == "rsync"]
    assert len(rsync_calls) == 2


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["cloud-dl", "--help"]), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "cloud-dl" in capsys.readouterr().out


def test_ensure_aws_auth_auto_login_triggers_and_verifies() -> None:
    reset_test_cfg()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    with (
        patch.object(aws, "get_aws_state", return_value="auth_expired"),
        patch.object(aws.sys.stdin, "isatty", return_value=True),
        patch.object(aws.subprocess, "run", side_effect=fake_run),
    ):
        assert aws.ensure_aws_auth(auto_login=True) is True

    assert ["aws", "login"] in calls
    assert ["aws", "sts", "get-caller-identity"] in calls


def test_ensure_aws_auth_no_auto_login_skips_browser() -> None:
    reset_test_cfg()
    with (
        patch.object(aws, "get_aws_state", return_value="auth_expired"),
        patch.object(aws.subprocess, "run") as mock_run,
    ):
        assert aws.ensure_aws_auth(auto_login=False) is False
    mock_run.assert_not_called()


def test_start_aws_instance_ssh_timeout_at_least_10_and_exhaustion() -> None:
    reset_test_cfg()
    config.CFG["aws"]["instance_id"] = "i-test0001"
    seen_timeouts: list[int] = []

    def fake_run_ssh(host, command, timeout=15, stdin_data=None):
        seen_timeouts.append(timeout)
        return -1, "", "Connection timed out"

    with (
        patch.object(aws, "get_aws_state", return_value="running"),
        patch.object(aws, "get_aws_public_ip", return_value="203.0.113.10"),
        patch.object(aws, "update_ssh_config_ip", return_value=False),
        patch.object(aws, "run_ssh", side_effect=fake_run_ssh),
        patch.object(aws.time, "sleep"),
    ):
        assert aws.start_aws_instance(auto_login=False) is False

    assert len(seen_timeouts) == 25
    assert all(t >= 10 for t in seen_timeouts)


def test_verify_synced_checksums_md5_sfv_and_traversal_guard(tmp_path: Path) -> None:
    pkg = tmp_path / "Repack"
    md5_dir = pkg / "MD5"
    md5_dir.mkdir(parents=True)

    payload = b"hello repack binary"
    bin_file = pkg / "fg-01.bin"
    bin_file.write_bytes(payload)

    md5_hex = hashlib.md5(payload).hexdigest()
    (md5_dir / "fitgirl-bins.md5").write_text(f"{md5_hex} *..\\fg-01.bin\n", encoding="utf-8")
    crc_hex = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
    (pkg / "files.sfv").write_text(f"fg-01.bin {crc_hex}\n", encoding="utf-8")

    assert sync.verify_synced_checksums(pkg) is True

    bin_file.write_bytes(b"corrupted")
    assert sync.verify_synced_checksums(pkg) is False

    (md5_dir / "fitgirl-bins.md5").write_text(f"{md5_hex} ../../outside.txt\n", encoding="utf-8")
    assert sync.verify_synced_checksums(pkg) is False


def test_cmd_sync_verify_and_stop_after_flow(tmp_path: Path) -> None:
    reset_test_cfg()
    config.CFG["aws"]["instance_id"] = "i-test0001"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = pkg / "data.bin"
    data.write_bytes(b"valid")
    (pkg / "check.md5").write_text(f"{hashlib.md5(b'valid').hexdigest()}  data.bin\n", encoding="utf-8")

    args = cli.build_parser().parse_args(
        [
            "sync",
            "pkg",
            "--cloud",
            "aws",
            "--backend",
            "rsync",
            "--dest",
            str(tmp_path),
            "--verify",
            "--stop-after",
            "--no-clean",
        ]
    )
    with (
        patch.object(sync.shutil, "which", return_value="/usr/bin/rsync"),
        patch.object(sync, "require_aws_auth"),
        patch.object(sync, "get_aws_state", return_value="running"),
        patch.object(sync, "send_notification"),
        patch.object(sync, "stop_aws_instance", return_value=True) as mock_stop,
        patch.object(sync.subprocess, "run", return_value=subprocess.CompletedProcess(["rsync"], 0, "", "")),
    ):
        sync.cmd_sync(args)

    mock_stop.assert_called_once()


def test_cmd_init_writes_0600_config_with_random_secret(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg" / "config.json"
    reset_test_cfg(cfg_file)
    args = cli.build_parser().parse_args(
        [
            "init",
            "--instance-id",
            "i-abc12345",
            "--region",
            "us-west-2",
            "--local-dir",
            str(tmp_path),
            "--non-interactive",
        ]
    )
    config.cmd_init(args)
    assert cfg_file.is_file()
    if os.name != "nt":
        assert (cfg_file.stat().st_mode & 0o777) == 0o600
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["aws"]["instance_id"] == "i-abc12345"
    assert saved["aws"]["region"] == "us-west-2"
    assert len(saved["rpc_secret"]) == 32


def test_cmd_doctor_reports_health_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {"aws": {"instance_id": "i-test0001"}, "rpc_secret": "a" * 32, "preferred_local_dir": str(tmp_path)}
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        os.chmod(cfg_file, 0o600)
    reset_test_cfg(cfg_file)
    with (
        patch.object(doctor, "get_aws_state", return_value="stopped"),
        patch.object(doctor.os.path, "ismount", return_value=True),
        patch.object(doctor.shutil, "which", return_value="ok"),
        patch.object(doctor, "_is_local_port_open", return_value=True),
        patch.object(doctor, "query_status_payload", return_value=([], [], [], "", None)),
    ):
        doctor.cmd_doctor(None)

    captured = capsys.readouterr()
    assert "All subsystems healthy." in (captured.out + captured.err)


def test_ssh_controlmaster_args_and_socket_reset_on_ip_rotation() -> None:
    reset_test_cfg()
    config.CFG["aws"]["instance_id"] = "i-test0001"
    with patch.object(ssh.sys, "platform", "linux"):
        args_list = ssh.build_ssh_base_args()
        joined = " ".join(args_list)
        assert "ControlMaster=auto" in joined
        assert "ControlPersist=300" in joined

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "-O" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1 if len([c for c in calls if "-O" not in c]) == 1 else 0, "ok\n", "")

    with (
        patch.object(ssh.sys, "platform", "linux"),
        patch.object(aws, "get_aws_state", return_value="running"),
        patch.object(aws, "get_aws_public_ip", return_value="203.0.113.77"),
        patch.object(ssh, "update_ssh_config_ip", return_value=True),
        patch.object(ssh.subprocess, "run", side_effect=fake_run),
    ):
        code, stdout, _ = ssh.run_ssh(config.CFG["aws"]["host"], "uptime")

    assert code == 0
    assert stdout == "ok\n"
    assert any("-O" in c and "exit" in c for c in calls)


def test_windows_ssh_base_args_omit_controlmaster() -> None:
    with patch.object(ssh.sys, "platform", "win32"):
        args_list = ssh.build_ssh_base_args()
        joined = " ".join(args_list)
        assert "ControlMaster" not in joined
        assert "ControlPath" not in joined


def test_ui_tunnel_stop_kills_tracked_pid_without_pkill(tmp_path: Path) -> None:
    tunnel_file = tmp_path / "ui-tunnel.json"
    tunnel_file.write_text(
        json.dumps({"pid": 43210, "cloud": "aws", "host": "aws-vps", "port": 6800}), encoding="utf-8"
    )
    os.environ["CLOUD_DL_TUNNEL_STATE"] = str(tunnel_file)
    try:
        args = cli.build_parser().parse_args(["ui", "--stop"])
        with patch.object(doctor.sys, "platform", "linux"), patch.object(doctor.os, "kill") as mock_kill:
            doctor.cmd_ui(args)
        mock_kill.assert_called_once_with(43210, signal.SIGTERM)
        assert not tunnel_file.exists()
    finally:
        os.environ.pop("CLOUD_DL_TUNNEL_STATE", None)


def test_windows_tunnel_stop_uses_taskkill(tmp_path: Path) -> None:
    tunnel_file = tmp_path / "ui-tunnel.json"
    tunnel_file.write_text(
        json.dumps({"pid": 54321, "cloud": "aws", "host": "aws-vps", "port": 6800}), encoding="utf-8"
    )
    os.environ["CLOUD_DL_TUNNEL_STATE"] = str(tunnel_file)
    try:
        args = cli.build_parser().parse_args(["ui", "--stop"])
        with (
            patch.object(doctor.sys, "platform", "win32"),
            patch.object(
                doctor.subprocess, "run", return_value=subprocess.CompletedProcess(["taskkill"], 0, "", "")
            ) as mock_run,
        ):
            doctor.cmd_ui(args)
        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert cmd[:4] == ["taskkill", "/PID", "54321", "/T"]
        assert not tunnel_file.exists()
    finally:
        os.environ.pop("CLOUD_DL_TUNNEL_STATE", None)


def test_build_rsync_command_exfat_and_append_flags() -> None:
    cmd_fat = sync.build_rsync_command("aws-vps:/dl/pkg", "/mnt/DATA", is_fat=True, append=True)
    assert "-rltvh" in cmd_fat
    assert "--modify-window=1" in cmd_fat
    assert "--append" in cmd_fat
    assert "--info=progress2" in cmd_fat
    assert "-avh" not in cmd_fat

    cmd_posix = sync.build_rsync_command("aws-vps:/dl/pkg", "/tmp/out", is_fat=False, append=False)
    assert "-avh" in cmd_posix
    assert "--append" not in cmd_posix


def test_sync_unmounted_preferred_target_raises_sync_error() -> None:
    reset_test_cfg()
    config.CFG["preferred_local_dir"] = "/mnt/DATA"
    with (
        patch.object(sync.sys, "platform", "linux"),
        patch.object(sync.os.path, "ismount", return_value=False),
        patch.object(sync.os.path, "exists", return_value=False),
        pytest.raises(exceptions.SyncError),
    ):
        sync.ensure_local_dest_mounted("/mnt/DATA")


def test_windows_disconnected_override_drive_raises_sync_error() -> None:
    reset_test_cfg()
    with patch.object(sync.os.path, "exists", return_value=False):
        assert sync._resolve_windows_dest(None, r"Z:\Media") == str(Path.home() / "Downloads")
        with pytest.raises(exceptions.SyncError):
            sync._resolve_windows_dest(r"Z:\Media", r"Z:\Media")


def test_resolve_sync_backend_selects_ssh_on_windows_and_when_rsync_missing() -> None:
    with patch.object(sync.sys, "platform", "win32"), patch.object(sync.shutil, "which", return_value=None):
        assert sync.resolve_sync_backend("auto") == "ssh"
        with pytest.raises(exceptions.SyncError):
            sync.resolve_sync_backend("rsync")
    with patch.object(sync.sys, "platform", "linux"), patch.object(sync.shutil, "which", return_value="/usr/bin/rsync"):
        assert sync.resolve_sync_backend("auto") == "rsync"
        assert sync.resolve_sync_backend("ssh") == "ssh"


def test_ssh_stream_sync_resumes_partial_file_after_transient_drop(tmp_path: Path) -> None:
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    partial = dest_dir / "pkg" / "movie.bin"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"hello ")
    full_payload = b"hello world!"

    manifest = json.dumps([{"rel": "pkg/movie.bin", "size": len(full_payload), "remote": "/dl/pkg/movie.bin"}])
    call_count = 0

    class _FakeProc:
        def __init__(self, data: bytes, rc: int) -> None:
            import io

            self.stdout = io.BytesIO(data)
            self.stderr = io.BytesIO(b"drop" if rc else b"")
            self._rc = rc

        def wait(self) -> int:
            return self._rc

    def fake_popen(cmd, **_kwargs):
        nonlocal call_count
        call_count += 1
        assert "python3 -c" in cmd[-1]
        if call_count == 1:
            assert f" {len(b'hello ')}" in cmd[-1]
            return _FakeProc(b"wo", 255)
        assert f" {len(b'hello wo')}" in cmd[-1]
        return _FakeProc(b"rld!", 0)

    def fake_run_ssh(_host, command, **_kwargs):
        if "hashlib" in command:
            cur_len = partial.stat().st_size
            return 0, hashlib.sha256(full_payload[:cur_len]).hexdigest(), ""
        return 0, manifest, ""

    with (
        patch.object(sync, "run_ssh", side_effect=fake_run_ssh),
        patch.object(sync.subprocess, "Popen", side_effect=fake_popen),
        patch.object(sync.time, "sleep"),
    ):
        sync._run_ssh_stream_with_retries("aws-vps", "oci", "/dl", "pkg", str(dest_dir), 3)

    assert partial.read_bytes() == full_payload
    assert call_count == 2


def test_ariang_bundle_uses_stdlib_zipfile(tmp_path: Path) -> None:
    import zipfile

    zip_src = tmp_path / "src.zip"
    with zipfile.ZipFile(zip_src, "w") as zf:
        zf.writestr("index.html", "<html>AriaNg</html>")

    out_html = tmp_path / "ui" / "index.html"

    def fake_retrieve(_url: str, dst: str) -> tuple[str, None]:
        Path(dst).write_bytes(zip_src.read_bytes())
        return dst, None

    with patch.object(doctor.urllib.request, "urlretrieve", side_effect=fake_retrieve):
        doctor._ensure_ariang_bundle(out_html)

    assert out_html.read_text(encoding="utf-8") == "<html>AriaNg</html>"


def test_browse_env_outputs_powershell_on_windows(capsys: pytest.CaptureFixture[str]) -> None:
    reset_test_cfg()
    args = cli.build_parser().parse_args(["browse", "env"])
    with patch.object(doctor.sys, "platform", "win32"), patch.object(doctor, "_ensure_socks_tunnel"):
        doctor.cmd_browse(args)
    out = capsys.readouterr().out
    assert '$env:ALL_PROXY = "socks5h://127.0.0.1:1080"' in out


def test_browse_launches_edge_with_socks5_and_isolated_profile(tmp_path: Path) -> None:
    reset_test_cfg()
    args = cli.build_parser().parse_args(["browse", "edge", "https://example.com"])
    with (
        patch.object(doctor, "_ensure_socks_tunnel"),
        patch.object(doctor, "get_cache_dir", return_value=tmp_path),
        patch.object(doctor, "_find_browser_binary", return_value="msedge.exe"),
        patch.object(doctor, "_popen_detached") as mock_popen,
    ):
        doctor.cmd_browse(args)

    mock_popen.assert_called_once()
    launched_cmd = mock_popen.call_args.args[0]
    assert launched_cmd[0] == "msedge.exe"
    assert "--proxy-server=socks5://127.0.0.1:1080" in launched_cmd
    assert any(arg.startswith("--user-data-dir=") for arg in launched_cmd)
    assert "https://example.com" in launched_cmd


def test_init_shared_secret_and_ssh_config_aliases(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg" / "config.json"
    ssh_cfg = tmp_path / "ssh" / "config"
    reset_test_cfg(cfg_file)
    args = cli.build_parser().parse_args(
        [
            "init",
            "--instance-id",
            "",
            "--rpc-secret",
            "sharedfriendtoken1234",
            "--local-dir",
            str(tmp_path),
            "--non-interactive",
        ]
    )
    config.cmd_init(args)
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["rpc_secret"] == "sharedfriendtoken1234"
    assert saved["aws"]["instance_id"] == ""

    written_path, added = config.setup_ssh_config_aliases(
        oci_ip="203.0.113.44", key_path="/tmp/key", ssh_config_path=ssh_cfg
    )
    assert written_path == ssh_cfg
    assert "oci-vps" in added
    text = ssh_cfg.read_text(encoding="utf-8")
    assert "HostName 203.0.113.44" in text
    assert "StrictHostKeyChecking accept-new" in text

    # Second run updates HostName without duplicating blocks
    _, updated = config.setup_ssh_config_aliases(oci_ip="203.0.113.99", ssh_config_path=ssh_cfg)
    assert "oci-vps" in updated
    text2 = ssh_cfg.read_text(encoding="utf-8")
    assert text2.count("Host oci-vps") == 1
    assert "HostName 203.0.113.99" in text2


def test_doctor_oci_only_mode_healthy_when_socks_idle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"aws": {"instance_id": ""}, "rpc_secret": "s" * 16, "preferred_local_dir": str(tmp_path)}),
        encoding="utf-8",
    )
    if os.name != "nt":
        os.chmod(cfg_file, 0o600)
    reset_test_cfg(cfg_file)
    with (
        patch.object(doctor, "get_aws_state", return_value="unconfigured"),
        patch.object(doctor.os.path, "ismount", return_value=True),
        patch.object(doctor.shutil, "which", return_value="ssh"),
        patch.object(doctor, "_is_local_port_open", return_value=False),
        patch.object(doctor, "query_status_payload", return_value=([], [], [], "", None)),
    ):
        doctor.cmd_doctor(None)
    captured = capsys.readouterr()
    assert "All subsystems healthy." in (captured.out + captured.err)


def test_parse_remote_manifest_skips_aria2_and_sanitizes_windows_chars(tmp_path: Path) -> None:
    manifest = json.dumps(
        [
            {"rel": "Game/data.bin", "size": 100, "remote": "/dl/Game/data.bin"},
            {"rel": "Game/data.bin.aria2", "size": 50, "remote": "/dl/Game/data.bin.aria2"},
            {"rel": "Game/Read:Me?.txt ", "size": 12, "remote": "/dl/Game/Read:Me?.txt "},
        ]
    )
    with patch.object(sync.sys, "platform", "win32"):
        parsed = sync._parse_remote_manifest(manifest, tmp_path)
    rels = [entry[3] for entry in parsed]
    assert "Game/data.bin" in rels
    assert all(not r.endswith(".aria2") for r in rels)
    assert "Game/Read_Me_.txt" in rels


def test_ssh_stream_batches_small_files_via_tar(tmp_path: Path) -> None:
    import io
    import tarfile

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    files_data = {"pkg/a.txt": b"alpha", "pkg/sub/b.nfo": b"beta-info", "pkg/c.ini": b"[cfg]\n"}
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w|") as tf:
        for rel, body in files_data.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    tar_bytes = tar_buf.getvalue()

    manifest = json.dumps([{"rel": rel, "size": len(body), "remote": f"/dl/{rel}"} for rel, body in files_data.items()])

    class _FakeTarProc:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(tar_bytes)
            self.stderr = io.BytesIO(b"")

        def wait(self) -> int:
            return 0

    with (
        patch.object(sync, "run_ssh", return_value=(0, manifest, "")),
        patch.object(sync.subprocess, "Popen", return_value=_FakeTarProc()) as mock_popen,
    ):
        sync._run_ssh_stream_with_retries("oci-vps", "oci", "/dl", "pkg", str(dest_dir), 2)

    assert mock_popen.call_count == 1
    for rel, body in files_data.items():
        assert (dest_dir / rel).read_bytes() == body


def test_ssh_base_args_include_accept_new() -> None:
    args_str = " ".join(ssh.build_ssh_base_args())
    assert "StrictHostKeyChecking=accept-new" in args_str


def test_ssh_stream_restarts_from_zero_when_tail_sha256_mismatches(tmp_path: Path) -> None:
    import io

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    partial = dest_dir / "pkg" / "movie.bin"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"BAD!!!")
    full_payload = b"hello world!"
    manifest = json.dumps([{"rel": "pkg/movie.bin", "size": len(full_payload), "remote": "/dl/pkg/movie.bin"}])

    def fake_run_ssh(_host, command, **_kwargs):
        if "hashlib" in command:
            return 0, hashlib.sha256(b"hello ").hexdigest(), ""
        return 0, manifest, ""

    class _FullProc:
        def __init__(self, data: bytes) -> None:
            self.stdout = io.BytesIO(data)
            self.stderr = io.BytesIO(b"")

        def wait(self) -> int:
            return 0

    seen_cmds: list[str] = []

    def fake_popen(cmd, **_kwargs):
        seen_cmds.append(cmd[-1])
        assert cmd[-1].endswith(" 0")
        return _FullProc(full_payload)

    with (
        patch.object(sync, "run_ssh", side_effect=fake_run_ssh),
        patch.object(sync.subprocess, "Popen", side_effect=fake_popen),
    ):
        sync._run_ssh_stream_with_retries("oci-vps", "oci", "/dl", "pkg", str(dest_dir), 2)

    assert len(seen_cmds) == 1
    assert partial.read_bytes() == full_payload


def test_windows_long_path_adds_extended_prefix() -> None:
    deep_win = Path("C:\\" + ("segment\\" * 32) + "file.bin")
    short_win = Path(r"C:\Downloads\file.bin")
    with patch.object(sync.sys, "platform", "win32"):
        assert sync._as_os_path(deep_win).startswith("\\\\?\\")
        assert sync._as_os_path(short_win) == str(short_win)


def test_sync_lock_guard_blocks_concurrent_sync(tmp_path: Path) -> None:
    lock_file = tmp_path / "sync.lock"
    with sync._sync_lock_guard(lock_file), pytest.raises(exceptions.SyncError), sync._sync_lock_guard(lock_file):
        pass
    assert not lock_file.exists()
