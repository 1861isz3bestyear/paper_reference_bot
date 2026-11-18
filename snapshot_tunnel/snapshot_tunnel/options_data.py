# Copyright 2026 anchored-vwam-backtest contributors
"""Collect complete Deribit chains at their receipt-time availability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import sleep
from typing import TYPE_CHECKING, Literal

import requests

from snapshot_tunnel.options_depth import fetch_depth
from snapshot_tunnel.options_model import Option, Snapshot, positive_number
from snapshot_tunnel.validation import (
    boolean,
    integer,
    json_object,
    number,
    object_list,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


API = "https://www.deribit.com/api/v2/public/"
MAX_TIMESTAMP_MS = 253402300799000  # Last second of year 9999.


@dataclass(frozen=True, kw_only=True)
class ChainObservation:
    """Keep exchange observation and local availability distinct."""

    snapshot: Snapshot
    observed_at: datetime


def _download(
    session: requests.Session,
    method: str,
    params: Mapping[str, str],
    *,
    attempts_left: int = 2,
) -> requests.Response:
    """Retry public reads after transient connection failures.

    Returns:
        The response from the first successful transport attempt.

    Raises:
        requests.ConnectionError: If retries fail or TLS verification fails.
        requests.Timeout: If all attempts time out.
        requests.RequestException: If local transport configuration is invalid.

    """
    try:
        return session.get(API + method, params=params, timeout=(3, 5))
    except (requests.ConnectionError, requests.Timeout) as exc:
        if attempts_left <= 1 or isinstance(exc, requests.exceptions.SSLError):
            raise
        sleep(1)
        return _download(
            session, method, params, attempts_left=attempts_left - 1
        )
    except requests.RequestException:
        raise
    except OSError as exc:
        msg = f"HTTPS transport configuration failed: {exc}"
        raise requests.RequestException(msg) from exc


def _request(
    session: requests.Session, method: str, params: Mapping[str, str]
) -> dict[str, object]:
    """Fetch a public JSON-RPC result, rejecting exchange-level errors.

    Returns:
        Validated response envelope.

    Raises:
        ValueError: If the exchange returns an error or no result.

    """
    response = _download(session, method, params)
    response.raise_for_status()
    payload = json_object(response.text)
    if "error" in payload or "result" not in payload:
        msg = "Deribit returned an error or missing result."
        raise ValueError(msg)
    return payload


def _indexed_rows(value: object) -> dict[str, dict[str, object]]:
    """Index exchange rows while rejecting duplicate instruments.

    Returns:
        Rows indexed by instrument name.

    Raises:
        ValueError: If instrument names are duplicated.

    """
    result: dict[str, dict[str, object]] = {}
    for item in object_list(value):
        row = object_mapping(item)
        name = string(row["instrument_name"])
        if name in result:
            msg = "Duplicate Deribit instrument."
            raise ValueError(msg)
        result[name] = row
    return result


def _option(instrument: dict[str, object], quote: dict[str, object]) -> Option:
    """Normalize Deribit option OI in underlying units into contracts.

    Returns:
        A validated contract for the approximate USD Black-Scholes model.

    Raises:
        ValueError: If contract specifications are unsupported.

    """
    side = string(instrument["option_type"])
    if side not in {"call", "put"} or string(instrument["kind"]) != "option":
        msg = "Unsupported Deribit contract."
        raise ValueError(msg)
    expiry_ms = integer(instrument["expiration_timestamp"])
    if not 0 < expiry_ms <= MAX_TIMESTAMP_MS:
        msg = "Invalid Deribit expiration timestamp."
        raise ValueError(msg)
    multiplier = positive_number(str(number(instrument["contract_size"])))
    return Option(
        exchange="DERIBIT",
        expiry=datetime.fromtimestamp(expiry_ms / 1000, UTC),
        strike=positive_number(str(number(instrument["strike"]))),
        side="call" if side == "call" else "put",
        open_interest=positive_number(
            str(number(quote["open_interest"])), allow_zero=True
        )
        / multiplier,
        multiplier=multiplier,
        iv=positive_number(str(number(quote["mark_iv"]))) / 100,
    )


def normalize_chain(
    instruments: object, quotes: object, *, asset: Literal["BTC", "ETH"]
) -> tuple[Option, ...]:
    """Require every active selected contract to have a valid summary.

    Returns:
        Complete normalized chain, including zero-OI contracts.

    Raises:
        ValueError: If the chain is empty, incomplete or has foreign contracts.

    """
    definitions = _indexed_rows(instruments)
    summaries = _indexed_rows(quotes)
    options: list[Option] = []
    for name, instrument in definitions.items():
        if not boolean(instrument["is_active"]):
            continue
        if string(instrument["base_currency"]) != asset:
            msg = "Unexpected underlying currency."
            raise ValueError(msg)
        if name not in summaries:
            msg = f"Incomplete Deribit chain: no quote for {name}."
            raise ValueError(msg)
        options.append(_option(instrument, summaries[name]))
    if not options:
        msg = "Deribit returned an empty active chain."
        raise ValueError(msg)
    return tuple(options)


def fetch_snapshot(
    session: requests.Session, *, asset: Literal["BTC", "ETH"] = "BTC"
) -> ChainObservation:
    """Collect a chain, rejecting slow, future-dated or expired observations.

    Returns:
        Snapshot available only after all requests finished.

    Raises:
        ValueError: If response timestamps or chain validity are unsuitable.

    """
    started = datetime.now(UTC)
    params = {"currency": asset, "kind": "option"}
    instruments = _request(
        session,
        "get_instruments",
        {
            **params,
            "expired": "false",
        },
    )
    summary = _request(session, "get_book_summary_by_currency", params)
    index = _request(
        session,
        "get_index_price",
        {
            "index_name": asset.lower() + "_usd",
        },
    )
    received = datetime.now(UTC)
    server_us = integer(summary["usOut"])
    if not 0 <= server_us <= MAX_TIMESTAMP_MS * 1000:
        msg = "Invalid Deribit observation timestamp."
        raise ValueError(msg)
    observed = datetime.fromtimestamp(server_us / 1_000_000, UTC)
    if received - started > timedelta(minutes=2) or not (
        started - timedelta(minutes=2)
        <= observed
        <= received + timedelta(seconds=5)
    ):
        msg = "Deribit observation is stale or clocks disagree."
        raise ValueError(msg)
    options = normalize_chain(
        instruments["result"], summary["result"], asset=asset
    )
    order_book = fetch_depth(session, asset=asset)
    received = datetime.now(UTC)
    if any(option.expiry <= received for option in options):
        msg = "Chain expired during collection; retry with a new chain."
        raise ValueError(msg)
    spot = positive_number(
        str(number(object_mapping(index["result"])["index_price"]))
    )
    return ChainObservation(
        snapshot=Snapshot(
            timestamp=received,
            asset=asset,
            spot=spot,
            options=options,
            order_book=order_book,
        ),
        observed_at=observed,
    )
