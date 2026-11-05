# Energy Dashboard Operations

The dashboard is a private, read-only view of whole-home Octopus smart-meter data. It is intended to be published at `/energy-usage` only after cPanel directory protection is enabled.

The confirmed existing website is `https://iramkhan.com/energy-usage/`; the hostname is not hardcoded into the exporter. Confirm the HTTPS URL and protected directory before any authorized deployment. Read-only website verification does not authorize deployment.

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
- The page distinguishes export time, the latest reading for the selected fuel, and the last successful source sync. It warns when meter readings are over 48 hours old or the export is over 24 hours old. These are operational warnings, not a guarantee of complete meter coverage.
- Empty or unavailable data is shown explicitly, not as zero use. Network, authentication, JSON and timeout failures show a retryable error. Failed reloads retain the previous snapshot with a warning.
- Missing rates or unverified gas units produce unavailable totals and named quality reasons, not fabricated zero costs or gas kWh. An aggregate containing unknown usage or cost remains unknown.
- The browser reloads the published JSON without using its cache every five minutes while visible, on return to the tab, or when **Reload published data** is pressed. This does **not** call Octopus or refresh the server-side database.
- Weekly totals use Monday-start calendar weeks, including weeks crossing months or years. Current, missing-date and non-adjacent periods are excluded from percentage comparisons. Missing intervals within a recorded date are not corrected.
- Historical peak demand is not a cheapest-rate prediction. Event times and invitations are not connected to a live event feed.

## Deployment model

1. cPanel hosts a clone at `/home/iramkhan/personal-ai` outside the public web root.
2. A server-side `.env` in that directory supplies `ACCOUNT_NUMBER` and `API_KEY`.
3. `scripts/start-energy.sh` and `scripts/publish-energy-dashboard.sh` validate an explicit `$PYTHON` as Python 3.10+, or use the first compatible interpreter found by `scripts/find-python.sh`, including its listed CloudLinux `/opt/alt` paths, because the server's default `python3` may be older.
4. A successful GitHub `Test` workflow for a push to this repository's `main` branch triggers `Deploy protected energy dashboard`.
5. A configuration step checks all six deployment secrets. If any are absent, the connection and deployment steps are skipped.
6. Only successful, configured runs proceed. Each tested commit has a separate GitHub concurrency group, so a late older completion cannot replace a different commit's pending job.
7. The job connects over SSH with strict pinned host-key checking and acquires an exclusive `flock` on `.git/energy-dashboard.deploy.lock`. This server lock covers checkout checks, collection and publication. It refuses a dirty checkout or a branch other than `main`, fetches `main`, and skips a run if remote `main` has moved past its tested commit. It fast-forwards the clean clone to the exact tested commit, never resetting away server changes. A divergent checkout fails closed.
8. It runs a read-only incremental sync (90-day window on the first run), generates the dashboard, and fetches `main` again. A failed sync/fetch, changed checkout, or `main` moving during collection prevents publication. It copies only `index.html` and `data.json` to the protected web directory. Each file is replaced atomically; `.htaccess` and other existing files are preserved.

Required GitHub repository secrets:

- `CPANEL_SSH_HOST`
- `CPANEL_SSH_USERNAME`
- `CPANEL_SSH_PRIVATE_KEY`
- `CPANEL_SSH_KNOWN_HOSTS` (verified known-host entries; non-22 ports appear as `[host]:port`)
- `CPANEL_APP_PATH` (`/home/iramkhan/personal-ai`)
- `CPANEL_DASHBOARD_PATH` (the protected cPanel directory for `/energy-usage`)

Optional: `CPANEL_SSH_PORT`, which defaults to 22. If collecting candidate keys with `ssh-keyscan -p <port> <host>`, verify each fingerprint through a trusted hosting-provider channel before saving it. Do not trust scan output alone.

Remote application and dashboard paths must be absolute and contain only letters, digits, `/`, `_`, `-` and `.`. Use Python 3.10 or newer on the server with Europe/London timezone data available. The server must provide `flock` and an actual `.git` directory; deployment fails closed if either is missing. Verify these prerequisites and the intended application path before configuring deployment; repository documentation is not evidence of an existing server clone or `.env`.

### Protection checks before publishing

Use cPanel Directory Privacy to create the target directory and a direct `.htaccess` with unconditional `AuthType Basic`, `AuthUserFile` (an existing absolute file path), and `Require valid-user`. Do not place credentials in the public directory or Git.

The publisher refuses to create a web target, refuses missing protection, and rejects conditional or ambiguous authentication directives and authorization bypasses such as `Satisfy any` or `Require all granted`. Inherited-only protection and custom authentication arrangements are intentionally not accepted by this safeguard.

**The publisher checks Basic Auth configuration only.** It does not check HTTPS-only routing, HTTP response headers, HSTS or whether Apache honors `.htaccess`. Passing this local check does not mean the website is secure, connected or current.

A local `.htaccess` check cannot prove that Apache honors that configuration. Before publishing household telemetry, verify the confirmed HTTPS URL from an unauthenticated client: `/energy-usage/`, `/energy-usage/index.html` and `/energy-usage/data.json` must all return **401**, without exposing their bodies. Verify that HTTP redirects to HTTPS and that no public alternate path exposes the files. A public 200 response, login-page 200 response, redirect to a public copy or missing file is not evidence of directory protection.

After publication, repeat those checks, then verify authenticated access to the page and JSON. Configure the web server to return `Cache-Control: private, no-store` for the protected dashboard and exclude it from shared/CDN caching. Do not share credentials, readings or response bodies in logs.

### HTTPS before Basic Auth, recommended cPanel remediation

Read-only checks on 5 November 2026 found that HTTPS dashboard paths return a Basic Auth challenge, but HTTP also returns that challenge instead of redirecting. Never enter credentials at an HTTP URL. The following is guidance for a separately approved server change; no live setting or `.htaccess` was changed during verification.

1. In cPanel, open **Domains → Domains**, find `iramkhan.com`, and confirm a valid certificate is installed. Review the effect on the rest of the domain: the next setting is domain-wide.
2. Set **Force HTTPS Redirect** to **On** for that domain. If the toggle is unavailable, ask the hosting provider to enable the supported redirect for the domain. Keep **Directory Privacy** enabled for `energy-usage`; do not remove or replace its existing authentication directives.
3. From an unauthenticated client, check HTTP `/energy-usage`, `/energy-usage/`, `/energy-usage/index.html` and `/energy-usage/data.json` without supplying credentials or automatically following redirects. Each must return a redirect, normally **301** or **308**, whose `Location` points to the same path beneath `https://iramkhan.com/energy-usage/`. The HTTP response must not contain `WWW-Authenticate` or telemetry.
4. Follow only the HTTPS redirect without credentials. The directory, HTML and JSON must return **401** with a Basic challenge. Then sign in over HTTPS and separately check the actual files, JSON schema and reading freshness. A blanket 401 does not prove those files exist.
5. If HTTP still returns 401, ask the hosting provider to put the redirect in the **existing port-80 virtual host**, before directory authentication, using cPanel's supported Apache include mechanism. Do not rely on a per-directory rewrite that executes after authentication. For example, a provider can scope a server-context redirect to this dashboard:

   ```apache
   # Existing HTTP virtual host only, not the dashboard's .htaccess.
   RedirectMatch 308 "^/energy-usage(?:/(.*))?$" "https://iramkhan.com/energy-usage/$1"
   ```

   The provider must validate Apache configuration and apply it through cPanel without replacing the generated virtual host or its HTTPS protection. Repeat steps 3–4 after the change. Behind a reverse proxy, the provider must also account for trusted HTTPS forwarding to avoid redirect loops.

Only consider HSTS after confirming that the whole domain works over HTTPS. Do not add `includeSubDomains` or preload as part of this dashboard-only fix without reviewing their wider effect.

Reference: [cPanel Domains, Force HTTPS Redirect](https://docs.cpanel.net/cpanel/domains/domains/#force-https-redirect).

## Operations

- Make code changes and push `main`; tests run first. A successful run triggers deployment when all secrets are present.
- If a deploy fails, check the GitHub Actions log. Never paste credentials into an issue, commit, or log.
- To refresh locally: `./scripts/start-energy.sh --days 90`.
- To review locally after a successful sync, serve only the generated dashboard directory over loopback:

  ```sh
  python3 -m http.server 8765 --bind 127.0.0.1 --directory reports/energy/dashboard
  ```

  Open `http://127.0.0.1:8765/`, then stop the foreground server with Ctrl-C. Opening the HTML using `file://` cannot reliably fetch its JSON. Never serve the repository root, `.env`, SQLite database or reports directory publicly.
- To refresh on cPanel manually, run from the non-public application clone:

  ```sh
  ./scripts/start-energy.sh --days 90
  DASHBOARD_TARGET=/absolute/protected/directory ./scripts/publish-energy-dashboard.sh
  ```

- There is **no scheduled source sync**. Without a new successful deployment or an explicitly authorized manual refresh, the website remains a snapshot and will become stale. The browser's five-minute reload does not change that. Creating a schedule requires separate approval.
- Run `python3 -m unittest discover -s tests -v` before hand-off, plus `ruff check .` and `ruff format --check .` using the version pinned in `pyproject.toml`. Node.js runs the dashboard JavaScript checks; CI installs Node 22.
- To revoke automation, remove the GitHub deployment secrets and delete the cPanel SSH public key. The read-only local tool will continue to work.

## Event guidance

Only act on event times confirmed in Octopus. Power Down sessions reward using less electricity than normal during the invited window. Power Up/Happy Hour sessions are appropriate for flexible demand, not safety-critical devices. Event availability, eligibility, exact times, and reward rules are determined by Octopus.

Sources: [Saving Sessions](https://octopus.energy/saving-sessions/), [Weekend Happy Hours](https://octopus.energy/saving-sessions/weekend-happy-hours/).
