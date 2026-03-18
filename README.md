# email-connector

Fetches event-related emails from a Gmail label via IMAP, extracts event details
(from `.ics` attachments or plain-text body), and writes structured output files
for the OpenClaw bot's calendar skill to read.

## How it works

1. Emails from your private groups arrive in Gmail
2. A Gmail filter auto-labels them with your chosen label (e.g. `miami-social-event-source`)
3. This script connects via IMAP, reads new emails from that label, and extracts events
4. Events are written to `events.json`; run status is written to `health.json`
5. A cron job runs the script every 30 minutes on the Lightsail instance

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
| `UPCOMING_EVENTS_PATH` | Where to write `upcoming_events.json` (absolute path) |
| `PAST_EVENTS_PATH` | Where to write `past_events.json` (absolute path) |
| `HEALTH_OUTPUT_PATH` | Where to write `health.json` (absolute path) |
| `LOG_PATH` | Where to write `connector.log` (absolute path) |
| `PROCESSED_IDS_PATH` | Tracks seen Message-IDs to prevent duplicates |

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
    "fetched_at": "2026-03-15T14:00:00Z"
  }
]
```

`parse_method` indicates how the event was extracted:

| Value | Source |
|---|---|
| `luma_jsonld` | JSON-LD structured data on the Luma event page |
| `luma_nextdata` | Next.js server-side props on the Luma event page |
| `luma_html_scrape` | Best-effort HTML scrape of the Luma page |
| `ics` | `.ics` calendar attachment in the email |
| `body_text` | Heuristic date/time extraction from plain text body |

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
  "total_events": 8
}
```

If `consecutive_failures > 0`, something is wrong. Common causes:
- `IMAP authentication failed` — app password expired or IMAP disabled in Gmail
- `[IMAP folder] not found` — the label name in `.env` doesn't match what's in Gmail

### `connector.log`

Append-only log of every run. Useful for diagnosing issues or reviewing history.

## Bot integration

Point the OpenClaw calendar skill at `UPCOMING_EVENTS_PATH` to read events.
`PAST_EVENTS_PATH` is available if the bot needs historical context.

For monitoring, the bot should read `health.json` on a schedule and alert if:
- `consecutive_failures > 0` — surface `last_error` to describe what went wrong
- `last_run` is more than ~1 hour old — cron may have stopped running

The `last_error` field contains the raw exception message, which the bot can translate
into a plain-English explanation and suggested fix for the user.

## Updating

```bash
cd /opt/email-connector
git pull
venv/bin/pip install -r requirements.txt  # only needed if requirements changed
```
