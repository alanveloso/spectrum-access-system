#!/usr/bin/env bash
# Block until wall-clock is inside 02:00–04:00 America/Los_Angeles (or fail).
# Used by scheduled FDB.8; does not fake container/system time.
set -euo pipefail

MAX_WAIT_MINUTES="${1:-120}"

python3 - "$MAX_WAIT_MINUTES" <<'PY'
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

max_wait = int(sys.argv[1])
tz = ZoneInfo("America/Los_Angeles")
deadline = datetime.now(tz) + timedelta(minutes=max_wait)


def in_window(now: datetime) -> bool:
    return 2 <= now.hour < 4


while True:
    now = datetime.now(tz)
    if in_window(now):
        print(f"in_window={now.isoformat()}")
        raise SystemExit(0)
    if now >= deadline:
        print(f"window_missed now={now.isoformat()} deadline={deadline.isoformat()}", file=sys.stderr)
        raise SystemExit(1)
    target = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if now >= target:
        # Past today's window start but not inside (hour>=4) → tomorrow 02:00.
        if now.hour >= 4:
            target = target + timedelta(days=1)
        else:
            target = now + timedelta(seconds=30)
    wait = int(min(60, max(1, (target - now).total_seconds())))
    print(f"waiting_for_la_window now={now.isoformat()} next_sleep_s={wait}")
    time.sleep(wait)
PY
