#!/usr/bin/env python3
"""Mint a refresh token for the Gmail + Google Health scopes.

Run locally (not in the container):

    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python3 get_refresh_token.py

Prerequisites in Google Cloud Console:
  * Google Health API enabled on the project
  * OAuth client of type "Web application"
  * Authorized redirect URI set to exactly  https://www.google.com
  * Your Google account added under Test users (or the app published to Production)

Note: while the OAuth consent screen sits in "Testing" status, refresh tokens
expire after 7 days. Publish to Production (still capped at 100 users while
unverified) to get long-lived tokens, otherwise the nightly job dies weekly.
"""

import os
import sys
import urllib.parse

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "https://www.google.com"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.writeonly",
]


def main():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.", file=sys.stderr)
        sys.exit(1)

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    print("\n1. Open this URL in a browser signed in as the account that owns the workouts:\n")
    print(f"{AUTH_URL}?{urllib.parse.urlencode(params)}\n")
    print("2. Approve both scopes. You will land on google.com with ?code=... in the URL bar.")
    print("3. Paste the value of the `code` parameter here (URL-decode %2F to / if needed).\n")

    code = input("code: ").strip()
    if not code:
        print("No code entered.", file=sys.stderr)
        sys.exit(1)

    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)

    if resp.status_code != 200:
        print(f"\nToken exchange failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    body = resp.json()
    refresh = body.get("refresh_token")
    if not refresh:
        print("\nNo refresh_token returned. Add prompt=consent and access_type=offline, "
              "or revoke the app's access and try again.", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(1)

    print("\nGranted scopes:", body.get("scope", "(none reported)"))
    print("\nGOOGLE_REFRESH_TOKEN=" + refresh)
    print("\nPut that in your .env / Portainer stack config.")


if __name__ == "__main__":
    main()
