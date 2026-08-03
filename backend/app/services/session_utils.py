"""
Trading session / kill-zone helpers.

Single source of truth for session boundaries — shared by scheduler.py
(job gating) and smc_engine / snapshot_builder (kill_zone context in the
Claude prompt). UTC-based.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

KillZones = {
    "ASIA":         (0,  4),   # 00:00–04:00 UTC
    "LONDON_OPEN":  (7,  10),  # 07:00–10:00 UTC  ← highest confluence
    "NY_AM":        (13, 16),  # 13:00–16:00 UTC  ← highest confluence
    "NY_PM":        (17, 20),  # 17:00–20:00 UTC
}

ACTIVE_SESSIONS = ("LONDON_OPEN", "NY_AM", "NY_PM")


def current_session(now: Optional[datetime] = None) -> str:
    """Return the name of the current trading session (UTC hour)."""
    h = (now or datetime.now(timezone.utc)).hour
    for name, (start, end) in KillZones.items():
        if start <= h < end:
            return name
    return "DEAD"


def is_active_session(now: Optional[datetime] = None) -> bool:
    return current_session(now) in ACTIVE_SESSIONS
