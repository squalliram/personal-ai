"""Command line: ``serve`` runs the app in the foreground; ``hash-password`` prints a config line."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

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
    args = parser.parse_args(argv)

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
