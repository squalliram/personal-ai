# Devin review brief: Phase 1

Please review independently and challenge the decisions in [architecture.md](architecture.md). Do not merge to `main`.

## Scope

Branch `droid/build-personal-ai-command`. New code: `command_centre/`, `tests/test_command_centre_*.py`, `tests/command_centre_fixtures.py`, `scripts/start-command-centre.sh`, `docs/command-centre/`. No existing module, schedule or workflow was changed.

## Please verify

1. **Read-only guarantee.** Reads against legacy and current energy databases leave the files byte-identical (`tests/test_command_centre_sources.py::ReadOnlyGuaranteeTests`). Try to find any path that opens an existing database for writing.
2. **Authentication and sessions.** Login throttling, idle and absolute expiry, cookie flags, `__Host-` prefix under TLS, forged cookies.
3. **CSRF and DNS rebinding.** Origin, `Sec-Fetch-Site`, `X-CSRF-Token`, Host allow-list.
4. **Permissions.** READ ceiling cannot reach RECOMMEND routes; the default is RECOMMEND, never EXECUTE.
5. **Secret exposure.** No response contains Octopus credentials, meter identifiers, the password hash, crontab text or briefing error text.
6. **Health honesty.** Nothing reports `operational` without evidence; missing integrations are listed.
7. **Mobile and desktop layout** at 390 px and 1440 px.
8. **Stop/start truth.** The registry reports `scripts/stop-energy.sh` as ineffective against cron.

## Known gaps to assess

- No live connector was exercised; only synthetic data.
- The 19 PHP tests skip locally.
- TLS mode is unit-tested for cookies and headers, but no real certificate handshake test exists.
- The TLS handshake runs on the accept thread, so a slow client can delay other connections.
- In-memory rate limits reset on restart.

## Report format

What you tested, what passed, what failed, security and architecture concerns, recommended improvements, and whether Phase 1 is ready for acceptance.
