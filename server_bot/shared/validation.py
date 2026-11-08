# Copyright 2026 anchored-vwam-backtest contributors
"""Runtime checks at untyped serialization and exchange boundaries."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping


def json_object(text: str) -> dict[str, object]:
    """Parse JSON text and require a JSON object at the root.

    Returns:
        The computed result.

    """
    value = cast("object", json.loads(text))
    return object_mapping(value)


def object_mapping(value: object) -> dict[str, object]:
    """Validate and return a mapping with string keys.

    Returns:
        The computed result.

    Raises:
        TypeError: If ``value`` is not an object with string keys.

    """
    if not isinstance(value, dict):
        msg = "Expected a JSON object."
        raise TypeError(msg)
    # The container check establishes object-valued entries; keys are checked
    # below.
    entries = cast("Mapping[object, object]", value)
    result: dict[str, object] = {}
    for key, item in entries.items():
        if not isinstance(key, str):
            msg = "Expected string object keys."
            raise TypeError(msg)
        result[key] = item
    return result


def object_list(value: object) -> list[object]:
    """Validate and return a JSON-style list.

    Returns:
        The computed result.

    Raises:
        TypeError: If ``value`` is not a list.

    """
    if not isinstance(value, list):
        msg = "Expected a JSON array."
        raise TypeError(msg)
    # No claims about element schemas: each consumer validates the object
    # elements.
    return cast("list[object]", value)


def string(value: object) -> str:
    """Validate and return a string value.

    Returns:
        The computed result.

    Raises:
        TypeError: If ``value`` is not a string.

    """
    if not isinstance(value, str):
        msg = "Expected a string."
        raise TypeError(msg)
    return value


def boolean(value: object) -> bool:
    """Validate and return a boolean value.

    Returns:
        The computed result.

    Raises:
        TypeError: If ``value`` is not a boolean.

    """
    if not isinstance(value, bool):
        msg = "Expected a boolean."
        raise TypeError(msg)
    return value


def integer(value: object) -> int:
    """Validate and return an integer that is not a boolean.

    Returns:
        The computed result.

    Raises:
        TypeError: If ``value`` is not an integer.

    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "Expected an integer."
        raise TypeError(msg)
    return value


def number(value: object) -> float:
    """Validate and return a finite numeric value.

    Returns:
        The computed result.

    Raises:
        TypeError: If ``value`` is not numeric.
        ValueError: If ``value`` is not finite.

    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = "Expected a number."
        raise TypeError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = "Expected a finite number."
        raise ValueError(msg)
    return result


def numeric_string(value: object) -> float:
    """Validate a number or numeric string and return it as a float.

    Returns:
        The computed result.

    """
    if isinstance(value, str):
        value = float(value)
    return number(value)


def numeric_rows(value: object, width: int) -> list[list[float]]:
    """Validate rows of numeric values with an exact width.

    Returns:
        The computed result.

    Raises:
        ValueError: If a row has the wrong width.

    """
    rows: list[list[float]] = []
    for item in object_list(value):
        row = object_list(item)
        if len(row) != width:
            msg = f"Expected {width} values per row."
            raise ValueError(msg)
        rows.append([numeric_string(cell) for cell in row])
    return rows


def database_rows(value: object) -> list[tuple[object, ...]]:
    """Validate a sequence of database rows represented as tuples.

    Returns:
        The computed result.

    Raises:
        TypeError: If an item is not a database row tuple.

    """
    rows: list[tuple[object, ...]] = []
    for row in object_list(value):
        if not isinstance(row, tuple):
            msg = "Expected a database row tuple."
            raise TypeError(msg)
        rows.append(cast("tuple[object, ...]", row))
    return rows
