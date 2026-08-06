#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""Mint a refresh token for ONE scope group: gmail or health.

The Google Health API rejects any access token that carries non-Health scopes
(403 DISALLOWED_OAUTH_SCOPES, e.g. "mail_readonly"), so Gmail and Health cannot
share a token. This mints them separately, from the same OAuth client.

    set -a; source .env; set +a
    uv run get_refresh_token.py health   -> GOOGLE_REFRESH_TOKEN_HEALTH
    uv run get_refresh_token.py gmail    -> GOOGLE_REFRESH_TOKEN_GMAIL

Prerequisites in Google Cloud Console:
  * Gmail API and Google Health API enabled on the project
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

GROUPS = {
    "gmail": {
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "env_var": "GOOGLE_REFRESH_TOKEN_GMAIL",
    },
    "health": {
        "scopes": ["https://www.googleapis.com/auth/googlehealth.activity_and_fitness.writeonly"],
        "env_var": "GOOGLE_REFRESH_TOKEN_HEALTH",
    },
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in GROUPS:
        print(f"Usage: {sys.argv[0]} {{{'|'.join(GROUPS)}}}", file=sys.stderr)
        sys.exit(2)

    group = GROUPS[sys.argv[1]]
    scopes = group["scopes"]
    env_var = group["env_var"]

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.", file=sys.stderr)
        sys.exit(1)

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        # Deliberately NOT sending include_granted_scopes: incremental auth would
        # merge previously granted scopes into this token, which is exactly what
        # the Health API rejects.
    }
    print(f"\nMinting a {sys.argv[1].upper()}-only token for:")
    for s in scopes:
        print(f"  {s}")
    print("\n1. Open this URL in a browser signed in as the account that owns the workouts:\n")
    print(f"{AUTH_URL}?{urllib.parse.urlencode(params)}\n")
    print("2. Approve the scope. You will land on google.com with ?code=... in the URL bar.")
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
    granted = body.get("scope", "")

    if not refresh:
        print("\nNo refresh_token returned. Revoke the app at "
              "https://myaccount.google.com/permissions and try again.", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(1)

    granted_set = set(granted.split())
    expected_set = set(scopes)
    extra = granted_set - expected_set

    print(f"\nGranted scopes: {granted or '(none reported)'}")
    if extra:
        print("\nWARNING: this token carries scopes beyond what was requested:")
        for s in sorted(extra):
            print(f"  {s}")
        print("A Health token contaminated with other scopes will fail with 403 "
              "DISALLOWED_OAUTH_SCOPES. Revoke the app at "
              "https://myaccount.google.com/permissions, then mint the health token "
              "first and the gmail token second.")
    elif granted_set == expected_set:
        print("Scope set is clean.")

    print(f"\n{env_var}={refresh}")
    print("\nPut that in your .env / Portainer stack config.")


if __name__ == "__main__":
    main()
