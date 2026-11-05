# Energy readiness, 5 November 2026

## Current outcome

The baseline release `28132f9f0a15e35d6a283dee6093597864ed8e67` passed GitHub checks and completed a real source sync plus dashboard-file publication. This does not establish authenticated page/data freshness. The operator subsequently confirmed that source gas readings are already kWh; this revision adds an explicit opt-in while keeping unknown units unavailable by default. No account mutation, source-refresh schedule or manual host configuration change was performed.

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

## Earlier validation after upstream reconciliation and queue correction

Checks at the earlier queue-correction checkpoint:

- Python 3.12 `-m unittest discover -s tests -v`: **103 tests passed**, 13.588 seconds.
- Required `python3 -m unittest discover -s tests -v`: **103 tests passed**, 18.723 seconds.
- Pinned Ruff **0.16.10** lint and formatting checks: passed across all 27 Python files.
- Dashboard Node tests, JavaScript syntax and all three energy shell-script syntax checks: passed.
- Eighteen isolated release tests exercise the real workflow commands without network access. They preserve the original deployment, checkout, sync-failure and invalid-configuration cases, and add real concurrent C/B/A processes in both waiter orders. They verify non-interleaving checkout/sync/publication, eventual publication of current B only, lock release after failure, missing-lock prerequisites, and refusal to publish after main/HEAD changes or a failed post-sync fetch. Linux CI uses `flock`; local macOS tests use standard-library `fcntl` with the same kernel-lock lifetime.
- Independent merged-backend review passed fresh/incremental/repeated ingestion, backfill, historical tariffs, WAL snapshots, rollback and unavailable gas-value checks.
- No unresolved merge paths or conflict markers remained. Hosting prerequisites blocked publication at that checkpoint; the later baseline deployment below succeeded.

The integration owner reports the merged fresh-process **default 400-day** collection completing in **6.45 seconds**, followed by an idempotent repeat in **4.84 seconds**, with independent provider interval-count checks. These are read-only live-source checks, not website deployment evidence.

The queue correction gives different tested commits separate GitHub concurrency groups. A late older completion can no longer replace another commit's pending job. The host must provide `flock`; an exclusive lock inside `.git` serializes checkout, collection and publication. The workflow rechecks remote main and checkout HEAD after collection. These guards subsequently ran successfully in the baseline deployment.

## Verified baseline deployment before the gas-unit update

Baseline main `28132f9f0a15e35d6a283dee6093597864ed8e67` passed [Test run 42078984355](https://github.com/squalliram/personal-ai/actions/runs/42078984355): 113 tests in 3.074 seconds, plus pinned Ruff lint and formatting across 28 files.

[Deployment run 42079031286](https://github.com/squalliram/personal-ai/actions/runs/42079031286) completed both preflight and deployment successfully. Actual output contained one successful Octopus source-sync message and one dashboard-publication message, with no superseded-run skip. This was real artifact publication, not a green skipped job. It does not prove two years of production history or real-time meter updates.

At that release, seven deployment-secret names were configured, including the optional SSH port. No secret values were inspected or changed. The successful workflow exercised the host's interpreter, locking, checkout, source credentials and protected-publication guards. No cPanel pull/deploy control or manual host command was used.

The gas-unit update preserves those safeguards and sets the source-sync command to `--days 90 --gas-units kwh`. Units resolve as `--gas-units` > process `GAS_UNITS` > `.env` `GAS_UNITS` > `unknown`. The mode is stored per database and retained by standalone report refreshes that omit it; MCP reads it read-only. A successful unconfigured CLI run explicitly reverts to `unknown`; failed or incomplete collections preserve the previous mode.

## Gas-unit validation

- Explicit Python 3.12.16: 126 tests passed in 12.707 seconds. The required `python3 -m unittest discover -s tests -v` command, with the supported interpreter selected through command-scoped `PATH`, passed all 126 tests in 14.422 seconds.
- Pinned Ruff 0.16.10 lint and formatting passed across 28 Python files. Dashboard Node tests, JavaScript syntax, all four energy shell-script syntax checks and whitespace checks passed.
- Regression cases cover exact CLI/environment precedence, unconfigured-run reversion, retained raw readings, report-only preservation, transactional metadata/cost rollback, incomplete pricing, confirmed gas analyses and read-only MCP behavior without metadata. Node checks cover confirmed gas peak-hour cards, fuel switching and the unconfirmed fallback. The release fixture verifies the actual sync arguments include `--gas-units kwh`.
- Independent QA reports that the final local preview passed all eight fuel/period views, including confirmed gas peak-hour cards, with unchanged electricity exports. This preview used a private local copy, not production. Synthetic checks and local previews are not evidence of live website content or freshness.

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

Authenticated page/JSON status, schema, export time, per-fuel reading freshness and authenticated cache headers were not independently established by this release check. The successful GitHub deployment separately establishes a usable source-refresh/publication path without exposing server environment contents. Website verification does not authorize a source-refresh schedule.

After separate authorization, one new pinned tab was requested at the confirmed HTTPS URL in the same assigned embedded browser to reuse any existing login. That attempt did not expose a usable dashboard tab. An offline browser diagnostic passed, and the subsequent assigned-session tab listing still contained no confirmed dashboard tab. Tab creation was not retried, and no authenticated HTML/JSON requests were made.

That dashboard-browser verification stopped there. No alternate browser/profile was opened, no authentication guesses or cookie/state exports were attempted, and no server or security configuration was changed.

## Limits and outstanding work

- **Gas units:** the operator has confirmed kWh for this source. Explicit `kwh` mode enables gas estimates without changing raw readings; `unknown` remains the default and never fabricates gas kWh. Missing required tariff coverage still makes electricity or gas costs unavailable.
- **Freshness:** there is no source-sync scheduler. Browser reloads only fetch the published snapshot; they do not call Octopus. Source refresh requires an authorized manual run or authorized deployment. Creating a schedule needs separate approval.
- **Website:** complete authenticated HTTPS page/JSON and freshness checks; confirm server-side configuration. Configure HTTP-to-HTTPS redirection before authentication and private, non-shared caching on the server. No server changes are authorized by verification alone.
- **Estimates:** retrieved intervals may not cover complete billing days. Costs are estimates, not reconciled bills; tariff changes inside an interval are not arbitrarily allocated.

See [Energy Dashboard Operations](energy-dashboard.md) for safe local viewing, protection checks and manual operations.
