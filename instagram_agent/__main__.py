import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .cli import main
except Exception as exc:  # noqa: BLE001
    if "daily" in sys.argv[1:]:
        try:
            from notifications import command_centre_link, notify
            from octopus_energy.cli import load_env

            now = datetime.now(timezone.utc)
            today = now.astimezone(ZoneInfo("Europe/London")).date().isoformat()
            env = {**load_env(Path(".env")), **os.environ}
            notify(
                "instagram",
                "failed",
                f"Instagram cron failed to start ({type(exc).__name__})",
                command_centre_link(env, "instagram"),
                key=f"instagram-cron-start:{today}:{type(exc).__name__}",
                env=env,
                now=now,
            )
        except Exception:  # noqa: BLE001
            pass
    print(f"Instagram agent failed to start ({type(exc).__name__})", file=sys.stderr)
    sys.exit(1)

sys.exit(main())
