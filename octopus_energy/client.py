from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

BASE_URL = "https://api.octopus.energy/v1/"
PAGE_SIZE = 25000


class OctopusAPIError(RuntimeError):
    """A safe API error: it never includes credentials or response bodies."""


@dataclass(frozen=True)
class Agreement:
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
    agreements: tuple[Agreement, ...] = ()


class OctopusClient:
    def __init__(self, api_key: str, *, base_url: str = BASE_URL, timeout: float = 20.0, retries: int = 3) -> None:
        if not api_key:
            raise ValueError("API key is required")
        self.base_url, self.timeout, self.retries = base_url.rstrip("/") + "/", timeout, retries
        token = base64.b64encode((api_key + ":").encode()).decode()
        self._auth = "Basic " + token

    def get(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("https://") else urljoin(self.base_url, path_or_url.lstrip("/"))
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        for attempt in range(self.retries + 1):
            request = Request(
                url,
                headers={
                    "Authorization": self._auth,
                    "Accept": "application/json",
                    "User-Agent": "personal-ai-octopus-readonly/1.0",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:  # nosec B310: fixed HTTPS base or API pagination URL
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == self.retries:
                    raise OctopusAPIError(f"Octopus API request failed (HTTP {exc.code})") from None
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == self.retries:
                    raise OctopusAPIError(f"Octopus API request failed ({type(exc).__name__})") from None
            time.sleep(min(8, 2**attempt))
        raise AssertionError("unreachable")

    def paginated(self, path: str, params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
        next_url: str | None = path
        next_params: dict[str, str] | None = {"page_size": str(PAGE_SIZE), **(params or {})}
        while next_url:
            page = self.get(next_url, next_params)
            yield from page.get("results", [])
            next_url, next_params = page.get("next"), None

    def account(self, account_number: str) -> dict[str, Any]:
        return self.get(f"accounts/{account_number}/")

    @staticmethod
    def meters(account: dict[str, Any]) -> list[Meter]:
        found: list[Meter] = []
        for prop in account.get("properties", []):
            for fuel, point_key, id_key in (
                ("electricity", "electricity_meter_points", "mpan"),
                ("gas", "gas_meter_points", "mprn"),
            ):
                for point in prop.get(point_key, []):
                    agreements = point.get("agreements", [])
                    agreement = agreements[-1] if agreements else {}
                    history = tuple(
                        Agreement(a["tariff_code"], a.get("valid_from"), a.get("valid_to"))
                        for a in agreements
                        if a.get("tariff_code")
                    )
                    for meter in point.get("meters", []):
                        serial = meter.get("serial_number")
                        if serial and point.get(id_key):
                            found.append(
                                Meter(
                                    fuel,
                                    str(point[id_key]),
                                    str(serial),
                                    agreement.get("tariff_code"),
                                    agreement.get("valid_from"),
                                    agreement.get("valid_to"),
                                    history,
                                )
                            )
        return found

    def consumption(
        self, meter: Meter, period_from: str | None = None, period_to: str | None = None
    ) -> Iterator[dict[str, Any]]:
        endpoint = f"{meter.fuel}-meter-points/{meter.point_id}/meters/{meter.serial_number}/consumption/"
        params = {key: value for key, value in {"period_from": period_from, "period_to": period_to}.items() if value}
        yield from self.paginated(endpoint, params)

    def tariff_rates(
        self,
        meter: Meter,
        kind: str,
        period_from: str | None = None,
        period_to: str | None = None,
        tariff_code: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        tariff_code = tariff_code or meter.tariff_code
        if not tariff_code:
            return
        endpoint = f"products/{product_code(tariff_code)}/{meter.fuel}-tariffs/{tariff_code}/{kind}/"
        params = {key: value for key, value in {"period_from": period_from, "period_to": period_to}.items() if value}
        yield from self.paginated(endpoint, params)


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
