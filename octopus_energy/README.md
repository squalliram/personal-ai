# Octopus Energy insights

This package turns my Octopus Energy smart-meter data into answers I can act on:

- **How much gas and electricity did I use, and what did it cost?** Daily, weekly, monthly and yearly.
- **Which days were most expensive, and was anything unusual?**
- **How much do I use overnight while everything should be idle?** A high overnight baseline usually means something is left on.
- **When is my peak hour, and what should I move?** For example, shifting laundry or EV charging out of the peak, into a Power Up / Happy Hour slot, or away from a Power Down event. Octopus event times aren't fetched; check them in the Octopus app.

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

Each run finds your electricity and gas meters and relevant tariff agreements. It downloads consumption, unit rates and standing charges, works out available daily electricity estimates, and regenerates the reports and dashboard after a complete successful collection. Failed, empty or partial collections preserve the previous complete snapshot and outputs rather than claiming success.

Later consumption runs are **incremental**. Consumption is re-read from 7 days before the newest stored reading, because Octopus sometimes fills in late readings. The lookback is clamped to the requested `--days` window unless the stored watermark is older. Rerunning is safe; nothing is duplicated. Tariff coverage is checked against historical agreements rather than assumed from the latest tariff alone.

Missing tariff coverage makes costs unavailable, not zero. Gas source units are currently unverified: raw readings are retained, but gas kWh and unit costs are withheld. Estimates cover retrieved intervals and are not reconciled bills or a guarantee of complete billing-day coverage.

`--days` limits how far back the first consumption sync of each meter goes. It also bounds rate downloads: when the account returns tariff agreement history, unit rates and standing charges are re-read for the whole `--days` window on each sync; otherwise they resume from the newest stored rate.

Other options: `--database`, `--env-file`, `--report-dir`, `--dashboard-dir` (see `python3 -m octopus_energy.cli --help`).

## What you get

| Output | What's in it |
| --- | --- |
| `reports/energy/YYYY-MM-DD.md` | Stored tariff records, available usage/cost estimates, electricity overnight baseline (00:00–05:00 UK time), and unusual days (more than two standard deviations above normal) |
| `reports/energy/monthly-cost-analysis.md` | Highest-cost days with complete pricing: date, fuel, usage and total cost (unit rates plus one standing charge per supply point) |
| `reports/energy/dashboard/index.html` | Interactive dashboard (see below) |
| `data/octopus_energy.sqlite3` | Local database of meters, readings, rates, daily costs and sync history |

None of these are committed to Git: they contain household energy data.

### Dashboard

After a successful sync, serve only the dashboard directory over loopback:

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory reports/energy/dashboard
```

Open `http://127.0.0.1:8765/`, then stop the foreground server with Ctrl-C. Opening the HTML using `file://` cannot reliably fetch its JSON. Never expose the repository root or serve `.env`, SQLite databases or reports publicly.

You can switch between electricity and gas, and between daily, weekly, monthly and yearly views. It shows:

- available usage and estimated cost for the latest period, with comparisons only for adjacent complete periods
- a consumption trend chart
- your peak electricity hour and the best next action
- Power Down guidance: which flexible loads to delay, and which essential devices to leave on
- actual reading and successful-sync freshness, separately from export time, with stale/missing-data warnings and retryable errors

Historical peak demand is not a cheapest-tariff prediction. Event guidance is a suggestion, not a connected live event feed. The page reloads the published JSON every five minutes while visible; this does not call Octopus or refresh the source database. There is no source-refresh scheduler.

The dashboard's `data.json` holds only aggregate figures. It never includes the account number, meter point numbers or serial numbers.

The existing website is `https://iramkhan.com/energy-usage/`. Website Basic Auth is separate from Octopus API authentication. To publish privately through GitHub Actions after explicit approval and hosting preflight, see [Energy dashboard operations](../docs/energy-dashboard.md). A passing test or skipped deployment does not prove the website is current; see the [dated readiness evidence](../docs/energy-readiness.md).

## Using it from an AI assistant (MCP)

`mcp_server.py` is a read-only Model Context Protocol server that communicates over stdio, so an AI assistant can query the local database:

```sh
python3 -m octopus_energy.mcp_server [path/to/octopus_energy.sqlite3]
```

| Tool | Returns |
| --- | --- |
| `get_energy_summary` | Available usage and estimated cost per fuel, optionally bounded by `start` and/or `end` |
| `get_daily_consumption` | Daily usage and cost between `start` and `end` |
| `get_monthly_bill_comparison` | Usage and cost per month and fuel |
| `get_tariff_details` | Stored tariff code and validity per fuel |
| `get_expensive_days` | Most expensive days (`fuel`, `limit`) |
| `get_overnight_baseline` | Average overnight usage per fuel |
| `detect_energy_anomalies` | Days unusually far above normal |
| `get_savings_recommendations` | Stored savings recommendations (none are generated yet) |

There are no mutation tools. Initialization, discovery and calls use standard MCP stdio messages. Database queries open SQLite read-only and return safe errors without exposing paths, payloads or credentials. An old derived-cost schema requires a local collector/report refresh before queries become available; the MCP tool never migrates it.

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
