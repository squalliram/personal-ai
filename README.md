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

For setup, [screenshots](octopus_energy/README.md#screenshots), a local preview and testing instructions, see the [Octopus Energy guide](octopus_energy/README.md#quick-start), or [try the synthetic demo without an API key](octopus_energy/README.md#try-it-with-demo-data). The guide also covers privacy and the read-only MCP tools.

Costs are estimates, not bills. Missing tariff coverage and unverified gas energy units remain unavailable rather than zero. Confirmed gas kWh readings can be enabled explicitly with `--gas-units kwh`; unconfigured runs default to `unknown`. The dashboard separates export time, source refresh status and reading freshness. Its normal reload fetches the published snapshot; the protected source-refresh button queues a real Octopus sync for the server's next cron tick. Event suggestions are not a live event feed.

For safe local viewing, serve only the generated dashboard directory over loopback rather than opening `file://`. See [operations](docs/energy-dashboard.md) and the [dated readiness evidence](docs/energy-readiness.md). The existing private website is `https://iramkhan.com/energy-usage/`; local validation alone does not establish its deployed data or freshness.

### 3. Command Centre: "One private place to see all of it"

A local, read-only web app that brings the briefing history, energy insights, integration health and automation status into one dashboard. It needs a passphrase sign-in, only reads the existing databases, and labels every figure as measured, estimated or inferred. Modules without a connected source (career, finance, family, health, calendar sync) show as "not connected" rather than placeholder data. It cannot start or stop schedules yet.

See the [Command Centre architecture](docs/command-centre/architecture.md) and [operations guide](docs/command-centre/operations.md).

### 4. Instagram agent: "Draft this week's posts in my voice"

An agent that writes Instagram content: reel scripts with scored hooks, captions, carousels, stories, DM replies, weekly plans and post audits. It is the 13 `/ig-*` skills from [instagram-agent-skill](https://github.com/squalliram/instagram-agent-skill), run through Jarvis or Claude Code. **It drafts only. Nothing is posted automatically.** Fill in `~/.claude/instagram/voice.md` first. See [agents/instagram.md](agents/instagram.md).

## Quick start

Requires Python 3.10 or newer and has no third-party runtime dependencies.

```sh
# Daily briefing from a connector snapshot
./scripts/start.sh --snapshot snapshot.json

# Energy sync + reports + dashboard (needs .env, see octopus_energy/README.md)
./scripts/start-energy.sh

# Command Centre (loopback only; set a passphrase hash first, see docs/command-centre/operations.md)
./scripts/start-command-centre.sh
```

The briefing and energy start commands run once, in the foreground, and then exit. The Command Centre runs in the foreground until you stop it with Ctrl-C. The separately approved website deployment installs a one-minute energy cron tick, collecting when due (60 minutes by default) or requested. It does not schedule the daily briefing. The `stop*.sh` scripts exist only for symmetry and do nothing; they do not disable cron.

### Briefing snapshot format

The connector writes a JSON file in this format. Fields that aren't available can be left out. Malformed items are dropped and counted in the report's connector-failures section rather than crashing the run (`daily_briefing/snapshot.py` is the contract):

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

### 4. Instagram pipeline: "What should @instafoodielove post today?"

Collects my Instagram analytics and benchmarks the food and coffee accounts I follow: their best recent posts, the hashtags doing best and the hours my followers are online. It also remembers which drafts I approved and how they then performed. Every day it writes three AI-generated carousel drafts, ready by 12:00, and puts them on the Command Centre's Instagram page with Approve and Reject buttons. Nothing is posted automatically: I download the approved slides, copy the caption and post them myself. See the [Instagram pipeline guide](docs/instagram-pipeline.md).

## What's in the repo

| Path | What it is |
| --- | --- |
| `daily_briefing/` | Builds the daily briefing from a snapshot |
| `octopus_energy/` | Octopus Energy sync, cost analysis, dashboard and MCP server ([README](octopus_energy/README.md)) |
| `command_centre/` | Command Centre web app and API; its only write is recording Instagram Approve/Reject decisions locally ([architecture](docs/command-centre/architecture.md)) |
| `agents/` | Instructions for the AI agents that run each workflow ([daily briefing](agents/daily-briefing.md), [energy analyst](agents/energy-analyst.md), [Instagram](agents/instagram.md)). The Command Centre's AI Agents section lists them |
| `instagram_agent/` | Instagram analytics, benchmarks, daily drafts and approved-post memory ([guide](docs/instagram-pipeline.md)) |
| `scripts/` | Start, stop and dashboard-publish scripts |
| `docs/` | Operating guides, e.g. [energy dashboard deployment](docs/energy-dashboard.md) |
| `reports/`, `data/` | Generated output and local SQLite databases (not committed) |
| `tests/` | Unit and integration tests |

## Privacy and safety

- **No writes to outside services.** Calendar, Notion, Octopus and Instagram are only read. Adding any write capability would need my explicit approval.
- **What goes to the model provider.** The daily Instagram drafts send Cloudflare Workers AI (or OpenAI) the voice profile, my approved examples, the topics of recent and rejected drafts, and public benchmark captions and stats. Nothing else from this repo is sent.
- **Content is data, never instructions.** Text inside events and tasks is never followed as a command.
- **Secrets stay local.** Credentials live in `.env`, which is ignored by Git. Run logs record only counts, timings and connector error messages. They never contain tokens, account numbers or meter identifiers, or the contents of events and tasks.
- **Generated data isn't committed.** Reports, dashboards and databases are all ignored by Git.

The full contributor rules are in [`AGENTS.md`](AGENTS.md).

## Development

```sh
python3 -m unittest discover -s tests -v
ruff check . && ruff format --check .   # also run in CI; config in pyproject.toml
```

CI runs Ruff, PHP syntax/endpoint checks and tests on every push and pull request, with Python 3.12 and Node 22. A successful `main` push triggers deployment only when all six cPanel secrets are configured. The workflow queues eligible runs, skips superseded tested commits, refuses dirty server checkouts and never resets away server changes. It installs one marked energy cron entry, preserving all unrelated entries. See [refresh setup and operations](octopus_energy/README.md#scheduled-and-on-demand-refresh).
