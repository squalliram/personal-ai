# Command Centre operations

## Telegram Ask Jarvis replies

No additional owner setup is needed: when the deployment has `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID` secrets, it registers the webhook on the cPanel host after
a successful install. Registration outcomes are reported safely in the deployment JSON
(`telegram_webhook` is `registered` or `failed`) without failing deployment.
The webhook secret defaults to a deterministic HMAC-SHA256 of the bot token.
An optional `COMMAND_CENTRE_TELEGRAM_WEBHOOK_SECRET` in the private host environment
overrides it (16–256 letters, digits, underscores or hyphens).
Token rotation changes the derived secret; redeploy to register the new value.
For manual management, load the host's private environment and run:

```sh
export COMMAND_CENTRE_URL=https://iramkhan.com/command-centre/
python3.12 -m command_centre telegram-webhook set
python3.12 -m command_centre telegram-webhook info
```

`set` preserves pending updates and subscribes only to messages, as does automatic registration.
`delete` unregisters the webhook. Webhooks disable Telegram's `getUpdates` polling API;
this repository does not use it.
Only private text messages sent by the configured owner receive read-only Ask Jarvis answers.
Replies share the website's AI providers, context and hourly question limit.
Update IDs are persisted in the audit database before answering: retries cannot send twice,
but a crash or delivery failure may lose a reply; send a new message to try again.
Do not delete this database while the webhook is active. No message text or IDs are audited.

## Run locally

Requires Python 3.10 or newer. On this Mac `python3` is 3.9.6, so the launcher picks a supported interpreter through `scripts/find-python.sh`.

1. Create a passphrase hash (12+ characters) and add the printed line to `.env`:

   ```sh
   python3.12 -m command_centre hash-password
   ```

   The line looks like `COMMAND_CENTRE_PASSWORD_HASH='pbkdf2_sha256$600000$...'`. Keep the single quotes. Only the hash is stored.

2. Point the app at your existing data if it is not in this checkout (paths are optional; relative paths resolve from the repository root):

   ```sh
   COMMAND_CENTRE_ENERGY_DB=/Users/iramkhan/personal-ai/data/octopus_energy.sqlite3
   COMMAND_CENTRE_BRIEFING_DB=/Users/iramkhan/personal-ai/data/daily_briefing.sqlite3
   COMMAND_CENTRE_BRIEFING_REPORTS=/Users/iramkhan/personal-ai/reports/daily
   ```

3. Start it in the foreground and open `http://127.0.0.1:8770/`:

   ```sh
   ./scripts/start-command-centre.sh
   ```

   Stop it with Ctrl-C. It creates no scheduled job, so there is no stop script.

### Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMMAND_CENTRE_PASSWORD_HASH` | none (server refuses to start) | PBKDF2 hash from `hash-password` |
| `COMMAND_CENTRE_PERMISSION` | `recommend` | Session ceiling: `read`, `recommend` or `execute` |
| `COMMAND_CENTRE_ENERGY_DB` | `data/octopus_energy.sqlite3` | Opened read-only |
| `COMMAND_CENTRE_ENERGY_STATUS` | `reports/energy/dashboard/status.json` | Refresh status, validated. The Energy view also reads the published `data.json` beside it |
| `COMMAND_CENTRE_ENERGY_REFRESH_DIR` | `data/energy-refresh` | Private (0700) runner directory; **Fetch latest data** only publishes `request.flag` here |
| `COMMAND_CENTRE_BRIEFING_DB` | `data/daily_briefing.sqlite3` | Opened read-only |
| `COMMAND_CENTRE_BRIEFING_REPORTS` | `reports/daily` | Only file names are read |
| `COMMAND_CENTRE_AUDIT_DB` | `data/command_centre.sqlite3` | Audit log (mode 0600) |
| `COMMAND_CENTRE_INSTAGRAM_DB` | `data/instagram.sqlite3` | Read for the Instagram queue; Approve/Reject updates only a draft's status and the examples table |
| `COMMAND_CENTRE_INSTAGRAM_MEDIA` | `data/instagram/drafts` | Generated slide images, served by draft ID |
| `COMMAND_CENTRE_INSPECT_CRONTAB` | `1` | Count marked energy cron entries with `crontab -l` |
| `COMMAND_CENTRE_ALLOWED_HOSTS` | none | Extra Host header values, comma-separated (used when hosted) |
| `COMMAND_CENTRE_SESSIONS_DB` | none (in-memory sessions) | SQLite session store for multi-process hosting (0600) |

For hosting on iramkhan.com see [deployment](deployment.md).

Only `COMMAND_CENTRE_*` lines are read from `.env`; Octopus credentials are ignored.

### What you will see with the current local data

The energy databases in the main checkout use the legacy schema, so Energy shows **Refresh required** and no totals. That is deliberate: the app never migrates data. An approved sync with the current code (`./scripts/start-energy.sh --days 90 --gas-units kwh`) updates the schema; it writes to the database, so run it only when you choose to.

## Remote access

Do not expose the app without TLS. Binding to anything other than loopback requires a certificate:

```sh
./scripts/start-command-centre.sh --host 0.0.0.0 --port 8443 \
  --tls-cert cert.pem --tls-key key.pem --allowed-host my.host.example:8443
```

cPanel hosting is planned for Phase 6 and needs approval.

## Back up your data

Reports and databases under `reports/` and `data/` are deliberately not in Git, so a lost disk means lost history. To back up a SQLite database safely while it may be in use, use the online backup API rather than copying the file:

```sh
mkdir -p ~/personal-ai-backups
for db in command_centre daily_briefing octopus_energy octopus_energy_live; do
  [ -f "data/$db.sqlite3" ] && sqlite3 "data/$db.sqlite3" ".backup '~/personal-ai-backups/$db.sqlite3'"
done
```

Run it from each checkout that holds data you care about (this branch and the main checkout). Copy `~/personal-ai-backups` somewhere off the machine. Restore by stopping the app, replacing the file and restarting.

## Verification

```sh
python3.12 -m unittest discover -s tests -v      # full suite, including command centre tests
ruff check . && ruff format --check .
```

## Rollback

The command centre is additive. It changes no existing module, schedule, database or deployment workflow. To roll back, stop the foreground process and revert the commit. Optionally delete `data/command_centre.sqlite3` (audit log only) and remove the `COMMAND_CENTRE_*` lines from `.env`.
