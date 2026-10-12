"""ENTSO-E Transparency Platform client for all zones.

Shape adapted from `energy-predictor` (`sources/entsoe.py`: httpx call, error redaction) and the
author's `entsoe-transparency` downloader (`client.py`: rate limiter, retries, Retry-After,
acknowledgement handling). The token only appears in the outgoing query string. It never
reaches an exception message, a log line, a manifest or the returned parameters.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from .parser import LOAD, PRICES, local_name
from .zones import Zone

ENTSOE_ENDPOINT = "https://web-api.tp.entsoe.eu/api"
# 599 is not a standard code; the ENTSO-E gateway returned it for a failed pull (NO_2) and a
# retry is the right answer to it, as for the other transient gateway errors.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 599}

# httpx logs full request URLs (including the query string) at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class EntsoeApiError(RuntimeError):
    """Raised for rejected or failed requests. Messages never contain the token."""


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    status: str  # "data" or "no_data"
    params: dict[str, str]  # request parameters without the token
    sha256: str
    retrieved_at: datetime


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.interval
        if delay:
            time.sleep(delay)


def build_params(dataset: str, zone: Zone, start: datetime, end: datetime) -> dict[str, str]:
    """Query parameters (without token) for one zone and window."""
    params = {
        "periodStart": start.astimezone(UTC).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(UTC).strftime("%Y%m%d%H%M"),
    }
    if dataset == PRICES:
        return {
            **params,
            "documentType": "A44",
            "in_Domain": zone.eic_code,
            "out_Domain": zone.eic_code,
        }
    if dataset == LOAD:
        return {
            **params,
            "documentType": "A65",
            "processType": "A16",
            "outBiddingZone_Domain": zone.eic_code,
        }
    raise ValueError(f"Unknown dataset: {dataset}")


def inspect_response(content: bytes) -> str:
    """Return "data" or "no_data". Raise for any other acknowledgement."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise EntsoeApiError("ENTSO-E returned invalid XML") from None
    if local_name(root.tag) != "Acknowledgement_MarketDocument":
        return "data"
    code = text = None
    for element in root.iter():
        name = local_name(element.tag)
        if name == "code" and code is None:
            code = element.text
        elif name == "text" and text is None:
            text = element.text
    normalized = (text or "").lower()
    if "no matching data" in normalized or "no data" in normalized:
        return "no_data"
    raise EntsoeApiError(f"ENTSO-E rejected the request (reason {code or 'unknown'})")


class EntsoeClient:
    def __init__(
        self,
        token: SecretStr | str,
        *,
        base_url: str = ENTSOE_ENDPOINT,
        timeout: float = 60,
        max_retries: int = 4,
        requests_per_second: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        raw = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not raw:
            raise ValueError("ENTSO-E token is required")
        self._token = raw
        self._base_url = base_url
        self._max_retries = max_retries
        self._limiter = RateLimiter(requests_per_second)
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "entsoe-observatory/0.1", "Accept": "application/xml,text/xml"},
        )

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:
        return f"EntsoeClient(base_url={self._base_url!r}, token=***)"

    def fetch(self, dataset: str, zone: Zone, start: datetime, end: datetime) -> FetchResult:
        params = build_params(dataset, zone, start, end)
        for attempt in range(self._max_retries + 1):
            self._limiter.wait()
            try:
                response = self._http.get(
                    self._base_url, params={"securityToken": self._token, **params}
                )
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise EntsoeApiError(
                        f"ENTSO-E connection failed after retries ({type(exc).__name__})"
                    ) from None
                self._sleep(min(60.0, 2**attempt + random.random()))
                continue
            status = response.status_code
            if status in RETRYABLE_STATUS and attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                self._sleep(min(60.0, delay))
                continue
            if status == 401:
                raise EntsoeApiError("ENTSO-E rejected the token (HTTP 401)")
            if status >= 400:
                # A 400 can carry an acknowledgement that explains why (e.g. no data).
                try:
                    kind = inspect_response(response.content)
                except EntsoeApiError:
                    raise EntsoeApiError(f"ENTSO-E request failed (HTTP {status})") from None
                if kind != "no_data":
                    raise EntsoeApiError(f"ENTSO-E request failed (HTTP {status})")
            content = response.content
            return FetchResult(
                content=content,
                status=inspect_response(content),
                params=params,
                sha256=hashlib.sha256(content).hexdigest(),
                retrieved_at=datetime.now(UTC),
            )
        raise AssertionError("unreachable")
