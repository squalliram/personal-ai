from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .openai_client import AIError, image_extension

API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/"
TEXT_MODEL = "@cf/meta/llama-4-scout-17b-16e-instruct"
IMAGE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
IMAGE_SIZE = "1024x1280"
MULTIPART_MODELS = ("@cf/black-forest-labs/flux-2-",)
NO_RETRY_STATUS = {400, 401, 403, 404}


class CloudflareError(AIError):
    pass


def _json_object(text: str) -> dict | None:
    """The JSON object in a model reply, tolerating a code fence or a sentence around it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class CloudflareAI:
    """Workers AI over its REST API: text and images within the free daily allocation."""

    def __init__(
        self,
        account_id: str,
        api_token: str,
        *,
        text_model: str = TEXT_MODEL,
        image_model: str = IMAGE_MODEL,
        image_size: str = IMAGE_SIZE,
        timeout: float = 180.0,
        retries: int = 2,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", account_id.strip()) or not api_token.strip():
            raise ValueError("A 32-character Cloudflare account ID and an API token are required")
        self._base = API_BASE.format(account=account_id.strip())
        self._token = api_token.strip()
        self.text_model, self.image_model = text_model, image_model
        self.width, self.height = (int(v) for v in image_size.lower().split("x"))
        self.timeout, self.retries = timeout, retries

    def _run(self, model: str, body: bytes, content_type: str) -> dict[str, Any]:
        if not re.fullmatch(r"@(cf|hf)/[A-Za-z0-9._/-]+", model):
            raise ValueError("Not a Workers AI model name")
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": content_type}
        for attempt in range(self.retries + 1):
            try:
                with urlopen(Request(self._base + model, body, headers), timeout=self.timeout) as response:
                    reply = json.loads(response.read().decode("utf-8"))
                if not isinstance(reply, dict) or not reply.get("success", True) or "result" not in reply:
                    raise CloudflareError("Workers AI returned no result")
                return reply["result"]
            except HTTPError as exc:
                if exc.code in NO_RETRY_STATUS or attempt == self.retries:
                    raise CloudflareError(f"Workers AI request failed (HTTP {exc.code})") from None
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == self.retries:
                    raise CloudflareError(f"Workers AI request failed ({type(exc).__name__})") from None
            time.sleep(min(30, 5 * 2**attempt))
        raise AssertionError("unreachable")

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }
        result = self._run(self.text_model, json.dumps(payload).encode(), "application/json")
        reply = result.get("response") if isinstance(result, dict) else None
        parsed = reply if isinstance(reply, dict) else _json_object(reply) if isinstance(reply, str) else None
        if parsed is None:
            raise CloudflareError("Workers AI returned no JSON reply")
        return parsed

    def image(self, prompt: str) -> bytes:
        fields = {"prompt": prompt[:2048], "width": str(self.width), "height": str(self.height)}
        if self.image_model.startswith(MULTIPART_MODELS):
            boundary = uuid.uuid4().hex
            body = (
                b"".join(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                    for name, value in fields.items()
                )
                + f"--{boundary}--\r\n".encode()
            )
            result = self._run(self.image_model, body, f"multipart/form-data; boundary={boundary}")
        else:
            result = self._run(self.image_model, json.dumps({"prompt": fields["prompt"]}).encode(), "application/json")
        try:
            data = base64.b64decode(result["image"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise CloudflareError("Workers AI returned no image") from None
        if image_extension(data) is None:
            raise CloudflareError("Workers AI returned an image that is not a PNG or JPEG")
        return data
