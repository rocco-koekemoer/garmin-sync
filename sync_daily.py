"""
sync_daily.py - saves Garmin's daily health data (sleep, HRV, daily stats)
into the daily_health table in Supabase.

Normal run:      python3 sync_daily.py        (last 3 days)
Backfill:        python3 sync_daily.py 60     (last 60 days)

Reuses the login code from sync_garmin.py, so keep both files in the same folder.
The raw Garmin data is saved as-is, so nothing is lost if Garmin renames a field.
"""

import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from sync_garmin import BASE_DIR, get_garmin_client, get_supabase, log, save_tokens

PAUSE_SECONDS = 1.5


def safe(fetch, day):
    """Return the data, or None if Garmin has nothing for that day."""
    try:
        return fetch(day)
    except Exception:
        return None


def main():
    load_dotenv(BASE_DIR / ".env")
    days = 3
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        days = int(sys.argv[1])

    sb = get_supabase()
    saved = 0
    with tempfile.TemporaryDirectory() as tmp:
        token_dir = Path(tmp)
        client = get_garmin_client(sb, token_dir)

        for offset in range(days):
            day = (date.today() - timedelta(days=offset)).isoformat()
            row = {
                "day": day,
                "sleep": safe(client.get_sleep_data, day),
                "hrv": safe(client.get_hrv_data, day),
                "stats": safe(client.get_stats, day),
            }
            sb.table("daily_health").upsert(row, on_conflict="day").execute()
            saved += 1
            time.sleep(PAUSE_SECONDS)

        save_tokens(sb, token_dir)

    log(f"Saved daily health data for {saved} days.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        if os.getenv("DEBUG"):
            log(f"ERROR: {error!r}")
        else:
            log(f"ERROR: {type(error).__name__} (run with DEBUG=1 locally for details)")
        sys.exit(1)
