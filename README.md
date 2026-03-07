# F45 Lionheart to Google Fit Sync

Syncs F45 Lionheart workout reports from Gmail to Google Fit, logging activity type (HIIT), estimated calories, and session metadata.

## Prerequisites

A Google Cloud project with Gmail API and Fitness API enabled, plus OAuth 2.0 credentials with these scopes:
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/fitness.activity.write`
- `https://www.googleapis.com/auth/fitness.body.write`

## Getting a Refresh Token

1. Create OAuth 2.0 credentials (Desktop app) in [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Use the OAuth 2.0 Playground or a script to complete the consent flow and obtain a refresh token

## Configuration

Set these environment variables (via `.env` file or Portainer stack config):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CLIENT_ID` | Yes | | OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | | OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | Yes | | OAuth refresh token |
| `LOCAL_TIMEZONE` | No | `America/Denver` | Timezone for workout time calculation |

## Running

### Docker Compose

```bash
cp .env.example .env  # fill in your credentials
docker compose up --build
```

### Portainer

1. Create a new stack with the `docker-compose.yml` contents
2. Set environment variables in the stack config
3. Use Portainer's job scheduler to run on a cron schedule (e.g., every 2 hours)

## How It Works

1. Refreshes OAuth access token
2. Searches Gmail for Lionheart report emails from the last 7 days
3. Parses workout details (class name, time, studio, points, BPM) from email snippets
4. Calculates estimated calories using the Keytel formula
5. Logs activity segment (HIIT), calories, and session to Google Fit
6. Tracks processed emails in `/data/sync_state.json` for idempotency

## State

State is persisted at `/data/sync_state.json` (Docker volume). It tracks:
- `processed_ids`: Gmail message IDs already handled
- `synced_workouts`: Dedup keys (`date|time|class`) to prevent duplicate entries
