# Command Centre hosting on iramkhan.com

Status: implemented on branch `droid/command-centre-live`; not yet deployed. This document is the change plan for the production hosting; nothing on the server changes until the one-time steps below are done and the branch merges.

## What runs where

```
browser ── HTTPS ──> Apache (iramkhan.com, TLS termination, Force HTTPS Redirect)
                       └── /command-centre/ ──> Passenger python app (~/command-centre-app)
                                                  ├── passenger_wsgi.py     (from deploy/command-centre/)
                                                  ├── command-centre.env    (0600, written by the workflow)
                                                  ├── data/audit.sqlite3    (0600, audit log)
                                                  ├── data/sessions.sqlite3 (0600, token hashes only)
                                                  └── code: imported from the existing personal-ai checkout
                                                        (reads its energy DB and status.json read-only)
```

The existing energy dashboard, refresh cron and publisher are untouched. The app reuses the six `CPANEL_*` deployment secrets; the only new secret is `COMMAND_CENTRE_PASSWORD_HASH`.

## Prerequisites

- cPanel with **Setup Python App** (Passenger) and a Python 3.10+ interpreter. The workflow's `probe` job checks this over SSH before anything is installed and stops with a clear message if the hosting cannot run Python apps.
- The existing server checkout (`CPANEL_APP_PATH`) clean and on `main`, same rule as the energy deploy.

## One-time setup (owner, about 5 minutes)

1. Add the passphrase hash as a GitHub secret without displaying it. Run in the checkout where you created `.env`:

   ```sh
   cd ~/personal-ai   # or the worktree where you ran hash-password
   sed -n "s/^COMMAND_CENTRE_PASSWORD_HASH=['\"]\?//; s/['\"]\$//p" .env \
     | gh secret set COMMAND_CENTRE_PASSWORD_HASH
   ```

   Reusing the same hash means the same passphrase works locally and on the site. The hash (not the passphrase) is stored by GitHub.

2. cPanel → **Setup Python App** → **Create application**:
   - Python version: 3.11 or newer (whatever the selector offers above 3.10)
   - Application root: `command-centre-app` next to the personal-ai checkout (the probe job prints the exact absolute path)
   - Application URL: `iramkhan.com/command-centre`
   - Application startup file: `passenger_wsgi.py`, entry point: `application`

3. cPanel → Domains → **Force HTTPS Redirect: On** for iramkhan.com. This also fixes the previously documented finding that the energy dashboard answers HTTP with a Basic Auth challenge instead of redirecting.

4. Merge the `droid/command-centre-live` PR. The deploy workflow runs automatically after tests pass.

## What the workflow does

`.github/workflows/deploy-command-centre.yml`, after a successful tested push to `main` (or manual dispatch):

1. **preflight** — verifies all secrets are configured; otherwise skips without touching the server.
2. **probe** — read-only SSH check: Python 3.10+, Passenger/selector presence, checkout state, energy databases, cron marker, app dir. Fails safe if the hosting cannot run the app.
3. **deploy** — with the same flock + clean-checkout + tested-commit guards as the energy deploy: fast-forwards the server checkout to the tested commit, creates `~/command-centre-app` (mode 0700), uploads `passenger_wsgi.py`, writes `command-centre.env` (0600, via a `.new` file and rename), and touches `tmp/restart.txt` so Passenger reloads.
4. **verify** — requires `https://iramkhan.com/command-centre/api/v1/health` to return `{"status":"ok"}` and the page to load over HTTPS. The deploy is not reported successful without this evidence.

## Security notes

- TLS terminates at Apache. The app builds with `secure=True`: HSTS, `Secure` + `__Host-` cookies, and a 308 redirect for any request that arrives over plain HTTP.
- Sessions move from process memory to `sessions.sqlite3` because Passenger can run multiple processes. Only SHA-256 token hashes are stored, never tokens; the file is mode 0600. A server restart no longer signs you out; the 30-minute idle and 12-hour absolute expiries are unchanged.
- Rate limiting remains per process; with several Passenger processes the effective limit is multiplied by the process count. The passphrase hash and login throttling still apply per process. Accepted risk for a single-owner app.
- The energy dashboard's Basic Auth layer does not extend to `/command-centre/`: the Passenger URL is not a physical directory, so Directory Privacy cannot wrap it. The app's own passphrase, CSRF, host allow-list and rate limiting are the gate.
- On the server, Briefing shows **Not configured** (the briefing runs locally only) and Energy reads the server's live database read-only. Automations shows the server's real cron entry.
- If `iramkhan.com` should serve something else, set the `COMMAND_CENTRE_HOSTS` repository variable (comma-separated Host values).

## Rollback

1. cPanel → Setup Python App → stop or remove the application. The URL immediately stops serving; no other site content is affected.
2. Optionally `rm -rf ~/command-centre-app` over SSH and delete the `COMMAND_CENTRE_PASSWORD_HASH` secret.
3. Revert the merge commit to remove the workflow. The energy deploy workflow is independent and unaffected.

## Operations

- Restart the app: `touch ~/command-centre-app/tmp/restart.txt` (the deploy workflow does this automatically).
- Audit log: `~/command-centre-app/data/audit.sqlite3` (metadata only, 0600). Include it in the SQLite backup routine from [operations](operations.md#back-up-your-data).
- Passenger stderr appears in the cPanel Python App interface and the domain's error log.
