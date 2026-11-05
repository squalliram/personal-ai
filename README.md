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

## Protected cPanel dashboard deployment

The dashboard is not deployed by default. Household energy telemetry should not be published to an open web path. To deploy it at `/energy-usage`, first password-protect that directory in cPanel, clone this repository into a non-public application directory, and create a server-side `.env` containing the Octopus account number and API key. Do not commit that file.

The included GitHub Actions deploy workflow starts only after the `Test` workflow succeeds and all of these repository secrets have been set: `CPANEL_SSH_HOST`, `CPANEL_SSH_USERNAME`, `CPANEL_SSH_PRIVATE_KEY`, `CPANEL_SSH_KNOWN_HOSTS`, `CPANEL_APP_PATH`, and `CPANEL_DASHBOARD_PATH`. It updates the server clone, performs the read-only sync, and copies only the dashboard's `index.html` and identifier-free `data.json` into the protected directory. Until all secrets exist, the deployment job is skipped.

See [Energy Dashboard Operations](docs/energy-dashboard.md) for the privacy model, deployment architecture, required secrets, and operating instructions.

Use `./scripts/stop-energy.sh` only for operational symmetry; there is no daemon to stop. Run tests with `python3 -m unittest discover -s tests -v`.

The MCP stdio server is `python3 -m octopus_energy.mcp_server [database]` and exposes only the documented read tools. It has no mutation tools.
