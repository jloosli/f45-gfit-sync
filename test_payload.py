#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""Offline checks for parsing and Google Health payload construction.

    uv run test_payload.py

No network, no credentials. Exits non-zero on failure. `requests` is declared
because importing `sync` pulls it in, not because anything here makes a request.
"""

import json
from zoneinfo import ZoneInfo

from sync import (
    DEFAULT_EXERCISE_TYPE,
    build_exercise_datapoint,
    calculate_calories,
    parse_workout,
)

SNIPPET = (
    "Your 5:15 AM Athletica class at F45 Training Draper summary "
    "42 POINTS 132 AVG BPM 168 MAX BPM"
)
# 2026-08-04 07:10 America/Denver (UTC-6), i.e. shortly after a 5:15 AM class
EMAIL_MS = 1785849000000

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


tz = ZoneInfo("America/Denver")
workout = parse_workout(SNIPPET, EMAIL_MS, tz)

print("parse_workout")
check("parsed", workout is not None)
if workout is None:
    raise SystemExit(1)

check("class name", workout["class_name"] == "Athletica", workout["class_name"])
check("studio", workout["studio"] == "F45 Training Draper", workout["studio"])
check("points", workout["points"] == 42)
check("avg bpm", workout["avg_bpm"] == 132)
check("max bpm", workout["max_bpm"] == 168)
check("start is 5:15 local", workout["start_rfc3339"].endswith("11:15:00Z"),
      workout["start_rfc3339"])
check("end is 45 min later", workout["end_rfc3339"].endswith("12:00:00Z"),
      workout["end_rfc3339"])
check("utc offset", workout["start_utc_offset"] == "-21600s", workout["start_utc_offset"])
check("duration", workout["duration_s"] == 2700)

calories = calculate_calories(workout["avg_bpm"], workout["points"])
print("\ncalculate_calories")
check("plausible range", 200 < calories < 1500, str(calories))

payload = build_exercise_datapoint(workout, calories, DEFAULT_EXERCISE_TYPE)
ex = payload["exercise"]

print("\nbuild_exercise_datapoint")
check("top-level keys", set(payload) == {"dataSource", "exercise"})
check("recordingMethod", payload["dataSource"]["recordingMethod"] == "ACTIVELY_MEASURED")
check("interval keys",
      set(ex["interval"]) == {"startTime", "startUtcOffset", "endTime", "endUtcOffset"})
check("exerciseType", ex["exerciseType"] == "HIIT", ex["exerciseType"])
check("displayName", ex["displayName"] == "F45 Athletica", ex["displayName"])
check("activeDuration is a Duration", ex["activeDuration"] == "2700s", ex["activeDuration"])
check("calories metric", ex["metricsSummary"]["caloriesKcal"] == float(calories))
check("avg hr metric", ex["metricsSummary"]["averageHeartRateBeatsPerMinute"] == 132)
check("max bpm captured in notes", "168 max BPM" in ex["notes"], ex["notes"])
check("json serializable", json.dumps(payload) is not None)

print("\npayload:")
print(json.dumps(payload, indent=2))

if failures:
    print(f"\n{len(failures)} check(s) failed")
    raise SystemExit(1)
print("\nall checks passed")
