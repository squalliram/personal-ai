import unittest

from command_centre_fixtures import Clock

from command_centre.security import Permission, RateLimiter, SessionStore, hash_password, verify_password


class PasswordTests(unittest.TestCase):
    def test_round_trip_and_rejections(self):
        encoded = hash_password("a long enough passphrase", iterations=1000)
        self.assertTrue(encoded.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(verify_password("a long enough passphrase", encoded))
        self.assertFalse(verify_password("a long enough passphrasE", encoded))
        self.assertFalse(verify_password("", encoded))
        self.assertFalse(verify_password("x" * 2000, encoded))
        self.assertFalse(verify_password("a long enough passphrase", None))
        for malformed in ("", "plain", "md5$1000$a$b", "pbkdf2_sha256$1$AA$AA", "pbkdf2_sha256$x$AA$AA"):
            self.assertFalse(verify_password("a long enough passphrase", malformed))

    def test_short_passphrase_and_unsafe_iterations_are_refused(self):
        with self.assertRaises(ValueError):
            hash_password("short")
        with self.assertRaises(ValueError):
            hash_password("a long enough passphrase", iterations=10)

    def test_default_hash_uses_production_iterations_and_random_salt(self):
        first, second = hash_password("a long enough passphrase"), hash_password("a long enough passphrase")
        self.assertIn("$600000$", first)
        self.assertNotEqual(first, second)


class PermissionTests(unittest.TestCase):
    def test_levels_are_ordered_and_parsed_strictly(self):
        self.assertLess(Permission.READ, Permission.RECOMMEND)
        self.assertLess(Permission.RECOMMEND, Permission.EXECUTE)
        self.assertEqual(Permission.parse(" Recommend "), Permission.RECOMMEND)
        for bad in ("admin", "", None):
            with self.assertRaises(ValueError):
                Permission.parse(bad)


class RateLimiterTests(unittest.TestCase):
    def test_sliding_window(self):
        clock = Clock(0)
        limiter = RateLimiter(2, 10, clock=clock)
        self.assertTrue(limiter.hit("a"))
        self.assertTrue(limiter.hit("a"))
        self.assertFalse(limiter.hit("a"))
        self.assertTrue(limiter.hit("b"))
        clock.now = 10.5
        self.assertTrue(limiter.hit("a"))

    def test_key_count_is_bounded(self):
        limiter = RateLimiter(1, 60, clock=Clock(0), max_keys=3)
        for key in "abcdef":
            limiter.record(key)
        self.assertLessEqual(len(limiter._events), 3)


class SessionTests(unittest.TestCase):
    def test_idle_and_absolute_expiry_and_revocation(self):
        clock = Clock(1000)
        store = SessionStore(idle_seconds=60, absolute_seconds=150, clock=clock)
        token, session = store.create(Permission.READ)
        self.assertNotIn(token, store._sessions)  # only the hash is kept
        clock.now += 59
        self.assertIs(store.get(token), session)
        clock.now += 59
        self.assertIsNotNone(store.get(token))
        clock.now += 61
        self.assertIsNone(store.get(token))
        token, _ = store.create(Permission.READ)
        for _ in range(4):
            clock.now += 50
            store.get(token)
        self.assertIsNone(store.get(token))  # 200 s exceeds the absolute limit despite activity
        token, _ = store.create(Permission.READ)
        store.revoke(token)
        self.assertIsNone(store.get(token))
        self.assertIsNone(store.get(None))

    def test_session_count_is_bounded(self):
        store = SessionStore(idle_seconds=60, absolute_seconds=60, clock=Clock(0), max_sessions=2)
        tokens = [store.create(Permission.READ)[0] for _ in range(3)]
        self.assertEqual(len(store._sessions), 2)
        self.assertIsNone(store.get(tokens[0]))


if __name__ == "__main__":
    unittest.main()
