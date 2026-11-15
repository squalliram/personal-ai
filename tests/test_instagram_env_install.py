import fcntl
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from instagram_agent import env_install

ACCOUNT = "0123456789abcdef0123456789abcdef"


class EnvInstallTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / ".env"

    def test_upsert_keeps_other_lines_and_replaces_owned_keys(self):
        self.path.write_text("# comment\nOCTOPUS_API_KEY=x\nCLOUDFLARE_ACCOUNT_ID=old\n")
        values = env_install.parse(f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\nCLOUDFLARE_API_TOKEN=tok_en-1\n")
        self.assertTrue(env_install.upsert(self.path, values))
        self.assertEqual(
            self.path.read_text(),
            f"# comment\nOCTOPUS_API_KEY=x\nCLOUDFLARE_ACCOUNT_ID={ACCOUNT}\nCLOUDFLARE_API_TOKEN=tok_en-1\n",
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertFalse(env_install.upsert(self.path, values))

    def test_empty_values_are_skipped(self):
        self.assertEqual(env_install.parse("CLOUDFLARE_ACCOUNT_ID=\nCLOUDFLARE_API_TOKEN=\n"), {})
        self.assertFalse(env_install.upsert(self.path, {}))
        self.assertFalse(self.path.exists())

    def test_unknown_keys_and_unsafe_values_are_refused(self):
        for text in ("PATH=/tmp\n", "CLOUDFLARE_API_TOKEN=a b\n", "CLOUDFLARE_API_TOKEN=$(id)\n", "junk\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                env_install.parse(text)

    def test_duplicate_owned_keys_collapse_to_the_new_value(self):
        self.path.write_text("CLOUDFLARE_API_TOKEN=old1\nX=1\nCLOUDFLARE_API_TOKEN=old2\n")
        self.assertTrue(env_install.upsert(self.path, {"CLOUDFLARE_API_TOKEN": "new"}))
        self.assertEqual(self.path.read_text(), "CLOUDFLARE_API_TOKEN=new\nX=1\n")

    def test_unchanged_file_is_still_made_private(self):
        self.path.write_text("CLOUDFLARE_API_TOKEN=tok\n")
        self.path.chmod(0o644)
        self.assertFalse(env_install.upsert(self.path, {"CLOUDFLARE_API_TOKEN": "tok"}))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_writes_use_a_private_unique_temp_file(self):
        (self.path.parent / ".env.tmp").write_text("held by another deploy")
        self.assertTrue(env_install.upsert(self.path, {"CLOUDFLARE_API_TOKEN": "tok"}))
        self.assertEqual(self.path.read_text(), "CLOUDFLARE_API_TOKEN=tok\n")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [".env", ".env.lock", ".env.tmp"])

    def test_preserve_existing_bootstraps_but_never_replaces_a_token(self):
        self.assertTrue(
            env_install.upsert(
                self.path,
                {"INSTAGRAM_ACCESS_TOKEN": "bootstrap", "INSTAGRAM_AUTO_PUBLISH": "1"},
                preserve={"INSTAGRAM_ACCESS_TOKEN"},
            )
        )
        self.assertEqual(self.path.read_text(), "INSTAGRAM_ACCESS_TOKEN=bootstrap\nINSTAGRAM_AUTO_PUBLISH=1\n")
        self.assertTrue(
            env_install.upsert(
                self.path,
                {"INSTAGRAM_ACCESS_TOKEN": "stale", "INSTAGRAM_AUTO_PUBLISH": "0"},
                preserve={"INSTAGRAM_ACCESS_TOKEN"},
            )
        )
        self.assertEqual(self.path.read_text(), "INSTAGRAM_ACCESS_TOKEN=bootstrap\nINSTAGRAM_AUTO_PUBLISH=0\n")

    def test_write_lock_is_private_and_blocks_a_competing_writer(self):
        self.path.write_text("CLOUDFLARE_API_TOKEN=old\n")
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.touch(mode=0o600)
        started = threading.Event()
        finished = []
        with lock_path.open("rb+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            def write():
                started.set()
                finished.append(env_install.upsert(self.path, {"CLOUDFLARE_API_TOKEN": "new"}))

            contender = threading.Thread(target=write)
            contender.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.05)
            self.assertTrue(contender.is_alive())
            fcntl.flock(held, fcntl.LOCK_UN)
            contender.join(2)
        self.assertFalse(contender.is_alive())
        self.assertEqual(finished, [True])
        self.assertEqual(self.path.read_text(), "CLOUDFLARE_API_TOKEN=new\n")
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_deploy_preserves_existing_token_and_always_sends_kill_switch(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/deploy-dashboard.yml").read_text()
        self.assertIn("--preserve-existing INSTAGRAM_ACCESS_TOKEN", workflow)
        self.assertIn("printf 'INSTAGRAM_AUTO_PUBLISH=%s\\n'", workflow)


if __name__ == "__main__":
    unittest.main()
