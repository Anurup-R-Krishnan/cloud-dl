"""Domain exception hierarchy for cloud-dl."""

from __future__ import annotations


class CloudDlError(Exception):
    """Base error raised by cloud-dl subsystems with an explicit exit status."""

    exit_code: int = 1


class UsageError(CloudDlError):
    """Invalid user arguments or CLI invocation."""

    exit_code: int = 2


class ConfigError(CloudDlError):
    """Missing or malformed user configuration."""


class AuthError(CloudDlError):
    """Cloud CLI authentication expired or rejected."""


class RpcError(CloudDlError):
    """Aria2 JSON-RPC communication or protocol failure."""


class SyncError(CloudDlError):
    """File synchronization, mount, or checksum verification failure."""
