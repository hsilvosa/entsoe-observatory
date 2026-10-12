from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from entsoe_observatory.client import EntsoeApiError, EntsoeClient, build_params
from entsoe_observatory.zones import load_registry

TOKEN = "super-secret-token-123"
ES = load_registry()["ES"]
START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 3, 3, tzinfo=UTC)
ACK_NO_DATA = (
    '<Acknowledgement_MarketDocument'
    ' xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">'
    "<Reason><code>999</code><text>No matching data found for Data item</text></Reason>"
    "</Acknowledgement_MarketDocument>"
)
ACK_ERROR = ACK_NO_DATA.replace("No matching data found for Data item", "Invalid argument")
DATA = "<Publication_MarketDocument><mRID>x</mRID></Publication_MarketDocument>"


def make_client(handler: httpx.MockTransport, sleeps: list[float] | None = None) -> EntsoeClient:
    log = sleeps if sleeps is not None else []
    return EntsoeClient(
        TOKEN, transport=handler, requests_per_second=1000, sleep=log.append, max_retries=3
    )


def test_params_for_prices_and_load() -> None:
    prices = build_params("day_ahead_prices", ES, START, END)
    assert prices["documentType"] == "A44"
    assert prices["in_Domain"] == prices["out_Domain"] == ES.eic_code
    assert prices["periodStart"] == "202603010000"
    load = build_params("actual_load", ES, START, END)
    assert load["documentType"] == "A65"
    assert load["processType"] == "A16"
    assert load["outBiddingZone_Domain"] == ES.eic_code
    with pytest.raises(ValueError):
        build_params("generation", ES, START, END)


def test_token_is_sent_but_never_returned() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=DATA)

    client = make_client(httpx.MockTransport(handler))
    result = client.fetch("day_ahead_prices", ES, START, END)
    assert seen[0].url.params["securityToken"] == TOKEN
    assert result.status == "data"
    assert TOKEN not in str(result.params)
    assert TOKEN not in repr(client)


def test_secret_str_token_is_accepted() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=DATA))
    client = EntsoeClient(SecretStr(TOKEN), transport=transport)
    assert client.fetch("actual_load", ES, START, END).status == "data"


def test_retries_transient_errors_with_retry_after() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, text=DATA)

    client = make_client(httpx.MockTransport(handler), sleeps)
    assert client.fetch("actual_load", ES, START, END).status == "data"
    assert calls["n"] == 3
    assert sleeps[0] == 7.0


def test_gateway_status_599_is_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(599) if calls["n"] == 1 else httpx.Response(200, text=DATA)

    client = make_client(httpx.MockTransport(handler), [])
    assert client.fetch("actual_load", ES, START, END).status == "data"
    assert calls["n"] == 2


def test_no_data_acknowledgement_is_not_an_error() -> None:
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, text=ACK_NO_DATA)))
    assert client.fetch("day_ahead_prices", ES, START, END).status == "no_data"


def test_errors_never_contain_the_token() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    cases = [
        httpx.MockTransport(lambda r: httpx.Response(401)),
        httpx.MockTransport(lambda r: httpx.Response(400, text=ACK_ERROR)),
        httpx.MockTransport(lambda r: httpx.Response(500)),
        httpx.MockTransport(lambda r: httpx.Response(200, text=ACK_ERROR)),
        httpx.MockTransport(lambda r: httpx.Response(200, text="not xml")),
        httpx.MockTransport(failing),
    ]
    for transport in cases:
        with pytest.raises(EntsoeApiError) as info:
            make_client(transport).fetch("day_ahead_prices", ES, START, END)
        assert TOKEN not in str(info.value)
        assert info.value.__cause__ is None


def test_missing_token_is_rejected() -> None:
    with pytest.raises(ValueError):
        EntsoeClient("")
