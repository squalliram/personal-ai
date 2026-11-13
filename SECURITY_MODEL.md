<!-- Shared threat model for all five squalliram personal projects; keep the copies in sync. -->

# Threat model: squalliram personal projects

Scope: `squalliram/personal-ai`, `squalliram/jarvis`, `squalliram/devin-orchestrator`, `squalliram/instagram-agent-skill`, `gitlab.com/squalliram/todo-app`. Single owner (Iram Khan), no other users. No separate infrastructure repository: hosting is described by `personal-ai/deploy/` and `personal-ai/.github/workflows/`.

## Owner's answers (user-provided facts)

- **Assets:** all equally important — the cPanel host and SSH deploy keys (iramkhan.com), command-centre data (energy, calendar/Notion briefings), the owner's local machine and Claude account (via Jarvis), the Devin API key and GitHub token, and Instagram account content.
- **Attackers:** all of them matter — internet attackers, prompt injection via web pages / calendar / Notion content, other devices on the local network, malicious dependencies / supply chain.
- **devin-orchestrator is no longer in use and is out of scope.** Its findings matter only insofar as committed secrets or data in the repository itself are exposed.
- **Jarvis exposure:** the owner is not sure whether it is only ever reached on loopback. Treat LAN exposure as possible; do not lower Jarvis findings on the assumption of loopback-only.
- **Nothing else is out of scope or accepted risk.**

## Components

| Component | Repository | Code | Trust boundary |
| --- | --- | --- | --- |
| Command Centre | personal-ai | `command_centre/` | Internet → WSGI app at `https://iramkhan.com/command-centre/` (Passenger/LiteSpeed) and local loopback server |
| Energy Pipeline | personal-ai | `octopus_energy/` (sync, storage, dashboard, `refresh.php`, MCP server) | Octopus API → SQLite; published static dashboard behind cPanel Basic Auth at `iramkhan.com/energy-usage/` |
| Daily Briefing | personal-ai | `daily_briefing/`, `agents/` | Untrusted Notion/Calendar snapshot JSON → Markdown/SQLite |
| Deploy Pipeline | personal-ai | `deploy/`, `scripts/`, `.github/workflows/` | GitHub Actions secrets (`CPANEL_SSH_*`, `COMMAND_CENTRE_PASSWORD_HASH`) → SSH into cPanel host |
| Jarvis Bridge | jarvis | `bridge/` (`server.mjs`, `policy.mjs`, `net.mjs`, `chrome.mjs`, `page.mjs`) | Browser/WebSocket on `127.0.0.1:8787` → Claude Agent SDK, MCP servers, local files, outbound fetch |
| Jarvis HUD | jarvis | `src/` (`ui/sanitise.ts`, `ui/Panels.tsx`, `lib/`) | Model-authored HTML/media → browser DOM |
| Orchestrator | devin-orchestrator | `src/`, `public/`, `data/` | GitHub webhook / HTTP → Devin API + GitHub token (retired) |
| Instagram Skills | instagram-agent-skill | `skills/` | Local CLI text processing; vendored into `jarvis/bridge/plugins/instagram-agent/` |
| Todo App | todo-app | (docs only today; planned React PWA, IndexedDB, Google Calendar OAuth) | n/a until code lands |

## Entry points

- **Command Centre:** `GET /api/v1/health` and `POST /api/v1/session` are public; all other `/api/v1/*` routes need a session (READ). Hosted over TLS only (`SSL_PROTOCOL`/`SSL_CIPHER` check in `deploy/command-centre/passenger_wsgi.py`).
- **Energy dashboard:** static files plus `refresh.php` behind cPanel Basic Auth.
- **Jarvis bridge:** WebSocket agent socket and HTTP endpoints (`/health`, `/stt`, `/img`, `/media`, `/file`, page proxy) on `JARVIS_BRIDGE_HOST` (default `127.0.0.1`). Indirect entry: any content the agent reads (web pages, search results, account connectors) can carry prompt injection.
- **Orchestrator:** `POST /trigger`, `POST /webhook` (HMAC only if `GITHUB_WEBHOOK_SECRET` set), `GET /api/sessions` — no auth. Retired.
- **CI/CD:** GitHub Actions workflows on `main` / `workflow_dispatch` with cPanel SSH secrets.

## Assets

- cPanel SSH private key, known_hosts, app path; the cPanel account hosting iramkhan.com.
- `COMMAND_CENTRE_PASSWORD_HASH`, session tokens, audit DB (`data/command_centre.sqlite3`).
- Octopus API key, MPAN/MPRN, energy SQLite databases; Notion/Calendar briefing content.
- The owner's workstation (files, shell) and Claude/ElevenLabs/MCP credentials reachable through the Jarvis bridge.
- Devin API key, GitHub PAT (orchestrator `.env`).
- Instagram account content and drafts.

## Existing controls

- Command Centre: PBKDF2-SHA256 600k passphrase, in-memory/SQLite sessions storing token hashes, `__Host-` cookies, CSRF token + exact Origin + `Sec-Fetch-Site`, Host allow-list (421), rate limits, strict CSP, `textContent` rendering, read-only (`mode=ro`) adapters, no EXECUTE routes (`command_centre/security.py`, `app.py`; `docs/command-centre/architecture.md`).
- Jarvis: loopback bind by default, Origin and Host allow-lists (DNS rebinding), PreToolUse hook enforcing `policy.mjs` (writes gated by `JARVIS_ALLOW_WRITES`, file roots limited to workspace/tmp), SSRF gate in `net.mjs`, DOMPurify + class allow-list + CSP in the HUD.
- Personal-ai CI secret scan; workflow deploys only from `main`.
- Orchestrator: optional webhook HMAC with `timingSafeEqual`.

## Open questions

- Is the Jarvis bridge ever bound to a non-loopback address or port-forwarded (e.g. WSL mirrored networking)?
- Is `GITHUB_WEBHOOK_SECRET` still set anywhere the orchestrator might run, and have its Devin/GitHub tokens been revoked?
