"""Atomic JSON I/O utilities.

All writes use the .tmp.json + os.replace() pattern so readers never see
a partially written file.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def write_json_atomic(path: Path, data) -> None:
    """Write *data* as JSON to *path* atomically.

    The content is first written to a sibling temp file
    ``<path>.tmp.json``, then renamed over *path* with ``os.replace()``.
    This guarantees that any reader either sees the old complete file or
    the new complete file — never a partial write.

    ``path.parent`` is created (including any intermediate directories)
    if it does not already exist.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".tmp.json")
    try:
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file before re-raising.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_append_to_json_list(path: Path, record: dict) -> None:
    """Append *record* to the JSON array stored at *path*.

    If *path* does not exist the file is created with ``[record]`` as its
    content.  The existing array is read, *record* is appended, and the
    result is written back using the ``.tmp.json`` + ``os.replace()``
    pattern.

    Concurrent writers are serialised with an exclusive ``fcntl.flock``
    on a sidecar lock file (``<path>.lock``).  On platforms where
    ``fcntl`` is unavailable (e.g. Windows) a warning is logged and the
    write proceeds without locking.

    ``path.parent`` is created (including any intermediate directories)
    if it does not already exist.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = path.with_suffix(path.suffix + ".lock")

    try:
        import fcntl

        _append_with_lock(path, record, lock_path, fcntl)
    except ImportError:
        logger.warning(
            "fcntl is not available on this platform; "
            "appending to %s without an exclusive lock.",
            path,
        )
        _append_no_lock(path, record)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_existing_list(path: Path) -> list:
    """Return the JSON array stored at *path*, or an empty list."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON array in {path}, got {type(data).__name__}")
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Cannot parse existing JSON list at {path}: {exc}") from exc


def _write_list(path: Path, records: list) -> None:
    """Write *records* to *path* using the atomic tmp + replace pattern."""
    tmp_path = path.with_suffix(".tmp.json")
    try:
        tmp_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _append_with_lock(path: Path, record: dict, lock_path: Path, fcntl) -> None:
    """Append *record* inside an exclusive flock on *lock_path*."""
    with open(lock_path, "a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            records = _read_existing_list(path)
            records.append(record)
            _write_list(path, records)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _append_no_lock(path: Path, record: dict) -> None:
    """Append *record* without any locking (fallback for non-POSIX platforms)."""
    records = _read_existing_list(path)
    records.append(record)
    _write_list(path, records)
