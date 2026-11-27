"""CLI entrypoint, help formatter, and subcommand dispatch."""

from __future__ import annotations

import argparse
import signal
import sys
import traceback
from typing import Any

from . import VERSION
from .aws import cmd_start, cmd_stop
from .config import Terminal, cmd_init, get_cfg, log_err
from .cost import cmd_cost, cmd_speed, cmd_test
from .doctor import cmd_browse, cmd_doctor, cmd_ui, cmd_watchdog
from .exceptions import CloudDlError
from .rpc import cmd_add, cmd_cancel, cmd_limit, cmd_pause, cmd_resume, cmd_status, cmd_top
from .sync import cmd_clean, cmd_extract, cmd_list, cmd_sync


def _sigint_handler(_signum: int, _frame: Any) -> None:
    sys.stderr.write("\n[INFO] Operation canceled by user.\n")
    sys.exit(130)


def print_custom_help() -> None:
    b = Terminal.BOLD
    r = Terminal.RESET
    d = Terminal.DIM
    help_text = f"""{b}cloud-dl{r} - Personal Multi-Cloud Download & Torrent Engine (v{VERSION})

{b}USAGE:{r}
  cloud-dl <command> [options] [arguments]

{b}TRANSFER & QUEUE COMMANDS:{r}
  {b}add{r}       Queue a download (magnet link, HTTP/HTTPS URL, or local .torrent file)
  {b}sync{r}      Synchronize completed files to local storage with resume & auto-retry
  {b}list{r}      List files currently residing in the remote download directory
  {b}clean{r}     Delete files from the remote cloud disk to reclaim SSD storage
  {b}extract{r}   Remotely extract an archive (.zip, .tar.gz, .7z, .rar) on the VPS

{b}MONITORING & CONTROL COMMANDS:{r}
  {b}status{r}    Display current transfer status, speeds, ETA, and remote disk usage
  {b}top{r}       Live interactive terminal dashboard (auto-refreshing every 2s)
  {b}ui{r}        Launch the AriaNg graphical web dashboard via tracked SSH tunnel
  {b}pause{r}     Pause an active task by GID
  {b}resume{r}    Resume a paused task by GID
  {b}cancel{r}    Cancel and remove a task by GID

{b}INFRASTRUCTURE & NETWORK COMMANDS:{r}
  {b}cost{r}      Audit month-to-date spending, burn rate, and credit longevity
  {b}speed{r}     Measure raw network pipe bandwidth from the cloud VPS nodes
  {b}test{r}      Benchmark latency, ping, and SSH connectivity across providers
  {b}start{r}     Wake or boot an EC2 cloud instance
  {b}stop{r}      Stop a cloud instance to pause compute billing ($0.00/hr)
  {b}watchdog{r}  Inspect remote auto-shutdown timer and active connections
  {b}limit{r}     Inspect or adjust global download/upload bandwidth throttles
  {b}browse{r}    Launch isolated browser routed via OCI SOCKS5 proxy
  {b}init{r}      Generate or rotate user config (~/.config/cloud-dl or %APPDATA%\\cloud-dl)
  {b}doctor{r}    Verify config, SSH/AWS tools, storage, proxy, and Aria2 RPC health

{b}GLOBAL OPTIONS:{r}
  -h, --help         Show this manual and exit
  -v, --version      Show version information and exit
  --no-auto-login    Skip automatic browser 'aws login' on expired session
  --debug            Show full Python tracebacks on unexpected errors

{b}EXAMPLES:{r}
  {d}# Queue a magnet link on AWS:{r}
  cloud-dl add "magnet:?xt=urn:btih:..." --cloud aws

  {d}# Queue links in batch from a text file:{r}
  cloud-dl add -f links.txt --cloud aws

  {d}# Live terminal monitor:{r}
  cloud-dl top

  {d}# Fast resume to exFAT HDD, verify checksums, and stop AWS EC2 when done:{r}
  cloud-dl sync "Package Name" --dest /mnt/DATA --append --verify --stop-after

  {d}# Audit AWS credit burn rate and billing:{r}
  cloud-dl cost

Run 'cloud-dl <command> --help' for options specific to a command.
"""
    sys.stdout.write(help_text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloud-dl",
        description="Personal Multi-Cloud Download & Torrent Engine",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="store_true", help="Show help message and exit")
    parser.add_argument("-v", "--version", action="version", version=f"cloud-dl {VERSION}")
    parser.add_argument("--no-auto-login", action="store_true", help="Do not launch browser 'aws login' automatically")
    parser.add_argument("--debug", action="store_true", help="Show full tracebacks on unexpected failures")

    subparsers = parser.add_subparsers(dest="command", help=argparse.SUPPRESS)

    p_add = subparsers.add_parser("add", help="Queue a new download", add_help=True)
    p_add.add_argument("target", nargs="?", help="Magnet link, HTTP/HTTPS URL, or local .torrent file")
    p_add.add_argument("--file", "-f", help="Path to text file containing URLs or magnets to add in batch")
    p_add.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider (aws or oci)")
    p_add.add_argument("--dest", "-d", help="Custom destination folder on the VPS")
    p_add.add_argument(
        "--no-auto-login", action="store_true", help="Disable automatic 'aws login' prompt on expired session"
    )

    p_status = subparsers.add_parser("status", help="Show download status", add_help=True)
    p_status.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")

    p_top = subparsers.add_parser("top", help="Live auto-refreshing monitor", add_help=True)
    p_top.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")

    p_list = subparsers.add_parser("list", help="List remote downloaded files", add_help=True)
    p_list.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")

    p_sync = subparsers.add_parser("sync", help="Sync files to local storage", add_help=True)
    p_sync.add_argument("item", nargs="?", help="Specific folder or file name to sync")
    p_sync.add_argument("--dest", "-d", help="Local target path (defaults to /mnt/DATA on Linux or ~/Downloads)")
    p_sync.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")
    p_sync.add_argument(
        "--backend",
        choices=["auto", "rsync", "ssh"],
        default="auto",
        help="Transfer engine: auto (rsync on Linux, native ssh stream on Windows), rsync, or ssh",
    )
    p_sync.add_argument(
        "--retries", "-r", type=int, default=5, help="Auto-retry count on transient network drops (default: 5)"
    )
    p_sync.add_argument(
        "--append", action="store_true", help="Resume partial files sequentially without slow local copy"
    )
    p_sync.add_argument("--verify", action="store_true", help="Verify local .md5/.sfv manifests after sync completes")
    p_sync.add_argument(
        "--stop-after", action="store_true", help="Stop AWS EC2 instance immediately after sync finishes"
    )
    p_sync.add_argument("--clean", action="store_true", help="Automatically delete source on VPS after sync")
    p_sync.add_argument("--no-clean", action="store_true", help="Preserve source on VPS without prompting")
    p_sync.add_argument(
        "--no-auto-login", action="store_true", help="Disable automatic 'aws login' prompt on expired session"
    )

    p_clean = subparsers.add_parser("clean", help="Delete remote files from VPS", add_help=True)
    p_clean.add_argument("item", nargs="?", help="Name of file or directory to delete")
    p_clean.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")
    p_clean.add_argument("--yes", "-y", action="store_true", help="Bypass interactive confirmation prompt")

    p_limit = subparsers.add_parser("limit", help="Set or inspect speed limits", add_help=True)
    p_limit.add_argument("--down", help="Download rate limit (e.g., 10M, 500K, 0=unlimited)")
    p_limit.add_argument("--up", help="Upload rate limit (e.g., 1M, 50K, 0=unlimited)")
    p_limit.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")

    p_extract = subparsers.add_parser("extract", help="Extract remote archive on VPS", add_help=True)
    p_extract.add_argument("item", help="Name of archive file to extract (.zip, .tar.gz, .7z, .rar)")
    p_extract.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")

    subparsers.add_parser("test", help="Test provider latency and status", add_help=True)
    subparsers.add_parser("speed", help="Test VPS pipe throughput", add_help=True)
    p_cost = subparsers.add_parser("cost", help="Analyze cloud spending and burn rate", add_help=True)
    p_cost.add_argument(
        "--no-auto-login", action="store_true", help="Disable automatic 'aws login' prompt on expired session"
    )

    p_browse = subparsers.add_parser("browse", help="Launch proxied browser via OCI SOCKS5 tunnel", add_help=True)
    p_browse.add_argument(
        "browser",
        nargs="?",
        choices=["chrome", "edge", "zen", "firefox", "status", "env", "stop"],
        default="chrome",
    )
    p_browse.add_argument("target", nargs="?", help="Optional URL to open")

    p_ui = subparsers.add_parser("ui", help="Launch AriaNg web dashboard", add_help=True)
    p_ui.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")
    p_ui.add_argument("--stop", action="store_true", help="Stop background SSH tunnel")
    p_ui.add_argument(
        "--no-auto-login", action="store_true", help="Disable automatic 'aws login' prompt on expired session"
    )

    for verb, desc in [("pause", "Pause task"), ("resume", "Resume task"), ("cancel", "Cancel task")]:
        p_task = subparsers.add_parser(verb, help=desc, add_help=True)
        p_task.add_argument("gid", help="Task GID")
        p_task.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")

    p_start = subparsers.add_parser("start", help="Start cloud VPS", add_help=True)
    p_start.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider (default: aws)")
    p_start.add_argument(
        "--no-auto-login", action="store_true", help="Disable automatic 'aws login' prompt on expired session"
    )

    p_stop = subparsers.add_parser("stop", help="Stop cloud VPS", add_help=True)
    p_stop.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider (default: aws)")
    p_stop.add_argument(
        "--no-auto-login", action="store_true", help="Disable automatic 'aws login' prompt on expired session"
    )

    p_wd = subparsers.add_parser("watchdog", help="Check auto-shutdown watchdog", add_help=True)
    p_wd.add_argument("--cloud", "-c", choices=["aws", "oci"], help="Target cloud provider")

    p_init = subparsers.add_parser("init", help="Create or update user config.json", add_help=True)
    p_init.add_argument("--instance-id", help="AWS EC2 instance ID")
    p_init.add_argument("--region", help="AWS region")
    p_init.add_argument("--local-dir", help="Preferred local sync directory")
    p_init.add_argument("--keep-secret", action="store_true", help="Preserve existing RPC secret instead of rotating")
    p_init.add_argument("--non-interactive", action="store_true", help="Write config without interactive prompts")

    subparsers.add_parser("doctor", help="Run system health diagnostics", add_help=True)

    return parser


def main() -> None:
    signal.signal(signal.SIGINT, _sigint_handler)
    get_cfg()
    parser = build_parser()

    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ["-h", "--help"]):
        print_custom_help()
        sys.exit(0)

    args = parser.parse_args()

    if getattr(args, "help", False) and not args.command:
        print_custom_help()
        sys.exit(0)

    dispatch = {
        "add": cmd_add,
        "status": cmd_status,
        "top": cmd_top,
        "list": cmd_list,
        "sync": cmd_sync,
        "clean": cmd_clean,
        "limit": cmd_limit,
        "extract": cmd_extract,
        "test": cmd_test,
        "speed": cmd_speed,
        "cost": cmd_cost,
        "browse": cmd_browse,
        "ui": cmd_ui,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "cancel": cmd_cancel,
        "start": cmd_start,
        "stop": cmd_stop,
        "watchdog": cmd_watchdog,
        "init": cmd_init,
        "doctor": cmd_doctor,
    }

    handler = dispatch.get(args.command)
    if not handler:
        print_custom_help()
        sys.exit(2)

    try:
        handler(args)
    except CloudDlError as exc:
        log_err(str(exc))
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        sys.stderr.write("\n[INFO] Operation canceled by user.\n")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "debug", False):
            traceback.print_exc()
        else:
            log_err(f"Unexpected error: {exc} (re-run with --debug for traceback)")
        sys.exit(1)
