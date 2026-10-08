"""Unit and Integration Tests for FastMediaDownloader, Resuming, Chunk Retries, and Diagnostics."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.tl.custom.message import Message
from telethon.tl.types import Document, MessageMediaDocument
from telethon.errors import FloodWaitError, TimedOutError

from app.config import settings
from app.transfer.downloader import (
    DownloadMetadata,
    DownloadProgressInfo,
    DownloadSpeedTracker,
    FastMediaDownloader,
)


def test_speed_tracker_moving_average_and_eta():
    """Verify sliding window moving average calculation and ETA."""
    total_size = 100 * 1024 * 1024  # 100 MB
    tracker = DownloadSpeedTracker(total_size=total_size, window_seconds=2.0)

    # Simulate progressive downloads
    tracker.add_progress(10 * 1024 * 1024)
    assert tracker.average_speed >= 0

    # Add second progress snapshot
    tracker.add_progress(20 * 1024 * 1024)
    spd = tracker.current_speed
    assert spd >= 0
    eta = tracker.eta_seconds
    assert eta >= 0


def test_download_metadata_save_load_and_resume(tmp_path):
    """Verify metadata stores completed chunks and recovers missing chunks."""
    meta_path = tmp_path / "test_file.mp4.part.json"
    media_id = "doc_98765_4321"
    file_size = 10 * 524288  # 10 chunks of 512KB

    meta = DownloadMetadata.load_or_create(
        meta_path=meta_path,
        media_id=media_id,
        source_msg_id=55,
        file_size=file_size,
        chunk_size=524288,
    )
    assert meta.total_chunks == 10
    assert len(meta.completed_chunks) == 0

    # Mark chunks 0, 1, 2, 3 as completed
    for i in range(4):
        meta.mark_chunk_completed(i)
    meta.save()

    assert meta_path.exists()

    # Re-load from disk
    loaded = DownloadMetadata.load_or_create(
        meta_path=meta_path,
        media_id=media_id,
        source_msg_id=55,
        file_size=file_size,
        chunk_size=524288,
    )
    assert loaded.completed_chunks == {0, 1, 2, 3}
    missing = [i for i in range(10) if i not in loaded.completed_chunks]
    assert missing == [4, 5, 6, 7, 8, 9]

    # Delete metadata
    loaded.delete()
    assert not meta_path.exists()


@pytest.mark.asyncio
async def test_parallel_chunk_download_and_atomic_finalize(tmp_path):
    """Verify multi-worker chunk download, correct file offsets, and atomic finalize."""
    downloader = FastMediaDownloader()
    client = MagicMock()
    client.session.dc_id = 2
    client._sender = MagicMock()

    chunk_size = 524288
    total_chunks = 6
    file_size = total_chunks * chunk_size

    # Simulated chunk data generator
    chunk_data_map = {i: f"CHUNK_{i}_DATA_".encode("utf-8").ljust(chunk_size, b"X") for i in range(total_chunks)}

    async def mock_call(sender, request):
        offset = request.offset
        idx = offset // chunk_size
        return MagicMock(bytes=chunk_data_map[idx])

    client._call = mock_call

    doc = MagicMock(spec=Document)
    doc.id = 12345
    doc.access_hash = 67890
    doc.file_reference = b"ref"
    doc.size = file_size
    doc.dc_id = 2

    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc

    msg = MagicMock(spec=Message)
    msg.id = 99
    msg.media = media
    msg.chat_id = -1001

    target_file = tmp_path / "video.mp4"
    res = await downloader.download_media(
        client=client,
        message=msg,
        target_path=target_file,
        workers=4,
        request_size=chunk_size,
    )

    assert res == str(target_file)
    assert target_file.exists()
    assert target_file.stat().st_size == file_size

    # Verify content at each offset
    with open(target_file, "rb") as f:
        for i in range(total_chunks):
            f.seek(i * chunk_size)
            data = f.read(chunk_size)
            assert data == chunk_data_map[i]

    # Part files and metadata should be cleaned up
    assert not (tmp_path / "video.mp4.part").exists()
    assert not (tmp_path / "video.mp4.part.json").exists()


@pytest.mark.asyncio
async def test_resumable_download_skips_existing_chunks(tmp_path):
    """Verify partially downloaded file resumes without re-requesting completed chunks."""
    downloader = FastMediaDownloader()
    client = MagicMock()
    client.session.dc_id = 2
    client._sender = MagicMock()

    chunk_size = 524288
    total_chunks = 4
    file_size = total_chunks * chunk_size

    # Pre-seed part file with chunks 0 and 1
    part_path = tmp_path / "video.mp4.part"
    meta_path = tmp_path / "video.mp4.part.json"

    with open(part_path, "wb") as f:
        f.truncate(file_size)
        f.seek(0)
        f.write(b"CHUNK_0".ljust(chunk_size, b"A"))
        f.seek(chunk_size)
        f.write(b"CHUNK_1".ljust(chunk_size, b"B"))

    meta_payload = {
        "media_id": "doc_111_222",
        "source_message_id": 100,
        "file_size": file_size,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "completed_chunks": [0, 1],
        "timestamp": 123456789.0,
    }
    meta_path.write_text(json.dumps(meta_payload), encoding="utf-8")

    requested_offsets = []

    async def mock_call(sender, request):
        requested_offsets.append(request.offset)
        return MagicMock(bytes=b"NEW_CHUNK".ljust(request.limit, b"C"))

    client._call = mock_call

    doc = MagicMock(spec=Document)
    doc.id = 111
    doc.access_hash = 222
    doc.file_reference = b"ref"
    doc.size = file_size
    doc.dc_id = 2

    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc

    msg = MagicMock(spec=Message)
    msg.id = 100
    msg.media = media

    target_file = tmp_path / "video.mp4"
    await downloader.download_media(
        client=client,
        message=msg,
        target_path=target_file,
        workers=2,
        request_size=chunk_size,
    )

    # Chunks 0 and 1 should NOT have been requested from Telegram!
    assert 0 not in requested_offsets
    assert chunk_size not in requested_offsets
    assert 2 * chunk_size in requested_offsets
    assert 3 * chunk_size in requested_offsets

    assert target_file.exists()
    assert target_file.stat().st_size == file_size


@pytest.mark.asyncio
async def test_chunk_retry_with_transient_error_and_flood_wait(tmp_path):
    """Verify individual chunk retries on transient errors and FloodWait."""
    downloader = FastMediaDownloader()
    client = MagicMock()
    client.session.dc_id = 2
    client._sender = MagicMock()

    chunk_size = 524288
    file_size = 2 * chunk_size

    call_count = 0
    flood_err = FloodWaitError(request=None, capture=0)
    flood_err.seconds = 1

    async def mock_call(sender, request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First attempt fails with transient timeout
            raise TimedOutError(request=None, message="Chunk timeout")
        if call_count == 2:
            # Second attempt encounters FloodWait(1s)
            raise flood_err
        return MagicMock(bytes=b"DATA".ljust(request.limit, b"Z"))

    client._call = mock_call

    doc = MagicMock(spec=Document)
    doc.id = 555
    doc.access_hash = 666
    doc.file_reference = b"ref"
    doc.size = file_size
    doc.dc_id = 2

    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc

    msg = MagicMock(spec=Message)
    msg.id = 101
    msg.media = media

    target_file = tmp_path / "test_retry.mp4"

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await downloader.download_media(
            client=client,
            message=msg,
            target_path=target_file,
            workers=1,
            request_size=chunk_size,
        )

    assert target_file.exists()
    # At least 2 retries occurred
    assert call_count >= 3
    assert mock_sleep.call_count >= 2


@pytest.mark.asyncio
async def test_benchmark_download_measures_workers(tmp_path):
    """Verify internal benchmark runs across 1, 2, and 4 workers."""
    downloader = FastMediaDownloader()
    client = MagicMock()
    client.session.dc_id = 2
    client._sender = MagicMock()

    chunk_size = 524288
    file_size = 4 * chunk_size

    async def mock_call(sender, request):
        # Simulate small network delay
        await asyncio.sleep(0.01)
        return MagicMock(bytes=b"X" * request.limit)

    client._call = mock_call

    doc = MagicMock(spec=Document)
    doc.id = 777
    doc.access_hash = 888
    doc.file_reference = b"ref"
    doc.size = file_size
    doc.dc_id = 2

    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc

    msg = MagicMock(spec=Message)
    msg.id = 102
    msg.media = media

    result = await downloader.benchmark_download(
        client=client,
        message=msg,
        sample_mb=2.0,
        worker_options=[1, 2, 4],
    )

    assert 1 in result.worker_speeds
    assert 2 in result.worker_speeds
    assert 4 in result.worker_speeds
    assert result.recommended_workers in [1, 2, 4]
    report = result.format_report()
    assert "Download Benchmark Report" in report
    assert "Recommended Workers:" in report
