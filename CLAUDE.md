# CLAUDE.md — Running OS Automation Protocol
# Luu Hoang Duc | duchluu1-sys
# Version: July 26, 2026

This file governs all Claude Desktop automation for the Running OS system.
Read before executing any update.

---

## Repositories

| Repo | Visibility | Contents |
|---|---|---|
| duchluu1-sys/running-dashboard | Public | index.html, data.json (training data only) |
| duchluu1-sys/running-kb | Private | training_kb.md, run_archive.md, decision_log.md, personal_notes.md |

**NEVER write health data, medications, diagnoses, or blood results to the public repo.**

---

## Tool Loading Protocol

Execute at the start of every automation session, before any analysis.

**0A — Load GitHub tools first.**
Search for and confirm `push_files`, `get_file_contents`, `create_or_update_file` are available before proceeding. If unavailable, stop and report.

**0B — Pull all reads in one batch:**
- `duchluu1-sys/running-dashboard/data.json` (last run number, current ATHLETE state)
- `duchluu1-sys/running-kb/run_archive.md` (last entry for continuity)
- `duchluu1-sys/running-kb/training_kb.md` (current week table, trend tables)

**0C — Load Garmin tools. Pull activity data.**

All reads must complete before any analysis or writing begins.

**Write rule — two atomic commits, both after go-ahead only:**
- Commit 1: `data.json` → `duchluu1-sys/running-dashboard` (one `push_files` call)
- Commit 2: `run_archive.md` + `training_kb.md` → `duchluu1-sys/running-kb` (one `push_files` call, both files together)

**Verify each commit:** Confirm the returned SHA after each `push_files`. If no SHA returned, state "Commit [1/2] failed — [repo] not updated." Do not report done.

---

## Post-Run Workflow

When instructed to "update my run":

### Step 1 — Pull from Garmin
Use `get_activity_details` with the most recent activity. If athlete specifies a date, find by date. Pull: distance, elapsed time, moving time, avg HR, max HR, training effect (aerobic + anaerobic), GCT, VR, vertical oscillation, stride length, cadence, temp, lap data.

### Step 2 — Determine run type
- **Easy**: HR-governed aerobic run, Z2 target, no quality block
- **Quality A**: Interval session (reps of 2:30 / 3 / 4 / 6 / 8 / 10 / 12 min with full walk recovery)
- **Quality B**: Continuous tempo block (15 / 20 / 25 / 30 / 35 min unbroken)
- **Long Run**: 14km+ easy effort, capped at 22km
- **Flush**: Short easy recovery run, typically post-quality or post-double day
- **Strides**: Easy run with 6×20s strides appended
- **Race**: Competitive race event

### Step 3 — Build data.json RUNS entry

Append one object to the RUNS array in `duchluu1-sys/running-dashboard/data.json`.

**Schema version: 2 — every field required, use null if not available:**

```json
{
  "r": <run number — increment +1 from last entry>,
  "date": "<Mon Day>",
  "type": "<easy|quality_a|quality_b|long|flush|strides|race>",
  "wk": <current week number — integer>,
  "dist": <distance km — 2 decimal places>,
  "duration": <whole-session elapsed minutes — integer, includes warm-up/cool-down/walks>,
  "moving": <running-only minutes — integer, excludes walk breaks, null if unknown>,
  "pace": <quality-block pace min/km — float, null on easy/long runs>,
  "hrAvg": <session average HR — integer or null>,
  "hrMax": <session max HR — integer or null>,
  "te": <aerobic training effect — float e.g. 3.4 or null>,
  "te_an": <anaerobic training effect — float e.g. 0.0 or null>,
  "gct": <ground contact time ms — integer or null>,
  "vr": <vertical ratio % — float e.g. 8.8 or null>,
  "cadence": <running cadence spm — integer or null>,
  "pctCeiling": <% session time above HR ceiling — float, quality sessions only, else null>,
  "drift": <cardiac drift bpm — OUTDOOR EASY/LONG ONLY — else null>,
  "walkRatio": <walk time as % of total — float or null>,
  "hrZones": <[Z1,Z2,Z3,Z4,Z5] as percentages summing to 100 — array or null>,
  "temp": <temperature °C — integer or null>,
  "surface": "<outdoor|treadmill>",
  "conditions": "<start time HH:MM or brief context>",
  "flag": "<✅|✅✅|✅✅✅|⚠️|○>",
  "note": "<brief session note>",
  "verdict": null
}
```

**Field rules:**
- `duration` = whole session elapsed in minutes (integer). Includes warm-up, cool-down, walk breaks.
- `moving` = running-only minutes (integer). Excludes walk breaks. Required for AEI calculation.
- `pace` = quality-block pace only (min/km). Null on easy and long runs.
- `pctCeiling` = % of session above 150 bpm. Quality sessions only. 0 = ceiling-clean.
- `drift` = Q4 avg HR minus Q1 avg HR (running segments only). Populate ONLY for outdoor easy and long runs ≥4km. Null for all treadmill, all quality, and outdoor <4km.
- `temp` = integer °C only. Do NOT write strings like "27–30°C".
- `hrZones` = [Z1,Z2,Z3,Z4,Z5] as percentages. Must sum to 100. Null if not available.
- `flag` logic: ✅✅✅ program best + exceptional · ✅✅ new PB or near-perfect · ✅ clean · ○ suboptimal · ⚠️ ceiling breach/injury/major gap
- `verdict` = coaching interpretation, 2–4 sentences. Leave as null in the entry — athlete fills this in before committing. Dashboard falls back to `note` field in render if null.
- **Source precedence:** Screenshots win over API for `hrZones`, `moving`, `walkRatio`. API wins for `dist`, `hrAvg`, `hrMax`, `gct`, `vr`, `te`, `te_an`. When sources disagree, state the disagreement explicitly in the verdict before writing.

**After appending run, update ATHLETE block — these fields only:**

```json
"date": "<July DD, YYYY>",
"week": <current week number>,
"weekVolOnBoard": <total km this week including new run — 2 decimal places>,
"gctBest": <update only if new run GCT beats current best>,
"vrBest": <update only if new run VR beats current best>
```

Commit message: `R[N] — [type] [date]`

---

### Step 4 — Write run_archive.md entry

Append to end of `duchluu1-sys/running-kb/run_archive.md`.

**Exact required format:**

```
---

### Run [N] — [Session Type] · [Context] ([Month Day, Year])
**Source:** Garmin MCP · **Conditions:** [temp] · [location if known] · [start time] local · Statin Day [N].

**Session Metrics:**
| Metric | Value | Flag |
|---|---|---|
| Distance | [X] km | — |
| Elapsed | [time] | — |
| Moving time | [time] | — |
| Avg HR | [X] bpm | — |
| Max HR | [X] bpm | [✅ if ≤150 quality / ≤143 easy, else ⚠️] |
| TE | [X]/[Y] ([Garmin label]) | [✅ or ⚠️] |
| GCT | [X] ms | [✅ or flag] |
| VR | [X]% | [✅ if <9%, else flag] |
| Cadence | [X] spm | — |
| Temp | [X]°C | — |
| Statin Day | [N] | [✅ clean or issue] |

**Key Findings:**

**1. [Most significant finding]**

**2. [Second finding]**

[Continue numbered. Always end with statin monitoring note.]

---

**📊 DATA VERDICT**
[One dense summary line]
**Objective rating: [Clean / Good / Execution incomplete / Outstanding]**

**🧠 FELT EXPERIENCE**
[Athlete's own words if provided. If not: "Not recorded."]
**Subjective rating: [descriptor]**

**⚡ THE GAP**
**Type [A/B/C] — [aligned/misaligned/partial]**
[One paragraph.]

**✅ DECISIONS**
1. **Run [N] logged: [X] km. Week [N] on board: [X] km.**
2. [Any training adjustments]
```

---

### Step 5 — Update training_kb.md

Targeted edits only. Never rewrite the whole file.

1. Update "Last run" row in Status Snapshot
2. Update "Weekly volume" row
3. Append new row to Run Log table
4. Append new row to GCT Trend Table (if GCT data available)
5. Append new row to Cardiac Drift Trend Table (OUTDOOR EASY/LONG ONLY)
6. Mark session complete in Current Week table
7. Update GCT / VR / HR recovery bests if new PBs
8. Update version timestamp at top of file

Commit message: `KB update R[N] — [date]`

---

## Verdict & Write Protocol

After completing Steps 1–5 analysis, produce all three outputs simultaneously — no go-ahead gate:
1. data.json run entry (verdict: null — athlete fills in before committing)
2. training_kb.md full replacement file
3. run_archive.md full replacement file

Athlete commits all three via GitHub Desktop.

---

## Morning Readiness Protocol

**Trigger:** user types "morning", "readiness", or "good morning".

### Step 1 — Load data (one batch before any analysis)

**1A — Load GitHub tools.** Confirm `get_file_contents` and `push_files` available.

**1B — Pull from GitHub:**
- `duchluu1-sys/running-dashboard/data.json` → read `ATHLETE.weekSchedule`, `ATHLETE.formCue`, `ATHLETE.week`, last run entry from RUNS, full READINESS_LOG array
- `duchluu1-sys/running-kb/training_kb.md` → read Current Week table for today's exact targets

**1C — Pull from Garmin:** overnight HRV average, RHR, sleep score, Body Battery, HRV status.

All reads before any analysis.

### Step 2 — Compute readiness status

Compare HRV to baseline: **53–68 ms**
Compare RHR to 7-day rolling average (compute from last 7 READINESS_LOG entries).

| Condition | Status |
|---|---|
| HRV below baseline 2+ consecutive days OR RHR ≥ +8 above 7-day avg | 🔴 REST |
| HRV below baseline OR RHR +5 to +7 above 7-day avg | 🟡 CAUTION — easy only, no quality |
| Sleep score <45 | 🔴 REST |
| Sleep score 45–59 | 🟡 CAUTION — easy only |
| Two caution signals simultaneously | 🟡 CAUTION regardless |
| Sleep 60–74 · HRV balanced · RHR within +4 | 🟢 GREEN — monitor during session |
| Sleep 75+ · HRV balanced · RHR within +4 | 🟢 GREEN — train as planned |

### Step 3 — Determine today's session

1. Read `ATHLETE.weekSchedule` → find today's day → get session type
2. Cross-reference training_kb.md Current Week table → get exact targets
3. Apply readiness modifier: GREEN = prescribed session · CAUTION = downgrade quality to easy · REST = no session

### Step 4 — Build briefing string

4 parts, dense, no filler, ~3–5 sentences total.

**Part 1:** HRV vs baseline, RHR vs 7d avg, status verdict.
**Part 2:** Today's session with exact targets (speed, reps, HR ceiling, duration, surface). If downgraded: `[Original] → easy run (CAUTION: [reason])`
**Part 3:** `Shoes: [model] · [Location] · Cue: [ATHLETE.formCue]`
- Quality A/B → Evo SL · Easy/Long/Flush → Novablast 5
- Quality + Long → Elite Fitness Xuân Diệu · Easy outdoor → outdoor
**Part 4:** Last run (R[N], type, dist, date). ACWR note if >1.3 or <0.8. Active injury monitors.

### Step 5 — Write to data.json

Append to READINESS_LOG in `duchluu1-sys/running-dashboard/data.json`:

```json
{
  "date": "<YYYY-MM-DD>",
  "hrv": <integer or null>,
  "rhr": <integer or null>,
  "sleepScore": <integer 0-100 or null>,
  "status": "GREEN|CAUTION|REST",
  "note": "<brief note ≤10 words>",
  "briefing": "<Part 1. Part 2. Part 3. Part 4.>"
}
```

**Do NOT overwrite the array. Append to end only.**
**Do NOT write to training_kb.md or run_archive.md.**

Commit message: `Readiness [Mon Day]`

Confirm SHA. Done message:
```
✅ Readiness logged — [date]
Status: [GREEN/CAUTION/REST]
Today: [one-line session summary]
Dashboard: [SHA]
```

---

## Protocol Constants

| Parameter | Value |
|---|---|
| Easy HR range | 122–139 bpm |
| Walk trigger | **143 bpm** — walk immediately |
| Resume after walk | **128 bpm** |
| Quality HR ceiling | **150 bpm** — hard ceiling |
| Quality entry HR | 115–120 bpm (8+ min warm-up) |
| Recovery between reps | ≤128 bpm |
| Treadmill incline | 1% always |
| Pace ceiling Phase 2 | sub-6:30/km (connective tissue rule) |
| Weekly volume increase | max 10% |
| Deload frequency | every 4th week, 30% reduction |
| Quality B ceiling (20 min) | 8.6 km/h — recalibrated R45 |

---

## Current Phase Context

**Phase 2 — Quality Development**
- Current week: Week 12 (Jul 27 – Aug 2, 2026)
- Phase 2 ends: October 25, 2026 (Week 24)
- Deload weeks: 21 (Sep 28 – Oct 4) · 24 (Oct 19–25)
- Consolidation weeks: 22–23 (Oct 5–18)
- Next race: VPBank Hanoi 10K, October 19, 2026 (Race 0 — inside W24 deload)

**Quality A — treadmill intervals (paired 2-week blocks):**
W9–10: 7×2:30 min ✅ → W11–12: 5×4 min (current) → W13–14: 4×6 min → W15–16: 4×8 min → W17–18: 3×12 min → W19–20: 2×20 min → W21: 4×4 min DELOAD → W22–23: 2×20 min hold → W24: 4×4 min DELOAD

**Quality B — continuous tempo (paired 2-week blocks):**
W9–10: 15 min ✅ → W11–12: 20 min (current) → W13–14: 25 min → W15–16: 30 min → W17–18: 35 min → W19–20: 2×20 min → W21: 10 min DELOAD → W22–23: 2×20 min hold → W24: 10 min DELOAD

**Long run targets (paired 2-week blocks):**
W9–10: 13–15 km ✅ → W11–12: 14–15 km (current) → W13–14: 15–16 km → W15–16: 16–18 km → W17–18: 18–20 km → W19–20: 20–22 km → W21: 12 km DELOAD → W22–23: 20–22 km hold → W24: 12 km DELOAD

**Weekly volume targets (paired 2-week blocks):**
W9–10: 33–36 km ✅ → W11–12: 33–37 km (current) → W13–14: 35–38 km → W15–16: 36–40 km → W17–18: 37–42 km → W19–20: 38–44 km → W21: 26–28 km DELOAD → W22–23: 38–42 km hold → W24: 26–28 km DELOAD

---

## Statin Monitoring

Medication: **Atoyze 20/10 (Atorvastatin 20mg + Ezetimibe 10mg)** — started May 30, 2026.

Calculate statin day: Day 1 = May 30, 2026. Increment +1 per calendar day.

After every run:
- Clean: `Statin Day [N] — no unusual muscle heaviness ✅`
- Issue: `Statin Day [N] — [describe: unusual heaviness / deep ache / disproportionate fatigue]`

**Red flags — stop training immediately:**
- Unusual leg heaviness beyond normal DOMS
- Deep aches independent of movement
- Fatigue disproportionate to session load
- Muscle weakness

---

## Health Data Security

**NEVER include in data.json or running-dashboard repo:**
- HEALTH_FLAGS, MEDICATIONS, diagnoses, blood results, imaging findings, InBody data

**These belong exclusively in training_kb.md in running-kb (private).**

---

## Commit Message Convention

| Action | Format |
|---|---|
| Run update | `R[N] — [type] [Mon Day]` |
| Morning readiness | `Readiness [Mon Day]` |
| KB only | `KB update — [Mon Day]` |
| data.json fix | `Schema fix — [field] [Mon Day]` |
| Phase transition | `Phase [N] start — [Mon Day]` |

---
*Update "Current Phase Context" at each phase transition and each week rollover.*
