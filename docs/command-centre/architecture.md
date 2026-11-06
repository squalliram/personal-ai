# Command Centre architecture and plan

Status: Phase 0 complete, Phase 1 implemented on branch `droid/build-personal-ai-command`. Factory owns this document. Devin's independent review is still required (see [Devin review brief](devin-review-brief.md)).

## 1. Phase 0 assessment: what exists today

| Area | Reality in the repository | Reuse decision |
| --- | --- | --- |
| Daily briefing (`daily_briefing/`) | Builds Markdown from a connector snapshot JSON. Logs counts and timings to `data/daily_briefing.sqlite3` (`execution_history`). Manual only, not scheduled. | Read `execution_history` read-only. Never read the `errors` column (it can hold connector text). |
| Calendar and Notion | No connector code in the repository. An approved MCP bridge outside the repository writes the snapshot. | Reported as `unverified`; health cannot be observed here. |
| CalDAV | Described by the owner, but no CalDAV code exists in this repository. | Listed as `not_implemented` until its location is confirmed. |
| Octopus Energy (`octopus_energy/`) | Read-only API sync, SQLite storage, cost analytics, static dashboard, refresh runner, eight read-only MCP tools, cPanel deploy with one marked cron entry. | Reuse `mcp_server.dispatch` (opens SQLite with `mode=ro`, refuses legacy schemas) and `refresh.validate_status`. Do not rebuild. |
| Career, finance, family, health | Nothing exists. | Shown as `not_implemented`. No sample data. |
| Authentication | Only cPanel Basic Auth around the published energy directory. | New app-level authentication. |
| Scheduling | One cron entry on the cPanel host (`# personal-ai-energy-refresh`), installed by the deploy workflow. `scripts/stop.sh` and `scripts/stop-energy.sh` are no-ops and do **not** stop cron. | Reported truthfully in the registry. No changes until Phase 5 with approval. |
| Tests | 260 tests pass with Python 3.12 (19 PHP tests skip locally). The default `python3` on the owner's Mac is 3.9.6, which fails 18 tests because the project needs 3.10+. | New tests follow the same `unittest` style. |

### Risks found

1. **Energy writers mutate data.** `octopus_energy.storage.connect()` runs the schema, enables WAL and, on a legacy database, drops and rebuilds `daily_costs`. `dashboard_data()` and `write_reports()` call it. The command centre must never call them. A test scans the adapter source for these names.
2. **The owner's local energy databases use the legacy schema**, so every read-only query returns "refresh required". Migrating them needs an approved sync with the current code. The app shows this state instead of migrating.
3. **Untrusted text.** Briefing content and connector errors are untrusted. The app never returns `execution_history.errors` and renders everything with `textContent`.
4. **Stop scripts do not stop cron.** This contradicts the requirement that stop/start controls work as documented. Fixing it changes production scheduling, so it needs an approved change plan (Phase 5).
5. **Interpreter mismatch.** `python3` is 3.9.6 locally. Use `python3.12` or `scripts/find-python.sh`.
6. **`.env` in the main checkout is world-readable** (`-rw-r--r--`). Recommend `chmod 600 .env`; not changed without approval.

## 2. Target architecture

One Python application, standard library only, with modular read-only adapters over the existing data.

```
browser ── HTTPS/loopback ──> command_centre (WSGI)
                                ├── security: PBKDF2 passphrase, sessions, CSRF, rate limits, permissions
                                ├── audit: data/command_centre.sqlite3 (metadata only)
                                ├── sources: read-only adapters + evidence-based health
                                │     ├── octopus_energy.mcp_server.dispatch  (mode=ro)
                                │     ├── daily_briefing execution_history     (mode=ro)
                                │     └── refresh status.json                  (validated, no symlinks)
                                └── automations: registry, cron marker count (read-only `crontab -l`)
```

- **WSGI** so the same app runs locally (`wsgiref`, threaded) and later under cPanel's Passenger "Setup Python App" without a framework.
- **Separate app database** (`data/command_centre.sqlite3`) for the audit log only. Existing databases are never opened for writing.
- **Sessions in memory.** No session token is written to disk; a restart signs the owner out.

### Health states

`operational`, `running`, `degraded`, `stale`, `failed`, `refresh_required`, `not_configured`, `not_implemented`, `unverified`. A source is `operational` only when its own execution evidence confirms it (recent successful sync, today's clean briefing run, recent successful refresh status).

### Security model

| Control | Implementation |
| --- | --- |
| Authentication | Single owner passphrase, PBKDF2-SHA256 600,000 iterations, stored as `COMMAND_CENTRE_PASSWORD_HASH`. Missing hash means the server refuses to start. |
| Sessions | 256-bit random token, only its SHA-256 kept in memory. `HttpOnly; SameSite=Strict; Path=/`, plus `Secure` and the `__Host-` prefix under TLS. 30-minute idle and 12-hour absolute expiry. |
| CSRF | Every non-GET needs an exact `Origin` match and no cross-site `Sec-Fetch-Site`. Session routes also need `X-CSRF-Token`. Login needs `application/json`. |
| DNS rebinding | `Host` must be in an allow-list (`127.0.0.1:PORT`, `localhost:PORT`). Otherwise 421. |
| HTTPS | Loopback by default. Any other bind address requires `--tls-cert` and `--tls-key`, and HSTS is sent. |
| Rate limiting | 5 failed logins per client and 20 overall per 15 minutes; 240 API requests per session per minute. |
| Permissions | READ < RECOMMEND < EXECUTE. Default ceiling RECOMMEND. Each route declares its level. No EXECUTE routes exist. |
| Headers | Strict CSP with no inline script, `frame-ancestors 'none'`, `nosniff`, `no-referrer`, `no-store`, no Python version in `Server`. |
| Secrets | Only `COMMAND_CENTRE_*` keys are read from `.env`. Octopus credentials never enter the app. Tests scan every response for credentials, hash and meter identifiers. |
| Audit | Login, logout, permission, CSRF, cross-origin and rate-limit events. Never passwords, content or request bodies. |

### API (v1, JSON)

| Method and path | Permission | Purpose |
| --- | --- | --- |
| `GET /api/v1/health` | public | Liveness only |
| `POST /api/v1/session` | public | Sign in |
| `GET /api/v1/session` | READ | Session, permission, CSRF token |
| `DELETE /api/v1/session` | READ + CSRF | Sign out |
| `GET /api/v1/overview` | READ | Attention items (facts), counts, briefing, energy, automations, missing integrations |
| `GET /api/v1/modules` | READ | 13 navigation sections with honest states |
| `GET /api/v1/integrations` | READ | Source health with evidence |
| `GET /api/v1/automations` | READ | Registry, last execution, history, cron marker count, stop-control truth |
| `GET /api/v1/energy` | READ | Totals, 14-day daily, monthly estimates, tariffs, anomalies, sync history, labelled `measured` / `estimated` / `inferred` |
| `GET /api/v1/briefing/runs?limit=1..100` | READ | Run metadata without error text |
| `GET /api/v1/audit` | READ | Last 50 audit entries |

Errors are always `{"error": "<category>"}`.

## 3. Phase 0 review: agreements and disagreements

An independent Factory review agent (not Devin) assessed the repository separately. Outcome:

| Topic | Review position | Decision |
| --- | --- | --- |
| Stdlib, loopback, authenticated app | Agree | Adopted |
| "Reuse existing domain modules" | Unsafe: storage, dashboard and report entry points write | Adopted the restriction: only `mcp_server.dispatch`, `refresh.validate_status`, `refresh_install.MARKER` and `storage.get_gas_units`-free paths. Enforced by test. |
| Legacy local databases | Show "refresh required", do not migrate | Adopted |
| Authentication | Suggested a random token printed at startup | **Disagreed.** A stored passphrase hash survives restarts and suits future cPanel hosting. Both keep the secret out of Git. Owner sign-off requested. |
| Sessions | In memory | Adopted |
| No sync, refresh or publish buttons in Phase 1 | Agree | Adopted |
| Package name `personal_ai_app` | Suggested | Used `command_centre` to match the product name |

## 4. Milestones

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Assessment, architecture, plan | Done (this document) |
| 1 | Shell, navigation, auth, API, read-only adapters, health framework, automation registry, audit | **Implemented; awaiting Devin review** |
| 2 | My Day, Tasks, Calendar from the briefing snapshot; safe rendering of untrusted content; editable top-3 priorities (local RECOMMEND write); notifications | Next |
| 3 | Energy depth (anomaly and forecast views, bill comparison once a bill source exists); career, finance, family, health adapters once sources are chosen | Needs owner input on sources |
| 4 | Insights engine: evidence-linked recommendations, lifecycle (new, acknowledged, resolved), change detection | Planned |
| 5 | Automation controls: enable, disable, run now, with approval and audit; fix stop scripts so they control cron | Needs approved change plan |
| 6 | Deployment (cPanel Passenger or loopback-only), monitoring, release review | Planned |

## 5. Decisions needed from the owner

1. Authentication model: stored passphrase hash (implemented) or startup token.
2. Which energy database is canonical (`octopus_energy.sqlite3` or `octopus_energy_live.sqlite3`), and approval for a sync that migrates it off the legacy schema.
3. Where the CalDAV integration lives.
4. Hosting target for the command centre: local only, or cPanel behind HTTPS.
5. Data sources for career, finance, family and health.
6. Approval to tighten `.env` permissions in the main checkout.
