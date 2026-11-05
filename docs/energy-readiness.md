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

## Validation evidence

Independent final checks on this working tree:

- `python3 -m unittest discover -s tests -v`: **76 tests passed**, 1.257 seconds. This includes dashboard JavaScript checks and 15 MCP subprocess tests.
- Python AST parsing: all 15 files in `octopus_energy/` and `tests/` passed.
- `node --check octopus_energy/dashboard.js`, energy shell-script syntax checks and `git diff --check`: passed.
- Synthetic migration checks through the real report entry point preserve raw readings and replace false-zero derived costs with unavailable values. A failed migration preserves the original schema/data and releases the database lock.
- Synthetic failed, empty and partial collections preserve the previous complete snapshot. Foreign pagination and chained redirects cannot forward API credentials to another origin.
- Independent MCP review passed: all eight tools, unchanged legacy database on refusal, then real report migration with unavailable gas values and preserved raw readings.

The integration owner separately reports a successful fresh-process **default 400-day** `./scripts/start-energy.sh` run: exit 0 in **40.37 seconds**, no stderr, both fuels accessible, successful history and generated temporary reports/dashboard. The original checkout data was untouched. An earlier 180-second timeout was resolved by bounded 1,000-record API pages. This live check is separate from the synthetic tests above.

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

Authenticated page/JSON status, schema, export time, per-fuel reading freshness, authenticated cache headers and deployed source linkage remain **unverified** until the signed-in dashboard is available in the same authorized browser context. No server SSH credentials or session are available, so the server-side `.env` and actual server refresh/deploy configuration also remain unverified. Website verification does not authorize a source-refresh schedule or deployment.

After separate authorization, one new pinned tab was requested at the confirmed HTTPS URL in the same assigned embedded browser to reuse any existing login. That attempt did not expose a usable dashboard tab. An offline browser diagnostic passed, and the subsequent assigned-session tab listing still contained no confirmed dashboard tab. Tab creation was not retried, and no authenticated HTML/JSON requests were made.

Verification stopped there. No alternate browser/profile was opened, no authentication guesses or cookie/state exports were attempted, and no server or security configuration was changed.

## Limits and outstanding work

- **Gas units:** raw source gas units are not verified. Raw readings remain stored, but gas kWh, unit costs and associated comparisons are unavailable rather than fabricated. Electricity estimates also remain unavailable if any required rate coverage is missing.
- **Freshness:** there is no source-sync scheduler. Browser reloads only fetch the published snapshot; they do not call Octopus. Source refresh requires an authorized manual run or authorized deployment. Creating a schedule needs separate approval.
- **Website:** complete authenticated HTTPS page/JSON and freshness checks; confirm server-side configuration. Configure HTTP-to-HTTPS redirection before authentication and private, non-shared caching on the server. No server changes are authorized by verification alone.
- **Estimates:** retrieved intervals may not cover complete billing days. Costs are estimates, not reconciled bills; tariff changes inside an interval are not arbitrarily allocated.

See [Energy Dashboard Operations](energy-dashboard.md) for safe local viewing, protection checks and manual operations.
