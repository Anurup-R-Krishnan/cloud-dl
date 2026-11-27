#!/usr/bin/env python3
"""
Regression Test Suite 5: General-Purpose Cloud Downloader (cloud-dl)
Validates CLI, dual-cloud RPC endpoints, AriaNg UI assets, and watchdog.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cloud_dl.config import deep_merge, get_cfg  # noqa: E402
from cloud_dl.cost import format_usd  # noqa: E402
from cloud_dl.ssh import is_aws_auth_error, parse_df_bytes_line, update_ssh_config_ip  # noqa: E402

passed = 0
failed = 0


def record_result(test_name: str, success: bool, details: str = "") -> None:
    global passed, failed
    if success:
        passed += 1
        print(f" [PASS] {test_name:<45} | {details}")
    else:
        failed += 1
        print(f" [FAIL] {test_name:<45} | {details}")


print("=" * 80)
print("REGRESSION TEST SUITE: GENERAL-PURPOSE CLOUD DOWNLOADER (cloud-dl)")
print("=" * 80)

# 1. Check CLI binary installed and executable
cli_path = os.path.expanduser("~/.local/bin/cloud-dl")
if os.path.isfile(cli_path) and os.access(cli_path, os.X_OK):
    record_result("cloud-dl CLI Executable in PATH", True, f"Found at {cli_path}")
else:
    record_result("cloud-dl CLI Executable in PATH", False, "Missing or not executable")

# 2. Check AriaNg Web UI bundle installed
ui_path = os.path.expanduser(str(get_cfg().get("ariang_path", "~/.local/share/cloud-dl/index.html")))
if os.path.isfile(ui_path) and os.path.getsize(ui_path) > 1000000:
    record_result("AriaNg AllInOne HTML Bundle", True, f"Present ({os.path.getsize(ui_path) / (1024 * 1024):.1f} MB)")
else:
    record_result("AriaNg AllInOne HTML Bundle", False, "Missing or incomplete")

# 3. Test RPC response on AWS Sydney
try:
    p = subprocess.run(
        ["cloud-dl", "status", "--cloud", "aws"],
        check=False,
        capture_output=True,
        text=True,
        timeout=12,
    )
    if "Download Status: AWS Sydney" in p.stdout and "Disk:" in p.stdout:
        record_result("AWS Sydney Aria2 JSON-RPC Endpoint", True, "Successfully queried via SSH")
    else:
        record_result("AWS Sydney Aria2 JSON-RPC Endpoint", False, f"Output: {p.stdout[:150]}")
except (OSError, subprocess.SubprocessError) as e:
    record_result("AWS Sydney Aria2 JSON-RPC Endpoint", False, str(e))

# 4. Test RPC response on OCI Hyderabad
try:
    p = subprocess.run(
        ["cloud-dl", "status", "--cloud", "oci"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if "Download Status: OCI Hyderabad" in p.stdout and "Disk:" in p.stdout:
        record_result("OCI Hyderabad Aria2 JSON-RPC Endpoint", True, "Successfully queried via SSH")
    else:
        record_result("OCI Hyderabad Aria2 JSON-RPC Endpoint", False, f"Output: {p.stdout[:150]}")
except (OSError, subprocess.SubprocessError) as e:
    record_result("OCI Hyderabad Aria2 JSON-RPC Endpoint", False, str(e))

# 5. Check Watchdog v2 active on AWS
try:
    p = subprocess.run(
        ["cloud-dl", "watchdog", "--cloud", "aws"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if "active rpc jobs:" in p.stdout.lower() and "ssh sessions:" in p.stdout.lower():
        record_result("AWS Watchdog v2 RPC Status Probe", True, "Active and monitoring idle state")
    else:
        record_result("AWS Watchdog v2 RPC Status Probe", False, f"Output: {p.stdout[:150]}")
except (OSError, subprocess.SubprocessError) as e:
    record_result("AWS Watchdog v2 RPC Status Probe", False, str(e))

# 6. Unit checks on cloud-dl helpers
try:
    merged = deep_merge({"aws": {"host": "aws-vps", "region": "ap-southeast-2"}}, {"aws": {"host": "custom"}})
    df_res = parse_df_bytes_line("/dev/nvme0n1p1 85899345920 42949672960 42949672960 50% /")
    usd_ok = format_usd(-0.0001) == "$0.00 USD" and format_usd(1.234) == "$1.23 USD"
    auth_ok = is_aws_auth_error("Your session has expired. Please reauthenticate using 'aws login'.")

    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
        tf.write("Host oci-vps\n  HostName 1.2.3.4\nHost aws-vps\n  HostName 5.6.7.8\n")
        tmp_cfg = tf.name
    try:
        changed = update_ssh_config_ip("aws-vps", "9.9.9.9", ssh_config_path=tmp_cfg)
        with open(tmp_cfg, encoding="utf-8") as fh:
            cfg_txt = fh.read()
        ssh_ok = changed and "HostName 1.2.3.4" in cfg_txt and "HostName 9.9.9.9" in cfg_txt
    finally:
        os.unlink(tmp_cfg)

    all_ok = (
        merged["aws"]["region"] == "ap-southeast-2"
        and merged["aws"]["host"] == "custom"
        and df_res is not None
        and df_res["avail_bytes"] == 42949672960
        and usd_ok
        and auth_ok
        and ssh_ok
    )
    record_result("cloud-dl Core Helper Unit Checks", all_ok, "deep_merge, df-B1, format_usd, ssh-config")
except (OSError, RuntimeError, ValueError, KeyError, AssertionError) as e:
    record_result("cloud-dl Core Helper Unit Checks", False, str(e))

print("=" * 80)
print(f"CLOUD-DL REGRESSION RESULTS: {passed} PASSED, {failed} FAILED")
print("=" * 80)
sys.exit(0 if failed == 0 else 1)
