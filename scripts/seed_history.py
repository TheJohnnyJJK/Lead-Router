"""Backfills realistic history so the ROI dashboard has something to plot.

A brand-new agent with zero history proves nothing - the whole point of
project #01 is a *sustained* before/after, not a day-one demo number (see
the "ship it where people already are" section of the portfolio report).
This script simulates DAYS worth of inbound traffic at a plausible SMB
volume, spread across business hours, calling the exact same graph a real
webhook would hit - no shortcuts, no separate code path.

    python -m scripts.seed_history            # 60 days, resets the DB first
    python -m scripts.seed_history --days 30 --keep
"""
from __future__ import annotations

import argparse
import datetime
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Same reasoning as data/synthetic_leads.py: make sure the project root is
# importable regardless of where this script is invoked from, before the
# imports below that depend on it.
sys.path.insert(0, str(ROOT))

from agent import store  # noqa: E402
from agent.graph import run_lead  # noqa: E402
from data.synthetic_leads import generate_lead  # noqa: E402

BUSINESS_HOUR_START = 8
BUSINESS_HOUR_END = 18


def business_hours_timestamp(day: datetime.date, rng: random.Random) -> str:
    """A random moment between 8am and 6pm on the given calendar day, as an
    ISO timestamp. Real inbound leads cluster in business hours even for a
    web form that's technically open 24/7 - this keeps the seeded history
    looking like real traffic instead of uniformly random noise."""
    hour = rng.randint(BUSINESS_HOUR_START, BUSINESS_HOUR_END - 1)
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    dt = datetime.datetime.combine(day, datetime.time(hour, minute, second))
    return dt.isoformat()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--min-per-day", type=int, default=14)
    parser.add_argument("--max-per-day", type=int, default=38)
    # Fixed default seed (not a random one) so re-running this script with
    # the same flags reproduces the exact same history - useful for
    # comparing dashboard output before/after a code change.
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--keep", action="store_true", help="don't wipe existing data first")
    args = parser.parse_args()

    if not args.keep:
        store.reset_db()
    else:
        store.init_db()

    rng = random.Random(args.seed)
    today = datetime.date.today()
    total = 0

    # Walk backwards from `days` days ago up to (but not including) today,
    # one calendar day at a time, generating a plausible day's worth of
    # leads for each. `offset` counts down so the oldest day is seeded
    # first and the progress messages below read as "day N of days".
    for offset in range(args.days, 0, -1):
        day = today - datetime.timedelta(days=offset)
        if day.weekday() >= 5:  # Saturday/Sunday: light trickle, not zero (web forms don't sleep)
            n_leads = rng.randint(1, 5)
        else:
            n_leads = rng.randint(args.min_per_day, args.max_per_day)

        for _ in range(n_leads):
            lead = generate_lead(rng)
            created_at = business_hours_timestamp(day, rng)
            # Goes through the real graph (agent/graph.py's run_lead), the
            # same path a live n8n webhook call takes - seeding never uses a
            # shortcut that a real request wouldn't, so the resulting
            # history is honest, not just plausible-looking.
            run_lead(lead, created_at=created_at)
            total += 1

        if offset % 10 == 0 or offset == 1:
            print(f"...{args.days - offset + 1}/{args.days} days seeded ({total} leads so far)")

    print(f"\nSeeded {total} leads across {args.days} days into {store.DB_PATH}")
    print("Run `python -m dashboard.generate_dashboard` to build the ROI report.")


if __name__ == "__main__":
    main()
