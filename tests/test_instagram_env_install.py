import stat
import tempfile
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


if __name__ == "__main__":
    unittest.main()
