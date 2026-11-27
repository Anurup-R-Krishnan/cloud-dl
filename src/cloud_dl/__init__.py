"""cloud-dl: Personal Multi-Cloud Download & Torrent Engine."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    VERSION = version("cloud-dl")
except PackageNotFoundError:
    VERSION = "2.5.0"
