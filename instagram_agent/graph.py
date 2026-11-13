from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

GRAPH_HOST = "https://graph.facebook.com/"
INSTAGRAM_HOST = "https://graph.instagram.com/"
GRAPH_VERSION = "v27.0"
MEDIA_FIELDS = "id,caption,media_type,timestamp,permalink,like_count,comments_count"
MEDIA_METRICS = "reach,likes,comments,saved,shares,views,total_interactions"
BENCHMARK_MEDIA_FIELDS = "id,caption,media_type,timestamp,permalink,like_count,comments_count,view_count"


class GraphAPIError(RuntimeError):
    """A safe API error: it never includes the access token, URLs or response bodies."""


def _origin(url: str) -> tuple[str, str | None, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or 443


def _meta_code(exc: HTTPError) -> str:
    """Meta's numeric error code and subcode only, never its message."""
    try:
        error = json.loads(exc.read().decode("utf-8")).get("error") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    codes = [f"{k} {error[k]}" for k in ("code", "error_subcode") if isinstance(error.get(k), int)]
    return ", " + ", ".join(codes) if codes else ""


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise GraphAPIError("Graph API redirect left the trusted origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GraphClient:
    """Client for the Instagram API. Only `instagram_agent.publish` uses `post`."""

    def __init__(
        self,
        access_token: str,
        user_id: str,
        *,
        version: str = GRAPH_VERSION,
        host: str = GRAPH_HOST,
        timeout: float = 20.0,
        retries: int = 3,
    ) -> None:
        if not access_token.strip() or not user_id.strip().isdigit():
            raise ValueError("An access token and a numeric Instagram user ID are required")
        self.base_url = urljoin(host, version.strip("/") + "/")
        if urlsplit(self.base_url).scheme != "https":
            raise ValueError("Graph API requires HTTPS")
        self.user_id, self.timeout, self.retries = user_id.strip(), timeout, retries
        self._token = access_token.strip()
        self._opener = build_opener(_SameOriginRedirectHandler())

    def _url(self, path_or_url: str) -> str:
        url = urljoin(self.base_url, path_or_url)
        parts = urlsplit(url)
        if _origin(url) != _origin(self.base_url) or parts.username or parts.password:
            raise GraphAPIError("Graph API URL left the trusted origin")
        return url

    def _send(self, request: Request, retries: int) -> dict[str, Any]:
        for attempt in range(retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == retries:
                    raise GraphAPIError(f"Graph API request failed (HTTP {exc.code}{_meta_code(exc)})") from None
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == retries:
                    raise GraphAPIError(f"Graph API request failed ({type(exc).__name__})") from None
            time.sleep(min(8, 2**attempt))
        raise AssertionError("unreachable")

    def get(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = self._url(path_or_url)
        query = dict(params or {})
        if "access_token=" not in urlsplit(url).query:
            query["access_token"] = self._token
        if query:
            url += ("&" if "?" in url else "?") + urlencode(query)
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "personal-ai-instagram/1.0"})
        return self._send(request, self.retries)

    def post(self, path: str, params: dict[str, str], *, retry: bool = True) -> dict[str, Any]:
        """A form POST with the token in the body. Pass retry=False where a repeat could post twice."""
        body = urlencode({**params, "access_token": self._token}).encode()
        request = Request(
            self._url(path),
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "personal-ai-instagram/1.0",
            },
        )
        return self._send(request, self.retries if retry else 0)

    def paginated(self, path: str, params: dict[str, str] | None = None, *, limit: int = 100) -> Iterator[dict]:
        page = self.get(path, params)
        seen: set[str] = set()
        count = 0
        while True:
            for item in page.get("data", []):
                yield item
                count += 1
                if count >= limit:
                    return
            next_url = (page.get("paging") or {}).get("next")
            if not next_url:
                return
            if next_url in seen:
                raise GraphAPIError("Graph API pagination repeated a page")
            seen.add(next_url)
            page = self.get(next_url)

    def own_media(self, limit: int = 100) -> list[dict]:
        return list(self.paginated(f"{self.user_id}/media", {"fields": MEDIA_FIELDS}, limit=limit))

    def media_insights(self, media_id: str, metrics: str = MEDIA_METRICS) -> dict[str, int]:
        if not media_id.isdigit():
            raise ValueError("Media IDs are numeric")
        data = self.get(f"{media_id}/insights", {"metric": metrics}).get("data", [])
        values = {}
        for entry in data:
            points = entry.get("values") or []
            value = points[0].get("value") if points else (entry.get("total_value") or {}).get("value")
            if isinstance(value, int):
                values[entry.get("name")] = value
        return values

    def online_followers(self) -> dict[int, int]:
        """Followers online per hour of day, averaged over the days Meta returns."""
        data = self.get(f"{self.user_id}/insights", {"metric": "online_followers", "period": "lifetime"}).get(
            "data", []
        )
        totals: dict[int, list[int]] = {}
        for entry in data:
            for point in entry.get("values") or []:
                for hour, count in (point.get("value") or {}).items():
                    if str(hour).isdigit() and isinstance(count, int) and 0 <= int(hour) < 24:
                        totals.setdefault(int(hour), []).append(count)
        return {hour: round(sum(counts) / len(counts)) for hour, counts in totals.items()}

    def business_discovery(self, username: str, media_limit: int = 25) -> dict:
        if not username or not all(c.isalnum() or c in "._" for c in username) or len(username) > 30:
            raise ValueError("Not an Instagram username")
        fields = (
            f"business_discovery.username({username})"
            f"{{username,followers_count,media_count,media.limit({int(media_limit)}){{{BENCHMARK_MEDIA_FIELDS}}}}}"
        )
        return self.get(self.user_id, {"fields": fields}).get("business_discovery") or {}
