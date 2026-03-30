"""Centralized logger with category-based filtering.

All log calls should go through ``ulog.<category>.<level>(...)``
instead of calling ``logger.info(...)`` directly.  This lets you toggle
visibility per category at runtime or via the UPDOWN_LOG_CATEGORIES
environment variable.

Usage::

    from common.log import ulog

    ulog.poly.info("YES=%.4f NO=%.4f", yes, no)
    ulog.signal.warning("Edge too small: %.4f", edge)

To filter categories, set UPDOWN_LOG_CATEGORIES to a comma-separated
allowlist::

    UPDOWN_LOG_CATEGORIES=poly,signal,exit   # only show these
    UPDOWN_LOG_CATEGORIES=*                  # show all (default)

To mute specific categories, prefix with ``-``::

    UPDOWN_LOG_CATEGORIES=*,-binance,-backpressure
"""

from __future__ import annotations

import logging
import os
from typing import Any

_base_logger = logging.getLogger("updown")

# ---------------------------------------------------------------------------
# Parse category filter from environment
# ---------------------------------------------------------------------------

_raw_filter = os.environ.get("UPDOWN_LOG_CATEGORIES", "*")
_show_all = False
_allowed: set[str] = set()
_denied: set[str] = set()

for _tok in (t.strip().lower() for t in _raw_filter.split(",") if t.strip()):
    if _tok == "*":
        _show_all = True
    elif _tok.startswith("-"):
        _denied.add(_tok[1:])
    else:
        _allowed.add(_tok)

# If no explicit allowlist and no wildcard, default to showing everything.
if not _allowed and not _show_all:
    _show_all = True


# Categories that are always shown regardless of filter settings.
_ALWAYS_ON: set[str] = {"startup"}


def _is_enabled(category: str) -> bool:
    cat = category.lower()
    if cat in _ALWAYS_ON:
        return True
    if cat in _denied:
        return False
    if _show_all:
        return True
    return cat in _allowed


# ---------------------------------------------------------------------------
# Category logger
# ---------------------------------------------------------------------------

class CategoryLogger:
    """Thin wrapper that prefixes messages with ``[category]`` and respects
    the category filter."""

    __slots__ = ("_category", "_tag", "_enabled")

    def __init__(self, category: str) -> None:
        self._category = category
        self._tag = f"[{category}]"
        self._enabled = _is_enabled(category)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self._enabled:
            _base_logger.debug(f"{self._tag} {msg}", *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self._enabled:
            _base_logger.info(f"{self._tag} {msg}", *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self._enabled:
            _base_logger.warning(f"{self._tag} {msg}", *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self._enabled:
            _base_logger.error(f"{self._tag} {msg}", *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self._enabled:
            _base_logger.exception(f"{self._tag} {msg}", *args, **kwargs)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class UpdownLog:
    """Attribute-based access to category loggers.

    ``ulog.poly`` returns (and caches) a ``CategoryLogger("poly")``.
    """

    def __init__(self) -> None:
        self._categories: dict[str, CategoryLogger] = {}

    def __getattr__(self, name: str) -> CategoryLogger:
        if name.startswith("_"):
            raise AttributeError(name)
        cat = self._categories.get(name)
        if cat is None:
            cat = CategoryLogger(name)
            self._categories[name] = cat
        return cat

    def set_filter(self, category: str, enabled: bool) -> None:
        """Toggle a category at runtime."""
        cat = getattr(self, category)
        cat.enabled = enabled

    def mute_all_except(self, *categories: str) -> None:
        """Mute every known category except the ones listed."""
        keep = {c.lower() for c in categories}
        for name, cat in self._categories.items():
            cat.enabled = name.lower() in keep

    def unmute_all(self) -> None:
        """Re-enable all known categories."""
        for cat in self._categories.values():
            cat.enabled = True


ulog = UpdownLog()


def apply_filter(filter_str: str) -> None:
    """Re-parse a filter string and update the global state.

    Called by the CLI ``--filter`` flag *before* any category loggers
    are created, so the new rules take effect for all subsequent
    ``ulog.<category>`` accesses.

    Also retroactively updates any CategoryLogger instances that were
    already created (e.g. at module import time).
    """
    global _show_all, _allowed, _denied

    _show_all = False
    _allowed.clear()
    _denied.clear()

    for tok in (t.strip().lower() for t in filter_str.split(",") if t.strip()):
        if tok == "*":
            _show_all = True
        elif tok.startswith("-"):
            _denied.add(tok[1:])
        else:
            _allowed.add(tok)

    if not _allowed and not _show_all:
        _show_all = True

    # Retroactively update any already-created category loggers.
    for cat in ulog._categories.values():
        cat.enabled = _is_enabled(cat._category)
