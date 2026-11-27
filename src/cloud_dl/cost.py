"""Cloud billing analytics, bandwidth speedtests, and SSH latency checks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime

from .aws import ensure_aws_auth, get_aws_state
from .config import Terminal, get_cfg, resolve_bin
from .ssh import get_disk_free, is_aws_auth_error, run_ssh


def format_usd(amount: float) -> str:
    clean_val = 0.0 if abs(amount) < 0.005 else amount
    return f"${clean_val:.2f} USD"


def benchmark_node(label: str, host: str, test_bytes: int) -> None:
    mb_size = test_bytes // (1000 * 1000)
    sys.stdout.write(f"Testing {label}...\n")
    code, stdout, _ = run_ssh(
        host,
        f"curl -o /dev/null -s -w '%{{speed_download}} %{{time_total}}' https://speed.cloudflare.com/__down?bytes={test_bytes}",
        timeout=20,
    )
    if code == 0 and stdout.strip():
        parts = stdout.strip().split()
        if len(parts) >= 2:
            try:
                speed_bps = float(parts[0])
                t_total = float(parts[1])
                speed_mbps = (speed_bps * 8) / (1000 * 1000)
                sys.stdout.write(f"  Bandwidth: {speed_bps / (1024 * 1024):.1f} MB/s ({speed_mbps:.1f} Mbps)\n")
                sys.stdout.write(f"  Duration:  {mb_size} MB transferred in {t_total:.2f}s\n")
                return
            except ValueError:
                pass
    sys.stdout.write("  Status:    Benchmark failed or instance offline\n")


def cmd_test(_args: argparse.Namespace) -> None:
    cfg = get_cfg()
    sys.stdout.write(f"\n{Terminal.BOLD}Provider Latency & Connectivity Test{Terminal.RESET}\n")
    sys.stdout.write("-" * 60 + "\n")
    for c_key in ["aws", "oci"]:
        c_info = cfg[c_key]
        host = c_info["host"]
        sys.stdout.write(f"Target: {c_info['name']} ({host})\n")
        t0 = time.time()
        code, _, stderr = run_ssh(host, "uptime", timeout=8)
        rtt = (time.time() - t0) * 1000
        if code == 0:
            disk = get_disk_free(host)
            disk_str = f"{disk['avail']} free ({disk['use_pct']} used)" if disk else "N/A"
            sys.stdout.write(f"  SSH latency: {rtt:.1f} ms\n")
            sys.stdout.write(f"  Disk status: {disk_str}\n")
            sys.stdout.write(f"  Shape:       {c_info['specs']}\n")
        else:
            sys.stdout.write(f"  Status:      Offline ({stderr.strip() or 'timed out'})\n")
        sys.stdout.write("\n")


def cmd_speed(_args: argparse.Namespace) -> None:
    cfg = get_cfg()
    sys.stdout.write(f"\n{Terminal.BOLD}VPS Network Pipe Benchmark{Terminal.RESET}\n")
    sys.stdout.write("-" * 60 + "\n")
    benchmark_node(f"{cfg['aws']['name']} ({cfg['aws']['region']})", cfg["aws"]["host"], 50_000_000)
    sys.stdout.write("\n")
    benchmark_node(f"{cfg['oci']['name']} ({cfg['oci'].get('region', 'oci')})", cfg["oci"]["host"], 25_000_000)


def _query_cost_explorer() -> str:
    start_date = time.strftime("%Y-%m-01")
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    try:
        ce_res = subprocess.run(
            [
                resolve_bin("aws"),
                "ce",
                "get-cost-and-usage",
                "--time-period",
                f"Start={start_date},End={tomorrow}",
                "--granularity",
                "MONTHLY",
                "--metrics",
                "UnblendedCost",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ce_res.returncode == 0:
            results = json.loads(ce_res.stdout).get("ResultsByTime", [])
            if results:
                amt = float(results[0].get("Total", {}).get("UnblendedCost", {}).get("Amount", "0"))
                return format_usd(amt)
            return "$0.00 USD"
        if is_aws_auth_error(ce_res.stderr):
            return "AUTH EXPIRED (run 'aws login')"
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        pass
    return "UNAVAILABLE"


def _query_ec2_uptime(instance_id: str, region: str) -> tuple[float, str]:
    if not instance_id:
        return 0.0, "N/A"
    try:
        ec2_res = subprocess.run(
            [
                resolve_bin("aws"),
                "ec2",
                "describe-instances",
                "--instance-ids",
                instance_id,
                "--region",
                region,
                "--query",
                "Reservations[0].Instances[0].LaunchTime",
                "--output",
                "text",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if ec2_res.returncode == 0 and ec2_res.stdout.strip() and ec2_res.stdout.strip() != "None":
            launch_str = ec2_res.stdout.strip()
            launch_dt = datetime.fromisoformat(launch_str)
            if launch_dt.tzinfo is None:
                launch_dt = launch_dt.replace(tzinfo=UTC)
            uptime_hrs = max(0.0, (datetime.now(UTC) - launch_dt).total_seconds() / 3600.0)
            return uptime_hrs, launch_str
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 0.0, "N/A"


def cmd_cost(args: argparse.Namespace | None = None) -> None:
    cfg = get_cfg()
    auto_login = not getattr(args, "no_auto_login", False) if args is not None else False
    if args is not None:
        ensure_aws_auth(auto_login=auto_login)

    sys.stdout.write(f"\n{Terminal.BOLD}Cloud Spending & Cost Analytics{Terminal.RESET}\n")
    sys.stdout.write("=" * 70 + "\n")

    aws_cfg = cfg["aws"]
    oci_cfg = cfg["oci"]
    instance_id = aws_cfg.get("instance_id", "").strip()
    aws_region = aws_cfg.get("region", "ap-southeast-2")

    ce_display = _query_cost_explorer()
    st = get_aws_state()
    uptime_hrs, launch_str = _query_ec2_uptime(instance_id, aws_region) if st == "running" else (0.0, "N/A")

    disk_gb = float(aws_cfg.get("disk_size_gb", 80))
    compute_per_hr = float(aws_cfg.get("compute_per_hr", 0.0211))
    storage_per_gb_mo = float(aws_cfg.get("storage_per_gb_mo", 0.088))
    credit_balance = float(aws_cfg.get("credit_balance_usd", 180.0))

    storage_per_mo = disk_gb * storage_per_gb_mo
    storage_per_hr = storage_per_mo / 730.0
    storage_per_day = storage_per_mo / 30.4
    idle_months = (credit_balance / storage_per_mo) if storage_per_mo > 0 else 0.0

    compute_accrued = uptime_hrs * compute_per_hr if st == "running" else 0.0
    burn_running = compute_per_hr + storage_per_hr
    burn_stopped = storage_per_hr

    st_labels = {
        "auth_expired": "AUTH EXPIRED (run 'aws login')",
        "unconfigured": "UNCONFIGURED (run 'cloud-dl init')",
    }
    st_display = st_labels.get(st, st.upper())

    sys.stdout.write(f"AWS Project ({aws_region}):\n")
    sys.stdout.write(f"  Cost Explorer (MTD):       {ce_display}\n")
    sys.stdout.write(f"  Instance Status:           {st_display} ({instance_id or 'none'})\n")
    if st == "running":
        sys.stdout.write(f"  Session Uptime:            ~{uptime_hrs:.1f} hours (started {launch_str[:16]})\n")
        sys.stdout.write(f"  Compute Accrued:           ~${compute_accrued:.3f} USD\n")
        sys.stdout.write(f"  Active Burn Rate:          ${burn_running:.4f}/hour (~${burn_running * 24:.2f}/day)\n")
    else:
        sys.stdout.write(f"  Idle Burn Rate:            ${burn_stopped:.4f}/hour ($0.00 compute)\n")
    sys.stdout.write(
        f"  {int(disk_gb)} GB gp3 Root Volume:     ${storage_per_mo:.2f}/month (~${storage_per_day:.2f}/day)\n"
    )
    sys.stdout.write("  Data Transfer Out:         $0.00 (within 100 GB/month free tier)\n")
    sys.stdout.write(
        f"  Estimated Credit Lifespan: At idle rate, ${credit_balance:.0f} covers ~{idle_months:.1f} months of storage\n"
    )
    sys.stdout.write("  AWS Settings Console:      https://settings.aws.com/ (Billing & Limits)\n\n")

    sys.stdout.write(f"Oracle Cloud Always Free ({oci_cfg.get('region', 'ap-hyderabad-1')}):\n")
    sys.stdout.write("  Compute (VM.Standard):     $0.00/month (Always Free)\n")
    sys.stdout.write(f"  Boot Volume ({int(oci_cfg.get('disk_size_gb', 45))} GB):       $0.00/month (Always Free)\n")
    sys.stdout.write("  Monthly Egress Allowance:  10,000 GB (10 TB) free\n")
    sys.stdout.write("  Total Incurred Cost:       $0.00 USD\n")
    sys.stdout.write("=" * 70 + "\n")
