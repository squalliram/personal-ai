# Octopus Energy insights

This package turns my Octopus Energy smart-meter data into answers I can act on:

- **How much gas and electricity did I use, and what did it cost?** Daily, weekly, monthly and yearly.
- **Which days were most expensive, and was anything unusual?**
- **How much do I use overnight while everything should be idle?** A high overnight baseline usually means something is left on.
- **When is my peak hour, and what should I move?** For example, shifting laundry or EV charging into an Octopus Power Up / Happy Hour slot, or cutting back during a Power Down event.

It only **reads** from Octopus. It never switches tariffs, changes account settings or controls appliances. Smart meters measure the whole home, so it never claims to know what individual appliances used.

## Setup

1. Copy `.env.example` to `.env` in the repo root and fill in your details. `.env` is ignored by Git.
   ```
   ACCOUNT_NUMBER=A-XXXXXXXX
   API_KEY=sk_live_...
   ```
   Generate the API key from the API access section of your Octopus online account.
2. Run a sync:
   ```sh
   ./scripts/start-energy.sh            # first run fetches up to 400 days of history
   ./scripts/start-energy.sh --days 90  # or choose a shorter first-run window
   ```

Each run finds your electricity and gas meters and their current tariffs. It then downloads half-hourly consumption, unit rates and standing charges, works out daily costs, and regenerates the reports and dashboard.

Later runs are **incremental**: they only fetch what's new. Consumption is re-read from 7 days before the newest stored reading, because Octopus sometimes fills in late readings. Rerunning is safe; nothing is duplicated.

Other options: `--database`, `--env-file`, `--report-dir`, `--dashboard-dir` (see `python3 -m octopus_energy.cli --help`).

## What you get

| Output | What's in it |
| --- | --- |
| `reports/energy/YYYY-MM-DD.md` | Active tariffs, total usage and cost, overnight baseline (00:00–05:00 UK time), and unusual days (more than two standard deviations above normal) |
| `reports/energy/monthly-cost-analysis.md` | Highest-cost days, including unit rates plus the daily standing charge |
| `reports/energy/dashboard/index.html` | Interactive dashboard (see below) |
| `data/octopus_energy.sqlite3` | Local database of meters, readings, rates, daily costs and sync history |

None of these are committed to Git: they contain household energy data.

### Dashboard

Open `reports/energy/dashboard/index.html` after a sync. You can switch between electricity and gas, and between daily, weekly, monthly and yearly views. It shows:

- usage and estimated cost for the latest period, and the change from the previous one
- a consumption trend chart
- your peak electricity hour and the best next action
- Power Down guidance: which flexible loads to delay, and which essential devices to leave on

The dashboard's `data.json` holds only aggregate figures. It never includes the account number, meter point numbers or serial numbers.

To publish it privately at `/energy-usage` on cPanel (password-protected, deployed automatically from GitHub Actions), see [Energy dashboard operations](../docs/energy-dashboard.md).

## Using it from an AI assistant (MCP)

`mcp_server.py` is a read-only Model Context Protocol server that communicates over stdio, so an AI assistant can query the local database:

```sh
python3 -m octopus_energy.mcp_server [path/to/octopus_energy.sqlite3]
```

| Tool | Returns |
| --- | --- |
| `get_energy_summary` | Total usage and cost per fuel, optionally between `start` and `end` |
| `get_daily_consumption` | Daily usage and cost between `start` and `end` |
| `get_monthly_bill_comparison` | Usage and cost per month and fuel |
| `get_tariff_details` | Current tariff code and validity per fuel |
| `get_expensive_days` | Most expensive days (`fuel`, `limit`) |
| `get_overnight_baseline` | Average overnight usage per fuel |
| `detect_energy_anomalies` | Days unusually far above normal |
| `get_savings_recommendations` | Stored savings recommendations (none are generated yet) |

There are no tools that write or change anything.

## Code map

| File | Role |
| --- | --- |
| `cli.py` | Entry point. Runs the incremental sync, then reports and dashboard |
| `client.py` | Read-only Octopus REST client (HTTPS Basic auth, paginated) |
| `storage.py`, `schema.sql` | SQLite schema and batched upserts |
| `analytics.py` | Daily costs, expensive days, anomalies and overnight baseline |
| `reports.py` | Markdown reports |
| `dashboard.py` | Dashboard HTML and `data.json` |
| `mcp_server.py` | Read-only MCP tool server |

The guardrails for the AI energy analyst are in [`agents/energy-analyst.md`](../agents/energy-analyst.md).
