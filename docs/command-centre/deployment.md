# Command Centre hosting on iramkhan.com

## Approved production change plan

Deploy Phase 1 at `https://iramkhan.com/command-centre/`, using the existing
`CPANEL_*` SSH settings and the approved local passphrase hash in GitHub's
`COMMAND_CENTRE_PASSWORD_HASH` encrypted secret. Phase 2 remains parked.

Read-only SSH inspection confirmed:

- CloudLinux Python Selector is enabled for `iramkhan.com`.
- Python 3.12 is enabled (`/opt/alt/python312/bin/python3`, version 3.12.16).
- `/command-centre` is not registered and the proposed app directory does not exist.
- The live energy database exists.

These are capability checks, not evidence of deployment. A release is live only
after the deployment workflow completes its public and authenticated checks.

## Scope

Register only the Python application at `iramkhan.com/command-centre`, with
startup file `passenger_wsgi.py` and entry point `application`. The private
application directory is `command-centre-app` beside `CPANEL_APP_PATH`, outside
`public_html`. CloudLinux manages the app's Passenger mapping and Python
virtual environment. No packages need installing.

The installer does **not** check out, merge, reset, or edit the shared energy
checkout. It does not change energy tables, refresh jobs, cron, the protected
energy dashboard, or domain-wide HTTPS settings. The existing energy deployment
workflow still runs independently under its normal triggers.

```
Apache TLS → /command-centre/ → Passenger (Python 3.12)
  command-centre-app/
    passenger_wsgi.py
    command-centre.env          private atomic configuration, mode 0600
    releases/<tested SHA>/     separate versioned code
    data/audit.sqlite3         operational metadata only
    data/sessions.sqlite3      hashed session tokens + shared rate counters
    tmp/restart.txt

  Existing checkout:
    data/octopus_energy.sqlite3          read-only source
    reports/energy/dashboard/status.json read-only source
    data/daily_briefing.sqlite3          optional read-only source
```

No credentials or source content enter deployment logs. The password hash travels
over SSH stdin, not in process arguments. App directories are mode 0700. Private
files are owner-only from creation, not chmodded after exposure.

## Release workflow

`.github/workflows/deploy-command-centre.yml` runs after a successful `Test`
push on `main`. Manual dispatch is allowed only on `main` and requires a
successful push test for that exact SHA.

1. Check the latest `main` SHA and successful tests using GitHub's API. Stale
   workflow runs skip all remote work.
2. Over one SSH connection, hold the existing
   `.git/energy-dashboard.deploy.lock` across the **entire** release, including
   installation, app registration, restart, verification, and failure recovery.
3. Fetch Git objects and recheck `origin/main` under that lock. A stale run
   returns before any app-directory, configuration, entrypoint or restart write.
4. Validate the domain-scoped selector registry. Stop on a conflicting app root
   or URL. Never overwrite an unmanaged app directory.
5. Extract the tested Git archive into a separate versioned release. Explicit
   configuration points data reads back at the existing live files.
6. Save owner-only previous configuration and entrypoint copies. Atomically
   install the new files. Create the domain-scoped app if needed, check that the
   entrypoint imports successfully, and request a Passenger restart.
7. Require the release SHA header and healthy JSON over HTTPS, security headers,
   page and static assets, anonymous API rejection, and HTTP-to-HTTPS redirect
   even with a spoofed `X-Forwarded-Proto` header.
8. Create a short-lived READ session for authenticated HTTPS smoke checks of
   session, overview, energy, integrations and automations. Revoke it in all
   cases. Log only source states and row counts, never source payloads or tokens.

The temporary READ session does **not** test the owner's passphrase sign-in.
The owner should confirm sign-in with the same passphrase used locally.

## Security and honest source status

- Apache/Passenger must set `wsgi.url_scheme` correctly. The app never trusts
  client-supplied forwarded-protocol headers. An incorrect WSGI scheme causes
  verification to fail rather than bypassing HTTPS enforcement.
- Startup fails on missing/invalid password hashes, missing shared sessions, or
  a permission ceiling above RECOMMEND. Phase 1 has no execute routes.
- Cookies are Secure, HttpOnly, SameSite=Strict, and use the `__Host-` prefix.
  Idle expiry is 30 minutes; absolute expiry is 12 hours.
- Hosted sessions and rate counters share the private session database across
  Passenger workers. Login attempts reserve their rate-limit slot atomically
  before password verification.
- The app uses its own passphrase authentication, CSRF checks, host allow-list
  and security headers. It does not inherit the energy dashboard's Basic Auth.
- Energy uses existing SQLite `mode=ro` adapters. Legacy or unavailable sources
  stay visibly degraded; the app does not migrate them. Briefing remains
  unconfigured if no server-side run history exists.

## Failure recovery and rollback

If a replacement release fails, the installer restores the previous configuration
and entrypoint and requests a restart while still holding the lock. If first-time
registration succeeds but verification fails, it stops that new app. Files are
retained for diagnosis, not deleted. A failed workflow is not a live sign-off.

For an operator-approved rollback, stop the **Command Centre app only** in cPanel
Setup Python App. To restore the previous release, restore the owner-only
`command-centre.env.previous` and `passenger_wsgi.py.previous` files under the
same deployment lock, then touch `tmp/restart.txt` and rerun the HTTPS checks.
Do not reset the energy checkout or delete app databases.

## Operations

- Restart: touch `tmp/restart.txt` inside the private app directory.
- Include `data/audit.sqlite3` in the SQLite backup routine described in
  [operations](operations.md#back-up-your-data).
- cPanel's Python App interface and domain error log expose Passenger failures.
- `.github/workflows/probe-command-centre.yml` provides a separate read-only
  hosting inventory. It never prints raw registry environment values.
