"""
sync_garmin.py  (version 3 - Supabase)

Normal run:   python3 sync_garmin.py          -> saves only NEW activities (fast)
Full backfill: python3 sync_garmin.py --full  -> saves your whole history

Activities are saved to Supabase. Your Garmin login token is also stored in
Supabase, so runs on GitHub (which forgets everything between runs) can reuse
it instead of logging in with your password every time.

Logs are kept short on purpose, because logs on a public GitHub repo are public.
Set DEBUG=1 to see full error details when running on your own computer.
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin
from supabase import create_client

BASE_DIR = Path(__file__).resolve().parent
FULL_BATCH = 100        # batch size for a full backfill
SYNC_BATCH = 20         # batch size for a normal check for new activities
PAUSE_SECONDS = 1.5     # pause between requests to Garmin
TOKEN_ROW_ID = 1


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("Missing SUPABASE_URL or SUPABASE_KEY.")
    return create_client(url, key)


def restore_tokens(sb, token_dir):
    """Copy the saved Garmin login token from Supabase into a folder."""
    result = (
        sb.table("garmin_tokens")
        .select("token_files")
        .eq("id", TOKEN_ROW_ID)
        .execute()
    )
    if result.data:
        for name, content in result.data[0]["token_files"].items():
            (token_dir / name).write_text(content)


def save_tokens(sb, token_dir):
    """Save the current Garmin login token from the folder back to Supabase."""
    files = {p.name: p.read_text() for p in token_dir.iterdir() if p.is_file()}
    if files:
        sb.table("garmin_tokens").upsert(
            {
                "id": TOKEN_ROW_ID,
                "token_files": files,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()


def get_garmin_client(sb, token_dir):
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise SystemExit("Missing GARMIN_EMAIL or GARMIN_PASSWORD.")

    restore_tokens(sb, token_dir)
    client = Garmin(email, password)
    # Uses the saved token if there is one; otherwise logs in with your
    # password once and writes a token into the folder.
    client.login(str(token_dir))
    save_tokens(sb, token_dir)
    return client


def to_row(a):
    return {
        "activity_id": a.get("activityId"),
        "name": a.get("activityName"),
        "start_time": a.get("startTimeLocal"),
        "activity_type": (a.get("activityType") or {}).get("typeKey"),
        "distance_m": a.get("distance"),
        "duration_s": a.get("duration"),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "elevation_m": a.get("elevationGain"),
        "avg_speed_mps": a.get("averageSpeed"),
        "calories": a.get("calories"),
        "source": "garmin",
        "raw_json": a,
    }


def upsert(sb, activities):
    rows = [to_row(a) for a in activities]
    sb.table("activities").upsert(rows, on_conflict="activity_id").execute()


def run_full(sb, client):
    start = 0
    total = 0
    while True:
        batch = client.get_activities(start, FULL_BATCH)
        if not batch:
            break
        upsert(sb, batch)
        total += len(batch)
        start += FULL_BATCH
        log(f"Saved {total} activities so far...")
        time.sleep(PAUSE_SECONDS)
    log(f"Done. {total} activities are in Supabase.")


def run_sync(sb, client):
    start = 0
    new_total = 0
    while True:
        batch = client.get_activities(start, SYNC_BATCH)
        if not batch:
            break

        ids = [a["activityId"] for a in batch]
        existing = (
            sb.table("activities")
            .select("activity_id")
            .in_("activity_id", ids)
            .execute()
        )
        existing_ids = {row["activity_id"] for row in existing.data}
        new_count = len([i for i in ids if i not in existing_ids])

        upsert(sb, batch)  # also refreshes any recently edited activities
        new_total += new_count

        # If some of this batch were already saved, we've caught up.
        if new_count < len(batch):
            break
        start += SYNC_BATCH
        time.sleep(PAUSE_SECONDS)

    if new_total:
        log(f"Saved {new_total} new activities.")
    else:
        log("No new activities.")


def main():
    load_dotenv(BASE_DIR / ".env")
    full = "--full" in sys.argv

    sb = get_supabase()
    with tempfile.TemporaryDirectory() as tmp:
        token_dir = Path(tmp)
        client = get_garmin_client(sb, token_dir)
        if full:
            run_full(sb, client)
        else:
            run_sync(sb, client)
        save_tokens(sb, token_dir)  # keep any refreshed token


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