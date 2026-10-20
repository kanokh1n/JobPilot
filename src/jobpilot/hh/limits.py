from __future__ import annotations

import random
from datetime import datetime

from .models import Account


def daily_limit(account: Account, first_day: datetime, now: datetime) -> int:
    """Start low and grow by ramp_step each day up to max_limit."""
    days = max((now.date() - first_day.date()).days, 0)
    return min(account.start_limit + account.ramp_step * days, account.max_limit)


def in_working_hours(now: datetime, start_hour: int = 9, end_hour: int = 21) -> bool:
    return start_hour <= now.hour < end_hour


def can_apply(now: datetime, applied_today: int, limit: int) -> bool:
    return in_working_hours(now) and applied_today < limit


def pause_seconds(rng: random.Random | None = None) -> float:
    """Human-like gap between two applications."""
    return (rng or random).uniform(60, 240)
