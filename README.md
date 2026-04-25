# email-connector (now broadened to Luma calendars)

Fetches event-related emails from a Gmail label via IMAP *and* directly fetches
upcoming events from key Luma calendars (e.g. FTLYR, hello_miami, labmiami,
delphica). Dedups them into `upcoming_events.json`, rotates past events out,
and — when there's something worth saying — hands the newly discovered events
to an OpenClaw TaskFlow that DMs you about them on Slack.

## How it works

1. Emails from your private groups arrive in Gmail
2. A Gmail filter auto-labels them with your chosen label (e.g. `miami-social-event-source`)
3. This script connects via IMAP to read new emails *and* directly scrapes key Luma calendars
4. Events from both sources are dedup'd, filtered to the next 7 days, and merged into `upcoming_events.json` (with rotated-out events moving to `past_events.json`)
5. Run status is written to `health.json`
6. If this run discovered any *newly* upcoming events — or the run errored — a JSON trigger artifact is written and the connector POSTs to the Gateway's Webhooks plugin, creating a managed TaskFlow. The agent reads the artifact, composes a summary, and DMs you on Slack. Runs with nothing new and no errors are silent.
7. A cron job runs the script every 30 minutes on the Lightsail instance

## Prerequisites

### 1. Enable IMAP in Gmail

Gmail Settings > See all settings > Forwarding and POP/IMAP > Enable IMAP

### 2. Generate an App Password

Google Account > Security > 2-Step Verification > App passwords

Create one named "email-connector" scoped to Mail. This is what goes in your `.env`.
Do not use your real Gmail password.

### 3. Create a Gmail label and filter

- In Gmail, create a label (e.g. `miami-social-event-source`)
- Create a filter: From: [sender addresses of your private groups] > Apply label: `miami-social-event-source`
- Existing emails can be manually labeled; new ones will be labeled automatically

## Installation (on Lightsail)

```bash
git clone <your-repo> /opt/email-connector
cd /opt/email-connector

python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env  # fill in your credentials and output paths
```

## Configuration

All configuration lives in `.env`. Copy `.env.example` and fill in:

| Variable | Description |
|---|---|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | App password from Google account security settings |
| `GMAIL_LABEL` | Gmail label to read from (default: `miami-social-event-source`) |
| `LUMA_CALENDARS` | Comma-separated Luma calendar slugs (e.g. FTLYR,hello_miami,labmiami,delphica) |
| `UPCOMING_EVENTS_PATH` | Where to write `upcoming_events.json` (absolute path) |
| `PAST_EVENTS_PATH` | Where to write `past_events.json` (absolute path) |
| `HEALTH_OUTPUT_PATH` | Where to write `health.json` (absolute path) |
| `LOG_PATH` | Where to write `connector.log` (absolute path) |
| `PROCESSED_IDS_PATH` | Tracks seen Message-IDs to prevent duplicates |
| `TASKFLOW_USER_TARGET` | Slack user ID the agent should DM with its final message (i.e. you) |
| `TASKFLOW_TRIGGER_PATH` | Where to write the JSON trigger artifact the agent reads |
| `TASKFLOW_WEBHOOK_URL` | Gateway Webhooks plugin URL that creates the managed TaskFlow (usually `http://localhost:18789/plugins/webhooks/<route>`) |
| `TASKFLOW_WEBHOOK_SECRET` | Shared secret matching the webhook route's configured secret on the Gateway |

## Running manually

```bash
cd /opt/email-connector
venv/bin/python fetch_events.py
```

## Cron setup

Edit the crontab with `crontab -e` and add:

```
*/30 * * * * /opt/email-connector/venv/bin/python /opt/email-connector/fetch_events.py
```

This runs every 30 minutes. Adjust the interval to taste.

## Output files

### `upcoming_events.json`

Events whose date is today or in the future. This is the primary file the OpenClaw bot
reads to suggest things to do.

### `past_events.json`

Events that have already passed. On each run, any event in `upcoming_events.json` whose
date is now before today is automatically moved here. The bot can use this for context
or ignore it entirely.

Both files contain the same event object shape:

```json
[
  {
    "title": "Summer Rooftop Gathering",
    "date": "2026-04-12",
    "time": "18:00",
    "end_date": "2026-04-12",
    "end_time": "21:00",
    "location": "123 Main St, Rooftop",
    "description": "Bring a dish to share...",
    "luma_url": "https://lu.ma/abc123?tk=xxxxxx",
    "parse_method": "luma_nextdata",
    "source_email": "events@privategroup.org",
    "source_message_id": "<abc123@mail.gmail.com>",
    "source": "luma_calendar:FTLYR",
    "fetched_at": "2026-03-15T14:00:00Z"
  }
]
```

`parse_method` indicates how the event was extracted:

| Value | Source |
|---|---|
| `luma_jsonld` | JSON-LD structured data on the Luma event page |
| `luma_nextdata` | Next.js server-side props on the Luma event page |
| `luma_calendar_nextdata` | Featured-events block on a Luma calendar/group page |
| `luma_html_scrape` | Best-effort HTML scrape of the Luma page |
| `ics` | `.ics` calendar attachment in the email |
| `body_text` | Heuristic date/time extraction from plain text body |

`source` is set on events discovered by directly crawling a Luma calendar
(`luma_calendar:<slug-or-url>`). Events parsed out of emails leave it unset
but carry `source_email` / `source_message_id` instead.

### `health.json`

Current-state snapshot written after every run. The bot checks this to detect failures.

```json
{
  "last_run": "2026-03-15T14:30:00Z",
  "last_success": "2026-03-15T14:30:00Z",
  "consecutive_failures": 0,
  "last_error": null,
  "last_error_time": null,
  "emails_processed_last_run": 2,
  "upcoming_events": 6,
  "past_events": 12
}
```

If `consecutive_failures > 0`, something is wrong. Common causes:
- `IMAP authentication failed` — app password expired or IMAP disabled in Gmail
- `[IMAP folder] not found` — the label name in `.env` doesn't match what's in Gmail

### `connector.log`

Append-only log of every run. Useful for diagnosing issues or reviewing history.

### Trigger artifact (`TASKFLOW_TRIGGER_PATH`)

Written (and overwritten) at the end of each run that has something to surface.
Consumed by the agent via `--path` on the trigger Slack message.

```json
{
  "triggered_at": "2026-04-23T18:05:00",
  "health": { "...": "same shape as health.json" },
  "newEvents": [ { "...": "events discovered for the first time this run" } ],
  "hasErrors": false,
  "targetUserId": "U0AL1GKMR6J",
  "requirements": {
    "step1": "If hasErrors, DM the user a brief description of health.last_error.",
    "step2": "Otherwise, read through newEvents, cross-ref committed-events.md and pm-rhythm, flag 1-2 best, suggest balance, and offer to commit."
  }
}
```

## Agent integration

The connector pushes to the agent rather than the agent polling. On each run:

1. `health.json` and `upcoming_events.json` / `past_events.json` are written as usual.
2. If there are newly discovered events *or* the run errored, the connector
   writes the trigger artifact to `TASKFLOW_TRIGGER_PATH` and POSTs to
   `TASKFLOW_WEBHOOK_URL` with `Authorization: Bearer <TASKFLOW_WEBHOOK_SECRET>`.
   The body is a `create_flow` action whose `goal` points the agent at the
   artifact on disk.
3. The Gateway's Webhooks plugin creates a managed TaskFlow bound to the
   configured agent session. The agent reads the artifact, works through
   `newEvents` itself (no keyword pre-filter), and DMs its final message to
   `TASKFLOW_USER_TARGET`.
4. If `hasErrors` is true, the agent synthesizes a human-readable summary from
   `health.last_error` instead of running the radar.

Runs that find nothing new and have a clean health status are silent — no
TaskFlow is created, no DM is produced.

### Gateway setup (one-time)

The Webhooks plugin (OpenClaw 2026.4.7+) must be configured on the Gateway.
Add a route under `plugins.entries.webhooks.config.routes`:

```json5
{
  miami_social_radar: {
    path: "/plugins/webhooks/miami-social-radar",
    sessionKey: "agent:main:main",
    secret: {
      source: "env",
      provider: "default",
      id: "MIAMI_SOCIAL_RADAR_WEBHOOK_SECRET",
    },
    description: "Trigger miami-social event radar flow from email-connector",
  },
}
```

Set `MIAMI_SOCIAL_RADAR_WEBHOOK_SECRET` in the Gateway's environment to a
strong random string, restart the Gateway, then put the same string into this
project's `.env` as `TASKFLOW_WEBHOOK_SECRET`.

## Updating

```bash
cd /opt/email-connector
git pull
venv/bin/pip install -r requirements.txt  # only needed if requirements changed
```