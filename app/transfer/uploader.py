"""High-Performance, Parallel Telegram Media Uploader.

Features:
- Official MTProto SaveBigFilePartRequest and SaveFilePartRequest pipelining over existing persistent Telethon client.
- Multi-worker concurrent chunk uploading (default 4-8 workers).
- 512 KB maximum MTProto part size.
- Random-access non-blocking file-backed reading (os.pread) without buffering the entire file in RAM.
- Non-blocking async progress notification so UI edits never stall network transmission.
- Full support for both InputFileBig (> 10MB) and InputFile (<= 10MB) with MD5 checksum.
"""

import asyncio
from collections import deque
import hashlib
import logging
import math
import os
from pathlib import Path
import time
from typing import Callable, Coroutine, Dict, List, Optional

from telethon import TelegramClient, helpers, types
from telethon.errors import (
    FloodWaitError,
    RpcCallFailError,
    ServerError,
    TimedOutError,
)
from telethon.tl import functions

from app.config import settings
from app.transfer.downloader import DownloadProgressInfo
from app.transfer.retry import NonRetryableTransferError

logger = logging.getLogger(__name__)


class FastMediaUploader:
    """High-throughput chunked Telegram media uploader with MTProto pipelining."""

    def __init__(self) -> None:
        pass

    async def upload_file(
        self,
        client: TelegramClient,
        file_path: Path | str,
        file_name: Optional[str] = None,
        progress_callback: Optional[
            Callable[[DownloadProgressInfo], Coroutine]
        ] = None,
        job_id: Optional[int] = None,
        workers: Optional[int] = None,
        part_size: int = 524288,  # 512 KB
    ) -> types.InputFile | types.InputFileBig:
        """Upload a file using parallel MTProto chunk streaming.

        Returns an InputFile or InputFileBig ready to be passed directly to client.send_file.
        """
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"File to upload does not exist: {path}")

        file_size = path.stat().st_size
        if file_size == 0:
            raise NonRetryableTransferError("Cannot upload empty (0 byte) file.")

        name = file_name or path.name

        # Ensure part_size is a multiple of 1024 and <= 512KB
        part_size = max(1024, min(524288, (part_size // 1024) * 1024))
        part_count = (file_size + part_size - 1) // part_size

        is_big = file_size > 10 * 1024 * 1024  # > 10 MB uses InputFileBig
        file_id = helpers.generate_random_long()

        worker_count = workers if workers is not None else getattr(settings, "UPLOAD_WORKERS", 8)
        worker_count = min(worker_count, max(1, part_count))

        # For small files (<= 10 MB), calculate MD5 checksum for InputFile
        md5_hash = ""
        if not is_big:
            def _calc_md5() -> str:
                hasher = hashlib.md5()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(262144), b""):
                        hasher.update(chunk)
                return hasher.hexdigest()
            md5_hash = await asyncio.to_thread(_calc_md5)

        # Open file descriptor for random-access pread
        fd = os.open(path, os.O_RDONLY)

        # Queue parts
        part_queue: asyncio.Queue[int] = asyncio.Queue()
        for idx in range(part_count):
            part_queue.put_nowait(idx)

        uploaded_bytes = 0
        bytes_lock = asyncio.Lock()
        t_start = time.perf_counter()
        last_progress_time = [0.0]

        async def notify_progress(force: bool = False) -> None:
            if not progress_callback:
                return
            now = time.perf_counter()
            if not force and (now - last_progress_time[0] < getattr(settings, "DOWNLOAD_PROGRESS_INTERVAL", 2.0)):
                return
            last_progress_time[0] = now
            elapsed = max(0.001, now - t_start)
            current_up = uploaded_bytes
            spd = current_up / elapsed
            rem = max(0, file_size - current_up)
            eta = (rem / spd) if spd > 0 else 0.0
            pct = (current_up / file_size * 100.0) if file_size > 0 else 0.0

            prog = DownloadProgressInfo(
                file_name=name,
                file_size=file_size,
                downloaded_bytes=current_up,
                percent=pct,
                current_speed=spd,
                average_speed=spd,
                peak_speed=spd,
                eta_seconds=eta,
                workers=worker_count,
                is_resumed=False,
                is_upload=True,
            )
            # Spawn in background so it never pauses transmission
            try:
                asyncio.create_task(progress_callback(prog))
            except Exception:
                pass

        async def worker_loop() -> None:
            nonlocal uploaded_bytes

            while not part_queue.empty():
                try:
                    part_idx = part_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                offset = part_idx * part_size
                length = min(part_size, file_size - offset)

                # Read chunk from disk via os.pread without moving file cursor
                chunk_data = await asyncio.to_thread(os.pread, fd, length, offset)
                if not chunk_data:
                    part_queue.task_done()
                    continue

                if is_big:
                    req = functions.upload.SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=part_idx,
                        file_total_parts=part_count,
                        bytes=chunk_data,
                    )
                else:
                    req = functions.upload.SaveFilePartRequest(
                        file_id=file_id,
                        file_part=part_idx,
                        bytes=chunk_data,
                    )

                retries = 0
                while True:
                    try:
                        call_res = client(req)
                        if asyncio.iscoroutine(call_res) or hasattr(call_res, "__await__"):
                            await call_res
                        async with bytes_lock:
                            uploaded_bytes += len(chunk_data)
                        await notify_progress(force=False)
                        part_queue.task_done()
                        break

                    except FloodWaitError as fe:
                        retries += 1
                        logger.warning(
                            "FloodWait uploading part %d: waiting %ds",
                            part_idx,
                            fe.seconds,
                        )
                        await asyncio.sleep(fe.seconds + 1)

                    except (
                        TimedOutError,
                        ServerError,
                        ConnectionError,
                        RpcCallFailError,
                        Exception,
                    ) as err:
                        retries += 1
                        if retries > getattr(settings, "MAX_RETRY_ATTEMPTS", 5):
                            logger.error(
                                "Part %d upload failed after %d retries: %s",
                                part_idx,
                                retries,
                                err,
                            )
                            part_queue.task_done()
                            raise NonRetryableTransferError(
                                f"Failed uploading file part {part_idx}: {err}"
                            ) from err

                        backoff = min(8.0, 1.5 ** retries)
                        await asyncio.sleep(backoff)

        try:
            tasks = [asyncio.create_task(worker_loop()) for _ in range(worker_count)]
            await asyncio.gather(*tasks)
        finally:
            try:
                os.close(fd)
            except Exception:
                pass

        t_end = time.perf_counter()
        total_time = max(0.001, t_end - t_start)
        avg_speed = (file_size / total_time) / (1024 * 1024)
        logger.info(
            "UPLOAD COMPLETE job=%s file=%s size=%.2fMB parts=%d time=%.2fs speed=%.2fMB/s",
            job_id if job_id is not None else "-",
            name,
            file_size / (1024 * 1024),
            part_count,
            total_time,
            avg_speed,
        )

        await notify_progress(force=True)

        if is_big:
            return types.InputFileBig(id=file_id, parts=part_count, name=name)
        else:
            return types.InputFile(
                id=file_id,
                parts=part_count,
                name=name,
                md5_checksum=md5_hash,
            )


fast_media_uploader = FastMediaUploader()
