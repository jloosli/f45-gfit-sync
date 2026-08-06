#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""Sync F45 Lionheart workout data from Gmail to Google Health.

Google Fit's REST API is deprecated (end of 2026). This writes to the Google
Health API instead: POST /v4/users/me/dataTypes/exercise/dataPoints.

Docs: https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
HEALTH_API = "https://health.googleapis.com/v4/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Google Health data type written to (kebab-case path segment).
EXERCISE_DATA_TYPE = "exercise"

# Exercise.ExerciseType enum. HIIT matches the activityType 113 this used to send
# to Google Fit. Also accepted (verified with probe_exercise_types.py): BOOTCAMP,
# CIRCUIT_TRAINING, INTERVAL_WORKOUT, AEROBIC_WORKOUT, WORKOUT, CROSS_TRAINING,
# FUNCTIONAL_STRENGTH_TRAINING, STRENGTH_TRAINING, CROSSFIT, WEIGHTS,
# WEIGHTLIFTING, CALISTHENICS, SPORT, OTHER.
DEFAULT_EXERCISE_TYPE = "HIIT"

# dataSource.recordingMethod -- the BPM and points come off a real Lionheart strap
# during a tracked class, so this is actively measured rather than hand-entered.
RECORDING_METHOD = "ACTIVELY_MEASURED"

CLASS_DURATION_MIN = 45

STATE_PATH = os.environ.get("STATE_PATH", "/data/sync_state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str,
                         label: str) -> str:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        log.error("%s token refresh failed (%d): %s", label, resp.status_code, resp.text)
        log.error("Re-run `get_refresh_token.py %s` to mint a new one. Note: if your "
                  "OAuth consent screen is still in Testing status, refresh tokens "
                  "expire after 7 days -- publish the app to Production to avoid that.",
                  label)
        sys.exit(1)
    token = resp.json().get("access_token")
    if not token:
        log.error("No access_token in %s response: %s", label, resp.text)
        sys.exit(1)
    log.info("%s access token refreshed successfully", label)
    return token


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log.info("No existing state file, starting fresh")
        return {"processed_ids": [], "synced_workouts": []}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log.error("Failed to write state file: %s", e)
        sys.exit(1)


def auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def search_lionheart_emails(access_token: str) -> list[dict]:
    query = 'from:no-reply@f45training.com subject:"Your LionHeart Report is ready" newer_than:7d'
    resp = requests.get(
        f"{GMAIL_API}/messages",
        headers=auth_headers(access_token),
        params={"q": query, "maxResults": 50},
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("Gmail search failed (%d): %s", resp.status_code, resp.text)
        sys.exit(1)
    messages = resp.json().get("messages", [])
    log.info("Found %d Lionheart email(s) in last 7 days", len(messages))
    return messages


def get_email(access_token: str, msg_id: str) -> dict | None:
    resp = requests.get(
        f"{GMAIL_API}/messages/{msg_id}",
        headers=auth_headers(access_token),
        params={"format": "full"},
        timeout=30,
    )
    if resp.status_code != 200:
        log.warning("Failed to fetch email %s (%d): %s", msg_id, resp.status_code, resp.text)
        return None
    return resp.json()


def _utc_offset_duration(dt: datetime) -> str:
    """Google Health wants UTC offsets as a protobuf Duration string, e.g. '-21600s'."""
    offset = dt.utcoffset() or timedelta(0)
    return f"{int(offset.total_seconds())}s"


def parse_workout(snippet: str, internal_date_ms: int, tz: ZoneInfo) -> dict | None:
    summary_match = re.search(
        r"Your\s+(\d{1,2}:\d{2}\s*[AP]M)\s+(.+?)\s+class\s+at\s+(.+?)\s+summary",
        snippet,
        re.IGNORECASE,
    )
    stats_match = re.search(
        r"(\d+)\s*POINTS\s+(\d+)\s*AVG\s*BPM\s+(\d+)\s*MAX\s*BPM",
        snippet,
        re.IGNORECASE,
    )
    if not summary_match or not stats_match:
        log.warning("Could not parse snippet: %s", snippet[:120])
        return None

    class_time_str = summary_match.group(1).strip()
    class_name = summary_match.group(2).strip()
    studio = summary_match.group(3).strip()

    points = int(stats_match.group(1))
    avg_bpm = int(stats_match.group(2))
    max_bpm = int(stats_match.group(3))

    # Use email internalDate for the date, parse class time for hour/minute
    email_dt = datetime.fromtimestamp(internal_date_ms / 1000, tz=tz)
    class_time = datetime.strptime(class_time_str.upper().replace(" ", ""), "%I:%M%p")
    start_dt = email_dt.replace(
        hour=class_time.hour, minute=class_time.minute, second=0, microsecond=0
    )
    # If computed start is after email time, class was previous day
    if start_dt > email_dt:
        start_dt -= timedelta(days=1)

    end_dt = start_dt + timedelta(minutes=CLASS_DURATION_MIN)

    return {
        "class_name": class_name,
        "studio": studio,
        "class_time": class_time_str,
        "points": points,
        "avg_bpm": avg_bpm,
        "max_bpm": max_bpm,
        "duration_s": CLASS_DURATION_MIN * 60,
        "start_rfc3339": start_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_rfc3339": end_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_utc_offset": _utc_offset_duration(start_dt),
        "end_utc_offset": _utc_offset_duration(end_dt),
        "dedup_key": f"{start_dt.date()}|{class_time_str}|{class_name}",
    }


def calculate_calories(avg_bpm: int, points: int, duration_min: int = CLASS_DURATION_MIN) -> int:
    # Keytel formula (male, weight=120.2kg, age=51)
    cal_per_min = (-55.0969 + 0.6309 * avg_bpm + 0.1988 * 120.2 + 0.2017 * 51) / 4.184
    base_calories = max(0, cal_per_min * duration_min)
    intensity_scale = 0.6 + 0.4 * min(points / 45.0, 1.0)
    return round(base_calories * intensity_scale)


def build_exercise_datapoint(workout: dict, calories: int, exercise_type: str) -> dict:
    """Build a DataPoint with the `exercise` union field set.

    Field names follow the v4 Exercise / MetricsSummary schemas. Only calories and
    average heart rate are sent as metrics -- max BPM and Lionheart points have no
    dedicated fields, so they ride along in `notes`.
    """
    return {
        "dataSource": {"recordingMethod": RECORDING_METHOD},
        "exercise": {
            "interval": {
                "startTime": workout["start_rfc3339"],
                "startUtcOffset": workout["start_utc_offset"],
                "endTime": workout["end_rfc3339"],
                "endUtcOffset": workout["end_utc_offset"],
            },
            "exerciseType": exercise_type,
            "displayName": f"F45 {workout['class_name']}",
            "activeDuration": f"{workout['duration_s']}s",
            "metricsSummary": {
                "caloriesKcal": float(calories),
                "averageHeartRateBeatsPerMinute": workout["avg_bpm"],
            },
            "notes": (
                f"{workout['class_name']} at {workout['studio']} - "
                f"{workout['points']} pts, {workout['avg_bpm']} avg BPM, "
                f"{workout['max_bpm']} max BPM, {calories} cal"
            ),
        }
    }


def log_to_google_health(access_token: str, workout: dict, calories: int,
                         exercise_type: str) -> None:
    payload = build_exercise_datapoint(workout, calories, exercise_type)

    if DRY_RUN:
        log.info("DRY_RUN -- would POST:\n%s", json.dumps(payload, indent=2))
        return

    url = f"{HEALTH_API}/dataTypes/{EXERCISE_DATA_TYPE}/dataPoints"
    resp = requests.post(
        url,
        headers={**auth_headers(access_token), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        log.error("Failed to create exercise data point (%d): %s",
                  resp.status_code, resp.text)
        if resp.status_code == 400 and "exercise_type" in resp.text:
            log.error("Invalid EXERCISE_TYPE. Accepted values include HIIT, BOOTCAMP, "
                      "CIRCUIT_TRAINING, INTERVAL_WORKOUT, AEROBIC_WORKOUT, WORKOUT, "
                      "CROSS_TRAINING, STRENGTH_TRAINING, OTHER. Run "
                      "probe_exercise_types.py to test others.")
        if resp.status_code == 403 and "DISALLOWED_OAUTH_SCOPES" in resp.text:
            log.error("The health token carries scopes the Health API refuses (it will "
                      "not accept a token that also holds Gmail scopes). Mint a "
                      "health-only token: `get_refresh_token.py health`.")
        elif resp.status_code == 403:
            log.error("403 can mean the googlehealth activity_and_fitness writeonly scope "
                      "is missing from the token, or the Google Health API is not enabled "
                      "on the Cloud project that owns your OAuth client.")
        raise RuntimeError("Exercise data point creation failed")

    try:
        body = resp.json()
    except ValueError:
        body = {}
    created = body.get("name") or body.get("dataPoint", {}).get("name")
    if created:
        log.info("Created exercise data point: %s (%d cal, %d avg BPM)",
                 created, calories, workout["avg_bpm"])
    else:
        # No name field in the response -- log the shape so the ID can be recovered.
        log.info("Created exercise data point (%d cal, %d avg BPM); response: %s",
                 calories, workout["avg_bpm"], json.dumps(body)[:400] or "(empty)")


def main():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    gmail_refresh = os.environ.get("GOOGLE_REFRESH_TOKEN_GMAIL")
    health_refresh = os.environ.get("GOOGLE_REFRESH_TOKEN_HEALTH")
    tz_name = os.environ.get("LOCAL_TIMEZONE", "America/Denver")
    exercise_type = os.environ.get("EXERCISE_TYPE", DEFAULT_EXERCISE_TYPE)

    if os.environ.get("GOOGLE_REFRESH_TOKEN") and not (gmail_refresh and health_refresh):
        log.error("GOOGLE_REFRESH_TOKEN is no longer used. The Google Health API rejects "
                  "tokens that also carry Gmail scopes, so this now needs two separate "
                  "tokens: GOOGLE_REFRESH_TOKEN_GMAIL and GOOGLE_REFRESH_TOKEN_HEALTH. "
                  "Mint them with `get_refresh_token.py gmail` and "
                  "`get_refresh_token.py health`.")
        sys.exit(1)

    if not all([client_id, client_secret, gmail_refresh, health_refresh]):
        log.error("Missing required env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
                  "GOOGLE_REFRESH_TOKEN_GMAIL, GOOGLE_REFRESH_TOKEN_HEALTH")
        sys.exit(1)

    tz = ZoneInfo(tz_name)
    log.info("Starting F45 Lionheart to Google Health sync (tz=%s, type=%s%s)",
             tz_name, exercise_type, ", DRY_RUN" if DRY_RUN else "")

    access_token = refresh_access_token(client_id, client_secret, gmail_refresh, "gmail")
    health_token = (
        refresh_access_token(client_id, client_secret, health_refresh, "health")
        if not DRY_RUN else ""
    )
    state = load_state(STATE_PATH)
    processed_ids = set(state.get("processed_ids", []))
    synced_keys = set(state.get("synced_workouts", []))

    messages = search_lionheart_emails(access_token)
    new_msgs = [m for m in messages if m["id"] not in processed_ids]
    log.info("%d new message(s) to process", len(new_msgs))

    synced = 0
    skipped = 0

    for msg in new_msgs:
        msg_id = msg["id"]
        email = get_email(access_token, msg_id)
        if email is None:
            skipped += 1
            continue

        snippet = email.get("snippet", "")
        internal_date_ms = int(email.get("internalDate", 0))
        workout = parse_workout(snippet, internal_date_ms, tz)
        if workout is None:
            skipped += 1
            continue

        # Dedup check
        if workout["dedup_key"] in synced_keys:
            log.info("Skipping duplicate workout: %s", workout["dedup_key"])
            processed_ids.add(msg_id)
            state["processed_ids"] = list(processed_ids)
            save_state(STATE_PATH, state)
            continue

        calories = calculate_calories(workout["avg_bpm"], workout["points"])

        try:
            log_to_google_health(health_token, workout, calories, exercise_type)
        except RuntimeError as e:
            log.error("Failed to sync workout %s: %s", workout["dedup_key"], e)
            skipped += 1
            continue

        if DRY_RUN:
            synced += 1
            continue

        # Mark processed
        processed_ids.add(msg_id)
        synced_keys.add(workout["dedup_key"])
        state["processed_ids"] = list(processed_ids)
        state["synced_workouts"] = list(synced_keys)
        save_state(STATE_PATH, state)
        synced += 1
        log.info("Synced: %s (%s) - %d cal", workout["class_name"], workout["dedup_key"], calories)

    log.info("Done. Synced: %d, Skipped: %d", synced, skipped)


if __name__ == "__main__":
    main()
