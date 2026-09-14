from __future__ import annotations

import os
import random
import time
from typing import Callable, TypeVar

from services.api_metrics import record_logical_call

T = TypeVar("T")

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.8
MAX_BACKOFF_SECONDS = 4.0

# 智谱GLM 免费额度限速是“每分钟请求数”超限（429 提示控制请求频率），
# 不是并发超限——V2.7.9 的串行（max_workers=1）仍会 429。这里再加两层：
# ① 请求节流：智谱端点两次请求至少间隔 N 秒；
# ② 429 长等待：撞限流后按 20/40/60/60 秒退避、放宽到 5 次尝试，
#    等限流窗口过去再发，而不是几秒内连打 3 次就把产品判死。
ZHIPU_PACE_SECONDS = 4.0
ZHIPU_429_WAIT_SECONDS = (20.0, 40.0, 60.0, 60.0)

_last_request_monotonic = 0.0


def _zhipu_rate_limited_endpoint() -> bool:
    return "bigmodel" in str(
        os.getenv("OPENAI_BASE_URL", "") or ""
    ).lower()


def _pace_zhipu_requests() -> None:
    global _last_request_monotonic
    if not _zhipu_rate_limited_endpoint():
        return
    now = time.monotonic()
    remaining = (
        _last_request_monotonic + ZHIPU_PACE_SECONDS - now
    )
    if remaining > 0:
        time.sleep(remaining)
    _last_request_monotonic = time.monotonic()


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

    attempt = 0
    while True:
        attempt += 1
        _pace_zhipu_requests()
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
            retryable = is_retryable_exception(exc)

            # 智谱 429：限流窗口（按分钟）没过去，短退避只会连着撞墙。
            # 改成长等待重试，并允许比常规更多的尝试次数。
            if (
                _status_code(exc) == 429
                and _zhipu_rate_limited_endpoint()
            ):
                zhipu_cap = max(
                    max_attempts,
                    1 + len(ZHIPU_429_WAIT_SECONDS),
                )
                if attempt >= zhipu_cap:
                    record_logical_call(
                        stage,
                        success=False,
                        elapsed=time.time() - started,
                        attempts=attempt,
                        error=str(exc),
                    )
                    raise
                delay = ZHIPU_429_WAIT_SECONDS[
                    min(
                        attempt - 1,
                        len(ZHIPU_429_WAIT_SECONDS) - 1,
                    )
                ]
                time.sleep(delay)
                continue

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
