# Copyright 2026 anchored-vwam-backtest contributors
"""Portable callbacks for shared strategy tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def ignoring_arguments[T](callback: Callable[[], T]) -> Callable[..., T]:
    """Return ignoring arguments.

    Returns:
        The computed result.

    """

    def invoke(*_args: object, **_kwargs: object) -> T:
        return callback()

    return invoke
