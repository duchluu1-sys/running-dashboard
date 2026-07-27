#!/usr/bin/env python3
"""
Running OS — Post-Run Logger
Pulls a Garmin activity, merges athlete-supplied judgement fields, validates,
appends to RUNS in data.json.

Triggered by workflow_dispatch only. There is no cron — a run entry requires
judgement fields that only the coaching layer can supply.

Design rules (mirrors morning_readiness.py):
  - NEVER fabricate. If Garmin gives us no matching activity, exit non-zero
    and leave data.json alone. A missing run is honest; a guessed one is not.
  - NEVER write a duplicate. Same date + distance within 50 m is the same run.
    Duplicates break `r` sequence, which trips the dashboard validator.
  - VALIDATE BEFORE WRITING. This script removes the human eyeball that used
    to catch schema errors, so it must catch them itself. A rejected entry
    costs one re-run; a committed bad entry costs a manual repair.
  - MECHANICAL fields come from Garmin. JUDGEMENT fields come from the athlete.
    Never guess a judgement field from API data.
  - hrZones is NOT auto-populated. get_activity_hr_in_timezones() exists, but
    the API inflates zone percentages relative to the watch screenshot. Zones
    are a judgement field, read off the screenshot. See CLAUDE.md source
    precedence.
  - weekVolOnBoard is RECOMPUTED from RUNS, never incremented. Recompute is
    idempotent; increment drifts silently on any re-run.
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

# workflow_dispatch inputs
JUDGEMENT_RAW = os.environ.get("JUDGEMENT", "").strip()
RUN_DATE      = os.environ.get("RUN_DATE", "").strip()      # YYYY-MM-DD, blank = today
DRY_RUN       = os.environ.get("DRY_RUN", "false").lower() == "true"

HANOI = datetime.timezone(datetime.timedelta(hours=7))

# Garmin activityType.typeKey -> surface
SURFACE_MAP = {
    "running":          "outdoor",
    "trail_running":    "outdoor",
    "street_running":   "outdoor",
    "track_running":    "outdoor",
    "treadmill_running": "treadmill",
    "indoor_running":   "treadmill",
}

TYPE_ENUM = ["easy", "quality_a", "quality_b", "long", "flush", "strides", "race"]

SCHEMA_FIELDS = [
    "r", "date", "type", "wk", "dist", "duration", "moving", "pace", "paceEasy",
    "hrAvg", "hrMax", "te", "te_an", "gct", "vr", "cadence", "pctCeiling",
    "drift", "walkRatio", "hrZones", "temp", "surface", "conditions",
    "flag", "note",
]

# Fields the athlete/coaching layer must supply. Everything else is mechanical.
JUDGEMENT_FIELDS = [
    "type", "pace", "paceEasy", "pctCeiling", "drift",
    "walkRatio", "hrZones", "flag", "note",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def i(v):
    """Safe int, preserving None."""
    return int(round(v)) if v is not None else None


def f2(v):
    """Safe float to 2dp, preserving None."""
    return round(float(v), 2) if v is not None else None


def first(d, *keys):
    """First non-None value among keys. Garmin renames fields between versions."""
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


# ── Garmin ────────────────────────────────────────────────────────────────────

def pull_activity(date_str):
    """
    Returns (mechanical_fields_dict, activity_name) or (None, None).
    Filters to running activities on date_str. If several, takes the longest —
    a pickleball session or a stray walk must never be logged as the run.
    """
    from garminconnect import Garmin
    api = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    api.login()

    acts = api.get_activities_by_date(date_str, date_str) or []
    runs = [
        a for a in acts
        if (a.get("activityType", {}) or {}).get("typeKey") in SURFACE_MAP
    ]
    if not runs:
        types = sorted({(a.get("activityType", {}) or {}).get("typeKey") for a in acts})
        print(f"✗ No running activity on {date_str}. "
              f"Activities found: {types or 'none'}", file=sys.stderr)
        return None, None

    if len(runs) > 1:
        print(f"  {len(runs)} running activities on {date_str} — taking longest")
    a = max(runs, key=lambda x: x.get("distance") or 0)

    aid      = a.get("activityId")
    type_key = (a.get("activityType", {}) or {}).get("typeKey")
    surface  = SURFACE_MAP[type_key]

    # Distance is metres. Durations are seconds.
    dist_m   = first(a, "distance")
    elapsed  = first(a, "elapsedDuration", "duration")
    moving   = first(a, "movingDuration")

    start_local = a.get("startTimeLocal", "")            # "2026-07-27 14:48:03"
    hhmm = start_local.split(" ")[1][:5] if " " in start_local else None
    date_label = None
    if start_local:
        dt = datetime.datetime.strptime(start_local.split(" ")[0], "%Y-%m-%d")
        date_label = dt.strftime("%b ") + str(dt.day)     # "Jul 27" — no %-d, portable

    # Temperature: activity payload first, weather endpoint as fallback.
    temp = first(a, "maxTemperature", "minTemperature", "averageTemperature")
    if temp is None and aid:
        try:
            w = api.get_activity_weather(aid) or {}
            temp = first(w, "temp", "apparentTemp")
        except Exception as e:
            print(f"   note: weather endpoint unavailable ({e})", file=sys.stderr)

    mech = {
        "date":     date_label,
        "dist":     f2(dist_m / 1000) if dist_m else None,
        "duration": i(elapsed / 60) if elapsed else None,
        "moving":   i(moving / 60) if moving else None,
        "hrAvg":    i(first(a, "averageHR")),
        "hrMax":    i(first(a, "maxHR")),
        "te":       first(a, "aerobicTrainingEffect"),
        "te_an":    first(a, "anaerobicTrainingEffect"),
        "gct":      i(first(a, "avgGroundContactTime")),
        "vr":       first(a, "avgVerticalRatio"),
        # Session-average cadence is suppressed by walk/idle segments on
        # run/walk sessions. Null is more honest than a misleading average.
        "cadence":  None,
        "temp":     i(temp),
        "surface":  surface,
        "conditions": hhmm,
    }
    return mech, a.get("activityName", "")


# ── Week derivation ───────────────────────────────────────────────────────────

def derive_week(date_str, progression):
    """
    Week number from PROGRESSION date ranges — never typed by hand.
    Returns None if the date falls outside every defined week.
    """
    for p in progression:
        if p.get("dateStart") and p.get("dateEnd"):
            if p["dateStart"] <= date_str <= p["dateEnd"]:
                return p["wk"]
    return None


# ── Validation ────────────────────────────────────────────────────────────────

def validate(entry, runs):
    """
    Port of index.html::validateRuns plus the CLAUDE.md field rules.
    Returns list of errors. Non-empty = refuse to write.
    """
    e = []
    r = entry

    # Required, non-null
    for k in ("r", "date", "type", "wk", "dist", "flag", "note"):
        if r.get(k) is None:
            e.append(f"required field '{k}' is null")

    # Unknown / missing schema keys
    for k in r:
        if k not in SCHEMA_FIELDS:
            e.append(f"unknown field '{k}'")
    for k in SCHEMA_FIELDS:
        if k not in r:
            e.append(f"schema field '{k}' absent (use null)")

    # Enum
    if r.get("type") not in TYPE_ENUM:
        e.append(f"type '{r.get('type')}' not in {TYPE_ENUM}")

    # Sequence
    if runs and r.get("r") != runs[-1]["r"] + 1:
        e.append(f"r={r.get('r')} not sequential after R{runs[-1]['r']}")

    # Duplicate guard — same date, distance within 50 m
    for prev in runs:
        if prev.get("date") == r.get("date") and prev.get("dist") and r.get("dist"):
            if abs(prev["dist"] - r["dist"]) < 0.05:
                e.append(f"duplicate: R{prev['r']} is {prev['dist']}km on {prev['date']}")

    # temp must be numeric
    if r.get("temp") is not None and not isinstance(r["temp"], (int, float)):
        e.append(f"temp is {type(r['temp']).__name__}, expected number")

    # hrZones: 5 elements summing to 100
    z = r.get("hrZones")
    if z is not None:
        if not isinstance(z, list) or len(z) != 5:
            e.append("hrZones must be an array of 5")
        elif abs(sum(z) - 100) > 1.5:
            e.append(f"hrZones sum to {sum(z)}, expected 100")

    # drift: outdoor easy/long >= 4 km only
    if r.get("drift") is not None:
        if r.get("surface") != "outdoor":
            e.append(f"drift set on {r.get('surface')} — outdoor only")
        if r.get("type") not in ("easy", "long"):
            e.append(f"drift set on {r.get('type')} — easy/long only")
        if r.get("dist") is not None and r["dist"] < 4:
            e.append(f"drift set on {r['dist']}km run — 4km minimum")

    # pctCeiling: quality sessions only
    if r.get("pctCeiling") is not None and not str(r.get("type", "")).startswith("quality"):
        e.append(f"pctCeiling set on {r.get('type')} — quality sessions only")

    # pace: quality block only
    if r.get("pace") is not None and r.get("type") in ("easy", "long", "flush"):
        e.append(f"pace set on {r.get('type')} — use paceEasy instead")

    return e


# ── GitHub ────────────────────────────────────────────────────────────────────

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


def gh_read():
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    meta = r.json()
    return json.loads(base64.b64decode(meta["content"]).decode()), meta["sha"]


def gh_write(content, sha, message):
    """PUT with one retry on 409 — the readiness Action may have raced us."""
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
    for attempt in (1, 2):
        encoded = base64.b64encode(
            json.dumps(content, indent=2, ensure_ascii=False).encode()
        ).decode()
        r = requests.put(url, headers=HEADERS, timeout=30,
                         json={"message": message, "content": encoded, "sha": sha})
        if r.status_code == 409 and attempt == 1:
            print("  409 conflict — re-reading SHA and retrying once")
            fresh, sha = gh_read()
            # Re-apply our entry onto the fresh copy.
            fresh["RUNS"].append(content["RUNS"][-1])
            fresh["ATHLETE"] = content["ATHLETE"]
            content = fresh
            continue
        r.raise_for_status()
        return r.json()["content"]["sha"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.datetime.now(HANOI).date()
    date_str = RUN_DATE or today.isoformat()

    print(f"Post-run logger — activity date {date_str} (Hanoi)")
    if DRY_RUN:
        print("  DRY RUN — nothing will be committed\n")

    # ── Judgement fields ──
    if not JUDGEMENT_RAW:
        print("✗ JUDGEMENT input empty. Required fields: "
              + ", ".join(JUDGEMENT_FIELDS), file=sys.stderr)
        return 1
    try:
        judgement = json.loads(JUDGEMENT_RAW)
    except json.JSONDecodeError as ex:
        print(f"✗ JUDGEMENT is not valid JSON: {ex}", file=sys.stderr)
        return 1

    unknown = [k for k in judgement if k not in JUDGEMENT_FIELDS]
    if unknown:
        print(f"✗ JUDGEMENT contains non-judgement fields: {unknown}\n"
              f"  Those are mechanical — the script pulls them from Garmin.",
              file=sys.stderr)
        return 1

    # ── Read repo state ──
    data, sha = gh_read()
    runs = data.setdefault("RUNS", [])
    athlete = data.setdefault("ATHLETE", {})
    progression = data.get("PROGRESSION", [])
    print(f"  data.json: {len(runs)} runs, last R{runs[-1]['r'] if runs else '—'}")

    # ── Pull Garmin ──
    try:
        mech, name = pull_activity(date_str)
    except Exception as ex:
        print(f"✗ Garmin pull failed: {ex}", file=sys.stderr)
        return 1
    if mech is None:
        return 1
    print(f"  Garmin: \"{name}\" — {mech['dist']}km, "
          f"{mech['duration']}min, HR {mech['hrAvg']}/{mech['hrMax']}, "
          f"{mech['surface']}")

    # ── Assemble ──
    wk = derive_week(date_str, progression)
    if wk is None:
        print(f"✗ {date_str} falls outside every PROGRESSION week range. "
              f"Extend PROGRESSION before logging.", file=sys.stderr)
        return 1

    entry = {k: None for k in SCHEMA_FIELDS}
    entry.update(mech)
    entry.update(judgement)
    entry["r"] = (runs[-1]["r"] + 1) if runs else 1
    entry["wk"] = wk

    # ── Validate ──
    errs = validate(entry, runs)
    if errs:
        print(f"\n✗ Validation failed — {len(errs)} error(s). NOT writing.",
              file=sys.stderr)
        for x in errs:
            print(f"    · {x}", file=sys.stderr)
        print(f"\nAssembled entry:\n{json.dumps(entry, indent=2, ensure_ascii=False)}",
              file=sys.stderr)
        return 1
    print("  Validation: clean ✓")

    # ── ATHLETE updates ──
    runs.append(entry)
    week_runs = [r for r in runs if r.get("wk") == wk]
    athlete["weekVolOnBoard"] = round(sum(r["dist"] for r in week_runs if r.get("dist")), 2)
    athlete["week"] = wk
    _d = datetime.date.fromisoformat(date_str)
    athlete["date"] = f"{_d.strftime('%B')} {_d.day}, {_d.year}"   # avoids %-d
    if entry.get("gct") and (not athlete.get("gctBest") or entry["gct"] < athlete["gctBest"]):
        print(f"  🏆 New GCT best: {entry['gct']}ms (was {athlete.get('gctBest')})")
        athlete["gctBest"] = entry["gct"]
    if entry.get("vr") and (not athlete.get("vrBest") or entry["vr"] < athlete["vrBest"]):
        print(f"  🏆 New VR best: {entry['vr']}% (was {athlete.get('vrBest')})")
        athlete["vrBest"] = entry["vr"]

    print(f"\nR{entry['r']} — {entry['type']} {entry['date']} · {entry['dist']}km "
          f"· W{wk} {athlete['weekVolOnBoard']}km on board · {entry['flag']}")
    print(json.dumps(entry, indent=2, ensure_ascii=False))

    if DRY_RUN:
        print("\n✓ DRY RUN complete. Nothing committed. "
              "Re-run with dry_run=false to write.")
        return 0

    new_sha = gh_write(data, sha, f"R{entry['r']} — {entry['type']} {entry['date']}")
    print(f"\n✅ Committed. SHA: {new_sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
