import base64
import hashlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from command_centre_fixtures import PASSWORD, Client, Clock, make_app, make_settings

from command_centre import passkeys
from command_centre.app import CommandCentreApp

HOST = "localhost:8770"
ORIGIN = f"http://{HOST}"
RP_ID = "localhost"


class LocalClient(Client):
    def request(self, method, path, **kwargs):
        kwargs.setdefault("origin", ORIGIN)
        return super().request(method, path, **kwargs)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def cbor(value) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
        raise ValueError(n)

    if value is None:
        return b"\xf6"
    if isinstance(value, bool):
        return bytes([0xF5 if value else 0xF4])
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        return head(3, len(value.encode())) + value.encode()
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(value)


class Authenticator:
    """A software platform authenticator producing real WebAuthn signatures."""

    def __init__(self, algorithm=passkeys.ES256, count=0):
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        self.algorithm, self.count, self.credential_id = algorithm, count, os.urandom(32)
        if algorithm == passkeys.ES256:
            self.key = ec.generate_private_key(ec.SECP256R1())
            numbers = self.key.public_key().public_numbers()
            self.cose = cbor({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            numbers = self.key.public_key().public_numbers()
            self.cose = cbor({1: 3, 3: -257, -1: numbers.n.to_bytes(256, "big"), -2: numbers.e.to_bytes(3, "big")})

    def _auth_data(self, rp_id, flags, attested=b""):
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + attested

    def register(self, challenge, *, origin=ORIGIN, rp_id=RP_ID, flags=0x45):
        client_data = json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": origin}).encode()
        attested = bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self.cose
        auth = self._auth_data(rp_id, flags, attested)
        attestation = cbor({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"client_data_json": b64(client_data), "attestation_object": b64(attestation), "label": "Test Mac"}

    def assert_(self, challenge, *, origin=ORIGIN, rp_id=RP_ID, flags=0x05, kind="webauthn.get", tamper=False):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding

        if self.count:
            self.count += 1
        client_data = json.dumps({"type": kind, "challenge": challenge, "origin": origin}).encode()
        auth = self._auth_data(rp_id, flags)
        signed = auth + hashlib.sha256(client_data).digest()
        if self.algorithm == passkeys.ES256:
            signature = self.key.sign(signed, ec.ECDSA(hashes.SHA256()))
        else:
            signature = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        if tamper:
            signature = signature[:-1] + bytes([signature[-1] ^ 1])
        return {
            "credential_id": b64(self.credential_id),
            "client_data_json": b64(client_data),
            "authenticator_data": b64(auth),
            "signature": b64(signature),
        }


class CborTests(unittest.TestCase):
    def test_round_trip_and_rejections(self):
        value = {1: 2, -1: b"\x00" * 3, "fmt": "none", "list": [True, False, None]}
        self.assertEqual(passkeys.cbor_decode(cbor(value)), value)
        for bad in (cbor(1) + b"\x00", b"\x5f\x41\x00\xff", b"\x9f\xff", b"\x5a\xff\xff\xff\xff", b""):
            with self.subTest(bad=bad), self.assertRaises(passkeys.PasskeyError):
                passkeys.cbor_decode(bad)


@unittest.skipUnless(passkeys.available(), "cryptography is not installed")
class PasskeyApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        settings = make_settings(self.root, passkeys_db=self.root / "passkeys.sqlite3")
        self.app = CommandCentreApp(
            settings,
            allowed_hosts={HOST, "127.0.0.1:8770"},
            clock=self.clock,
            monotonic=self.clock,
            cron_reader=lambda: ("absent", ""),
        )
        self.owner = LocalClient(self.app, host=HOST)
        self.assertEqual(self.owner.login().status, 200)

    def post(self, client, path, body, csrf=True):
        headers = {"X-CSRF-Token": client.csrf} if csrf else {}
        return client.request("POST", path, body=body, headers=headers)

    def register(self, authenticator=None, client=None):
        authenticator = authenticator or Authenticator()
        client = client or self.owner
        options = self.post(client, "/api/v1/passkeys/register/options", {"password": PASSWORD})
        self.assertEqual(options.status, 200, options.body)
        response = self.post(client, "/api/v1/passkeys/register", authenticator.register(options.json()["challenge"]))
        self.assertEqual(response.status, 200, response.body)
        return authenticator

    def sign_in(self, authenticator, client=None, **overrides):
        client = client or LocalClient(self.app, host=HOST)
        challenge = client.request("POST", "/api/v1/passkeys/login/options", body={})
        self.assertEqual(challenge.status, 200, challenge.body)
        body = {**authenticator.assert_(challenge.json()["challenge"], **overrides.pop("assertion", {})), **overrides}
        return client, client.request("POST", "/api/v1/passkeys/login", body=body)

    def test_register_then_sign_in_with_a_passkey(self):
        status = LocalClient(self.app, host=HOST).request("GET", "/api/v1/passkeys/status").json()
        self.assertEqual(status, {"available": True, "registered": False})
        authenticator = self.register()
        self.assertTrue(LocalClient(self.app, host=HOST).request("GET", "/api/v1/passkeys/status").json()["registered"])
        client, response = self.sign_in(authenticator, remember=True)
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue(response.json()["remembered"])
        self.assertIn("Max-Age=2592000", response.header("Set-Cookie"))
        self.assertEqual(client.request("GET", "/api/v1/session").status, 200)
        listed = self.owner.request("GET", "/api/v1/passkeys").json()["passkeys"]
        self.assertEqual([item["label"] for item in listed], ["Test Mac"])
        self.assertIsNotNone(listed[0]["last_used_at"])

    def test_rsa_passkeys_and_advancing_counters_work(self):
        authenticator = self.register(Authenticator(passkeys.RS256, count=1))
        for _ in range(2):
            self.assertEqual(self.sign_in(authenticator)[1].status, 200)

    def test_tampered_or_mismatched_assertions_are_rejected(self):
        authenticator = self.register()
        cases = {
            "bad signature": {"assertion": {"tamper": True}},
            "other origin": {"assertion": {"origin": "https://evil.example"}},
            "other relying party": {"assertion": {"rp_id": "evil.example"}},
            "no user verification": {"assertion": {"flags": 0x01}},
            "wrong ceremony": {"assertion": {"kind": "webauthn.create"}},
            "unknown credential": {"credential_id": b64(os.urandom(32))},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.app.login_per_client.reset("127.0.0.1")
                _, response = self.sign_in(authenticator, **overrides)
                self.assertEqual(response.status, 401)

    def test_challenges_are_single_use_and_expire(self):
        authenticator = self.register()
        client = LocalClient(self.app, host=HOST)
        challenge = client.request("POST", "/api/v1/passkeys/login/options", body={}).json()["challenge"]
        self.assertEqual(
            client.request("POST", "/api/v1/passkeys/login", body=authenticator.assert_(challenge)).status, 200
        )
        replay = LocalClient(self.app, host=HOST).request(
            "POST", "/api/v1/passkeys/login", body=authenticator.assert_(challenge)
        )
        self.assertEqual(replay.status, 401)
        challenge = client.request("POST", "/api/v1/passkeys/login/options", body={}).json()["challenge"]
        self.clock.now += passkeys.CHALLENGE_SECONDS + 1
        late = LocalClient(self.app, host=HOST).request(
            "POST", "/api/v1/passkeys/login", body=authenticator.assert_(challenge)
        )
        self.assertEqual(late.status, 401)

    def test_a_counter_that_goes_backwards_is_rejected(self):
        authenticator = self.register(Authenticator(count=5))
        self.assertEqual(self.sign_in(authenticator)[1].status, 200)
        authenticator.count = 3
        self.assertEqual(self.sign_in(authenticator)[1].status, 401)

    def test_registration_needs_the_passphrase_csrf_and_the_same_session(self):
        wrong = self.post(self.owner, "/api/v1/passkeys/register/options", {"password": "nope"})
        self.assertEqual(wrong.status, 401)
        no_csrf = self.post(self.owner, "/api/v1/passkeys/register/options", {"password": PASSWORD}, csrf=False)
        self.assertEqual(no_csrf.status, 403)
        anonymous = LocalClient(self.app, host=HOST).request(
            "POST", "/api/v1/passkeys/register/options", body={"password": PASSWORD}
        )
        self.assertEqual(anonymous.status, 401)
        options = self.post(self.owner, "/api/v1/passkeys/register/options", {"password": PASSWORD})
        other = LocalClient(self.app, host=HOST)
        other.login()
        stolen = self.post(other, "/api/v1/passkeys/register", Authenticator().register(options.json()["challenge"]))
        self.assertEqual(stolen.status, 400)
        unverified = Authenticator().register(options.json()["challenge"], flags=0x41)
        self.assertEqual(self.post(self.owner, "/api/v1/passkeys/register", unverified).status, 400)

    def test_failed_passkey_sign_ins_are_rate_limited(self):
        authenticator = self.register()
        statuses = [self.sign_in(authenticator, assertion={"tamper": True})[1].status for _ in range(5)]
        self.assertEqual(statuses, [401] * 5)
        blocked = LocalClient(self.app, host=HOST).request("POST", "/api/v1/passkeys/login/options", body={})
        self.assertEqual(blocked.status, 429)

    def test_options_and_removal(self):
        self.assertEqual(
            LocalClient(self.app, host=HOST).request("POST", "/api/v1/passkeys/login/options", body={}).status, 404
        )
        authenticator = self.register()
        options = self.post(self.owner, "/api/v1/passkeys/register/options", {"password": PASSWORD}).json()
        self.assertEqual(options["exclude_credentials"], [b64(authenticator.credential_id)])
        removed = self.post(self.owner, "/api/v1/passkeys/remove", {"credential_id": b64(authenticator.credential_id)})
        self.assertEqual(removed.json()["passkeys"], [])
        self.assertEqual(self.sign_in_status(authenticator), 404)

    def sign_in_status(self, authenticator):
        return LocalClient(self.app, host=HOST).request("POST", "/api/v1/passkeys/login/options", body={}).status

    def test_sign_in_challenges_are_rate_limited_and_cannot_evict_a_registration(self):
        self.register()
        options = self.post(self.owner, "/api/v1/passkeys/register/options", {"password": PASSWORD}).json()
        store = self.app.passkeys
        for _ in range(passkeys.MAX_CHALLENGES + 5):
            store.issue_challenge("login")
        second = Authenticator().register(options["challenge"])
        self.assertEqual(self.post(self.owner, "/api/v1/passkeys/register", second).status, 200)
        anonymous = LocalClient(self.app, host=HOST)
        statuses = [anonymous.request("POST", "/api/v1/passkeys/login/options", body={}).status for _ in range(21)]
        self.assertEqual(statuses[:20], [200] * 20)
        self.assertEqual(statuses[20], 429)

    def test_the_passkey_limit_holds(self):
        store = self.app.passkeys
        for n in range(passkeys.MAX_PASSKEYS + 2):
            store.add(os.urandom(32), Authenticator().cose, 0, f"key {n}")
        self.assertEqual(store.count(), passkeys.MAX_PASSKEYS)

    def test_passkeys_are_off_on_ip_literal_hosts(self):
        self.register()
        client = Client(self.app)
        self.assertEqual(client.request("GET", "/api/v1/passkeys/status").json()["available"], False)
        self.assertEqual(client.request("POST", "/api/v1/passkeys/login/options", body={}).status, 503)

    def test_passkeys_are_off_without_a_database(self):
        app = make_app(make_settings(self.root), clock=self.clock)
        client = Client(app)
        self.assertEqual(
            client.request("GET", "/api/v1/passkeys/status").json(), {"available": False, "registered": False}
        )
        self.assertEqual(client.request("POST", "/api/v1/passkeys/login/options", body={}).status, 503)


if __name__ == "__main__":
    unittest.main()
