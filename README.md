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

Use `./scripts/stop-energy.sh` only for operational symmetry; there is no daemon to stop. Run tests with `python3 -m unittest discover -s tests -v`.

The MCP stdio server is `python3 -m octopus_energy.mcp_server [database]` and exposes only the documented read tools. It has no mutation tools.
