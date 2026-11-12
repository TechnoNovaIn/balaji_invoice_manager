"""
SQLite-safe database transaction retry utilities.

SQLite does not support row-level `SELECT ... FOR UPDATE` or `SKIP LOCKED`.
Under concurrent writes, transient `OperationalError: database is locked` or
`database table is locked` errors can occur. This module retries complete
atomic transactions with bounded exponential backoff and jitter.
"""
import logging
import random
import time
from typing import Callable, TypeVar

from django.db import OperationalError, connection

logger = logging.getLogger(__name__)

T = TypeVar('T')


def is_sqlite_lock_error(exc: Exception) -> bool:
    """Return True if the exception represents a transient SQLite lock/busy error."""
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc).lower()
    return 'locked' in msg or 'busy' in msg


def run_with_sqlite_retry(
    func: Callable[[], T],
    max_attempts: int = 6,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
) -> T:
    """
    Execute `func` (typically containing a `with transaction.atomic():` block),
    retrying the complete transaction on transient SQLite lock errors with
    bounded exponential backoff and jitter.

    If already inside an outer atomic block (e.g., Django TestCase transaction),
    retries cannot safely roll back only the inner block on SQLite, so `func`
    is executed once directly.
    """
    if connection.in_atomic_block or max_attempts <= 1:
        return func()

    last_exc = None
    for attempt in range(max_attempts):
        try:
            return func()
        except OperationalError as exc:
            if not is_sqlite_lock_error(exc) or attempt >= max_attempts - 1:
                raise
            last_exc = exc
            delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0.0, 0.025)
            logger.warning(
                "Transient SQLite lock on attempt %d/%d; retrying full transaction in %.3fs (%s)",
                attempt + 1,
                max_attempts,
                delay,
                exc,
            )
            time.sleep(delay)
    raise last_exc  # pragma: no cover

