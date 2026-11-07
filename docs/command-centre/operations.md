# Command Centre operations

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
| `COMMAND_CENTRE_ENERGY_STATUS` | `reports/energy/dashboard/status.json` | Refresh status, validated |
| `COMMAND_CENTRE_BRIEFING_DB` | `data/daily_briefing.sqlite3` | Opened read-only |
| `COMMAND_CENTRE_BRIEFING_REPORTS` | `reports/daily` | Only file names are read |
| `COMMAND_CENTRE_AUDIT_DB` | `data/command_centre.sqlite3` | The app's only writable database (mode 0600) |
| `COMMAND_CENTRE_INSPECT_CRONTAB` | `1` | Count marked energy cron entries with `crontab -l` |

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
