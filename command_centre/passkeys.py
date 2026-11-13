"""Passkey (WebAuthn) sign-in: minimal CBOR, ES256/RS256 assertion checks and a SQLite credential store.

Attestation is not verified (options ask for "none"): Jarvis trusts a passkey because the owner
registered it from a signed-in session after re-entering the passphrase, not because of its maker.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

CHALLENGE_SECONDS = 5 * 60
MAX_CHALLENGES = 50
MAX_PASSKEYS = 10
MAX_LABEL = 60
FLAG_USER_PRESENT, FLAG_USER_VERIFIED, FLAG_ATTESTED = 0x01, 0x04, 0x40
ES256, RS256 = -7, -257


class PasskeyError(ValueError):
    pass


def available() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
    except ImportError:
        return False
    return True


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(value: object, *, limit: int = 2048) -> bytes:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise PasskeyError("invalid_encoding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise PasskeyError("invalid_encoding") from None


# CBOR (RFC 8949): only the definite-length subset authenticators emit.


def cbor_decode(data: bytes) -> object:
    value, offset = _cbor_item(data, 0, 0)
    if offset != len(data):
        raise PasskeyError("invalid_cbor")
    return value


def cbor_decode_prefix(data: bytes) -> tuple[object, int]:
    return _cbor_item(data, 0, 0)


def _cbor_item(data: bytes, offset: int, depth: int) -> tuple[object, int]:
    if depth > 8 or offset >= len(data):
        raise PasskeyError("invalid_cbor")
    head = data[offset]
    major, info = head >> 5, head & 0x1F
    offset += 1
    if info < 24:
        argument = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if offset + size > len(data):
            raise PasskeyError("invalid_cbor")
        argument = int.from_bytes(data[offset : offset + size], "big")
        offset += size
    else:
        raise PasskeyError("invalid_cbor")
    if major == 0:
        return argument, offset
    if major == 1:
        return -1 - argument, offset
    if major in (2, 3):
        if offset + argument > len(data):
            raise PasskeyError("invalid_cbor")
        raw = data[offset : offset + argument]
        try:
            return (raw if major == 2 else raw.decode()), offset + argument
        except UnicodeDecodeError:
            raise PasskeyError("invalid_cbor") from None
    if major == 4:
        if argument > 64:
            raise PasskeyError("invalid_cbor")
        items = []
        for _ in range(argument):
            item, offset = _cbor_item(data, offset, depth + 1)
            items.append(item)
        return items, offset
    if major == 5:
        if argument > 64:
            raise PasskeyError("invalid_cbor")
        mapping = {}
        for _ in range(argument):
            key, offset = _cbor_item(data, offset, depth + 1)
            if isinstance(key, (list, dict)):
                raise PasskeyError("invalid_cbor")
            mapping[key], offset = _cbor_item(data, offset, depth + 1)
        return mapping, offset
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], offset
    raise PasskeyError("invalid_cbor")


# WebAuthn checks ---------------------------------------------------------------------------


def client_data_challenge(client_data_json: bytes, *, kind: str, origin: str) -> str:
    """Check type and origin; return the challenge the caller must consume exactly once."""
    try:
        data = json.loads(client_data_json)
    except (ValueError, UnicodeDecodeError):
        raise PasskeyError("invalid_client_data") from None
    if not isinstance(data, dict) or data.get("type") != kind:
        raise PasskeyError("invalid_client_data")
    if data.get("origin") != origin or data.get("crossOrigin") is True:
        raise PasskeyError("origin_mismatch")
    challenge = data.get("challenge")
    if not isinstance(challenge, str) or not 0 < len(challenge) <= 128:
        raise PasskeyError("invalid_client_data")
    return challenge


def _authenticator_data(data: bytes, rp_id: str, *, attested: bool) -> tuple[int, int, bytes, bytes]:
    if len(data) < 37:
        raise PasskeyError("invalid_authenticator_data")
    if not hmac.compare_digest(data[:32], hashlib.sha256(rp_id.encode()).digest()):
        raise PasskeyError("rp_mismatch")
    flags, count = data[32], int.from_bytes(data[33:37], "big")
    if not flags & FLAG_USER_PRESENT or not flags & FLAG_USER_VERIFIED:
        raise PasskeyError("user_not_verified")
    if not attested:
        return flags, count, b"", b""
    if not flags & FLAG_ATTESTED or len(data) < 55:
        raise PasskeyError("invalid_authenticator_data")
    length = int.from_bytes(data[53:55], "big")
    credential_id = data[55 : 55 + length]
    if not 16 <= length <= 1023 or len(credential_id) != length:
        raise PasskeyError("invalid_authenticator_data")
    _, used = cbor_decode_prefix(data[55 + length :])
    return flags, count, credential_id, data[55 + length : 55 + length + used]


def _public_key(cose_key: bytes):
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    key = cbor_decode(cose_key)
    if not isinstance(key, dict):
        raise PasskeyError("unsupported_key")
    if key.get(1) == 2 and key.get(3) == ES256 and key.get(-1) == 1:
        x, y = key.get(-2), key.get(-3)
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
            raise PasskeyError("unsupported_key")
        try:
            numbers = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1())
            return ES256, numbers.public_key()
        except ValueError:
            raise PasskeyError("unsupported_key") from None
    if key.get(1) == 3 and key.get(3) == RS256:
        n, e = key.get(-1), key.get(-2)
        if not (isinstance(n, bytes) and isinstance(e, bytes) and 256 <= len(n) <= 512 and 0 < len(e) <= 4):
            raise PasskeyError("unsupported_key")
        try:
            return RS256, rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
        except ValueError:
            raise PasskeyError("unsupported_key") from None
    raise PasskeyError("unsupported_key")


def verify_registration(attestation_object: bytes, rp_id: str) -> tuple[bytes, bytes, int]:
    """Return (credential_id, cose_public_key, sign_count) from a "webauthn.create" response."""
    attestation = cbor_decode(attestation_object)
    if not isinstance(attestation, dict) or not isinstance(attestation.get("authData"), bytes):
        raise PasskeyError("invalid_attestation")
    _, count, credential_id, cose_key = _authenticator_data(attestation["authData"], rp_id, attested=True)
    _public_key(cose_key)
    return credential_id, cose_key, count


def verify_assertion(
    *,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature: bytes,
    rp_id: str,
    cose_key: bytes,
    stored_count: int,
) -> int:
    """Verify a "webauthn.get" signature and return the new signature counter."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    _, count, _, _ = _authenticator_data(authenticator_data, rp_id, attested=False)
    algorithm, key = _public_key(cose_key)
    signed = authenticator_data + hashlib.sha256(client_data_json).digest()
    try:
        if algorithm == ES256:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise PasskeyError("invalid_signature") from None
    # Synced passkeys report 0; a counter that does not advance otherwise suggests a cloned key.
    if (count or stored_count) and count <= stored_count:
        raise PasskeyError("counter_regressed")
    return count


# Storage -----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS passkeys (
  credential_id TEXT PRIMARY KEY,
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL,
  label TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_used_at REAL
);
CREATE TABLE IF NOT EXISTS passkey_challenges (
  challenge TEXT PRIMARY KEY,
  purpose TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


class PasskeyStore:
    """Credentials and single-use challenges, shared by Passenger workers through one SQLite file."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def issue_challenge(self, purpose: str) -> bytes:
        challenge, now = secrets.token_bytes(32), self.clock()
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM passkey_challenges WHERE created_at < ?", (now - CHALLENGE_SECONDS,))
            # Evict only within the same purpose, so a flood of sign-in challenges cannot
            # cancel a registration that is in progress (and vice versa).
            conn.execute(
                "DELETE FROM passkey_challenges WHERE purpose = ? AND challenge NOT IN "
                "(SELECT challenge FROM passkey_challenges WHERE purpose = ? ORDER BY created_at DESC LIMIT ?)",
                (purpose, purpose, MAX_CHALLENGES - 1),
            )
            conn.execute("INSERT INTO passkey_challenges VALUES(?,?,?)", (b64url_encode(challenge), purpose, now))
        return challenge

    def consume_challenge(self, challenge: str, purpose: str) -> bool:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM passkey_challenges WHERE challenge = ? AND purpose = ? AND created_at >= ?",
                (challenge, purpose, self.clock() - CHALLENGE_SECONDS),
            )
            return cursor.rowcount == 1

    def count(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0]

    def add(self, credential_id: bytes, public_key: bytes, sign_count: int, label: str) -> bool:
        with closing(self._connect()) as conn, conn:
            # One statement, so the limit check and the insert cannot interleave across workers.
            cursor = conn.execute(
                "INSERT OR IGNORE INTO passkeys SELECT ?,?,?,?,?,NULL WHERE (SELECT COUNT(*) FROM passkeys) < ?",
                (b64url_encode(credential_id), public_key, sign_count, label, self.clock(), MAX_PASSKEYS),
            )
            return cursor.rowcount == 1

    def get(self, credential_id: str) -> tuple[bytes, int] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT public_key, sign_count FROM passkeys WHERE credential_id = ?", (credential_id,)
            ).fetchone()
        return (bytes(row[0]), row[1]) if row else None

    def mark_used(self, credential_id: str, previous_count: int, sign_count: int) -> bool:
        with closing(self._connect()) as conn, conn:
            # Compare-and-set so two workers cannot both accept the same counter value.
            cursor = conn.execute(
                "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ? AND sign_count = ?",
                (sign_count, self.clock(), credential_id, previous_count),
            )
            return cursor.rowcount == 1

    def list(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT credential_id, label, created_at, last_used_at FROM passkeys ORDER BY created_at"
            ).fetchall()
        return [{"id": r[0], "label": r[1], "created_at": r[2], "last_used_at": r[3]} for r in rows]

    def credential_ids(self) -> list[str]:
        return [item["id"] for item in self.list()]

    def remove(self, credential_id: str) -> bool:
        with closing(self._connect()) as conn, conn:
            return conn.execute("DELETE FROM passkeys WHERE credential_id = ?", (credential_id,)).rowcount == 1

    def remove_all(self) -> int:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM passkey_challenges")
            return conn.execute("DELETE FROM passkeys").rowcount
