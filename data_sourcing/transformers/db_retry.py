"""
Retry/fallback utility for async database operations.

Provides retry logic with exponential backoff for transient connection
and timeout errors in the transform pipeline's DB lookups.
"""
import asyncio
import logging
from typing import Any, Callable, Sequence, Type

import asyncpg

logger = logging.getLogger(__name__)

# Exception types that are transient and worth retrying
RETRYABLE_EXCEPTIONS: tuple[Type[BaseException], ...] = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
    OSError,
)


async def retry_async(
    func: Callable,
    *args: Any,
    max_retries: int = 3,
    backoff_delays: Sequence[float] = (0.5, 1.0, 2.0),
    operation_name: str = "db_operation",
    **kwargs: Any,
) -> Any:
    """
    Retry an async function on transient connection/timeout errors.

    Args:
        func: Async callable to retry.
        *args: Positional arguments for func.
        max_retries: Maximum number of retry attempts.
        backoff_delays: Delay in seconds before each retry attempt.
        operation_name: Name for logging context.
        **kwargs: Keyword arguments for func.

    Returns:
        The return value of func on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exception: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS as exc:
            last_exception = exc
            if attempt < max_retries:
                delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
                logger.warning(
                    "%s: attempt %d/%d failed (%s: %s), retrying in %.1fs",
                    operation_name,
                    attempt + 1,
                    max_retries + 1,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s: all %d attempts exhausted, last error: %s: %s",
                    operation_name,
                    max_retries + 1,
                    type(exc).__name__,
                    exc,
                )

    raise last_exception  # type: ignore[misc]
