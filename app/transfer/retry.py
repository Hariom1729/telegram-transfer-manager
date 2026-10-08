"""Retry Policy and FloodWait Handler."""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional
from telethon.errors import (
    FloodWaitError,
    RPCError,
    ServerError,
    TimedOutError,
)
from app.config import settings

logger = logging.getLogger(__name__)


class NonRetryableTransferError(Exception):
    """Raised when an operation encounters an unrecoverable failure that should not be retried."""
    pass


class RetryExecutor:
    """Executes asynchronous operations with FloodWait handling and exponential backoff."""

    def __init__(
        self,
        max_retries: Optional[int] = None,
        backoff_base: float = 2.0,
    ) -> None:
        self.max_retries = (
            max_retries if max_retries is not None else settings.MAX_RETRY_ATTEMPTS
        )
        self.backoff_base = backoff_base

    async def execute(
        self,
        operation: Callable[[], Coroutine[Any, Any, Any]],
        job_id: int,
        message_id: int,
        on_flood_wait: Optional[Callable[[int], Coroutine[Any, Any, None]]] = None,
    ) -> Any:
        """Execute an async operation with automatic FloodWait and retry handling.

        Args:
            operation: The async callable to execute.
            job_id: The transfer job ID for logging.
            message_id: The source message ID being processed.
            on_flood_wait: Optional callback triggered when FloodWait occurs.
        """
        attempt = 0
        while True:
            try:
                return await operation()
            except FloodWaitError as e:
                wait_seconds = e.seconds
                logger.warning(
                    "FloodWait job=%s wait=%s source_message=%s",
                    job_id,
                    wait_seconds,
                    message_id,
                )
                if on_flood_wait:
                    try:
                        await on_flood_wait(wait_seconds)
                    except Exception as cb_err:
                        logger.debug("on_flood_wait callback error: %s", cb_err)

                # Wait required duration + 1s buffer
                await asyncio.sleep(wait_seconds + 1)
                # Flood wait does NOT consume standard retry count
                continue

            except (TimedOutError, ServerError, ConnectionError) as e:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error(
                        "Max retries exceeded for job=%s message=%s error=%s",
                        job_id,
                        message_id,
                        e,
                    )
                    raise

                delay = self.backoff_base ** attempt
                logger.warning(
                    "Transient error job=%s message=%s attempt=%s/%s retry_in=%0.1fs error=%s",
                    job_id,
                    message_id,
                    attempt,
                    self.max_retries,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

            except RPCError as e:
                # Specific unrecoverable RPC errors shouldn't be retried
                err_str = str(e)
                if any(
                    non_retry in err_str
                    for non_retry in [
                        "ChatWriteForbidden",
                        "ChatAdminRequired",
                        "MessageIdInvalid",
                        "UserBannedInChannel",
                        "ChannelPrivate",
                        "TopicClosed",
                    ]
                ):
                    logger.error(
                        "Non-retryable RPC error job=%s message=%s: %s",
                        job_id,
                        message_id,
                        e,
                    )
                    raise NonRetryableTransferError(err_str) from e

                attempt += 1
                if attempt > self.max_retries:
                    logger.error(
                        "Max RPC retries exceeded for job=%s message=%s error=%s",
                        job_id,
                        message_id,
                        e,
                    )
                    raise

                delay = self.backoff_base ** attempt
                logger.warning(
                    "RPC error job=%s message=%s attempt=%s/%s retry_in=%0.1fs error=%s",
                    job_id,
                    message_id,
                    attempt,
                    self.max_retries,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

            except Exception as e:
                logger.error(
                    "Unexpected error job=%s message=%s error=%s",
                    job_id,
                    message_id,
                    e,
                )
                raise


retry_executor = RetryExecutor()
