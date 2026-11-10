"""
coach.py - your AI running coach. Reads your Garmin data from Supabase and
asks Claude for a training review.

Weekly review:   python3 coach.py              -> saved to the coach_reports table
Ask a question:  python3 coach.py "Am I ready to race a 10K?"   -> printed here

The weekly review is saved to Supabase, NOT printed, because logs on a public
GitHub repo are public. Questions are printed, so only ask them on your own computer.

Needs ANTHROPIC_API_KEY in .env (local) or as a GitHub secret.
Create the coach_reports table once with sql/coach_reports.sql.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import anthropic
from dotenv import load_dotenv

from sync_garmin import BASE_DIR, get_supabase, log

MODEL = "claude-opus-5-5"
DETAIL_DAYS = 28      # activities and health shown day by day
TREND_WEEKS = 12      # weekly totals shown for the bigger picture

SYSTEM_PROMPT = """You are an experienced running and endurance coach reviewing an \
athlete's Garmin data. You get recent activities, weekly totals, and daily health \
data (sleep, resting heart rate, stress, Body Battery).

Coach like a good human coach would:
- Base every observation on the numbers you were given; cite them. If data is \
missing (for example HRV), say so briefly and don't guess.
- Look at load trends (sudden jumps in weekly volume or intensity), intensity \
balance (how much easy vs hard running, using HR zones), recovery signals (sleep, \
resting HR, stress, Body Battery) and how they relate to training.
- Give concrete, specific advice: what to do next week, with example sessions.
- Flag injury or overtraining risk plainly, but don't diagnose medical issues; \
suggest seeing a professional when something looks off.
- Be encouraging and direct. Keep it readable: short sections with headings, \
no filler. Show distances in both miles and km."""

WEEKLY_REQUEST = """Write my weekly training review. Today is {today}.

Use these sections:
1. **Last 7 days** - what I did and how it went
2. **Trends** - load, intensity balance and fitness over the last weeks
3. **Recovery** - what sleep, resting HR, stress and Body Battery say
4. **Next week** - a day-by-day suggested plan
5. **One thing to focus on**"""


# ---------- data ----------

def num(value, digits=1):
    return round(value, digits) if isinstance(value, (int, float)) else None


def pace(distance_m, duration_s):
    """min:sec per km and per mile, or None if it doesn't make sense."""
    if not distance_m or not duration_s or distance_m < 100:
        return None
    out = []
    for unit, meters in (("km", 1000), ("mi", 1609.34)):
        sec = duration_s / (distance_m / meters)
        out.append(f"{int(sec // 60)}:{int(sec % 60):02d}/{unit}")
    return " ".join(out)


def summarize_activity(row):
    raw = row.get("raw_json") or {}
    zones = [raw.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
    summary = {
        "date": (row["start_time"] or "")[:16].replace("T", " "),
        "type": row["activity_type"],
        "name": row["name"],
        "km": num((row["distance_m"] or 0) / 1000, 2),
        "minutes": num((row["duration_s"] or 0) / 60),
        "pace": pace(row["distance_m"], raw.get("movingDuration") or row["duration_s"]),
        "avg_hr": row["avg_hr"],
        "max_hr": row["max_hr"],
        "elevation_m": num(row["elevation_m"], 0),
        "training_load": num(raw.get("activityTrainingLoad")),
        "aerobic_te": num(raw.get("aerobicTrainingEffect")),
        "anaerobic_te": num(raw.get("anaerobicTrainingEffect")),
        "te_label": raw.get("trainingEffectLabel"),
        "vo2max": raw.get("vO2MaxValue"),
        "cadence": num(raw.get("averageRunningCadenceInStepsPerMinute"), 0),
        "hr_zone_minutes": [num((z or 0) / 60) for z in zones] if any(zones) else None,
    }
    return {k: v for k, v in summary.items() if v not in (None, "", [])}


def summarize_day(row):
    sleep = ((row.get("sleep") or {}).get("dailySleepDTO")) or {}
    stats = row.get("stats") or {}
    hrv = row.get("hrv") or {}
    hrv_summary = hrv.get("hrvSummary") or {}

    def hours(seconds):
        return num(seconds / 3600) if seconds else None

    summary = {
        "day": row["day"],
        "sleep_h": hours(sleep.get("sleepTimeSeconds")),
        "deep_h": hours(sleep.get("deepSleepSeconds")),
        "rem_h": hours(sleep.get("remSleepSeconds")),
        "awake_h": hours(sleep.get("awakeSleepSeconds")),
        "resting_hr": stats.get("restingHeartRate"),
        "avg_stress": stats.get("averageStressLevel"),
        "body_battery_wake": stats.get("bodyBatteryAtWakeTime"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "steps": stats.get("totalSteps"),
        "hrv_last_night": hrv_summary.get("lastNightAvg"),
        "hrv_status": hrv_summary.get("status"),
    }
    return {k: v for k, v in summary.items() if v is not None}


def weekly_totals(rows, first_day, last_day):
    # Start with every week in the range, so weeks with no training show up as zeros.
    weeks = {}
    monday = first_day - timedelta(days=first_day.weekday())
    while monday <= last_day:
        weeks[monday.isoformat()] = {"week_of": monday.isoformat(), "activities": 0,
                                     "run_km": 0.0, "total_minutes": 0.0, "training_load": 0.0}
        monday += timedelta(weeks=1)
    for row in rows:
        start = datetime.fromisoformat(row["start_time"]).date()
        monday = (start - timedelta(days=start.weekday())).isoformat()
        week = weeks[monday]
        raw = row.get("raw_json") or {}
        week["activities"] += 1
        week["total_minutes"] += (row["duration_s"] or 0) / 60
        week["training_load"] += raw.get("activityTrainingLoad") or 0
        if "running" in (row["activity_type"] or ""):
            week["run_km"] += (row["distance_m"] or 0) / 1000
    for week in weeks.values():
        for key in ("run_km", "total_minutes", "training_load"):
            week[key] = num(week[key])
    return [weeks[k] for k in sorted(weeks)]


def load_training_data(sb):
    today = date.today()
    trend_first_day = today - timedelta(weeks=TREND_WEEKS)
    trend_start = trend_first_day.isoformat()
    detail_start = (today - timedelta(days=DETAIL_DAYS)).isoformat()

    activities = (
        sb.table("activities")
        .select("start_time,name,activity_type,distance_m,duration_s,avg_hr,max_hr,elevation_m,raw_json")
        .gte("start_time", trend_start)
        .order("start_time")
        .execute()
        .data
    )
    health = (
        sb.table("daily_health")
        .select("day,sleep,hrv,stats")
        .gte("day", detail_start)
        .order("day")
        .execute()
        .data
    )
    recent = [a for a in activities if a["start_time"] >= detail_start]

    # Fitness context from the most recent VO2 max Garmin reported.
    vo2 = next((a["raw_json"].get("vO2MaxValue") for a in reversed(activities)
                if (a.get("raw_json") or {}).get("vO2MaxValue")), None)

    return {
        "today": today.isoformat(),
        "latest_vo2max": vo2,
        f"weekly_totals_last_{TREND_WEEKS}_weeks": weekly_totals(activities, trend_first_day, today),
        f"activities_last_{DETAIL_DAYS}_days": [summarize_activity(a) for a in recent],
        f"daily_health_last_{DETAIL_DAYS}_days": [summarize_day(d) for d in health],
    }


# ---------- Claude ----------

def ask_claude(data, request):
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "<training_data>\n"
                 + json.dumps(data, indent=1) + "\n</training_data>"},
                {"type": "text", "text": request},
            ],
        }],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer this request.")
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if message.stop_reason == "max_tokens":
        text += "\n\n_(Report was cut off - it hit the length limit.)_"
    return text, message.model


def main():
    load_dotenv(BASE_DIR / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Missing ANTHROPIC_API_KEY.")

    question = " ".join(sys.argv[1:]).strip()
    sb = get_supabase()
    data = load_training_data(sb)
    log(f"Loaded {len(data[f'activities_last_{DETAIL_DAYS}_days'])} recent activities "
        f"and {len(data[f'daily_health_last_{DETAIL_DAYS}_days'])} days of health data.")

    if question:
        answer, _ = ask_claude(data, question)
        print("\n" + answer + "\n")
        return

    report, model = ask_claude(data, WEEKLY_REQUEST.format(today=data["today"]))
    sb.table("coach_reports").upsert({
        "report_date": data["today"],
        "model": model,
        "report": report,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="report_date").execute()
    log(f"Saved weekly review for {data['today']} to coach_reports.")


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
