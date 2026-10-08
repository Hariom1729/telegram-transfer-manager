"""Tests for retry logic, FloodWait handling, and backoff."""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from telethon.errors import FloodWaitError, RPCError, TimedOutError
from app.transfer.retry import NonRetryableTransferError, RetryExecutor


@pytest.mark.asyncio
async def test_retry_success_on_first_attempt():
    """Verify operation succeeds immediately when no error is raised."""
    executor = RetryExecutor(max_retries=3)
    mock_op = AsyncMock(return_value="destination_message_id_123")

    result = await executor.execute(operation=mock_op, job_id=1, message_id=10)
    assert result == "destination_message_id_123"
    assert mock_op.call_count == 1


@pytest.mark.asyncio
async def test_retry_handles_flood_wait():
    """Verify FloodWaitError triggers wait and retries cleanly."""
    executor = RetryExecutor(max_retries=3)

    # First call raises FloodWaitError(request=None, capture=0), second succeeds
    flood_err = FloodWaitError(request=None, capture=0)
    flood_err.seconds = 2

    mock_op = AsyncMock(side_effect=[flood_err, 999])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await executor.execute(operation=mock_op, job_id=1, message_id=10)
        assert result == 999
        assert mock_op.call_count == 2
        # Assert slept for flood wait duration (2 + 1)
        mock_sleep.assert_called_with(3)


@pytest.mark.asyncio
async def test_retry_handles_transient_network_errors():
    """Verify transient timeouts retry with exponential backoff."""
    executor = RetryExecutor(max_retries=2, backoff_base=2.0)
    timeout_err = TimedOutError(request=None, message="Timed out")

    mock_op = AsyncMock(side_effect=[timeout_err, 888])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await executor.execute(operation=mock_op, job_id=1, message_id=10)
        assert result == 888
        assert mock_op.call_count == 2
        mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_stops_on_non_retryable_rpc_error():
    """Verify unrecoverable errors like ChatWriteForbidden raise NonRetryableTransferError."""
    executor = RetryExecutor(max_retries=3)
    perm_err = RPCError(request=None, message="ChatWriteForbiddenError")

    mock_op = AsyncMock(side_effect=perm_err)

    with pytest.raises(NonRetryableTransferError):
        await executor.execute(operation=mock_op, job_id=1, message_id=10)
    assert mock_op.call_count == 1
