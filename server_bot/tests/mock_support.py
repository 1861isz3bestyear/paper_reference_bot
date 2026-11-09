# Copyright 2026 anchored-vwam-backtest contributors
"""Validate values recorded by unittest mocks at test boundaries."""

from typing import cast
from unittest.mock import Mock

from shared.validation import object_mapping


def mock_child(parent: object, name: str) -> Mock:
    """Return mock child.

    Returns:
        The computed result.

    """
    assert isinstance(parent, Mock)
    value = cast("object", getattr(parent, name))
    assert isinstance(value, Mock)
    return value


def mock_result(parent: Mock) -> Mock:
    """Return mock result.

    Returns:
        The computed result.

    """
    value = cast("object", parent.return_value)
    assert isinstance(value, Mock)
    return value


def call_args_at(parent: Mock, index: int) -> tuple[object, ...]:
    """Return call args at.

    Returns:
        The computed result.

    """
    return cast("tuple[object, ...]", parent.call_args_list[index].args)


def call_kwargs_at(parent: Mock, index: int) -> dict[str, object]:
    """Return call kwargs at.

    Returns:
        The computed result.

    """
    return object_mapping(cast("object", parent.call_args_list[index].kwargs))


def call_args(parent: Mock) -> tuple[object, ...]:
    """Return call args.

    Returns:
        The computed result.

    """
    call = parent.call_args
    assert call is not None
    return cast("tuple[object, ...]", call.args)


def call_kwargs(parent: Mock) -> dict[str, object]:
    """Return call kwargs.

    Returns:
        The computed result.

    """
    call = parent.call_args
    assert call is not None
    return object_mapping(cast("object", call.kwargs))
