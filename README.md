# F45 Lionheart to Google Health Sync

Syncs F45 Lionheart workout reports from Gmail to **Google Health**, logging an
exercise session with activity type (HIIT), estimated calories, average heart
rate, and session metadata.

> **Migrated off Google Fit.** The Google Fit REST API is deprecated and goes away
> by the end of 2026, and Google has stated there is no direct replacement for it.
> This project now writes to the [Google Health API](https://developers.google.com/health)
> (`health.googleapis.com/v4`), which launched in March 2026 as the cloud successor
> to the legacy Fitbit Web API. Data lands in the Google Health account rather than
> Google Fit; on Android, the Google Health app syncs into Health Connect.

## Prerequisites

A Google Cloud project with the **Gmail API** and **Google Health API** enabled,
plus an OAuth 2.0 client of type **Web application** with:

- Authorized redirect URI set to exactly `https://www.google.com`
- These scopes:
  - `https://www.googleapis.com/auth/gmail.readonly`
  - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.writeonly`

All `googlehealth.*` scopes are classified **Restricted**. An unverified client is
capped at 100 users, which is plenty for personal use — a third-party security
review is only required beyond that.

## Getting Refresh Tokens

**Two tokens are required.** The Google Health API rejects any access token that
carries scopes outside `googlehealth.*` — a combined Gmail + Health token fails with
`403 DISALLOWED_OAUTH_SCOPES` (`disallowed_scopes: mail_readonly`). Same OAuth
client, two separate consent flows:

```bash
set -a; source .env; set +a
uv run get_refresh_token.py health   # -> GOOGLE_REFRESH_TOKEN_HEALTH
uv run get_refresh_token.py gmail    # -> GOOGLE_REFRESH_TOKEN_GMAIL
```

Follow each printed URL, approve the scope, then paste the `code` value from the
resulting `https://www.google.com/?code=...` URL. The script prints the granted
scope set and warns if the token came back contaminated with extra scopes — if it
does, revoke the app at https://myaccount.google.com/permissions and retry.

The scripts carry PEP 723 inline metadata, so `uv run` resolves `requests` on its
own — no venv to create. Without uv: `python3 -m venv .venv &&
.venv/bin/pip install -r requirements.txt && .venv/bin/python get_refresh_token.py health`.

> **Publish the consent screen to Production.** While it sits in *Testing* status,
> Google issues refresh tokens that expire after **7 days** — the nightly job will
> start failing every week. Production (still unverified, 100-user cap) gives
> normal long-lived tokens.

## Configuration

Set these environment variables (via `.env` file or Portainer stack config):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CLIENT_ID` | Yes | | OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | | OAuth client secret |
| `GOOGLE_REFRESH_TOKEN_GMAIL` | Yes | | Refresh token holding only `gmail.readonly` |
| `GOOGLE_REFRESH_TOKEN_HEALTH` | Yes | | Refresh token holding only the googlehealth writeonly scope |
| `LOCAL_TIMEZONE` | No | `America/Denver` | Timezone for workout time calculation |
| `EXERCISE_TYPE` | No | `HIIT` | `Exercise.ExerciseType` enum value |
| `DRY_RUN` | No | | Set to `1` to print the payload instead of writing |
| `STATE_PATH` | No | `/data/sync_state.json` | State file location |

Google's docs don't publish the full `Exercise.ExerciseType` enum, so
`probe_exercise_types.py` determines it empirically — it pairs each candidate with
a deliberately invalid `recordingMethod` so the request always fails at parse time
and never writes, then reads the `fieldViolations` to see which field was rejected.

Verified accepted: `HIIT`, `BOOTCAMP`, `CIRCUIT_TRAINING`, `INTERVAL_WORKOUT`,
`AEROBIC_WORKOUT`, `WORKOUT`, `CROSS_TRAINING`, `FUNCTIONAL_STRENGTH_TRAINING`,
`STRENGTH_TRAINING`, `CROSSFIT`, `WEIGHTS`, `WEIGHTLIFTING`, `CALISTHENICS`,
`SPORT`, `OTHER`, `RUNNING`.

Verified rejected: `HIGH_INTENSITY_INTERVAL_TRAINING`, `BOOT_CAMP`,
`MIXED_CARDIO`, `GYM`, `INDOOR_WORKOUT`.

## Running

### Docker Compose

```bash
cp .env.example .env  # fill in your credentials
docker compose up --build
```

### Dry run

```bash
set -a; source .env; set +a
DRY_RUN=1 STATE_PATH=/tmp/state.json uv run sync.py
```

Prints the exact `dataPoints` payload for each unsynced workout without writing
anything and without touching the state file.

### Portainer

1. Create a new stack pointing to this repo (or paste the `docker-compose.yml` contents)
2. Set environment variables in the stack config
3. Deploy — the sync runs once immediately, then on the Ofelia sidecar's schedule

## Scheduling

Scheduling is handled by [Ofelia](https://github.com/mcuadros/ofelia), a Docker-based
job scheduler included as a sidecar in `docker-compose.yml`. The schedule is
configured via labels on the `f45-gfit-sync` service:

```yaml
ofelia.job-exec.sync.schedule: "0 0 7-19 * * *"
```

Ofelia uses a 6-field cron format: `second minute hour day month weekday`.

## How It Works

1. Refreshes both OAuth access tokens (Gmail and Health separately)
2. Searches Gmail for Lionheart report emails from the last 7 days
3. Parses workout details (class name, time, studio, points, BPM) from email snippets
4. Calculates estimated calories using the Keytel formula
5. `POST`s one exercise data point to
   `https://health.googleapis.com/v4/users/me/dataTypes/exercise/dataPoints`
6. Tracks processed emails in `/data/sync_state.json` for idempotency

### What changed from the Google Fit version

| Google Fit | Google Health |
|-----------|---------------|
| 3 calls per workout (activity segment dataset, calories dataset, session) | 1 call per workout |
| `dataSources` had to be created up front | no data source scaffolding — attribution is automatic |
| nanosecond epoch timestamps | RFC 3339 timestamps plus a UTC-offset Duration |
| `activityType: 113` | `exerciseType: HIIT` |
| calories as a separate `com.google.calories.expended` stream | `metricsSummary.caloriesKcal` on the session |
| deterministic session ID gave server-side idempotency | server assigns the name; dedup is local-only (state file) |
| one token covered Gmail + Fit | two tokens — Health refuses any token carrying Gmail scopes |

Max BPM and Lionheart points have no dedicated fields in the exercise schema, so
they are written into the session `notes` alongside the rest of the summary.

## State

State is persisted at `/data/sync_state.json` (Docker volume). It tracks:
- `processed_ids`: Gmail message IDs already handled
- `synced_workouts`: Dedup keys (`date|time|class`) to prevent duplicate entries

Because Google Health assigns its own data point names, the state file is the only
thing preventing duplicates — don't delete it.
