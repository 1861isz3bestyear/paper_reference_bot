# Copyright 2026 anchored-vwam-backtest contributors
"""Regress margin credentials, signed requests and safe archive diagnostics."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests

from server_bot.tunnel_archive import collect
from shared.validation import object_mapping

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def margin_response() -> requests.Response:
    """Create a real response so HTTP and JSON validation both execute.

    Returns:
        A mutable fixture response for the private margin endpoint.

    """
    response = requests.Response()
    response.status_code = 200
    response.raw = BytesIO(b'{"retCode":0,"result":{}}')
    return response


@pytest.fixture
def exchange(
    monkeypatch: pytest.MonkeyPatch, margin_response: requests.Response
) -> requests.Session:
    """Keep all traffic local while collecting every production source.

    Returns:
        A session with deterministic public sources and margin response.

    """
    monkeypatch.setenv("BYBIT_API_KEY", "private-fixture-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "private-fixture-secret")
    monkeypatch.setattr(
        "server_bot.tunnel_archive.time", SimpleNamespace(sleep=Mock())
    )
    session = requests.Session()

    def get(url: str, **_kwargs: object) -> requests.Response:
        """Return exchange-specific response envelopes.

        Returns:
            The private fixture or a successful public response.

        """
        if "interest-rate-history?" in url:
            return margin_response
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(b'{"result":[],"code":"00000","data":[]}')
        return response

    monkeypatch.setattr(session, "get", get)
    return session


@dataclass(frozen=True, kw_only=True)
class MarginRejection:
    """A rejected exchange response and its archive representation."""

    code: int
    storage_mode: Literal["raw", "compact"]


@dataclass(frozen=True, kw_only=True)
class CredentialSetting:
    """One externally supplied credential setting."""

    name: str
    value: str


@pytest.mark.parametrize(
    "rejection",
    [
        MarginRejection(code=code, storage_mode=mode)
        for mode in ("raw", "compact")
        for code in (
            10001,
            10002,
            10003,
            10004,
            10005,
            10006,
            10007,
            10010,
            33004,
            99999,
        )
    ],
)
def test_margin_api_rejection_is_diagnosable_without_secrets(
    tmp_path: Path,
    exchange: requests.Session,
    margin_response: requests.Response,
    *,
    caplog: pytest.LogCaptureFixture,
    rejection: MarginRejection,
) -> None:
    """Keep rejection evidence while logging only safe codes and guidance."""
    payload = json.dumps({
        "retCode": rejection.code,
        "retMsg": "private-fixture-key private-fixture-secret signature-value",
        "result": {},
    })
    margin_response.raw = BytesIO(payload.encode())
    path = tmp_path / "archive.sqlite"
    assert collect(path, exchange, storage_mode=rejection.storage_mode) == 1
    assert f'"api_code": {rejection.code}' in caplog.text
    assert '"source": "margin"' in caplog.text
    assert '"http_status": 200' in caplog.text
    assert "private-fixture" not in caplog.text
    assert "signature-value" not in caplog.text
    with closing(sqlite3.connect(path)) as db:
        if rejection.storage_mode == "raw":
            rows: list[tuple[str, str]] = db.execute(
                "SELECT payload,error FROM tunnel_inputs WHERE source='margin'"
            ).fetchall()
            assert rows == [(payload, "ValueError")]
        else:
            compressed: list[tuple[bytes, str]] = db.execute(
                "SELECT p.payload,o.error FROM tunnel_observations o "
                "JOIN tunnel_payloads p USING(digest) WHERE source='margin'"
            ).fetchall()
            assert [
                (zlib.decompress(body).decode(), error)
                for body, error in compressed
            ] == [(payload, "ValueError")]


@pytest.mark.parametrize(
    "setting",
    [
        CredentialSetting(name=name, value=value)
        for name in ("BYBIT_API_KEY", "BYBIT_API_SECRET")
        for value in ("", "   ")
    ],
)
def test_missing_or_blank_margin_credentials_are_distinct(
    tmp_path: Path,
    exchange: requests.Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    caplog: pytest.LogCaptureFixture,
    setting: CredentialSetting,
) -> None:
    """Report service environment setup rather than an opaque API rejection."""
    monkeypatch.setenv(setting.name, setting.value)
    assert collect(tmp_path / "archive.sqlite", exchange) == 1
    assert "MissingMarginCredentialsError" in caplog.text
    assert "EnvironmentFile" in caplog.text
    assert '"http_status": null' in caplog.text
    assert "private-fixture" not in caplog.text


def test_margin_signs_the_exact_transmitted_query_after_trimming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sign URL-encoded mainnet requests with the cleaned credential pair."""
    monkeypatch.setenv("BYBIT_API_KEY", " fixture-key \n")
    monkeypatch.setenv("BYBIT_API_SECRET", " fixture-secret \n")
    monkeypatch.setattr(
        "server_bot.tunnel_archive.time", SimpleNamespace(sleep=Mock())
    )
    signed: list[tuple[str, object]] = []
    session = requests.Session()

    def get(url: str, **kwargs: object) -> requests.Response:
        """Capture the signed wire request while returning valid envelopes.

        Returns:
            A successful response for every source.

        """
        if "interest-rate-history?" in url:
            signed.append((url, kwargs.get("headers")))
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(b'{"result":[],"code":"00000","data":[]}')
        return response

    monkeypatch.setattr(session, "get", get)
    assert collect(tmp_path / "archive.sqlite", session) == 0
    url, headers = signed[0]
    headers = object_mapping(headers)
    assert headers["X-BAPI-API-KEY"] == "fixture-key"
    query = urlsplit(url).query
    assert "vipLevel=No%20VIP" in query
    assert urlsplit(url).netloc == "api.bybit.com"
    stamp = headers["X-BAPI-TIMESTAMP"]
    assert isinstance(stamp, str)
    assert requests.Request("GET", url).prepare().url == url
    expected = hmac.new(
        b"fixture-secret",
        f"{stamp}fixture-key5000{query}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected


@dataclass(frozen=True, kw_only=True)
class FailedResponse:
    """An untrusted failure envelope and the only safe code to log."""

    payload: str
    status: int
    api_code: int | str | None = None


@pytest.mark.parametrize(
    "failure",
    [
        FailedResponse(payload="<html>private-fixture-key</html>", status=403),
        FailedResponse(payload="[]", status=200),
        FailedResponse(payload='{"retCode":true,"result":{}}', status=200),
        FailedResponse(
            payload='{"retCode":"private-fixture-secret","result":{}}',
            status=200,
        ),
        FailedResponse(
            payload='{"retCode":"10005","result":{}}',
            status=200,
            api_code="10005",
        ),
        FailedResponse(
            payload='{"retCode":10005,"retMsg":"private-fixture-key"}',
            status=403,
            api_code=10005,
        ),
    ],
)
def test_margin_http_and_malformed_responses_keep_safe_diagnostics(
    tmp_path: Path,
    exchange: requests.Session,
    margin_response: requests.Response,
    *,
    caplog: pytest.LogCaptureFixture,
    failure: FailedResponse,
) -> None:
    """Handle HTTP failures and malformed envelopes without leaking text."""
    margin_response.status_code = failure.status
    margin_response.raw = BytesIO(failure.payload.encode())
    assert collect(tmp_path / "archive.sqlite", exchange) == 1
    assert f'"api_code": {json.dumps(failure.api_code)}' in caplog.text
    assert f'"http_status": {failure.status}' in caplog.text
    assert "private-fixture" not in caplog.text


def test_network_exception_messages_are_never_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Connection diagnostics must not repeat URLs containing credentials."""
    monkeypatch.setenv("BYBIT_API_KEY", "private-fixture-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "private-fixture-secret")
    monkeypatch.setattr(
        "server_bot.tunnel_archive.time", SimpleNamespace(sleep=Mock())
    )
    session = requests.Session()
    monkeypatch.setattr(
        session,
        "get",
        Mock(
            side_effect=requests.Timeout(
                "https://example.invalid?key=private-fixture-key&signature=secret"
            )
        ),
    )
    failures = collect(tmp_path / "archive.sqlite", session)
    expected_sources = 12
    assert failures == expected_sources
    assert "Timeout" in caplog.text
    assert "private-fixture" not in caplog.text
    assert "signature=secret" not in caplog.text
