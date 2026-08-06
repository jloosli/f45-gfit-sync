#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""Discover which Exercise.ExerciseType enum values the Google Health API accepts.

Non-destructive: every request also carries a deliberately invalid
dataSource.recordingMethod, so the API always rejects the request at proto-parse
time and never writes a data point. We then read the fieldViolations:

  * a violation on `exercise_type`  -> candidate is INVALID
  * violations only on `recording_method` -> candidate is VALID

Run with the same env as sync.py:

    set -a; source .env; set +a
    uv run probe_exercise_types.py
"""

import os
import sys

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
URL = "https://health.googleapis.com/v4/users/me/dataTypes/exercise/dataPoints"

POISON = "__PROBE_NOT_A_RECORDING_METHOD__"

# Ordered roughly by how likely they are to suit an F45 class.
CANDIDATES = [
    "RUNNING",                            # control: known valid
    "HIGH_INTENSITY_INTERVAL_TRAINING",   # control: known invalid
    "AEROBIC_WORKOUT",                    # seen in Google's own docs example
    "BOOTCAMP",
    "BOOT_CAMP",
    "CIRCUIT_TRAINING",
    "INTERVAL_WORKOUT",
    "HIIT",
    "WORKOUT",
    "FUNCTIONAL_STRENGTH_TRAINING",
    "STRENGTH_TRAINING",
    "CROSS_TRAINING",
    "CROSSFIT",
    "WEIGHTS",
    "WEIGHTLIFTING",
    "CALISTHENICS",
    "MIXED_CARDIO",
    "GYM",
    "INDOOR_WORKOUT",
    "SPORT",
    "OTHER",
]


def refresh_access_token() -> str:
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if not all([cid, secret, refresh]):
        print("Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN",
              file=sys.stderr)
        sys.exit(1)
    resp = requests.post(TOKEN_URL, data={
        "client_id": cid, "client_secret": secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        print(f"Token refresh failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)
    return resp.json()["access_token"]


def probe(token: str, candidate: str):
    """Return (verdict, detail). verdict in {VALID, INVALID, WROTE, UNKNOWN}."""
    payload = {
        "dataSource": {"recordingMethod": POISON},
        "exercise": {
            "interval": {
                "startTime": "2026-01-01T12:00:00Z",
                "startUtcOffset": "0s",
                "endTime": "2026-01-01T12:01:00Z",
                "endUtcOffset": "0s",
            },
            "exerciseType": candidate,
            "displayName": "probe",
            "activeDuration": "60s",
            "metricsSummary": {"caloriesKcal": 1.0},
        },
    }
    resp = requests.post(
        URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )

    if resp.status_code in (200, 201):
        return "WROTE", resp.json().get("name", "(unnamed)")

    body = resp.text
    if resp.status_code != 400:
        return "UNKNOWN", f"HTTP {resp.status_code}: {body[:200]}"

    # Field violations may be structured, or the message may just name the field.
    if "exercise_type" in body or "exerciseType" in body:
        return "INVALID", ""
    if "recording_method" in body or "recordingMethod" in body:
        return "VALID", ""
    return "UNKNOWN", body[:200]


def main():
    token = refresh_access_token()
    print("Self-test: confirming the probe never writes...\n")

    verdict, detail = probe(token, "RUNNING")
    if verdict == "WROTE":
        print("ABORT: the poison recordingMethod did not stop the write. "
              f"A data point was created: {detail}\n"
              "Delete it in the Google Health app and tell Claude -- the probe "
              "technique needs rethinking.", file=sys.stderr)
        sys.exit(1)
    if verdict != "VALID":
        print(f"ABORT: control value RUNNING came back {verdict} ({detail}).\n"
              "Expected VALID. The probe logic is unreliable here; stop and report this.",
              file=sys.stderr)
        sys.exit(1)
    print("  ok -- RUNNING reads as VALID and nothing was written.\n")

    valid, invalid, unknown = [], [], []
    for c in CANDIDATES:
        verdict, detail = probe(token, c)
        if verdict == "WROTE":
            print(f"  !! {c} unexpectedly wrote data point {detail} -- stopping.",
                  file=sys.stderr)
            break
        marker = {"VALID": "VALID  ", "INVALID": "  -    ", "UNKNOWN": "?????  "}[verdict]
        print(f"  {marker} {c}" + (f"   {detail}" if detail else ""))
        {"VALID": valid, "INVALID": invalid, "UNKNOWN": unknown}[verdict].append(c)

    print(f"\nAccepted ({len(valid)}): {', '.join(valid) or 'none'}")
    print(f"Rejected ({len(invalid)}): {', '.join(invalid) or 'none'}")
    if unknown:
        print(f"Inconclusive ({len(unknown)}): {', '.join(unknown)}")
    print("\nNothing was written to Google Health.")


if __name__ == "__main__":
    main()
