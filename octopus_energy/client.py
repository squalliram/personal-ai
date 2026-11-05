from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = "https://api.octopus.energy/v1/"


class OctopusAPIError(RuntimeError):
    """A safe API error: it never includes credentials or response bodies."""


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or 443


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise OctopusAPIError("Octopus API redirect left the trusted origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Octopus timestamps require a timezone")
    return result.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TariffAgreement:
    tariff_code: str
    valid_from: str | None
    valid_to: str | None


@dataclass(frozen=True)
class Meter:
    fuel: str
    point_id: str
    serial_number: str
    tariff_code: str | None
    valid_from: str | None
    valid_to: str | None
    agreements: tuple[TariffAgreement, ...] = ()


class OctopusClient:
    def __init__(self, api_key: str, *, base_url: str = BASE_URL, timeout: float = 20.0, retries: int = 3) -> None:
        if not api_key.strip():
            raise ValueError("API key is required")
        self.base_url, self.timeout, self.retries = base_url.rstrip("/") + "/", timeout, retries
        if urlsplit(self.base_url).scheme != "https":
            raise ValueError("Octopus API requires HTTPS")
        token = base64.b64encode((api_key + ":").encode()).decode()
        self._auth = "Basic " + token
        self._opener = build_opener(_SameOriginRedirectHandler())

    def get(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = urljoin(self.base_url, path_or_url.lstrip("/"))
        parts = urlsplit(url)
        if _origin(url) != _origin(self.base_url) or parts.username or parts.password:
            raise OctopusAPIError("Octopus API URL left the trusted origin")
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        for attempt in range(self.retries + 1):
            request = Request(url, headers={"Authorization": self._auth, "Accept": "application/json", "User-Agent": "personal-ai-octopus-readonly/1.0"})
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == self.retries:
                    raise OctopusAPIError(f"Octopus API request failed (HTTP {exc.code})") from None
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == self.retries:
                    raise OctopusAPIError(f"Octopus API request failed ({type(exc).__name__})") from None
            time.sleep(min(8, 2 ** attempt))
        raise AssertionError("unreachable")

    def paginated(self, path: str, params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
        next_url: str | None = path
        next_params = {"page_size": "1000", **(params or {})}
        while next_url:
            page = self.get(next_url, next_params)
            yield from page.get("results", [])
            next_url, next_params = page.get("next"), None

    def account(self, account_number: str) -> dict[str, Any]:
        return self.get(f"accounts/{account_number}/")

    @staticmethod
    def meters(account: dict[str, Any]) -> list[Meter]:
        found: list[Meter] = []
        now = datetime.now(timezone.utc)
        earliest = datetime.min.replace(tzinfo=timezone.utc)
        latest = datetime.max.replace(tzinfo=timezone.utc)
        for prop in account.get("properties", []):
            for fuel, point_key, id_key in (("electricity", "electricity_meter_points", "mpan"), ("gas", "gas_meter_points", "mprn")):
                for point in prop.get(point_key, []):
                    agreements = tuple(TariffAgreement(a["tariff_code"], _timestamp(_instant(a["valid_from"])) if a.get("valid_from") else None, _timestamp(_instant(a["valid_to"])) if a.get("valid_to") else None) for a in point.get("agreements", []) if a.get("tariff_code"))
                    active = [a for a in agreements if (_instant(a.valid_from) if a.valid_from else earliest) <= now < (_instant(a.valid_to) if a.valid_to else latest)]
                    agreement = max(active, key=lambda a: _instant(a.valid_from) if a.valid_from else earliest, default=None)
                    for meter in point.get("meters", []):
                        serial = meter.get("serial_number")
                        if serial and point.get(id_key):
                            found.append(Meter(fuel, str(point[id_key]), str(serial), agreement.tariff_code if agreement else None, agreement.valid_from if agreement else None, agreement.valid_to if agreement else None, agreements))
        return found

    def consumption(self, meter: Meter, period_from: str | None = None, period_to: str | None = None) -> Iterator[dict[str, Any]]:
        endpoint = f"{meter.fuel}-meter-points/{meter.point_id}/meters/{meter.serial_number}/consumption/"
        params = {key: value for key, value in {"period_from": period_from, "period_to": period_to}.items() if value}
        yield from self.paginated(endpoint, params)

    def tariff_rates(self, meter: Meter, kind: str, period_from: str | None = None, period_to: str | None = None) -> Iterator[dict[str, Any]]:
        earliest = datetime.min.replace(tzinfo=timezone.utc)
        latest = datetime.max.replace(tzinfo=timezone.utc)
        requested_from = _instant(period_from) if period_from else earliest
        requested_to = _instant(period_to) if period_to else latest
        agreements = meter.agreements or ((TariffAgreement(meter.tariff_code, meter.valid_from, meter.valid_to),) if meter.tariff_code else ())
        for agreement in agreements:
            start = _instant(agreement.valid_from) if agreement.valid_from else earliest
            end = _instant(agreement.valid_to) if agreement.valid_to else latest
            query_from, query_to = max(start, requested_from), min(end, requested_to)
            if query_from >= query_to:
                continue
            endpoint = f"products/{product_code(agreement.tariff_code)}/{meter.fuel}-tariffs/{agreement.tariff_code}/{kind}/"
            params = {}
            if query_from != earliest:
                params["period_from"] = _timestamp(query_from)
            if query_to != latest:
                params["period_to"] = _timestamp(query_to)
            for rate in self.paginated(endpoint, params):
                rate_from = max(start, _instant(rate["valid_from"]))
                rate_to = min(end, _instant(rate["valid_to"]) if rate.get("valid_to") else latest)
                if rate_from >= rate_to or rate_to <= requested_from or rate_from >= requested_to:
                    continue
                # Preserve true rate boundaries across repeated sync windows.
                yield {**rate, "valid_from": _timestamp(rate_from), "valid_to": _timestamp(rate_to) if rate_to != latest else None}


def product_code(tariff_code: str) -> str:
    parts = tariff_code.split("-", 2)
    if len(parts) != 3 or parts[0] not in {"E", "G"}:
        raise ValueError("Unrecognised tariff code")
    # The final tariff-code component is the regional GSP suffix, not part of
    # the public product code (for example E-1R-GO-VAR-22-10-14-A).
    product, separator, region = parts[2].rpartition("-")
    if not separator or len(region) != 1:
        raise ValueError("Unrecognised tariff code")
    return product


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
