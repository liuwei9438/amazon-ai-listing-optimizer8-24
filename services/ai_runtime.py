from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from services.api_metrics import record_logical_call

T = TypeVar("T")

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.8
MAX_BACKOFF_SECONDS = 4.0


class RetryableAIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, RetryableAIError):
        return True

    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name or "ratelimit" in name:
        return True

    code = _status_code(exc)
    if code in {408, 409, 429}:
        return True
    if code is not None and 500 <= code <= 599:
        return True
    return False


def execute_with_retry(
    operation: Callable[[], T],
    *,
    stage: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> T:
    max_attempts = max(1, int(max_attempts))
    started = time.time()
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = operation()
            record_logical_call(
                stage,
                success=True,
                elapsed=time.time() - started,
                attempts=attempt,
            )
            return result
        except Exception as exc:
            last_exc = exc
            retryable = is_retryable_exception(exc)
            if not retryable or attempt >= max_attempts:
                record_logical_call(
                    stage,
                    success=False,
                    elapsed=time.time() - started,
                    attempts=attempt,
                    error=str(exc),
                )
                raise

            delay = min(
                BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                MAX_BACKOFF_SECONDS,
            )
            delay += random.uniform(0.0, 0.25)
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc
