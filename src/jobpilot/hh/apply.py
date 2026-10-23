from __future__ import annotations

import time
from enum import Enum
from pathlib import Path


class ApplyResult(str, Enum):
    APPLIED = "applied"
    ALREADY = "already"
    NEEDS_TEST = "needs_test"
    CAPTCHA = "captcha"
    LIMIT = "limit"
    FAILED = "failed"


class CaptchaError(Exception):
    """hh asked for a captcha: stop touching hh with this session."""


def prune_screenshots(directory: Path, max_age_hours: float = 24) -> int:
    """Delete captcha pictures older than `max_age_hours`; the server disk is small."""
    if not directory.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for path in directory.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
