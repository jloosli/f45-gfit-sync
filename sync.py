#!/usr/bin/env python3
"""Sync F45 Lionheart workout data from Gmail to Google Fit."""

import json
import logging
import os
import re
import sys
import time
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
FIT_API = "https://www.googleapis.com/fitness/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"

PROJECT_NUMBER = "415791213904"
APP_NAME = "f45_lionheart_sync"
ACTIVITY_DS = f"raw:com.google.activity.segment:{PROJECT_NUMBER}:{APP_NAME}"
CALORIES_DS = f"raw:com.google.calories.expended:{PROJECT_NUMBER}:{APP_NAME}_calories"

STATE_PATH = os.environ.get("STATE_PATH", "/data/sync_state.json")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        log.error("Token refresh failed (%d): %s", resp.status_code, resp.text)
        log.error("Your refresh token may have expired. Re-run the OAuth flow to get a new one.")
        sys.exit(1)
    token = resp.json().get("access_token")
    if not token:
        log.error("No access_token in response: %s", resp.text)
        sys.exit(1)
    log.info("Access token refreshed successfully")
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

    end_dt = start_dt + timedelta(minutes=45)

    return {
        "class_name": class_name,
        "studio": studio,
        "class_time": class_time_str,
        "points": points,
        "avg_bpm": avg_bpm,
        "max_bpm": max_bpm,
        "start_ms": int(start_dt.timestamp() * 1000),
        "end_ms": int(end_dt.timestamp() * 1000),
        "start_ns": str(int(start_dt.timestamp() * 1_000_000_000)),
        "end_ns": str(int(end_dt.timestamp() * 1_000_000_000)),
        "dedup_key": f"{start_dt.date()}|{class_time_str}|{class_name}",
    }


def calculate_calories(avg_bpm: int, points: int, duration_min: int = 45) -> int:
    # Keytel formula (male, weight=120.2kg, age=51)
    cal_per_min = (-55.0969 + 0.6309 * avg_bpm + 0.1988 * 120.2 + 0.2017 * 51) / 4.184
    base_calories = max(0, cal_per_min * duration_min)
    intensity_scale = 0.6 + 0.4 * min(points / 45.0, 1.0)
    return round(base_calories * intensity_scale)


def ensure_fit_data_sources(access_token: str) -> None:
    sources = [
        {
            "dataStreamId": ACTIVITY_DS,
            "dataStreamName": APP_NAME,
            "type": "raw",
            "application": {"name": "F45 Lionheart Sync"},
            "dataType": {"name": "com.google.activity.segment"},
        },
        {
            "dataStreamId": CALORIES_DS,
            "dataStreamName": f"{APP_NAME}_calories",
            "type": "raw",
            "application": {"name": "F45 Lionheart Sync"},
            "dataType": {"name": "com.google.calories.expended"},
        },
    ]
    for src in sources:
        resp = requests.post(
            f"{FIT_API}/dataSources",
            headers={**auth_headers(access_token), "Content-Type": "application/json"},
            json=src,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            log.info("Created data source: %s", src["dataStreamId"])
        elif resp.status_code == 409:
            log.debug("Data source already exists: %s", src["dataStreamId"])
        else:
            log.error("Failed to create data source %s (%d): %s",
                       src["dataStreamId"], resp.status_code, resp.text)
            raise RuntimeError(f"Data source creation failed: {src['dataStreamId']}")


def _patch_dataset(access_token: str, data_source_id: str, start_ns: str, end_ns: str,
                   value_key: str, value) -> None:
    dataset_id = f"{start_ns}-{end_ns}"
    url = f"{FIT_API}/dataSources/{data_source_id}/datasets/{dataset_id}"
    body = {
        "dataSourceId": data_source_id,
        "minStartTimeNs": start_ns,
        "maxEndTimeNs": end_ns,
        "point": [{
            "dataTypeName": data_source_id.split(":")[1],
            "startTimeNanos": start_ns,
            "endTimeNanos": end_ns,
            "value": [{value_key: value}],
        }],
    }
    resp = requests.patch(
        url,
        headers={**auth_headers(access_token), "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        log.error("Failed to patch dataset %s (%d): %s", dataset_id, resp.status_code, resp.text)
        raise RuntimeError(f"Dataset patch failed for {data_source_id}")


def log_to_google_fit(access_token: str, workout: dict, calories: int) -> None:
    # Activity segment (HIIT = 113)
    _patch_dataset(access_token, ACTIVITY_DS,
                   workout["start_ns"], workout["end_ns"], "intVal", 113)
    log.info("Logged activity segment")

    # Calories
    _patch_dataset(access_token, CALORIES_DS,
                   workout["start_ns"], workout["end_ns"], "fpVal", float(calories))
    log.info("Logged calories: %d", calories)

    # Session
    session_id = f"f45_lionheart_{workout['start_ms']}"
    session_body = {
        "id": session_id,
        "name": f"F45 {workout['class_name']}",
        "description": (
            f"{workout['class_name']} at {workout['studio']} - "
            f"{workout['points']} pts, {workout['avg_bpm']} avg BPM, "
            f"{calories} cal"
        ),
        "startTimeMillis": workout["start_ms"],
        "endTimeMillis": workout["end_ms"],
        "activityType": 113,
        "application": {"name": "F45 Lionheart Sync"},
    }
    resp = requests.put(
        f"{FIT_API}/sessions/{session_id}",
        headers={**auth_headers(access_token), "Content-Type": "application/json"},
        json=session_body,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        log.error("Failed to create session (%d): %s", resp.status_code, resp.text)
        raise RuntimeError("Session creation failed")
    log.info("Created session: %s", session_id)


def main():
    # Read env vars
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    tz_name = os.environ.get("LOCAL_TIMEZONE", "America/Denver")

    if not all([client_id, client_secret, refresh_token]):
        log.error("Missing required env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN")
        sys.exit(1)

    tz = ZoneInfo(tz_name)
    log.info("Starting F45 Lionheart to Google Fit sync (tz=%s)", tz_name)

    access_token = refresh_access_token(client_id, client_secret, refresh_token)
    state = load_state(STATE_PATH)
    processed_ids = set(state.get("processed_ids", []))
    synced_keys = set(state.get("synced_workouts", []))

    messages = search_lionheart_emails(access_token)
    new_msgs = [m for m in messages if m["id"] not in processed_ids]
    log.info("%d new message(s) to process", len(new_msgs))

    data_sources_ensured = False
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
            if not data_sources_ensured:
                ensure_fit_data_sources(access_token)
                data_sources_ensured = True

            log_to_google_fit(access_token, workout, calories)
        except RuntimeError as e:
            log.error("Failed to sync workout %s: %s", workout["dedup_key"], e)
            skipped += 1
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
