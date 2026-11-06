"""Publish a protected dashboard transaction, or just its validated status."""

import argparse
import fcntl
import json
import os
import shlex
import shutil
import signal
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .refresh import validate_status as validate_public_status

FILES = ("refresh.php", "data.json", "index.html", "status.json")
PLACEHOLDER = "__ENERGY_REFRESH_DIRECTORY_HEX__"


class PublicationError(Exception):
    """Report whether a failed transaction left published files changed."""

    def __init__(self, category):
        self.category = category
        super().__init__(category)


def _unique_object(pairs):
    if len(pairs) != len({key for key, _ in pairs}):
        raise ValueError("Duplicate JSON keys")
    return dict(pairs)


def _invalid_number(value):
    raise ValueError("Non-finite JSON number")


def validate_status(body):
    """Validate the exact public status contract without exposing its contents."""
    if len(body) > 16 * 1024:
        raise ValueError("Status exceeds its size limit")
    value = json.loads(body, parse_constant=_invalid_number, object_pairs_hook=_unique_object)
    return validate_public_status(value)


def _read_regular(path, limit=None):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Dashboard files must be regular files")
        return stream.read() if limit is None else stream.read(limit + 1)


def _private_directory(app, target):
    app = Path(app)
    if not app.is_absolute() or not app.is_dir() or any(c in str(app) for c in ("\0", "\n", "\r")):
        raise ValueError("Invalid absolute application path")
    app = app.resolve()
    data, private = app / "data", app / "data/energy-refresh"
    if data.is_symlink() or private.is_symlink() or not private.is_dir():
        raise ValueError("Private refresh directory is unavailable")
    if private.is_relative_to(target) or target.is_relative_to(private):
        raise ValueError("Private state must not be published")
    info = private.stat()
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise ValueError("Refresh directory is not private")
    return private


def _verify_protection(target):
    # Keep the existing direct cPanel Basic Auth guard and never write .htaccess.
    directives, depth = {}, 0
    for line in (target / ".htaccess").read_text(encoding="utf-8").splitlines():
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        key = parts[0].lower()
        if key.startswith("</"):
            depth -= 1
        elif key.startswith("<"):
            depth += 1
        elif key in ("authtype", "authuserfile", "require", "satisfy", "authmerging", "allow", "deny", "order"):
            if depth or key in directives:
                raise ValueError("Ambiguous protection")
            directives[key] = parts[1:]
    if depth or directives.get("authtype") != ["Basic"] or directives.get("require") != ["valid-user"]:
        raise ValueError("Basic authentication is required")
    if any(key in directives for key in ("satisfy", "authmerging", "allow", "deny", "order")):
        raise ValueError("Unsupported authorization overrides")
    (password_file,) = directives.get("authuserfile", [])
    if not Path(password_file).is_absolute() or not Path(password_file).is_file():
        raise ValueError("Password file unavailable")


@contextmanager
def _publication_lock(private):
    with os.fdopen(os.open(private / "publication.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Invalid publication lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def publish(source, target, app, *, status_only=False):
    """Stage everything first, publish status last, and roll back failed swaps."""
    source, target = Path(source), Path(target)
    changed, recovery = [], False
    temporary = None
    try:
        if not target.is_absolute() or not target.is_dir():
            raise ValueError("Protected target is unavailable")
        target = target.resolve()
        private = _private_directory(app, target)
        with _publication_lock(private):
            _verify_protection(target)
            names = ("status.json",) if status_only else FILES
            status_body = _read_regular(source / "status.json", 16 * 1024)
            validate_status(status_body)
            bodies = {"status.json": status_body}
            if not status_only:
                for name in FILES[:-1]:
                    bodies[name] = _read_regular(source / name)
                php = bodies["refresh.php"].decode("utf-8")
                if php.count(PLACEHOLDER) != 1:
                    raise ValueError("Refresh PHP needs exactly one directory placeholder")
                bodies["refresh.php"] = php.replace(PLACEHOLDER, str(private).encode("utf-8").hex()).encode("utf-8")
                if not isinstance(json.loads(bodies["data.json"], parse_constant=_invalid_number), dict):
                    raise ValueError("Invalid dashboard JSON")
            temporary = Path(tempfile.mkdtemp(dir=target, prefix=".energy-publish-"))
            backups = temporary / "previous"
            backups.mkdir(mode=0o700)
            existed = set()
            for name in names:
                destination = target / name
                if destination.exists() or destination.is_symlink():
                    _read_regular(destination)  # Refuse links and non-regular destinations.
                    shutil.copy2(destination, backups / name, follow_symlinks=False)
                    existed.add(name)
                staged = temporary / name
                staged.write_bytes(bodies[name])
                staged.chmod(0o644)
            try:
                for name in names:
                    os.replace(temporary / name, target / name)
                    changed.append(name)
            except BaseException as exc:
                for name in reversed(changed):
                    try:
                        if name in existed:
                            os.replace(backups / name, target / name)
                        else:
                            (target / name).unlink(missing_ok=True)
                    except OSError:
                        recovery = True
                category = "partially_published" if recovery else ("rolled_back" if changed else "nothing_changed")
                raise PublicationError(category) from exc
    except PublicationError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise PublicationError("nothing_changed") from exc
    finally:
        if temporary is not None and not recovery:
            try:
                shutil.rmtree(temporary)
            except OSError:
                # The transaction is already committed or rolled back. Retain
                # private staging leftovers rather than misreporting its state.
                pass


def _interrupt(signum, frame):
    raise InterruptedError("Publication interrupted")


def main():
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--app-path", type=Path, default=Path.cwd())
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    try:
        publish(args.source, args.target, args.app_path, status_only=args.status_only)
    except PublicationError as exc:
        print(f"Dashboard publication failed: {exc.category}.", file=sys.stderr)
        return 2 if exc.category == "partially_published" else 1
    if args.status_only:
        print("Dashboard status published; data snapshot unchanged.")
    else:
        print("Dashboard files published; verify unauthenticated HTTP access remains denied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
