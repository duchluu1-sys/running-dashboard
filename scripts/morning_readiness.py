#!/usr/bin/env python3
"""
Running OS — Morning Readiness
Pulls Garmin sleep/HRV/RHR, computes readiness, appends to READINESS_LOG in data.json.
Runs daily at 06:00 Hanoi time via GitHub Actions.
"""

import json
import os
import sys
import base64
import datetime
import requests

# ── Config ────────────────────────────────────────────────────────────────────

REPO          = "duchluu1-sys/running-dashboard"
FILE          = "data.json"
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GARMIN_EMAIL  = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]

HRV_LOW  = 53   # baseline lower bound
HRV_HIGH = 68   # baseline upper bound

# ── Garmin ────────────────────────────────────────────────────────────────────

def pull_garmin(date_str):
    """Returns (hrv, hrv_status, rhr, sleep_score) — any may be None on failure."""
    try:
        from garminconnect import Garmin
        api = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        api.login()

        sleep_raw = api.get_sleep_data(date_str)
        dto = sleep_raw.get("dailySleepDTO", {})

        hrv         = sleep_raw.get("avgOvernightHrv")
        hrv_status  = sleep_raw.get("hrvStatus", "UNKNOWN")
        rhr         = sleep_raw.get("restingHeartRate")
        sleep_score = dto.get("sleepScores", {}).get("overall", {}).get("value")

        # Cast to int where expected
        hrv         = int(hrv)         if hrv         is not None else None
        rhr         = int(rhr)         if rhr         is not None else None
        sleep_score = int(sleep_score) if sleep_score is not None else None

        return hrv, hrv_status, rhr, sleep_score

    except Exception as e:
        print(f"⚠️  Garmin pull failed: {e}", file=sys.stderr)
        return None, None, None, None


# ── Readiness logic ───────────────────────────────────────────────────────────

def compute_status(hrv, hrv_status, rhr, sleep_score, rhr_7d_avg, prev_hrv_below):
    """
    Exact table from CLAUDE.md / training_kb.md.
    Returns "GREEN", "CAUTION", or "REST".
    """
    caution = 0

    # HRV: 2+ consecutive days below baseline → REST
    hrv_below = hrv is not None and hrv < HRV_LOW
    if hrv_below and prev_hrv_below:
        return "REST"

    # RHR delta
    if rhr is not None and rhr_7d_avg is not None:
        delta = rhr - rhr_7d_avg
        if delta >= 8:
            return "REST"
        if delta >= 5:
            caution += 1

    # Sleep score
    if sleep_score is not None:
        if sleep_score < 45:
            return "REST"
        if sleep_score < 60:
            caution += 1

    # HRV single day below
    if hrv_below:
        caution += 1

    # Two caution signals → CAUTION regardless
    if caution >= 2:
        return "CAUTION"
    if caution == 1:
        return "CAUTION"

    # GREEN bands
    rhr_ok = (rhr is None or rhr_7d_avg is None or (rhr - rhr_7d_avg) <= 4)
    balanced = (hrv_status == "BALANCED")

    if sleep_score is not None and sleep_score >= 60 and balanced and rhr_ok:
        return "GREEN"

    # Fallback
    return "CAUTION"


# ── GitHub read/write ─────────────────────────────────────────────────────────

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

def gh_read():
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    meta = r.json()
    content = json.loads(base64.b64decode(meta["content"]).decode())
    return content, meta["sha"]


def gh_write(content, sha, message):
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
    encoded = base64.b64encode(
        json.dumps(content, indent=2, ensure_ascii=False).encode()
    ).decode()
    payload = {"message": message, "content": encoded, "sha": sha}
    r = requests.put(url, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()["content"]["sha"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Use Hanoi time (UTC+7) — cron fires at 23:00 UTC = 06:00 Hanoi next day
    hanoi_offset = datetime.timezone(datetime.timedelta(hours=7))
    today = datetime.datetime.now(hanoi_offset).date()
    today_str = today.isoformat()
    today_label = today.strftime("%b %-d")

    print(f"Running readiness check for {today_str} (Hanoi)")

    # Pull Garmin
    hrv, hrv_status, rhr, sleep_score = pull_garmin(today_str)
    print(f"  HRV: {hrv} ms ({hrv_status}) | RHR: {rhr} | Sleep: {sleep_score}")

    # Pull data.json
    data, sha = gh_read()
    log = data.setdefault("READINESS_LOG", [])

    # 7-day rolling RHR average from log
    rhr_values = [e["rhr"] for e in log[-7:] if e.get("rhr") is not None]
    rhr_7d_avg = round(sum(rhr_values) / len(rhr_values), 1) if rhr_values else None

    # Was yesterday HRV below baseline?
    prev_hrv_below = (
        len(log) > 0
        and log[-1].get("hrv") is not None
        and log[-1]["hrv"] < HRV_LOW
    )

    # Compute status
    status = compute_status(hrv, hrv_status, rhr, sleep_score, rhr_7d_avg, prev_hrv_below)
    print(f"  Status: {status}")

    # Build note (≤10 words)
    flags = []
    if hrv is not None and hrv < HRV_LOW:
        flags.append(f"HRV {hrv} below baseline")
    if rhr is not None and rhr_7d_avg is not None and (rhr - rhr_7d_avg) >= 5:
        flags.append(f"RHR +{int(rhr - rhr_7d_avg)} above 7d avg")
    if sleep_score is not None and sleep_score < 60:
        flags.append(f"Sleep {sleep_score}")
    note = "; ".join(flags) if flags else "All signals clear"

    # Build briefing (dashboard Today tab first element)
    hrv_str    = f"HRV {hrv} ms" if hrv else "HRV unavailable"
    rhr_str    = f"RHR {rhr} (7d avg {rhr_7d_avg})" if rhr and rhr_7d_avg else f"RHR {rhr or '–'}"
    sleep_str  = f"Sleep {sleep_score}" if sleep_score else "Sleep unavailable"

    # Last run from RUNS array
    runs = data.get("RUNS", [])
    last_run = runs[-1] if runs else None
    last_run_str = (
        f"Last: R{last_run['r']} — {last_run['type']} {last_run['dist']}km ({last_run['date']})."
        if last_run else "No runs logged yet."
    )

    briefing = (
        f"{hrv_str} · {rhr_str} · {sleep_str}. "
        f"Status: {status}. "
        f"{last_run_str} "
        f"Check coaching layer for today's session prescription."
    )

    # Build entry
    entry = {
        "date":       today_str,
        "hrv":        hrv,
        "rhr":        rhr,
        "sleepScore": sleep_score,
        "status":     status,
        "note":       note,
        "briefing":   briefing,
    }

    # Append (never overwrite)
    log.append(entry)

    # Commit
    commit_msg = f"Readiness {today_label}"
    new_sha = gh_write(data, sha, commit_msg)

    print(f"\n✅ Committed. Status: {status} | SHA: {new_sha}")
    print(json.dumps(entry, indent=2))


if __name__ == "__main__":
    main()
