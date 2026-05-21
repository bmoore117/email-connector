# luma-events

Fetches event-related emails from a Gmail label via IMAP *and* directly fetches
upcoming events from key Luma calendars (e.g. FTLYR, hello_miami, labmiami,
delphica). Dedups them into `upcoming_events.json`, rotates past events out,
and — when there's something worth saying — POSTs a signed payload to a
[Hermes webhook](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks)
so the agent can highlight the best opportunities to attend.

## How it works

1. Emails from your private groups arrive in Gmail
2. A Gmail filter auto-labels them with your chosen label (e.g. `miami-social-event-source`)
3. This script connects via IMAP to read new emails *and* directly scrapes key Luma calendars
4. Events from both sources are dedup'd, filtered to the next 7 days, and merged into `run/upcoming_events.json` (with rotated-out events moving to `run/past_events.json`)
5. If this run discovered any *newly* upcoming events — or the run errored — the connector POSTs instructions plus run data to the Hermes gateway on localhost. The agent DMs a summary on Slack. Runs with nothing new and no errors are silent.
6. A cron job runs the script every 30 minutes on the same host as the Hermes gateway

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

## Installation

```bash
git clone <your-repo> /opt/luma-events
cd /opt/luma-events

python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env  # fill in Gmail credentials and Hermes webhook secret
```

## Configuration

All configuration lives in `.env`. Copy `.env.example` and fill in:

| Variable | Description |
|---|---|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | App password from Google account security settings |
| `GMAIL_LABEL` | Gmail label to read from (default: `miami-social-event-source`) |
| `LUMA_CALENDARS` | Comma-separated Luma calendar slugs (e.g. FTLYR,hello_miami,labmiami,delphica) |
| `HERMES_WEBHOOK_URL` | Hermes route URL. When webhooks are enabled and unset, defaults to `http://127.0.0.1:8644/webhooks/luma-events` |
| `HERMES_WEBHOOK_SECRET` | Route secret. When enabled and unset, defaults to `INSECURE_NO_AUTH` (local loopback only) |
| `HERMES_WEBHOOK_ENABLED` | Set to `false` to skip POSTing to Hermes |
| `HERMES_AGENT_PROMPT_PATH` | Optional path to agent instructions (default: `hermes/agent-prompt.txt`) |

Output paths are fixed under `./run/` (gitignored); no path overrides needed.

## Running manually

```bash
cd /opt/luma-events
venv/bin/python fetch_events.py
```

### Test Hermes webhook (no Gmail)

Posts a synthetic payload through the same path as a real run (instructions + sample
`newEvents`). Requires the Hermes gateway and `luma-events` route to be up.

```bash
venv/bin/python fetch_events.py test              # sample event → agent DM
venv/bin/python fetch_events.py test --error      # simulate scraper failure
venv/bin/python fetch_events.py test --dry-run    # print payload, do not POST
```

Exits `0` on success, `1` if the webhook is disabled, unreachable, or rejected.

## Cron setup

Edit the crontab with `crontab -e` and add:

```
*/30 * * * * /opt/luma-events/venv/bin/python /opt/luma-events/fetch_events.py
```

This runs every 30 minutes. Adjust the interval to taste.

## Output files

All paths are under `run/` next to `fetch_events.py`.

### `upcoming_events.json`

Events whose date is today or in the future.

### `past_events.json`

Events that have already passed. On each run, any event in `upcoming_events.json` whose
date is now before today is automatically moved here.

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

### `connector.log`

Rotating log of every run. Useful for diagnosing issues or reviewing history.

## Hermes agent integration

The connector and Hermes run on the same host. Each notify POST carries **everything**
the agent needs as separate JSON fields: `instructions` (from `hermes/agent-prompt.txt`),
`newEvents`, `health`, and `hasErrors`. No separate delta file.

1. `upcoming_events.json` / `past_events.json` are updated as usual.
2. If there are newly discovered events *or* the run errored, the connector POSTs to
   `HERMES_WEBHOOK_URL` (defaults to `http://127.0.0.1:8644/webhooks/luma-events`).
3. Hermes renders a short route template that references those fields (see
   `hermes/config-route.example.yaml`), then runs the agent. The agent sends a fresh
   top-level Slack DM to `U0AL1GKMR6J` per the instructions.
4. Use `deliver: log` on the route so Hermes does not also post a duplicate summary.

Runs that find nothing new and have a clean health status are silent — no webhook.

Webhook failures are logged but do not fail the fetch run (except the underlying
error path still exits with code 1).

### Agent prompt (in repo, not Hermes config)

Edit [`hermes/agent-prompt.txt`](hermes/agent-prompt.txt) to change behavior. Override path
with `HERMES_AGENT_PROMPT_PATH` in `.env` if needed.

### Gateway setup (one-time)

On the same machine as this script:

```bash
hermes gateway setup   # enable webhooks, port 8644
```

Merge [`hermes/config-route.example.yaml`](hermes/config-route.example.yaml) under
`platforms.webhook.extra.routes` in `~/.hermes/config.yaml`:

```yaml
luma-events:
  secret: "INSECURE_NO_AUTH"
  deliver: log
  prompt: |
    {instructions}
    ---
    hasErrors: {hasErrors}
    newEvents:
    {newEvents}
    health:
    {health}
```

Or subscribe via CLI (paste the `prompt` block from `hermes/config-route.example.yaml`).

For local deployment, leave `HERMES_WEBHOOK_SECRET` unset in `.env` (defaults to
`INSECURE_NO_AUTH`). Use a real shared secret if the gateway is reachable beyond localhost.

Start the gateway: `hermes gateway run`.

Verify:

```bash
curl http://127.0.0.1:8644/health
hermes webhook test luma-events --payload '{"instructions":"Test: reply OK","triggered_at":"test-1","hasErrors":false,"newEvents":[],"health":{}}'
```

### Testing without Gmail

Use `fetch_events.py test` (see above). Set `HERMES_WEBHOOK_ENABLED=false` only if you
want the full scraper to skip POSTing on real runs.

## Updating

```bash
cd /opt/luma-events
git pull
venv/bin/pip install -r requirements.txt  # only needed if requirements changed
```
