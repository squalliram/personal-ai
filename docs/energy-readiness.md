# Energy readiness, 5 November 2026

## Current outcome

Local collection, analysis, dashboard generation and read-only MCP tools are validated. The existing website is **not yet verified end to end**: authenticated page/data access and deployed reading freshness remain pending. No deployment, account mutation or source-refresh schedule was performed or created.

## Available capabilities

- Authenticated read-only Octopus collection, electricity/gas meter discovery, historical tariff coverage and paginated readings.
- Local SQLite history, London-date electricity usage and estimated costs, overnight baseline, anomalies and expensive-day reports.
- Identifier-free dashboard with calendar-period controls, actual reading/sync freshness, missing/stale-data warnings, retryable loads and five-minute published-snapshot reloads.
- Suggested whole-home demand shifting and confirmed-event safety guidance. No appliance attribution, appliance control or live event feed.
- Eight read-only MCP tools with initialization, discovery, validated calls and safe errors. MCP does not create or migrate databases; legacy derived schemas require a local refresh first.
- Protected cPanel publication safeguards, strict SSH host-key checks and tested-commit verification. These checks are not evidence that publication happened.

## Validation before upstream reconciliation

Independent checks on the pre-reconciliation readiness checkpoint:

- `python3 -m unittest discover -s tests -v`: **76 tests passed**, 1.257 seconds. This includes dashboard JavaScript checks and 15 MCP subprocess tests.
- Python AST parsing: all 15 files in `octopus_energy/` and `tests/` passed.
- `node --check octopus_energy/dashboard.js`, energy shell-script syntax checks and `git diff --check`: passed.
- Synthetic migration checks through the real report entry point preserve raw readings and replace false-zero derived costs with unavailable values. A failed migration preserves the original schema/data and releases the database lock.
- Synthetic failed, empty and partial collections preserve the previous complete snapshot. Foreign pagination and chained redirects cannot forward API credentials to another origin.
- Independent MCP review passed: all eight tools, unchanged legacy database on refusal, then real report migration with unavailable gas values and preserved raw readings.

The integration owner separately reports a successful fresh-process **default 400-day** `./scripts/start-energy.sh` run: exit 0 in **40.37 seconds**, no stderr, both fuels accessible, successful history and generated temporary reports/dashboard. The original checkout data was untouched. An earlier 180-second timeout was resolved by bounded 1,000-record API pages. This live check is separate from the synthetic tests above.

## Validation after upstream reconciliation and queue correction

Final checks on the reconciled application and workflow safety changes:

- Python 3.12 `-m unittest discover -s tests -v`: **103 tests passed**, 13.588 seconds.
- Required `python3 -m unittest discover -s tests -v`: **103 tests passed**, 18.723 seconds.
- Pinned Ruff **0.16.10** lint and formatting checks: passed across all 27 Python files.
- Dashboard Node tests, JavaScript syntax and all three energy shell-script syntax checks: passed.
- Eighteen isolated release tests exercise the real workflow commands without network access. They preserve the original deployment, checkout, sync-failure and invalid-configuration cases, and add real concurrent C/B/A processes in both waiter orders. They verify non-interleaving checkout/sync/publication, eventual publication of current B only, lock release after failure, missing-lock prerequisites, and refusal to publish after main/HEAD changes or a failed post-sync fetch. Linux CI uses `flock`; local macOS tests use standard-library `fcntl` with the same kernel-lock lifetime.
- Independent merged-backend review passed fresh/incremental/repeated ingestion, backfill, historical tariffs, WAL snapshots, rollback and unavailable gas-value checks.
- No unresolved merge paths or conflict markers remain. Repository checks pass; publication and deployment remain blocked on the hosting prerequisites below.

The integration owner reports the merged fresh-process **default 400-day** collection completing in **6.45 seconds**, followed by an idempotent repeat in **4.84 seconds**, with independent provider interval-count checks. These are read-only live-source checks, not website deployment evidence.

The queue correction gives different tested commits separate GitHub concurrency groups. A late older completion can no longer replace another commit's pending job. The host must provide `flock`; an exclusive lock inside `.git` serializes checkout, collection and publication. The workflow rechecks remote main and checkout HEAD after collection. No corrected workflow command has been executed on the host.

## Release preparation

Deployment through GitHub is now separately authorized, but no release or deployment has occurred. Reconciliation preserves incremental collection, seven-day backfill, WAL/batched writes, Python 3.10+ and pinned Ruff checks. Merged tests and fresh-process collection measurements pass; hosting prerequisites remain the release blocker.

GitHub read-only checks found no configured deployment secrets: `CPANEL_SSH_HOST`, `CPANEL_SSH_USERNAME`, `CPANEL_SSH_PRIVATE_KEY`, `CPANEL_SSH_KNOWN_HOSTS`, `CPANEL_APP_PATH` and `CPANEL_DASHBOARD_PATH` are all absent. The latest green deployment workflow skipped its deployment job; it did not update the website.

At **13:28:15 Europe/London**, the hosting owner verified an authenticated cPanel session and the expected private repository clone with the correct GitHub origin. Its displayed commit, `7bec26d4bfd822cd7e52f77ab3cb2f7ab6ddf35a`, predates the readiness work. Python version, `flock`, the `.git` directory, environment-file presence, target protection/artifacts and a usable deployment mechanism are still being checked read-only. The cPanel native Deploy control is disabled pending its `.cpanel.yml` prerequisite. No pull/deploy control was activated. Public dashboard authentication is not a substitute for this hosting access.

## Existing website verification

Target: `https://iramkhan.com/energy-usage/`.

The website owner reports these read-only public checks:

- The HTTPS certificate is valid.
- HTTPS `/energy-usage` redirects with 301 to `/energy-usage/`, which returns 401.
- HTTPS `/energy-usage/`, `/energy-usage/index.html` and `/energy-usage/data.json` all return 401 with a Basic Auth challenge.
- HTTP `/energy-usage/` and `/energy-usage/data.json` return 401 with Basic Auth directly, without an HTTPS redirect. No HSTS was observed on checked responses. No credentials were sent over HTTP.

These 401 responses prove an authentication gate on the requested paths, **not that dashboard artifacts exist behind it**. Do not enter website credentials over HTTP.

Earlier embedded-browser retries reopened and selected the confirmed HTTPS tab. The latest retry reported a visible, focused page, but still returned 401 with no exposed native authentication dialog.

The user subsequently reported signing in. At **13:12:11 Europe/London on 5 November 2026**, the read-only checker found that this agent's assigned browser context was not on the confirmed HTTPS origin or dashboard path. Its same-origin guard therefore made **no page or JSON requests**. A minimal tab-metadata check found no existing tab at the confirmed dashboard URL in that assigned session. This is a browser-context mismatch, not evidence that the user's credentials failed. No unrelated page content, credentials or household values were read.

Authenticated page/JSON status, schema, export time, per-fuel reading freshness, authenticated cache headers and deployed source linkage remain **unverified** until the signed-in dashboard is available in the same authorized browser context. Hosting sign-in has since been verified, as noted above, but server-side `.env` and the actual refresh/deploy configuration remain under read-only inspection. Website verification does not authorize a source-refresh schedule or deployment.

After separate authorization, one new pinned tab was requested at the confirmed HTTPS URL in the same assigned embedded browser to reuse any existing login. That attempt did not expose a usable dashboard tab. An offline browser diagnostic passed, and the subsequent assigned-session tab listing still contained no confirmed dashboard tab. Tab creation was not retried, and no authenticated HTML/JSON requests were made.

That dashboard-browser verification stopped there. No alternate browser/profile was opened, no authentication guesses or cookie/state exports were attempted, and no server or security configuration was changed.

## Limits and outstanding work

- **Gas units:** raw source gas units are not verified. Raw readings remain stored, but gas kWh, unit costs and associated comparisons are unavailable rather than fabricated. Electricity estimates also remain unavailable if any required rate coverage is missing.
- **Freshness:** there is no source-sync scheduler. Browser reloads only fetch the published snapshot; they do not call Octopus. Source refresh requires an authorized manual run or authorized deployment. Creating a schedule needs separate approval.
- **Website:** complete authenticated HTTPS page/JSON and freshness checks; confirm server-side configuration. Configure HTTP-to-HTTPS redirection before authentication and private, non-shared caching on the server. No server changes are authorized by verification alone.
- **Estimates:** retrieved intervals may not cover complete billing days. Costs are estimates, not reconciled bills; tariff changes inside an interval are not arbitrarily allocated.

See [Energy Dashboard Operations](energy-dashboard.md) for safe local viewing, protection checks and manual operations.
