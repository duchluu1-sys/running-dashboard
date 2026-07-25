# CLAUDE.md — Running OS Automation Protocol
# Đức Lưu | duchluu1-sys
# Version: July 24, 2026

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
- **Quality A**: Interval session (reps of 2:30 / 3 / 4 / 6 / 8 / 10 min with full walk recovery)
- **Quality B**: Continuous tempo block (15 / 20 / 25 / 30 / 35 min unbroken)
- **Long Run**: 14km+ easy effort, capped at 22km
- **Easy Flush**: Short easy recovery run, typically post-quality or post-double day
- **Strides**: Easy run with 6×20s strides appended

### Step 3 — Build data.json RUNS entry

Append one object to the RUNS array in `duchluu1-sys/running-dashboard/data.json`.

**Schema version: 2 — every field required, use null if not available:**

```json
{
  "r": <run number — increment +1 from last entry>,
  "date": "<Mon Day>",
  "type": "<easy|quality_a|quality_b|long|flush|strides>",
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
  "note": "<brief session note>"
}
```

**Type enum — use exactly these values:**
- `easy` — HR-governed aerobic run, Z2 target
- `quality_a` — interval session (reps × duration with full walk recovery)
- `quality_b` — continuous tempo block (single unbroken effort)
- `long` — 14km+ easy effort
- `flush` — short easy recovery run post-quality or post-double day
- `strides` — easy run with 6×20s strides appended

**Field rules:**
- `duration` = whole session elapsed in minutes (integer). Includes warm-up, cool-down, walk breaks.
- `moving` = running-only minutes (integer). Excludes walk breaks. Required for AEI calculation.
- `pace` = quality-block pace only (min/km). Null on easy and long runs.
- `pctCeiling` = % of session above 150 bpm. Quality sessions only. 0 = ceiling-clean.
- `drift` = Q4 avg HR minus Q1 avg HR (running segments only). Populate ONLY for outdoor easy and long runs ≥4km. Null for all treadmill, all quality, and outdoor <4km.
- `temp` = integer °C only. Do NOT write strings like "27–30°C".
- `hrZones` = [Z1,Z2,Z3,Z4,Z5] as percentages. Must sum to 100. Null if not available.
- `flag` logic: ✅✅✅ program best + exceptional · ✅✅ new PB or near-perfect · ✅ clean · ○ suboptimal · ⚠️ ceiling breach/injury/major gap
- **Source precedence:** Screenshots win over API for `hrZones`, `moving`, `walkRatio` (API inflates these with idle/transition time). API wins for `dist`, `hrAvg`, `hrMax`, `gct`, `vr`, `te`, `te_an`. When sources disagree, state the disagreement explicitly in the verdict before writing. Never silently pick one source.

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

**Session Structure:** (quality sessions only — omit for easy runs)
| Phase | Duration | Speed | Note |
|---|---|---|---|
| Warm-up jog | ~[N] min | [X] km/h | Target: HR 115–120 before quality block |
| [Quality block type] | [duration] | [X] km/h | [key note] |
| Cool-down | ~[N] min | Walk | — |

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
| VO | [X] cm | — |
| Cadence | [X] spm | — |
| Temp | [X]°C | — |
| Statin Day | [N] | [✅ clean or issue] |

**HR Zones (if available):**
| Zone | Range | Time | % |
|---|---|---|---|
| Z5 | >154 bpm | [time] | [%] |
| Z4 | 148–154 bpm | [time] | [%] |
| Z3 | 139–147 bpm | [time] | [%] |
| Z2 | 122–138 bpm | [time] | [%] |
| Z1 | 111–121 bpm | [time] | [%] |

**Key Findings:**

**1. [Most significant finding — be specific with numbers and what they mean]**

**2. [Second finding]**

[Continue numbered — 3 to 10 as warranted. Always end with statin monitoring note.]

**[Last]. Statin Day [N] — [no unusual muscle heaviness ✅] / [OR describe any issue].**

---

**📊 DATA VERDICT**
[One dense summary line: dist · elapsed · key HR metrics · TE · GCT · VR · temp · key outcome]
**Objective rating: [Clean / Good / Execution incomplete / Outstanding / etc.]**

**🧠 FELT EXPERIENCE**
[Athlete's own words if provided in conversation. If not provided: "Not recorded."]
**Subjective rating: [descriptor]**

**⚡ THE GAP**
**Type [A/B/C] — [aligned/misaligned/partial alignment]**
[One paragraph. Gap types: A = data and feel misaligned, data wins; B = aligned but both wrong; C = aligned, clean read]

**✅ DECISIONS**
1. **Run [N] logged: [X] km. Week [N] on board: [X] km.**
2. [Any training adjustments arising from this run]
3. [Any injury monitoring changes]
4. [Any protocol calibration updates]
```

---

### Step 5 — Update training_kb.md

Targeted edits only in `duchluu1-sys/running-kb/training_kb.md`. Never rewrite the whole file.

**1. Status Snapshot — update "Last run" row:**
```
| Last run | **Run [N]** — [type] · [dist]km · avg HR [hrAvg] · max [hrMax] [✅/⚠️] · GCT [X]ms · VR [X]% · TE [te_a]/[te_an] [✅/⚠️] · [temp] · [key note] | [Date] |
```

**2. Status Snapshot — update "Weekly volume" row:**
```
| Weekly volume | Week [N] closed: [X] km ✅ · Week [N+1] on board: [Y] km | [Date] |
```
(if mid-week, just update "on board" figure)

**3. Run Log — append new row at end of table:**
```
| [N] | [Mon Day] | [Type] [surface] [time] | [dist] km | [elapsed] | [hrAvg] | [hrMax] | [te_a]/[te_an] | [gct] ms | [temp] | [flag] [≤5 word note] |
```

**4. GCT Trend Table — append new row (if GCT data available):**
```
| [N] | [Mon Day] | [Session Type] | [gct] | [brief context] | [✅ if PB, else —] |
```

**5. Cardiac Drift Trend Table — append new row (OUTDOOR EASY/LONG ONLY):**
Do NOT add rows for treadmill runs, quality sessions, or outdoor runs under 4km.
```
| [N] | [Mon Day] | [dist] | [drift] bpm | [temp] | [conditions] | [note] |
```

**6. Current Week table — mark session complete:**
Find the matching row and update Status column:
```
✅ R[N] · [key stat] · [flag]
```

**7. If new GCT program best:**
Update Status Snapshot: `| GCT program best | **[X] ms** (R[N], [Mon Day]) | [Date] |`

**8. If new VR program best:**
Update Status Snapshot: `| VR best | **[X]%** (R[N], [Mon Day]) — [note] | [Date] |`

**9. If new HR recovery best:**
Update Status Snapshot: `| HR recovery best | **[X] bpm/30s** (R[N], [Mon Day]) | [Date] |`

**10. Update version timestamp at top of file.**

Commit message: `KB update R[N] — [date]`

---

## Verdict & Write Protocol

After completing Steps 1–5 analysis, present this before writing anything:

R[N] VERDICT — [date]
Type: [type] · Dist: [X]km · HR: [avg]/[max] · TE: [X]/[Y] · GCT: [X]ms · VR: [X]%
Flag: [emoji] · [one line summary]
[Source disagreements, if any]

Ready to write:
Commit 1: data.json → running-dashboard
Commit 2: run_archive.md + training_kb.md → running-kb
Go ahead?


On go-ahead:
1. `push_files` → `duchluu1-sys/running-dashboard` — confirm SHA
2. `push_files` → `duchluu1-sys/running-kb` — confirm SHA

Done message format:

✅ R[N] committed.
Dashboard: [SHA] · data.json updated
KB: [SHA] · run_archive + training_kb updated


---

## Morning Readiness Protocol

When asked to check morning readiness:

1. Pull from Garmin: overnight HRV average, RHR, sleep score, Body Battery, HRV status
2. Compare HRV to baseline: **53–68 ms**
3. Compare RHR to rolling 7-day average (pull from recent health summaries)
4. Return exactly this format:

```
[emoji] [STATUS]

HRV: [X] ms ([above/within/below] 53–68 ms baseline)
RHR: [X] bpm ([delta] vs 7-day avg [Y] bpm)
Sleep: [score] ([Good/Fair/Poor])
Body Battery: [X]

Today: [planned session from current week]
Verdict: [1–2 sentences — specific recommendation]
```

**Decision rules (most restrictive wins):**

| Condition | Status |
|---|---|
| HRV below baseline 2+ consecutive days OR RHR ≥ +8 above 7-day avg | 🔴 REST |
| HRV below baseline OR RHR +5 to +7 above 7-day avg | 🟡 CAUTION — easy only, no quality |
| Sleep score <45 | 🔴 REST |
| Sleep score 45–59 | 🟡 CAUTION — easy only |
| Two caution signals simultaneously | 🟡 CAUTION regardless |
| Sleep 60–74 · HRV balanced · RHR within +4 | 🟢 GREEN — monitor during session |
| Sleep 75+ · HRV balanced · RHR within +4 | 🟢 GREEN — train as planned |

**After evaluation, append one entry to READINESS_LOG array in data.json:**

```json
{
  "date": "<YYYY-MM-DD>",
  "hrv": <integer or null>,
  "rhr": <integer or null>,
  "sleepScore": <integer 0-100 or null>,
  "status": "GREEN|CAUTION|REST",
  "note": "<brief note ≤10 words>"
}
```

Do NOT overwrite the array. Append to end only. The dashboard computes rolling averages from the full history.

Commit message: `Readiness [date]`

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

## Current Phase Context (update this section at phase transitions)

**Phase 2 — Quality Development**
- Current week: Week 11 (Jul 20–26, 2026)
- Phase 2 ends: mid-September 2026
- Deload: Week 21
- Next race: VPBank Hanoi 10K, October 2026

**Quality A progression (treadmill intervals):**
W9–10: 5×4 min → W11: 5×4 min → W12: 4×6 min → W13: 4×6 min → W14: 4×8 min → W15: 3×10 min

**Quality B progression (continuous tempo):**
W9–10: 15 min → W11: 20 min → W12: 25 min → W13: 30 min → W14: 35 min → W15: 2×20 min

**LT1 calibration (treadmill):** 8.5–8.6 km/h for 20-min block = max HR 150 bpm

---

## Statin Monitoring

Medication: **Atoyze 20/10 (Atorvastatin 20mg + Ezetimibe 10mg)** — started May 30, 2026.

Calculate statin day: Day 1 = May 30, 2026. Increment +1 per calendar day.

After every run, include in archive entry:
- Clean: `Statin Day [N] — no unusual muscle heaviness ✅`
- Issue: `Statin Day [N] — [describe: unusual heaviness / deep ache / disproportionate fatigue]`

**Statin myopathy red flags — if any present, stop training and flag immediately:**
- Unusual leg heaviness beyond normal DOMS
- Deep aches independent of movement
- Fatigue disproportionate to session load
- Muscle weakness

---

## Health Data Security

**NEVER include in data.json or the public running-dashboard repo:**
- HEALTH_FLAGS array
- MEDICATIONS array
- Any diagnoses
- Blood test results
- Imaging findings (TI-RADS, spleen, gallbladder)
- InBody composition data

**These belong exclusively in training_kb.md in running-kb (private).**

---

## Commit Message Convention

| Action | Format |
|---|---|
| Run update (all files) | `R[N] — [type] [Mon Day]` |
| Morning readiness | `Readiness [Mon Day]` |
| KB only update | `KB update — [Mon Day]` |
| data.json fix | `Schema fix — [field] [Mon Day]` |
| Phase transition | `Phase [N] start — [Mon Day]` |

---
*This file governs all Claude Desktop automation for the Running OS system.*
*Update "Current Phase Context" section at each phase transition.*
