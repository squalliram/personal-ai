import hashlib
import hmac
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from command_centre_fixtures import Client, make_app, make_settings

from command_centre.__main__ import main
from command_centre.config import load_settings
from command_centre.telegram import register_webhook
from instagram_agent.openai_client import AIError


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(
            Path(self.tmp.name),
            telegram_bot_token="test-token",
            telegram_chat_id="123",
            telegram_webhook_secret="a" * 32,
        )
        self.app = make_app(self.settings)
        self.client = Client(self.app)
        self.provider = Mock(text_model="test-model")
        self.provider.chat_text.return_value = "Your data looks fine."
        self.send = self.enterContext(patch("notifications.send_telegram"))
        self.enterContext(patch.object(self.app, "_assistant_providers", return_value=[("fake", self.provider)]))
        self.context = self.enterContext(patch("command_centre.assistant.build_context", return_value="owner context"))

    def update(self, **message):
        return {
            "update_id": 1,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "is_bot": False},
                "text": "How are things?",
                **message,
            },
        }

    def post(self, body, secret="a" * 32):
        return self.client.request(
            "POST",
            "/api/v1/telegram/webhook",
            body=body,
            origin=False,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )

    def test_wrong_secret(self):
        self.assertEqual(self.post(self.update(), "wrong").status, 401)
        self.send.assert_not_called()

    def test_ignored(self):
        for body in (
            self.update(chat={"id": 456, "type": "private"}),
            self.update(**{"from": {"id": 456, "is_bot": False}}),
            self.update(chat={"id": 123, "type": "group"}),
            self.update(**{"from": {"id": 123, "is_bot": True}}),
            self.update(text=None),
            {},
            {"update_id": 1, "edited_message": self.update()["message"]},
        ):
            self.assertEqual(self.post(body).status, 200)
        self.send.assert_not_called()
        self.provider.chat_text.assert_not_called()

    def test_reply_and_persistent_dedupe(self):
        self.assertEqual(self.post(self.update()).status, 200)
        self.context.assert_called_once()
        self.assertIn("owner context", self.provider.chat_text.call_args.args[1])
        self.send.assert_called_once_with("test-token", "123", "Your data looks fine.")
        self.client = Client(make_app(self.settings))
        self.assertEqual(self.post(self.update()).status, 200)
        self.send.assert_called_once()

    def test_failure_apologises(self):
        self.provider.chat_text.side_effect = AIError("offline")
        self.assertEqual(self.post(self.update()).status, 200)
        self.assertIn("Sorry", self.send.call_args.args[2])

    def test_invalid_question_and_unavailable_provider_apologise(self):
        for index, text in enumerate(("", "x" * 501, "hello")):
            with self.subTest(text_length=len(text)):
                self.provider.reset_mock()
                if text == "hello":
                    self.app._assistant_providers.return_value = []
                body = self.update(text=text)
                body["update_id"] = index
                self.assertEqual(self.post(body).status, 200)
                self.assertIn("Sorry", self.send.call_args.args[2])
                self.provider.chat_text.assert_not_called()

    def test_delivery_failure_is_acknowledged(self):
        self.send.side_effect = OSError("offline")
        self.assertEqual(self.post(self.update()).status, 200)
        self.assertEqual(self.post(self.update()).status, 200)
        self.send.assert_called_once()

    def test_question_rate_limit_apologises(self):
        with patch.object(self.app.assistant_limiter, "hit", return_value=False):
            self.assertEqual(self.post(self.update()).status, 200)
        self.assertIn("Sorry", self.send.call_args.args[2])
        self.provider.chat_text.assert_not_called()

    def test_not_configured(self):
        from dataclasses import replace

        self.client = Client(make_app(replace(self.settings, telegram_webhook_secret=None)))
        self.assertEqual(self.post(self.update()).status, 503)
        self.send.assert_not_called()

    def test_secret_validation(self):
        for secret in ("short", "x" * 257, "x" * 16 + "!"):
            settings = load_settings(Path(self.tmp.name), {"COMMAND_CENTRE_TELEGRAM_WEBHOOK_SECRET": secret})
            self.assertIsNone(settings.telegram_webhook_secret)
        self.assertNotIn("a" * 32, repr(self.settings))

    def test_webhook_cli(self):
        response = Mock()
        response.read.return_value = b'{"ok":true,"result":{"url":"https://example.test/","pending_update_count":2}}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with (
            patch("command_centre.__main__.load_settings", return_value=self.settings),
            patch("command_centre.__main__.build_opener", return_value=opener),
            patch.dict("os.environ", {"COMMAND_CENTRE_URL": "https://example.test/command-centre/"}),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            for action, method in (("set", "setWebhook"), ("delete", "deleteWebhook"), ("info", "getWebhookInfo")):
                self.assertEqual(main(["telegram-webhook", action]), 0)
                request = opener.open.call_args.args[0]
                self.assertTrue(request.full_url.endswith("/" + method))
                if action == "set":
                    self.assertEqual(
                        json.loads(request.data),
                        {
                            "url": "https://example.test/command-centre/api/v1/telegram/webhook",
                            "secret_token": "a" * 32,
                            "allowed_updates": ["message"],
                            "drop_pending_updates": False,
                        },
                    )
            self.assertNotIn("test-token", output.getvalue())
            self.assertNotIn("a" * 32, output.getvalue())

    def test_derived_secret_and_override(self):
        env = {"COMMAND_CENTRE_TELEGRAM_BOT_TOKEN": "test-token"}
        derived = load_settings(Path(self.tmp.name), env)
        expected = hmac.new(b"test-token", b"command-centre-telegram-webhook", hashlib.sha256).hexdigest()
        self.assertEqual(derived.telegram_webhook_secret, expected)
        self.assertNotIn(expected, repr(derived))
        self.assertIsNone(load_settings(Path(self.tmp.name), {}).telegram_webhook_secret)
        env["COMMAND_CENTRE_TELEGRAM_WEBHOOK_SECRET"] = "explicit-secret-value"
        self.assertEqual(load_settings(Path(self.tmp.name), env).telegram_webhook_secret, "explicit-secret-value")
        env["COMMAND_CENTRE_TELEGRAM_WEBHOOK_SECRET"] = "invalid"
        self.assertIsNone(load_settings(Path(self.tmp.name), env).telegram_webhook_secret)

    def test_host_registration_payload_and_failure(self):
        with patch("command_centre.telegram.build_opener") as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"ok":true}'
            register_webhook(self.settings, "https://example.test/command-centre/")
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(
                json.loads(request.data),
                {
                    "url": "https://example.test/command-centre/api/v1/telegram/webhook",
                    "allowed_updates": ["message"],
                    "secret_token": "a" * 32,
                    "drop_pending_updates": False,
                },
            )
            response.read.return_value = b'{"ok":false}'
            with self.assertRaises(ValueError):
                register_webhook(self.settings, "https://example.test/")
