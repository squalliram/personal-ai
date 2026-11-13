from __future__ import annotations

import base64
import json
import re
import time
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_BASE = "https://api.openai.com/v1/"
TEXT_MODEL = "gpt-5-mini"
IMAGE_MODEL = "gpt-image-2.5-flare"
NO_RETRY_CODES = {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}
IMAGE_SIZE = "1024x1280"  # 4:5, Instagram's tallest feed ratio; both sides divisible by 16.


class AIError(RuntimeError):
    """A safe model-provider error: it never includes keys, prompts or response bodies."""


class OpenAIError(AIError):
    pass


class NoRedirects(HTTPRedirectHandler):
    """Model APIs never redirect; following one could send the bearer token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


urlopen = build_opener(NoRedirects()).open


IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}


def image_extension(data: bytes) -> str | None:
    return next((ext for magic, ext in IMAGE_TYPES.items() if data.startswith(magic)), None)


def _error_code(exc: HTTPError) -> str | None:
    """OpenAI's machine-readable error code (e.g. insufficient_quota), never its message."""
    try:
        code = json.loads(exc.read(4096) or b"{}").get("error", {}).get("code")
    except (OSError, ValueError, AttributeError):
        return None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else None


class OpenAIClient:
    draws_text = True

    def __init__(
        self,
        api_key: str,
        *,
        text_model: str = TEXT_MODEL,
        image_model: str = IMAGE_MODEL,
        image_size: str = IMAGE_SIZE,
        timeout: float = 180.0,
        retries: int = 2,
    ) -> None:
        if not api_key.strip():
            raise ValueError("An OpenAI API key is required")
        self._key = api_key.strip()
        self.text_model, self.image_model, self.image_size = text_model, image_model, image_size
        self.timeout, self.retries = timeout, retries

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        for attempt in range(self.retries + 1):
            try:
                with urlopen(Request(API_BASE + path, body, headers), timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                code = _error_code(exc)
                if code in NO_RETRY_CODES or not (exc.code == 429 or 500 <= exc.code < 600) or attempt == self.retries:
                    raise OpenAIError(f"OpenAI request failed (HTTP {exc.code}{', ' + code if code else ''})") from None
            except (URLError, HTTPException, OSError, ValueError) as exc:
                if attempt == self.retries:
                    raise OpenAIError(f"OpenAI request failed ({type(exc).__name__})") from None
            time.sleep(min(30, 5 * 2**attempt))
        raise AssertionError("unreachable")

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        reply = self._post(
            "chat/completions",
            {
                "model": self.text_model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
            },
        )
        try:
            parsed = json.loads(reply["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise OpenAIError("OpenAI returned no JSON reply") from None
        if not isinstance(parsed, dict):
            raise OpenAIError("OpenAI returned no JSON reply")
        return parsed

    def chat_text(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        reply = self._post(
            "chat/completions",
            {
                "model": self.text_model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_completion_tokens": max_tokens,
            },
        )
        try:
            content = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        if not isinstance(content, str) or not content.strip():
            raise OpenAIError("OpenAI returned no reply")
        return content.strip()

    def image(self, prompt: str) -> bytes:
        reply = self._post(
            "images/generations",
            {"model": self.image_model, "prompt": prompt, "size": self.image_size, "quality": "medium", "n": 1},
        )
        try:
            data = base64.b64decode(reply["data"][0]["b64_json"], validate=True)
        except (KeyError, IndexError, TypeError, ValueError):
            raise OpenAIError("OpenAI returned no image") from None
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise OpenAIError("OpenAI returned an image that is not a PNG")
        return data
