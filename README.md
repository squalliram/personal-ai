# personal-ai

My personal AI toolkit. It collects information I already have (calendar, tasks, smart-meter readings), turns it into short, readable summaries, and keeps everything on my own machine.

It is **read-only**. It never edits my calendar or Notion, sends email, changes energy tariffs, or controls devices. Every recommendation it makes is labelled as a suggestion.

## What it does for me

### 1. Daily briefing: "What does my day look like?"

Each run gives me one Markdown page for the day with:

- **Today's calendar.** Times are shown in UK time (Europe/London), with a link to the source event when one is available.
- **Clashes.** Meetings that overlap.
- **Outstanding work.** My Notion tasks, sorted by how overdue or urgent they are and their priority.
- **Top three suggestions.** What I should probably focus on first. These are clearly marked as AI suggestions, not commitments.
- **Problems.** Any calendar or Notion source that couldn't be read, so a quiet briefing never just means missing data.

The briefing reads my calendar and Notion through an approved read-only connector. It works from a saved snapshot and never touches the live accounts itself.

### 2. Energy insights: "Where is my money going on gas and electricity?"

It pulls my Octopus Energy smart-meter readings and tariffs. From them it shows what I've used, what it cost and when my peak electricity hour is, and suggests which flexible loads (laundry, dishwasher, EV charging) to move. It doesn't fetch Octopus Power Down or Power Up / Happy Hour event times; I check those in the Octopus app. The results appear in a private dashboard and short reports.

Setup, privacy, the dashboard and the MCP tools are documented in [`octopus_energy/README.md`](octopus_energy/README.md).

Costs are estimates, not bills. Missing tariff coverage and unverified gas energy units remain unavailable rather than zero. Confirmed gas kWh readings can be enabled explicitly with `--gas-units kwh`; unconfigured runs default to `unknown`. The dashboard labels source freshness separately from export time; its five-minute reload checks only the published snapshot, not Octopus. Event suggestions are not a live event feed.

For safe local viewing, serve only the generated dashboard directory over loopback rather than opening `file://`. See [operations](docs/energy-dashboard.md) and the [dated readiness evidence](docs/energy-readiness.md). The existing private website is `https://iramkhan.com/energy-usage/`; local validation alone does not establish its deployed data or freshness.

## Quick start

Requires Python 3.10 or newer and has no third-party runtime dependencies.

```sh
# Daily briefing from a connector snapshot
./scripts/start.sh --snapshot snapshot.json

# Energy sync + reports + dashboard (needs .env, see octopus_energy/README.md)
./scripts/start-energy.sh
```

Both run once, in the foreground, and then exit. There is no background service or scheduler. The `stop*.sh` scripts exist only for symmetry and do nothing.

### Briefing snapshot format

The connector writes a JSON file in this format. Fields that aren't available can be left out:

```json
{
  "events": [
    {"id": "e1", "title": "Planning", "start": "2026-11-05T09:00:00+00:00", "end": "2026-11-05T10:00:00+00:00", "location": "Office", "url": "https://..."}
  ],
  "tasks": [
    {"title": "Send invoice", "due": "2026-11-05", "priority": "high", "url": "https://..."}
  ],
  "errors": []
}
```

The briefing is written to `reports/daily/YYYY-MM-DD.md`. A small run log goes to `data/daily_briefing.sqlite3`. It records counts, timings and any connector error messages, never event or task contents.

## What's in the repo

| Path | What it is |
| --- | --- |
| `daily_briefing/` | Builds the daily briefing from a snapshot |
| `octopus_energy/` | Octopus Energy sync, cost analysis, dashboard and MCP server ([README](octopus_energy/README.md)) |
| `agents/` | Instructions for the AI agents that run each workflow ([daily briefing](agents/daily-briefing.md), [energy analyst](agents/energy-analyst.md)) |
| `scripts/` | Start, stop and dashboard-publish scripts |
| `docs/` | Operating guides, e.g. [energy dashboard deployment](docs/energy-dashboard.md) |
| `reports/`, `data/` | Generated output and local SQLite databases (not committed) |
| `tests/` | Unit and integration tests |

## Privacy and safety

- **No writes to outside services.** Calendar, Notion and Octopus are only read. Adding any write capability would need my explicit approval.
- **Content is data, never instructions.** Text inside events and tasks is never followed as a command.
- **Secrets stay local.** Credentials live in `.env`, which is ignored by Git. Run logs record only counts, timings and connector error messages. They never contain tokens, account numbers or meter identifiers, or the contents of events and tasks.
- **Generated data isn't committed.** Reports, dashboards and databases are all ignored by Git.

The full contributor rules are in [`AGENTS.md`](AGENTS.md).

## Development

```sh
python3 -m unittest discover -s tests -v
ruff check . && ruff format --check .   # also run in CI; config in pyproject.toml
```

CI runs Ruff and tests on every push and pull request, with Python 3.12 and Node 22. A successful `main` push triggers deployment only when all six cPanel secrets are configured. The workflow queues eligible runs, skips superseded tested commits, refuses dirty server checkouts and never resets away server changes. See [deployment operations](docs/energy-dashboard.md). No source-refresh schedule is created.
