# Personal AI OS — Daily Briefing and Octopus Energy Intelligence

This local Python project produces read-only daily intelligence briefings and Octopus Energy analysis. It has no scheduler and does not make account mutations.

## Daily Intelligence Briefing

An approved external bridge performs all Calendar and Notion reads, then writes a normalized snapshot locally. The briefing process never calls integration write APIs or stores connector credentials.

```sh
./scripts/start.sh --snapshot snapshot.json
```

Reports are written to `reports/daily/YYYY-MM-DD.md`; operational metadata is stored in `data/daily_briefing.sqlite3`. `scripts/stop.sh` is a no-op because runs are foreground and one-shot.

## Energy setup

Copy `.env.example` to `.env` and set `ACCOUNT_NUMBER` and `API_KEY`. Keep `.env` local; it is ignored by Git. Then run:

```sh
./scripts/start-energy.sh --days 400
```

The foreground run discovers the account's electricity and gas meter points, active tariff codes, historical consumption, unit rates, and standing charges; it stores the result in `data/octopus_energy.sqlite3`. It uses HTTPS Basic authentication with the API key and an empty password. A success message means an authenticated, read-only API request completed; it never prints credentials, MPANs, MPRNs, serials, or account details.

It also writes `reports/energy/YYYY-MM-DD.md` and `reports/energy/monthly-cost-analysis.md`. Reports use aggregate readings and never infer appliance-level usage.

Each sync also creates an interactive local dashboard at `reports/energy/dashboard/index.html`. It shows daily and hourly whole-home usage, estimated costs, Power Down planning advice, and Happy Hour/Power Up reminders. It deliberately does not identify appliances: smart-meter data alone cannot do that. The generated dashboard data is ignored by Git because it is sensitive household telemetry.

To view it locally, serve only that directory over loopback, then open `http://127.0.0.1:8765/`:

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory reports/energy/dashboard
```

Opening the HTML directly using `file://` cannot reliably load its JSON. The dashboard reports actual reading freshness separately from export time, warns about stale or missing data, and reloads the published snapshot every five minutes while visible. It does not contact Octopus itself. Missing costs or unverified gas energy units are unavailable, not zero.

## Protected cPanel dashboard deployment

The confirmed existing website is `https://iramkhan.com/energy-usage/`. Website Basic Auth credentials are separate from the Octopus API key. Always use HTTPS. Local code changes are not automatically evidence that the live site is updated.

The dashboard is not deployed by default from a new checkout. Household energy telemetry must not be published to an open web path. Any deployment needs explicit approval, cPanel directory protection, a non-public application clone, and a server-side `.env` containing the Octopus account number and API key. Do not commit that file. The publisher refuses missing or ambiguous local Basic Auth configuration; independently verify that both the HTML and JSON require authentication over HTTPS.

The included GitHub Actions deploy workflow starts after the `Test` workflow succeeds for a push to this repository's `main` branch. It connects only when all six repository secrets are set: `CPANEL_SSH_HOST`, `CPANEL_SSH_USERNAME`, `CPANEL_SSH_PRIVATE_KEY`, `CPANEL_SSH_KNOWN_HOSTS`, `CPANEL_APP_PATH`, and `CPANEL_DASHBOARD_PATH`. It verifies the tested commit on the server, performs a read-only sync, and copies only `index.html` and identifier-free `data.json` into the protected directory. Missing secrets skip the connection and deployment steps.

There is no scheduled source sync. Without a new deployment or an authorized manual sync and publish, the website remains a snapshot and becomes stale. Browser reloads do not refresh the source database. No schedule is created by this project.

See [Energy Dashboard Operations](docs/energy-dashboard.md) for the privacy model, deployment architecture, required secrets, and operating instructions.

Use `./scripts/stop-energy.sh` only for operational symmetry; there is no daemon to stop. Run tests with `python3 -m unittest discover -s tests -v`.

The MCP stdio server is `python3 -m octopus_energy.mcp_server [database]` and exposes only the documented read tools. It has no mutation tools.
