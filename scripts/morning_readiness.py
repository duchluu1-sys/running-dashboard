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
  - ALL concurrent signals are logged — if REST fires from sleep score AND RHR
    is CAUTION-level, both appear in note and briefing. The note describes
    reality, not just the worst single signal.
  - 7-day RHR average uses calendar days (entries within 7 days of today),
    not the last 7 log entries. Requires RHR_MIN_DAYS readings; otherwise the
    delta is undefined and cannot contribute to the verdict.
  - prev_hrv_below uses calendar consecutiveness: the prior entry must be
    dated yesterday. A gap in the log breaks the chain.
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

# Minimum RHR readings within the 7-day calendar window before computing a
# delta. Fewer readings produce false precision and misleading CAUTION flags.
RHR_MIN_DAYS = 3

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

    All signals are evaluated before the verdict is issued. REST-level and
    CAUTION-level signals are collected independently; the final reasons list
    contains all of them so note and briefing reflect the full picture.
    Most restrictive condition wins.
    Caller must guarantee at least one of hrv / rhr / sleep_score is present.
    """
    hrv_below  = hrv is not None and hrv < HRV_LOW
    rhr_delta  = (rhr - rhr_7d_avg) if (rhr is not None and rhr_7d_avg is not None) else None

    rest_flags   = []
    caution_flags = []

    # ── Classify every signal independently ──

    # HRV
    if hrv_below and prev_hrv_below:
        rest_flags.append(f"HRV {hrv} below baseline 2+ consecutive days")
    elif hrv_below:
        caution_flags.append(f"HRV {hrv} below baseline ({HRV_LOW})")

    # RHR delta (only available once RHR_MIN_DAYS readings exist)
    if rhr_delta is not None:
        if rhr_delta >= 8:
            rest_flags.append(f"RHR +{rhr_delta:.0f} above 7d avg ({rhr_7d_avg})")
        elif rhr_delta >= 5:
            caution_flags.append(f"RHR +{rhr_delta:.0f} above 7d avg ({rhr_7d_avg})")

    # Sleep score
    if sleep_score is not None:
        if sleep_score < 45:
            rest_flags.append(f"Sleep score {sleep_score}")
        elif sleep_score < 60:
            caution_flags.append(f"Sleep score {sleep_score}")

    # ── Verdict: most restrictive wins; all concurrent flags logged ──
    all_flags = rest_flags + caution_flags

    if rest_flags:
        return "REST", all_flags
    if caution_flags:
        return "CAUTION", all_flags

    # ── GREEN ──
    have = [n for n, v in (("HRV", hrv), ("RHR", rhr), ("sleep", sleep_score)) if v is not None]
    if len(have) == 3:
        return "GREEN", ["All signals nominal"]
    else:
        return "GREEN", [f"Nominal on {'/'.join(have)} — partial data"]


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
    today         = datetime.datetime.now(HANOI).date()
    today_str     = today.isoformat()
    yesterday_str = (today - datetime.timedelta(days=1)).isoformat()
    cutoff_str    = (today - datetime.timedelta(days=7)).isoformat()
    today_label   = today.strftime("%b ") + str(today.day)   # portable; avoids %-d

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

    # ── Prior entries (excludes today to avoid self-referential computation) ──
    prior = [e for e in log if e.get("date") != today_str]

    # ── 7-day RHR average: calendar window, minimum RHR_MIN_DAYS readings ──
    # Filter to entries within the last 7 calendar days (ISO string comparison
    # is safe because dates are zero-padded). Require RHR_MIN_DAYS readings;
    # fewer produce false precision.
    recent_prior = [e for e in prior if e.get("date", "") > cutoff_str]
    rhr_values   = [e["rhr"] for e in recent_prior if e.get("rhr") is not None]
    rhr_7d_avg   = (
        round(sum(rhr_values) / len(rhr_values), 1)
        if len(rhr_values) >= RHR_MIN_DAYS
        else None
    )
    if rhr_7d_avg is None:
        print(f"  RHR 7d avg: insufficient data "
              f"({len(rhr_values)}/{RHR_MIN_DAYS} readings in window) — delta undefined")

    # ── prev_hrv_below: calendar-consecutive only ──
    # The prior entry must be dated *yesterday*. A gap in the log — from a
    # missed Garmin pull, a travel day, anything — breaks the chain. An entry
    # from 3 days ago with low HRV does not constitute a consecutive streak.
    prev_entry = prior[-1] if prior else None
    prev_hrv_below = bool(
        prev_entry is not None
        and prev_entry.get("date") == yesterday_str
        and prev_entry.get("hrv") is not None
        and prev_entry["hrv"] < HRV_LOW
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
    sched   = (data.get("ATHLETE", {}) or {}).get("weekSchedule", {}) or {}
    dow     = today.strftime("%a")
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
