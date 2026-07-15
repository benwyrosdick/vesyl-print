"""Collect basic system information for display."""

from __future__ import annotations

import glob
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


def cpu_temp_c() -> str:
    """CPU temperature in °C, or a short fallback if unavailable.

    Prefers the first readable sysfs thermal zone (millidegrees C). On
    Raspberry Pi, falls back to `vcgencmd measure_temp` when sysfs is empty.
    """
    for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            with open(path, encoding="ascii") as f:
                milli = int(f.read().strip())
            return f"{milli / 1000.0:.0f} °C"
        except (OSError, ValueError):
            continue

    # Raspberry Pi firmware path
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        # e.g. "temp=48.2'C"
        if out.startswith("temp=") and out.endswith("'C"):
            return f"{float(out[5:-2]):.0f} °C"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    return "n/a"


def now() -> datetime:
    return datetime.now()
