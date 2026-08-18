"""Tiny retry decorator. Avoids the `tenacity` dependency.

Retries on any exception, with exponential backoff capped at `max_delay`.
"""
from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def retry(
    *,
    attempts: int = 3,
    min_delay: float = 1.0,
    max_delay: float = 20.0,
    jitter: float = 0.25,
) -> Callable[[F], F]:
    """Exponential-backoff retry with full jitter.

    Usage:
        @retry(attempts=3, min_delay=1, max_delay=10)
        def fetch(): ...
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for n in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if n == attempts - 1:
                        raise
                    delay = min(max_delay, min_delay * (2**n))
                    delay = delay + random.uniform(-jitter * delay, jitter * delay)
                    log.warning(
                        "Attempt %d/%d failed (%s) — retrying in %.1fs",
                        n + 1,
                        attempts,
                        exc,
                        delay,
                    )
                    time.sleep(max(0.0, delay))
            # unreachable, but mypy-friendly
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator
