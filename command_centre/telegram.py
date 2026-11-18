"""Host-side webhook registration; no message content is sent."""

import json
from urllib.request import Request, build_opener

import notifications

from .config import Settings


def register_webhook(settings: Settings, base: str) -> None:
    if not all((settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_webhook_secret)):
        raise ValueError("Telegram is not configured")
    request = Request(
        f"{notifications.TELEGRAM_API}bot{settings.telegram_bot_token}/setWebhook",
        data=json.dumps(
            {
                "url": base.rstrip("/") + "/api/v1/telegram/webhook",
                "secret_token": settings.telegram_webhook_secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": False,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with build_opener(notifications._NoRedirect()).open(request, timeout=15) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError("Telegram webhook registration failed")
