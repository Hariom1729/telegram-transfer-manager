"""Unit tests for FastMediaUploader."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon import types
from telethon.tl import functions

from app.transfer.uploader import FastMediaUploader


@pytest.mark.asyncio
async def test_fast_uploader_small_file(tmp_path):
    """Verify small file (<= 10MB) upload uses SaveFilePartRequest and produces InputFile."""
    uploader = FastMediaUploader()
    client = MagicMock()

    uploaded_parts = []

    async def mock_call(request):
        if isinstance(request, functions.upload.SaveFilePartRequest):
            uploaded_parts.append((request.file_part, len(request.bytes)))
            return True
        return True

    client.side_effect = mock_call

    # Create 2 MB test file
    test_file = tmp_path / "test_video.mp4"
    part_size = 524288  # 512 KB
    total_size = 4 * part_size  # 2 MB
    test_file.write_bytes(b"A" * total_size)

    input_file = await uploader.upload_file(
        client=client,
        file_path=test_file,
        file_name="test_video.mp4",
        workers=2,
        part_size=part_size,
    )

    assert isinstance(input_file, types.InputFile)
    assert input_file.name == "test_video.mp4"
    assert input_file.parts == 4
    assert len(uploaded_parts) == 4
    assert input_file.md5_checksum is not None


@pytest.mark.asyncio
async def test_fast_uploader_big_file(tmp_path):
    """Verify large file (> 10MB) upload uses SaveBigFilePartRequest and produces InputFileBig."""
    uploader = FastMediaUploader()
    client = MagicMock()

    uploaded_parts = []

    async def mock_call(request):
        if isinstance(request, functions.upload.SaveBigFilePartRequest):
            uploaded_parts.append((request.file_part, len(request.bytes), request.file_total_parts))
            return True
        return True

    client.side_effect = mock_call

    # Create 12 MB test file (needs > 10MB for InputFileBig)
    test_file = tmp_path / "big_video.mp4"
    part_size = 524288  # 512 KB
    total_size = 24 * part_size  # 12 MB
    test_file.write_bytes(b"B" * total_size)

    input_file = await uploader.upload_file(
        client=client,
        file_path=test_file,
        file_name="big_video.mp4",
        workers=4,
        part_size=part_size,
    )

    assert isinstance(input_file, types.InputFileBig)
    assert input_file.name == "big_video.mp4"
    assert input_file.parts == 24
    assert len(uploaded_parts) == 24
