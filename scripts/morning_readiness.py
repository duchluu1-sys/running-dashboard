#!/usr/bin/env python3
"""
Running OS — Morning Readiness  (S35)
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

S35 changes:
  - HRV_LOW / HRV_HIGH lifted from module constants → read from
    ATHLETE.hrvBaseline after data load. When the lactate test recalibrates
    thresholds in Sep 2026, one field update cascades to both Python and JS.
  - Briefing now names the exact session prescription from PROGRESSION
    (not just the session type string). Includes week context, race countdown,
    volume on board, and rolling ACWR.
  - Days since last quality session surfaced when today is a quality day and
    the gap is >3 days (missed session visible on Friday).
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

# S35: HRV thresholds are now read from ATHLETE.hrvBaseline after data load.
# These defaults only apply if the key is absent from data.json.
HRV_LOW_DEFAULT  = 53
HRV_HIGH_DEFAULT = 68

# Minimum RHR readings within the 7-day calendar window before computing a
# delta. Fewer readings produce false precision and misleading CAUTION flags.
RHR_MIN_DAYS = 3

HANOI = datetime.timezone(datetime.timedelta(hours=7))

# Shared month lookup for run date parsing
_MONTH_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


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

def compute_status(hrv, rhr, sleep_score, rhr_7d_avg, prev_hrv_below, hrv_low):
    """
    Decision table from CLAUDE.md / training_kb.md.
    Returns (status, reasons[]).

    S35: hrv_low is now passed in from ATHLETE.hrvBaseline[0] rather than
    being a module-level constant. Eliminates the lactate-test update problem.

    All signals are evaluated before the verdict is issued. REST-level and
    CAUTION-level signals are collected independently; the final reasons list
    contains all of them so note and briefing reflect the full picture.
    Most restrictive condition wins.
    """
    hrv_below  = hrv is not None and hrv < hrv_low
    rhr_delta  = (rhr - rhr_7d_avg) if (rhr is not None and rhr_7d_avg is not None) else None

    rest_flags   = []
    caution_flags = []

    # ── HRV ──
    if hrv_below and prev_hrv_below:
        rest_flags.append(f"HRV {hrv} below baseline 2+ consecutive days")
    elif hrv_below:
        caution_flags.append(f"HRV {hrv} below baseline ({hrv_low})")

    # ── RHR delta (only when RHR_MIN_DAYS readings exist) ──
    if rhr_delta is not None:
        if rhr_delta >= 8:
            rest_flags.append(f"RHR +{rhr_delta:.0f} above 7d avg ({rhr_7d_avg})")
        elif rhr_delta >= 5:
            caution_flags.append(f"RHR +{rhr_delta:.0f} above 7d avg ({rhr_7d_avg})")

    # ── Sleep ──
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


# ── ACWR (rolling windows) ────────────────────────────────────────────────────

def _run_date(r, wk_year):
    """Parse a run's date string ('Mon Jul 27') into datetime.date using
    PROGRESSION-anchored year. Returns None on failure."""
    yr = wk_year.get(r.get("wk"))
    if yr is None:
        return None
    parts = (r.get("date") or "").split()
    if len(parts) < 2:
        return None
    m = _MONTH_NUM.get(parts[-2])
    if m is None:
        return None
    try:
        return datetime.date(yr, m, int(parts[-1]))
    except (ValueError, TypeError):
        return None


def _build_wk_year(progression):
    """Build wk → year dict from PROGRESSION.dateStart values.
    Back-fills weeks before PROGRESSION with the first program year
    so Phase 1 runs (wk 1–8) don't silently drop from ACWR windows.
    Without this, ~31km of Phase 1 runs vanish from the 28-day chronic
    window, underestimating chronic load and producing false ELEVATED
    or HIGH RISK readings during peak weeks."""
    wk_year = {}
    first_prog_year = None

    for p in sorted(progression, key=lambda x: x.get("wk", 0)):
        ds = p.get("dateStart")
        if ds:
            try:
                yr = int(ds[:4])
                wk_year[p["wk"]] = yr
                if first_prog_year is None:
                    first_prog_year = yr
            except (KeyError, ValueError, TypeError):
                pass

    # Back-fill Phase 1 weeks (before first PROGRESSION entry)
    if wk_year and first_prog_year:
        first_prog_wk = min(wk_year.keys())
        for wk in range(1, first_prog_wk):
            wk_year[wk] = first_prog_year

    return wk_year


def compute_acwr(runs, progression, today):
    """
    7-day acute / 28-day chronic rolling ACWR.
    Mirrors computeACWR() in index.html (S34).
    Returns float or None if insufficient chronic data.
    """
    wk_year = _build_wk_year(progression)

    def sum_km(days):
        cutoff = today - datetime.timedelta(days=days)
        # >= matches S34 JS: c.setDate(c.getDate()-days) + d >= cutoff
        return round(
            sum(r.get("dist", 0) for r in runs
                if (d := _run_date(r, wk_year)) and d >= cutoff),
            1
        )

    acute   = sum_km(7)
    chronic = round(sum_km(28) / 4, 1)
    return round(acute / chronic, 2) if chronic >= 1 else None


# ── Directive engine (Layer 3: variance → imperative) ─────────────────────────

def ceiling_recent(runs, n=5):
    """
    Recency- and magnitude-weighted ceiling read over the last n quality
    sessions. Deliberately NOT a binary all-time rate: a 1% breach on a
    19-min tempo is ~11s at 153bpm, inside strap lag. Returns
    (clean_count, total, mean_magnitude_of_breaches) or None.
    """
    q = [r for r in runs
         if (r.get("type") or "").startswith("quality")
         and r.get("pctCeiling") is not None]
    if not q:
        return None
    recent = q[-n:]
    breaches = [r["pctCeiling"] for r in recent if r["pctCeiling"] > 0]
    clean = len(recent) - len(breaches)
    mean_mag = round(sum(breaches) / len(breaches), 1) if breaches else 0
    return clean, len(recent), mean_mag


def sleep_trend(log, n=4):
    """Mean sleepScore over last n logged entries that have one, or None."""
    vals = [e.get("sleepScore") for e in log[-n:] if e.get("sleepScore")]
    if not vals:
        return None
    return round(sum(vals) / len(vals))


def build_directives(runs, log, athlete, prog, acwr, q_days, session, today):
    """
    Turns computed variance into imperative statements. Each directive is
    a gap between actual and planned, phrased as an action.

    Rules:
      - Silence is information. No directive fires when a metric is in range.
      - Maximum 3 directives. Alert fatigue is a patient safety issue.
      - Ordered by leverage: today's session first, then week, then trend.
    """
    out = []

    # 1. Today's quality prescription — undercooked vs overcooked
    if session.startswith("quality"):
        under = (acwr is not None and acwr < 0.95) or (q_days is not None and q_days >= 5)
        cr = ceiling_recent(runs)
        if under:
            bits = []
            if q_days is not None and q_days >= 5:
                bits.append(f"{q_days}d without quality")
            if acwr is not None and acwr < 0.95:
                bits.append(f"ACWR {acwr}")
            out.append(
                f"Undercooked, not overcooked ({', '.join(bits)}) — "
                f"don't be conservative on the ladder."
            )
        if cr:
            clean, total, mag = cr
            if clean < total and mag >= 3:
                out.append(
                    f"Ceiling: {clean}/{total} recent clean, mean breach {mag}% — "
                    f"start at the bottom of the ladder."
                )

    # 2. Weekly volume pace
    vol_on = athlete.get("weekVolOnBoard") or 0
    if prog:
        v_min = prog.get("volMin")
        sched = athlete.get("weekSchedule", {})
        order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow = today.strftime("%a")
        try:
            idx = order.index(dow)
        except ValueError:
            idx = 0
        left = [d for d in order[idx:] if (sched.get(d, "rest") != "rest")]
        if v_min and left:
            need = round(v_min - vol_on, 1)
            if need > 0:
                per = round(need / len(left), 1)
                if per > 12:
                    out.append(
                        f"Volume: {need}km over {len(left)} sessions = {per}km each. "
                        f"Above sustainable — expect to land under {v_min}km."
                    )
                elif need > 0:
                    out.append(
                        f"Volume: {need}km left over {len(left)} sessions "
                        f"(~{per}km each) to clear {v_min}km."
                    )

    # 3. Sleep trend — only if genuinely low
    st = sleep_trend(log)
    if st is not None and st < 65:
        out.append(f"Sleep 4-night mean {st} — below 65. Earlier bedtime is the lever.")

    return out[:3]


# ── Days since last quality session ───────────────────────────────────────────

def days_since_quality(runs, progression, today):
    """
    Returns int days since last quality_a or quality_b session, or None.
    Used to surface a missed Wednesday session when Friday's briefing runs.
    """
    wk_year = _build_wk_year(progression)
    quality_runs = [r for r in runs if (r.get("type") or "").startswith("quality")]
    if not quality_runs:
        return None
    last_q = quality_runs[-1]
    q_date = _run_date(last_q, wk_year)
    if q_date is None:
        return None
    return (today - q_date).days


# ── Session description ────────────────────────────────────────────────────────

def session_description(session_type, prog, athlete):
    """
    Full session description from PROGRESSION and ATHLETE fields.
    Optional ATHLETE fields: qualityShoe, longShoe, qualityVenue.
    These can be added to data.json to override defaults.
    """
    ceiling   = athlete.get("qualityCeiling", 150)
    easy_band = athlete.get("easyHRBand", [122, 139])
    q_shoe    = athlete.get("qualityShoe", "Evo SL")
    l_shoe    = athlete.get("longShoe", "Novablast 5")
    q_venue   = athlete.get("qualityVenue", "Elite Fitness")

    if session_type == "quality_a":
        qa = (prog.get("qualityA") or "interval session") if prog else "interval session"
        return f"Quality A — {qa}, ceiling {ceiling} bpm. {q_shoe}, {q_venue}."

    elif session_type == "quality_b":
        qb = (prog.get("qualityB") or "tempo") if prog else "tempo"
        return f"Quality B — {qb} continuous, ceiling {ceiling} bpm. {q_shoe}, {q_venue}."

    elif session_type == "long":
        if prog:
            lr_min, lr_max = prog.get("lrMin"), prog.get("lrMax")
            if lr_min is not None and lr_max is not None:
                lr = f"{lr_min}km" if lr_min == lr_max else f"{lr_min}–{lr_max}km"
            else:
                lr = "—"
        else:
            lr = "—"
        return (
            f"Long run — {lr}, HR {easy_band[0]}–{easy_band[1]} bpm. "
            f"{l_shoe}. Gel 40 min + electrolytes."
        )

    elif session_type == "easy":
        return f"Easy run — HR {easy_band[0]}–{easy_band[1]} bpm. {l_shoe}."

    elif session_type == "flush":
        return f"Flush run — HR {easy_band[0]}–{easy_band[1]} bpm. {l_shoe}."

    elif session_type in ("rest", "unknown"):
        return "Rest day."

    else:
        return f"{session_type}."


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

    # ── S35: Read HRV baseline from ATHLETE (replaces module constants) ──
    athlete     = data.get("ATHLETE", {}) or {}
    hrv_base    = athlete.get("hrvBaseline", [HRV_LOW_DEFAULT, HRV_HIGH_DEFAULT])
    hrv_low     = hrv_base[0]
    hrv_high    = hrv_base[1]
    print(f"  HRV baseline: {hrv_low}–{hrv_high} ms (from ATHLETE.hrvBaseline)")

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
    prev_entry = prior[-1] if prior else None
    prev_hrv_below = bool(
        prev_entry is not None
        and prev_entry.get("date") == yesterday_str
        and prev_entry.get("hrv") is not None
        and prev_entry["hrv"] < hrv_low  # S35: dynamic hrv_low
    )

    status, reasons = compute_status(
        hrv, rhr, sleep_score, rhr_7d_avg, prev_hrv_below, hrv_low
    )
    print(f"  Status: {status} — {'; '.join(reasons)}")

    note = "; ".join(reasons)[:80]

    # ── S35: Rich briefing ───────────────────────────────────────────────────
    runs        = data.get("RUNS", [])
    progression = data.get("PROGRESSION", [])
    races       = data.get("RACES", [])

    # Session from weekly schedule
    sched   = athlete.get("weekSchedule", {})
    dow     = today.strftime("%a")
    session = sched.get(dow, "rest")

    # PROGRESSION entry for current week
    week = athlete.get("week")
    prog = next((p for p in progression if p.get("wk") == week), None)

    # Volume context
    total_weeks  = athlete.get("totalWeeks", 24)
    vol_on_board = athlete.get("weekVolOnBoard", 0)
    vol_min      = prog.get("volMin", "?") if prog else "?"
    vol_max      = prog.get("volMax", "?") if prog else "?"

    # Race 0 countdown (RACES[0])
    days_to_race_0 = None
    if races:
        raw_date = races[0].get("date")
        if raw_date:
            try:
                race_date      = datetime.date.fromisoformat(str(raw_date)[:10])
                days_to_race_0 = (race_date - today).days
            except (ValueError, TypeError):
                pass

    # ACWR (rolling 7/28 day, date-based — mirrors S34 JS)
    acwr     = compute_acwr(runs, progression, today)
    acwr_str = f"ACWR {acwr}" if acwr is not None else "ACWR —"

    # Days since last quality session (surfaces missed sessions)
    q_days     = days_since_quality(runs, progression, today)
    q_gap_note = ""
    if session.startswith("quality") and q_days is not None and q_days > 3:
        q_gap_note = f" [{q_days}d since last quality]"

    # Context line: W12/24 · 84d to Race 0 · 6.0km of 33–37km on board · ACWR 0.94
    ctx_parts = [f"W{week}/{total_weeks}"] if week else []
    if days_to_race_0 is not None:
        ctx_parts.append(f"{days_to_race_0}d to Race 0")
    ctx_parts.append(f"{vol_on_board}km of {vol_min}–{vol_max}km on board")
    ctx_parts.append(acwr_str)
    context_line = " · ".join(ctx_parts)

    # Full session description from PROGRESSION
    session_desc = session_description(session, prog, athlete)

    # Last run
    last_run = runs[-1] if runs else None
    last_run_str = (
        f"Last: R{last_run['r']} {last_run['type']} {last_run['dist']}km "
        f"({last_run['date']})."
        if last_run else "No runs logged."
    )

    # ── Directives (Layer 3) — variance turned imperative ────────────────────
    directives = build_directives(
        runs, log, athlete, prog, acwr, q_days, session, today
    )
    directive_str = (" " + " ".join(directives)) if directives else ""

    # Assemble briefing
    if status == "REST":
        # Lead with suppression, then context
        suppressed_type = session_desc.split(" —")[0]  # e.g. "Quality A"
        briefing = (
            f"REST. {'; '.join(reasons)}. "
            f"{suppressed_type} suppressed.{q_gap_note} "
            f"{context_line}. {last_run_str}"
        )
    elif status == "CAUTION" and session.startswith("quality"):
        # Downgrade quality → easy
        briefing = (
            f"CAUTION. {'; '.join(reasons)}. "
            f"EASY ONLY — {session.replace('_', ' ').upper()} downgraded.{q_gap_note} "
            f"{context_line}. {last_run_str}"
        )
    else:
        # GREEN (or CAUTION on a non-quality day) — session, directives, context
        briefing = (
            f"{session_desc}{q_gap_note}{directive_str} "
            f"{context_line}. {last_run_str}"
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
