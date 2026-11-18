"""Command line: ``serve`` runs the app in the foreground; ``hash-password`` prints a config line."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, build_opener

import notifications

from .config import load_settings
from .security import hash_password
from .server import build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="command_centre", description="Personal AI Command Centre")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the web app in the foreground (Ctrl-C stops it)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--tls-cert", type=Path)
    serve.add_argument("--tls-key", type=Path)
    serve.add_argument(
        "--allowed-host", action="append", default=[], help="Extra Host header value, e.g. app.example:443"
    )
    hasher = commands.add_parser("hash-password", help="Print a COMMAND_CENTRE_PASSWORD_HASH line for .env")
    hasher.add_argument("--stdin", action="store_true", help="Read the passphrase from standard input")
    webhook = commands.add_parser("telegram-webhook", help="Manage the owner's Telegram webhook")
    webhook.add_argument("action", choices=("set", "delete", "info"))
    args = parser.parse_args(argv)

    if args.command == "telegram-webhook":
        settings = load_settings()
        if not settings.telegram_bot_token:
            print("Telegram is not configured.", file=sys.stderr)
            return 2
        payload = {}
        if args.action == "set":
            base = os.environ.get("COMMAND_CENTRE_URL", "").rstrip("/")
            if not base.startswith("https://") or not settings.telegram_webhook_secret:
                print("Set COMMAND_CENTRE_URL (HTTPS) and COMMAND_CENTRE_TELEGRAM_WEBHOOK_SECRET.", file=sys.stderr)
                return 2
            payload = {
                "url": base + "/api/v1/telegram/webhook",
                "secret_token": settings.telegram_webhook_secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": False,
            }
        method = {"set": "setWebhook", "delete": "deleteWebhook", "info": "getWebhookInfo"}[args.action]
        request = Request(
            f"{notifications.TELEGRAM_API}bot{settings.telegram_bot_token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with build_opener(notifications._NoRedirect()).open(request, timeout=15) as response:
                result = json.loads(response.read())
            if result.get("ok") is not True:
                raise ValueError
        except Exception:
            print("Telegram webhook request failed.", file=sys.stderr)
            return 1
        print("Telegram webhook request succeeded.")
        if args.action == "info":
            info = result.get("result", {})
            print(
                json.dumps(
                    {"configured": bool(info.get("url")), "pending_update_count": info.get("pending_update_count", 0)}
                )
            )
        return 0

    if args.command == "hash-password":
        if args.stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Passphrase (12+ characters): ")
            if getpass.getpass("Repeat passphrase: ") != password:
                print("Passphrases did not match.", file=sys.stderr)
                return 1
        try:
            encoded = hash_password(password)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(f"COMMAND_CENTRE_PASSWORD_HASH='{encoded}'")
        return 0

    try:
        settings = load_settings()
        server, _ = build(
            settings,
            args.host,
            args.port,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            extra_hosts=tuple(args.allowed_host),
        )
    except (ValueError, OSError) as error:
        print(f"Command centre did not start: {error}", file=sys.stderr)
        return 2
    scheme = "https" if args.tls_cert else "http"
    host, port = server.server_address[:2]
    print(f"Command centre listening on {scheme}://{host}:{port}/ (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
