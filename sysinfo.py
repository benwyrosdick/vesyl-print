"""Collect basic system information for display."""

from __future__ import annotations

import socket
import subprocess
from datetime import datetime


def hostname() -> str:
    return socket.gethostname()


def ip_addresses() -> list[str]:
    """Return non-loopback IPv4 addresses currently assigned to the host."""
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=2
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""

    addrs = []
    for token in out.split():
        # keep IPv4 only (skip IPv6 which contains ':')
        if ":" not in token and token != "127.0.0.1":
            addrs.append(token)
    return addrs


def primary_ip() -> str:
    addrs = ip_addresses()
    return addrs[0] if addrs else "no network"


def now() -> datetime:
    return datetime.now()


def booting_units(limit: int = 3) -> list[str]:
    """Units systemd is actively starting right now, newest first.

    Parsed from `systemctl list-jobs`; the 'running' state means the unit's
    start job is in progress (vs. 'waiting' which is queued behind ordering).
    Returns friendly names with the '.service' suffix stripped.
    """
    try:
        out = subprocess.run(
            ["systemctl", "list-jobs", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    units = []
    for line in out.splitlines():
        parts = line.split()
        # columns: JOB UNIT TYPE STATE
        if len(parts) >= 4 and parts[2] == "start" and parts[3] == "running":
            name = parts[1]
            if name.endswith(".service"):
                name = name[: -len(".service")]
            units.append(name)
    return units[:limit]
