# Energy Dashboard Operations

The dashboard is a private, read-only view of whole-home Octopus smart-meter data. It is intended to be published at `/energy-usage` only after cPanel directory protection is enabled.

## What it answers

- How much electricity and gas was used each day, with estimated cost.
- Which hours have the highest average whole-home demand.
- When to reduce flexible electricity demand for an invited Power Down session.
- When to move flexible demand into an Octopus-confirmed Power Up or Happy Hour.

It does **not** identify appliances. Half-hourly meter data measures the whole property; use smart plugs or circuit-level monitoring for appliance attribution.

## Data and privacy

- The Octopus API is accessed read-only.
- `reports/energy/dashboard/data.json` contains aggregate household energy telemetry but no account number, meter point, or serial number.
- Dashboard output, local SQLite databases, and `.env` are Git-ignored.
- The web directory must remain password protected.

## Deployment model

1. cPanel hosts a clone at `/home/iramkhan/personal-ai` outside the public web root.
2. `scripts/start-energy.sh` uses `$PYTHON` or the first Python 3.10+ it finds, including CloudLinux's `/opt/alt/python3*/bin/python3`, because the server's default `python3` may be older.
3. A server-side `.env` in that directory supplies `ACCOUNT_NUMBER` and `API_KEY`.
4. A successful GitHub `Test` workflow triggers `Deploy protected energy dashboard`.
5. The deploy job SSHes to cPanel, resets the clone to the exact commit that passed `Test`, skipping if `main` has since moved on (untracked files such as `.env` and the SQLite database are kept), runs a read-only incremental sync (90-day window on first run), generates the dashboard, and copies only `index.html` and `data.json` to the protected web directory.

Required GitHub repository secrets:

- `CPANEL_SSH_HOST`
- `CPANEL_SSH_USERNAME`
- `CPANEL_SSH_PRIVATE_KEY`
- `CPANEL_SSH_KNOWN_HOSTS` (`ssh-keyscan -p <port> <host>` output; non-22 ports appear as `[host]:port`)
- `CPANEL_SSH_PORT` (optional, defaults to 22)
- `CPANEL_APP_PATH` (`/home/iramkhan/personal-ai`)
- `CPANEL_DASHBOARD_PATH` (the protected cPanel directory for `/energy-usage`)

## Operations

- Make code changes and push `main`; tests run first. A successful run triggers deployment when all secrets are present.
- If a deploy fails, check the GitHub Actions log. Never paste credentials into an issue, commit, or log.
- To refresh locally: `./scripts/start-energy.sh --days 90`.
- To review the local dashboard: open `reports/energy/dashboard/index.html` after a successful sync.
- To revoke automation, remove the GitHub deployment secrets and delete the cPanel SSH public key. The read-only local tool will continue to work.

## Event guidance

Only act on event times confirmed in Octopus. Power Down sessions reward using less electricity than normal during the invited window. Power Up/Happy Hour sessions are appropriate for flexible demand, not safety-critical devices. Event availability, eligibility, exact times, and reward rules are determined by Octopus.

Sources: [Saving Sessions](https://octopus.energy/saving-sessions/), [Weekend Happy Hours](https://octopus.energy/saving-sessions/weekend-happy-hours/).
