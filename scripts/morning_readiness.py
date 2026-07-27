#!/usr/bin/env python3
"""
Running OS — Morning Readiness
Pulls Garmin sleep/HRV/RHR, computes readiness, appends to READINESS_LOG in data.json.

Runs twice daily via GitHub Actions (06:23 + 07:47 Hanoi). The second run is a
backup: it exits without writing if a usable entry for today already exists.

Design rules:
  - NEVER write a fabricated verdict. If Garmin gives us nothing, exit non-zero
    and leave the log alone. A missing entry is honest; a false CAUTION is not.
  - NEVER write two entries for the same date. Duplicates corrupt the 7-day
    rolling RHR average, which corrupts every subsequent day's delta.
  - Readiness is derived from numeric HRV against the baseline, not from
    Garmin's hrvStatus string, which is not reliably present in the sleep payload.
"""

import json
import os
import sys
import base64
import datetime
import requests

# ── Config ────────────────────────────────────────────────────────────────────

REPO            = "duchluu1-sys/running-dashboard"
FILE            = "data.json"
GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GARMIN_EMAIL    = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]

# Manual dispatch overwrites today's entry instead of skipping
FORCE_WRITE = os.environ.get("FORCE_WRITE", "false").lower() == "true"

HRV_LOW  = 53   # baseline lower bound
HRV_HIGH = 68   # baseline upper bound

HANOI = datetime.timezone(datetime.timedelta(hours=7))


# ── Garmin ────────────────────────────────────────────────────────────────────

def pull_garmin(date_str):
    """
    Returns dict with hrv / rhr / sleep_score / hrv_status.
    Values are None where unavailable. hrv_status is best-effort only and is
    NOT used for the verdict.
    """
    out = {"hrv": None, "rhr": None, "sleep_score": None, "hrv_status": None}
    try:
        from garminconnect import Garmin
        api = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        api.login()

        sleep_raw = api.get_sleep_data(date_str) or {}
        dto = sleep_raw.get("dailySleepDTO", {}) or {}

        hrv         = sleep_raw.get("avgOvernightHrv")
        rhr         = sleep_raw.get("restingHeartRate")
        sleep_score = (dto.get("sleepScores", {}) or {}).get("overall", {}).get("value")

        out["hrv"]         = int(hrv)         if hrv         is not None else None
        out["rhr"]         = int(rhr)         if rhr         is not None else None
        out["sleep_score"] = int(sleep_score) if sleep_score is not None else None

        # hrvStatus is not reliably in the sleep payload. Try the dedicated
        # endpoint, but treat total absence as non-fatal — the verdict does
        # not depend on it.
        try:
            hrv_data = api.get_hrv_data(date_str) or {}
            summary  = hrv_data.get("hrvSummary", {}) or {}
            out["hrv_status"] = summary.get("status")
            if out["hrv"] is None and summary.get("lastNightAvg") is not None:
                out["hrv"] = int(summary["lastNightAvg"])
        except Exception as e:
            print(f"   note: hrv endpoint unavailable ({e})", file=sys.stderr)

    except Exception as e:
        print(f"⚠️  Garmin pull failed: {e}", file=sys.stderr)

    return out


# ── Readiness logic ───────────────────────────────────────────────────────────

def compute_status(hrv, rhr, sleep_score, rhr_7d_avg, prev_hrv_below):
    """
    Decision table from CLAUDE.md / training_kb.md.
    Returns (status, reasons[]).

    Derived from numeric values only. Most restrictive condition wins.
    Caller must guarantee at least one of hrv / rhr / sleep_score is present.
    """
    reasons = []
    caution = 0

    hrv_below = hrv is not None and hrv < HRV_LOW

    # ── REST conditions ──
    if hrv_below and prev_hrv_below:
        return "REST", [f"HRV {hrv} below baseline 2+ consecutive days"]

    if rhr is not None and rhr_7d_avg is not None:
        delta = rhr - rhr_7d_avg
        if delta >= 8:
            return "REST", [f"RHR +{delta:.0f} above 7d avg"]

    if sleep_score is not None and sleep_score < 45:
        return "REST", [f"Sleep score {sleep_score}"]

    # ── CAUTION signals ──
    if hrv_below:
        caution += 1
        reasons.append(f"HRV {hrv} below baseline ({HRV_LOW})")

    if rhr is not None and rhr_7d_avg is not None:
        delta = rhr - rhr_7d_avg
        if 5 <= delta < 8:
            caution += 1
            reasons.append(f"RHR +{delta:.0f} above 7d avg")

    if sleep_score is not None and 45 <= sleep_score < 60:
        caution += 1
        reasons.append(f"Sleep score {sleep_score}")

    if caution >= 1:
        return "CAUTION", reasons

    # ── GREEN ──
    # No caution signals fired. Note whether this is a full-confidence green
    # or a green computed from partial data.
    have = [n for n, v in (("HRV", hrv), ("RHR", rhr), ("sleep", sleep_score)) if v is not None]
    if len(have) == 3:
        reasons.append("All signals nominal")
    else:
        reasons.append(f"Nominal on {'/'.join(have)} — partial data")
    return "GREEN", reasons


# ── GitHub read/write ─────────────────────────────────────────────────────────

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

def gh_read():
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
    r = requests.get(url, headers=HEADERS, timeout=30)
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
    r = requests.put(url, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["content"]["sha"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today       = datetime.datetime.now(HANOI).date()
    today_str   = today.isoformat()
    today_label = today.strftime("%b ") + str(today.day)   # portable; avoids %-d

    print(f"Readiness check — {today_str} (Hanoi)")

    # ── Read current state first, so we can bail before hitting Garmin ──
    data, sha = gh_read()
    log = data.setdefault("READINESS_LOG", [])

    existing_idx = next(
        (i for i, e in enumerate(log) if e.get("date") == today_str), None
    )
    if existing_idx is not None and not FORCE_WRITE:
        print(f"✓ Entry for {today_str} already exists (status "
              f"{log[existing_idx].get('status')}). Backup run — nothing to do.")
        return 0

    # ── Pull Garmin ──
    g = pull_garmin(today_str)
    hrv, rhr, sleep_score = g["hrv"], g["rhr"], g["sleep_score"]
    print(f"  HRV: {hrv} ms | RHR: {rhr} | Sleep: {sleep_score} "
          f"| hrvStatus: {g['hrv_status']}")

    # ── HARD STOP: never fabricate a verdict from nothing ──
    if hrv is None and rhr is None and sleep_score is None:
        print("\n✗ Garmin returned no usable metrics. NOT writing an entry.",
              file=sys.stderr)
        print("  A missing entry is honest. A fabricated CAUTION is not.",
              file=sys.stderr)
        print("  The dashboard staleness badge will flag this correctly.",
              file=sys.stderr)
        return 1

    # ── 7-day rolling RHR, excluding today ──
    prior = [e for e in log if e.get("date") != today_str]
    rhr_values = [e["rhr"] for e in prior[-7:] if e.get("rhr") is not None]
    rhr_7d_avg = round(sum(rhr_values) / len(rhr_values), 1) if rhr_values else None

    # ── Was the most recent prior entry below baseline? ──
    prev_hrv_below = bool(
        prior
        and prior[-1].get("hrv") is not None
        and prior[-1]["hrv"] < HRV_LOW
    )

    status, reasons = compute_status(hrv, rhr, sleep_score, rhr_7d_avg, prev_hrv_below)
    print(f"  Status: {status} — {'; '.join(reasons)}")

    note = "; ".join(reasons)[:80]

    # ── Briefing ──
    parts = []
    if hrv is not None:
        parts.append(f"HRV {hrv}ms")
    if rhr is not None:
        parts.append(f"RHR {rhr}" + (f" (7d {rhr_7d_avg})" if rhr_7d_avg else ""))
    if sleep_score is not None:
        parts.append(f"Sleep {sleep_score}")
    vitals = " · ".join(parts) if parts else "vitals unavailable"

    missing = [n for n, v in (("HRV", hrv), ("RHR", rhr), ("sleep", sleep_score)) if v is None]
    gap = f" [{'/'.join(missing)} unavailable]" if missing else ""

    runs = data.get("RUNS", [])
    last_run = runs[-1] if runs else None
    last_run_str = (
        f"Last: R{last_run['r']} {last_run['type']} {last_run['dist']}km ({last_run['date']})."
        if last_run else "No runs logged."
    )

    # Today's scheduled session from weekSchedule
    sched = (data.get("ATHLETE", {}) or {}).get("weekSchedule", {}) or {}
    dow = today.strftime("%a")
    session = sched.get(dow, "unknown")
    if status == "REST":
        session_str = f"Scheduled: {session} — SUPPRESSED, rest."
    elif status == "CAUTION" and session.startswith("quality"):
        session_str = f"Scheduled: {session} — DOWNGRADE to easy."
    else:
        session_str = f"Scheduled: {session}."

    briefing = (
        f"{vitals}{gap} — {status}. "
        f"{'; '.join(reasons)}. "
        f"{session_str} {last_run_str}"
    )

    entry = {
        "date":       today_str,
        "hrv":        hrv,
        "rhr":        rhr,
        "sleepScore": sleep_score,
        "status":     status,
        "note":       note,
        "briefing":   briefing,
    }

    # ── Append, or replace on forced manual re-run. Never duplicate a date. ──
    if existing_idx is not None:
        print(f"  FORCE_WRITE — replacing existing {today_str} entry")
        log[existing_idx] = entry
        msg = f"Readiness {today_label} (re-run)"
    else:
        log.append(entry)
        msg = f"Readiness {today_label}"

    new_sha = gh_write(data, sha, msg)
    print(f"\n✅ Committed. Status: {status} | SHA: {new_sha[:12]}")
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
