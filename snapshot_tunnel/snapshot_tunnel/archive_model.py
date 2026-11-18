# Copyright 2026 anchored-vwam-backtest contributors
"""Immutable archive identities and UTC coverage without data access."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from market_storage.models import Window


@dataclass(frozen=True, kw_only=True)
class RecordedCollection:
    """Identity and receipt bounds of a complete options collection."""

    identity: str
    window: Window


@dataclass(frozen=True, kw_only=True)
class LocalArchiveSource:
    """A snapshot version and complete successful collection bounds."""

    path: Path
    modified_ns: int
    size: int
    collections: tuple[RecordedCollection, ...]

    @property
    def start(self) -> datetime:
        """First complete chain receipt."""
        return self.collections[0].window.end

    @property
    def end(self) -> datetime:
        """Last complete chain receipt."""
        return self.collections[-1].window.end
