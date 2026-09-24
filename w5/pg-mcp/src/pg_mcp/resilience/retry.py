"""Retry with exponential backoff for transient failures.

This module provides a generic retry helper used to wrap async operations
(such as LLM API calls or database queries) that may fail transiently.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    backoff_factor: float = 2.0,
    retryable: tuple[type[BaseException], ...] = (Exception,),
    operation_name: str = "operation",
) -> T:
    """Execute an async operation with exponential backoff retries.

    The operation is attempted once, then up to ``max_retries`` additional
    times. Before each retry the caller sleeps ``retry_delay * backoff_factor
    ** attempt`` seconds (1s, 2s, 4s, ... with defaults). Exceptions not in
    ``retryable`` are raised immediately without retrying.

    Args:
        operation: Zero-argument callable returning an awaitable.
        max_retries: Maximum number of retry attempts after the first try.
        retry_delay: Initial delay in seconds before the first retry.
        backoff_factor: Multiplier applied to the delay after each attempt.
        retryable: Exception types that trigger a retry; others propagate.
        operation_name: Human-readable name used in log messages.

    Returns:
        The result of the successful operation call.

    Raises:
        The last raised exception if all attempts fail.

    Example:
        >>> result = await retry_async(
        ...     lambda: client.fetch(),
        ...     max_retries=3,
        ...     retryable=(ConnectionError,),
        ...     operation_name="fetch",
        ... )
    """
    last_error: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            return await operation()
        except retryable as e:
            last_error = e
            if attempt >= max_retries:
                logger.error(
                    "%s failed after %d attempt(s)", operation_name, attempt + 1
                )
                raise
            delay = retry_delay * (backoff_factor**attempt)
            logger.warning(
                "%s failed (attempt %d/%d), retrying in %.2fs: %s",
                operation_name,
                attempt + 1,
                max_retries + 1,
                delay,
                e,
            )
            await asyncio.sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise AssertionError(f"{operation_name} retry loop exited unexpectedly: {last_error!r}")
