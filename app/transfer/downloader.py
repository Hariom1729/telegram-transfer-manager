"""High-Performance, Parallel, Resumable Telegram Media Downloader.

Features:
- Official MTProto GetFileRequest pipelining over existing persistent Telethon client.
- Native cryptg AES IGE acceleration.
- Random-access non-blocking file-backed writing (os.pwrite) with bounded RAM usage.
- Resumable downloads via .part and .part.json chunk tracking.
- Chunk-level retry with exponential backoff and FloodWait compliance.
- Moving-average sliding window throughput and ETA calculation.
- Internal benchmarking capability for worker optimization.
"""

from collections import deque
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
import asyncio

from telethon import TelegramClient, utils
from telethon.errors import (
    FileMigrateError,
    FileReferenceExpiredError,
    FilerefUpgradeNeededError,
    FloodWaitError,
    RpcCallFailError,
    ServerError,
    TimedOutError,
)
from telethon.tl import functions, types
from telethon.tl.custom.message import Message

from app.config import settings
from app.transfer.retry import NonRetryableTransferError

logger = logging.getLogger(__name__)


@dataclass
class DownloadProgressInfo:
    """Live progress snapshot for real-time UI display."""

    file_name: str
    file_size: int
    downloaded_bytes: int
    percent: float
    current_speed: float  # bytes/second (sliding window moving average)
    average_speed: float  # bytes/second (overall)
    peak_speed: float  # bytes/second
    eta_seconds: float
    workers: int
    is_resumed: bool
    is_upload: bool = False

    def format_progress_text(self) -> str:
        """Format the Telegram UI text matching Section 15 specification."""
        size_mb = self.file_size / (1024 * 1024)
        size_gb = self.file_size / (1024 * 1024 * 1024)
        dl_mb = self.downloaded_bytes / (1024 * 1024)
        dl_gb = self.downloaded_bytes / (1024 * 1024 * 1024)

        if self.file_size >= 1024 * 1024 * 1024:
            size_str = f"{size_gb:.2f} GB"
            dl_str = f"{dl_gb:.2f} GB"
        else:
            size_str = f"{size_mb:.1f} MB"
            dl_str = f"{dl_mb:.1f} MB"

        cur_speed_mb = self.current_speed / (1024 * 1024)
        avg_speed_mb = self.average_speed / (1024 * 1024)
        eta_str = f"{int(self.eta_seconds)}s" if self.eta_seconds > 0 else "--"

        # 10-char visual progress bar
        pct = min(100.0, max(0.0, self.percent))
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)

        status_header = "📤 Uploading" if self.is_upload else "📥 Downloading"
        label = "Uploaded" if self.is_upload else "Downloaded"

        return (
            f"{status_header}\n\n"
            f"File: {self.file_name}\n"
            f"Size: {size_str}\n"
            f"{label}: {dl_str} / {size_str}\n"
            f"Progress: {pct:.1f}% [{bar}]\n"
            f"Speed: Current: {cur_speed_mb:.1f} MB/s | Avg: {avg_speed_mb:.1f} MB/s\n"
            f"ETA: {eta_str}"
        )


class DownloadSpeedTracker:
    """Calculates real-time moving average throughput and ETA using a sliding window."""

    def __init__(self, total_size: int, window_seconds: float = 3.0) -> None:
        self.total_size = total_size
        self.window_seconds = window_seconds
        self.start_time = time.monotonic()
        self._samples: deque = deque()  # (monotonic_time, downloaded_bytes)
        self.peak_speed: float = 0.0

    def add_progress(self, current_downloaded: int) -> None:
        now = time.monotonic()
        self._samples.append((now, current_downloaded))
        cutoff = now - self.window_seconds
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def current_speed(self) -> float:
        """Sliding window speed in bytes/sec."""
        if len(self._samples) < 2:
            return self.average_speed
        t1, b1 = self._samples[0]
        t2, b2 = self._samples[-1]
        dt = t2 - t1
        if dt <= 0.05:
            return self.average_speed
        spd = max(0.0, (b2 - b1) / dt)
        if spd > self.peak_speed:
            self.peak_speed = spd
        return spd

    @property
    def average_speed(self) -> float:
        """Overall average speed since start in bytes/sec."""
        elapsed = time.monotonic() - self.start_time
        if elapsed <= 0.05 or not self._samples:
            return 0.0
        _, b_latest = self._samples[-1]
        return b_latest / elapsed

    @property
    def eta_seconds(self) -> float:
        """Estimated seconds until completion based on current throughput."""
        spd = self.current_speed
        if spd <= 1024 or not self._samples:
            return 0.0
        _, b_latest = self._samples[-1]
        remaining = max(0, self.total_size - b_latest)
        return remaining / spd


class DownloadMetadata:
    """Tracks chunk completion state on disk for robust resume support."""

    def __init__(
        self,
        media_id: str,
        source_message_id: int,
        file_size: int,
        chunk_size: int,
        meta_path: Path,
    ) -> None:
        self.media_id = media_id
        self.source_message_id = source_message_id
        self.file_size = file_size
        self.chunk_size = chunk_size
        self.total_chunks = (
            (file_size + chunk_size - 1) // chunk_size if chunk_size else 0
        )
        self.meta_path = meta_path
        self.completed_chunks: Set[int] = set()

    @classmethod
    def load_or_create(
        cls,
        meta_path: Path,
        media_id: str,
        source_msg_id: int,
        file_size: int,
        chunk_size: int,
    ) -> "DownloadMetadata":
        inst = cls(media_id, source_msg_id, file_size, chunk_size, meta_path)
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if (
                    data.get("media_id") == media_id
                    and data.get("file_size") == file_size
                    and data.get("chunk_size") == chunk_size
                ):
                    raw_completed = data.get("completed_chunks", [])
                    inst.completed_chunks = set(raw_completed)
                    pct = (
                        (len(inst.completed_chunks) / inst.total_chunks * 100)
                        if inst.total_chunks
                        else 0
                    )
                    logger.info(
                        "Found valid resumable state for %s: %d/%d chunks (%.1f%%) completed",
                        media_id,
                        len(inst.completed_chunks),
                        inst.total_chunks,
                        pct,
                    )
                    return inst
            except Exception as e:
                logger.debug(
                    "Could not load metadata %s (%s). Starting fresh.", meta_path, e
                )
        return inst

    def mark_chunk_completed(self, chunk_idx: int) -> None:
        self.completed_chunks.add(chunk_idx)

    def save(self) -> None:
        try:
            payload = {
                "media_id": self.media_id,
                "source_message_id": self.source_message_id,
                "file_size": self.file_size,
                "chunk_size": self.chunk_size,
                "total_chunks": self.total_chunks,
                "completed_chunks": list(sorted(self.completed_chunks)),
                "timestamp": time.time(),
            }
            tmp = self.meta_path.with_suffix(self.meta_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.meta_path)
        except Exception as e:
            logger.debug("Failed to persist download metadata: %s", e)

    def delete(self) -> None:
        if self.meta_path.exists():
            try:
                self.meta_path.unlink()
            except Exception:
                pass


@dataclass
class BenchmarkResult:
    """Benchmark evaluation summary across multiple worker configurations."""

    file_size: int
    chunk_size: int
    worker_speeds: Dict[int, float]  # workers -> MB/s
    worker_times: Dict[int, float]  # workers -> seconds
    cpu_percent: float
    recommended_workers: int

    def format_report(self) -> str:
        size_mb = self.file_size / (1024 * 1024)
        lines = [
            f"📊 Download Benchmark Report",
            f"Tested Data: {size_mb:.1f} MB (chunk_size={self.chunk_size // 1024} KB)",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for w, spd in sorted(self.worker_speeds.items()):
            t = self.worker_times.get(w, 0.0)
            lines.append(f"Workers: {w} ➔ {spd:.2f} MB/s ({t:.2f}s)")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"Recommended Workers: {self.recommended_workers}")
        return "\n".join(lines)


class FastMediaDownloader:
    """High-throughput chunked Telegram media downloader with MTProto pipelining."""

    def __init__(self) -> None:
        self._borrow_locks: Dict[int, asyncio.Lock] = {}

    def _get_media_identifier(self, media: Any, message_id: int) -> str:
        """Derive a stable unique identifier for the media object."""
        if hasattr(media, "document") and media.document:
            return f"doc_{media.document.id}_{media.document.access_hash}"
        if hasattr(media, "photo") and media.photo:
            return f"photo_{media.photo.id}_{media.photo.access_hash}"
        return f"media_msg_{message_id}"

    async def download_media(
        self,
        client: TelegramClient,
        message: Message,
        target_path: Path | str,
        progress_callback: Optional[
            Callable[[DownloadProgressInfo], Coroutine]
        ] = None,
        job_id: Optional[int] = None,
        workers: Optional[int] = None,
        request_size: Optional[int] = None,
    ) -> str:
        """Download message media using parallel MTProto chunk streaming.

        - Streams directly to disk via os.pwrite without buffering the file in RAM.
        - Supports pause/resume via companion .part.json.
        - Preserves existing persistent Telethon client session.
        - Automatically falls back to standard client.download_media for small/unsupported media.
        """
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Resolve media file location and size
        if not message.media:
            raise NonRetryableTransferError("Message has no media to download.")

        info = None
        try:
            info = utils._get_file_info(message.media)
        except Exception:
            pass

        # Determine file size
        file_size = (
            getattr(info, "size", None)
            or getattr(getattr(message, "file", None), "size", None)
            or getattr(getattr(message, "document", None), "size", None)
        )

        chunk_size = (
            request_size
            if request_size is not None
            else settings.DOWNLOAD_REQUEST_SIZE
        )
        chunk_size = (chunk_size // 4096) * 4096
        chunk_size = max(4096, min(524288, chunk_size))

        worker_count = workers if workers is not None else settings.DOWNLOAD_WORKERS
        if worker_count not in (1, 2, 4, 8):
            worker_count = 4

        # For small files (<= chunk_size) or unsupported location types, use standard download_media
        if not info or not file_size or file_size <= chunk_size:
            logger.debug(
                "Using standard download_media for small/special media message=%s (size=%s)",
                message.id,
                file_size,
            )
            downloaded = await client.download_media(message, file=str(target_path))
            if not downloaded or not os.path.exists(downloaded):
                raise NonRetryableTransferError(
                    "Telegram did not make the media available to this account."
                )
            return str(downloaded)

        # 2. Setup Resumable Paths (.part and .part.json)
        media_id = self._get_media_identifier(message.media, message.id)
        part_path = target_path.with_name(f"{target_path.name}.part")
        meta_path = target_path.with_name(f"{target_path.name}.part.json")

        meta = None
        is_resumed = False
        if settings.DOWNLOAD_RESUME:
            meta = DownloadMetadata.load_or_create(
                meta_path=meta_path,
                media_id=media_id,
                source_msg_id=message.id,
                file_size=file_size,
                chunk_size=chunk_size,
            )
            if meta.completed_chunks and part_path.exists():
                is_resumed = True
        else:
            meta = DownloadMetadata(
                media_id=media_id,
                source_message_id=message.id,
                file_size=file_size,
                chunk_size=chunk_size,
                meta_path=meta_path,
            )

        # Pre-allocate or open file
        mode = "r+b" if (part_path.exists() and is_resumed) else "w+b"
        fd = os.open(part_path, os.O_RDWR | os.O_CREAT)
        try:
            # Ensure target file length
            os.ftruncate(fd, file_size)
        except Exception:
            pass

        # 3. Queue Missing Chunks
        total_chunks = meta.total_chunks
        missing_chunks = [i for i in range(total_chunks) if i not in meta.completed_chunks]

        chunk_queue: asyncio.Queue[int] = asyncio.Queue()
        for idx in missing_chunks:
            chunk_queue.put_nowait(idx)

        downloaded_bytes = len(meta.completed_chunks) * chunk_size
        # Account for possible smaller last chunk in completed calculation
        if (total_chunks - 1) in meta.completed_chunks:
            last_chunk_len = file_size - ((total_chunks - 1) * chunk_size)
            downloaded_bytes = (
                (len(meta.completed_chunks) - 1) * chunk_size + last_chunk_len
            )

        speed_tracker = DownloadSpeedTracker(total_size=file_size)
        speed_tracker.add_progress(downloaded_bytes)

        # 4. Resolve MTProto Sender (Exported sender for foreign DC if necessary)
        dc_id = info.dc_id
        is_borrowed = False
        sender = client._sender
        if dc_id and dc_id != client.session.dc_id:
            logger.debug(
                "Borrowing exported MTProto sender for dc_id=%s on message=%s",
                dc_id,
                message.id,
            )
            sender = await client._borrow_exported_sender(dc_id)
            is_borrowed = True

        input_location = info.location
        file_ref_lock = asyncio.Lock()
        active_location = input_location
        total_retries = 0
        last_meta_save = time.monotonic()
        last_progress_update = time.monotonic()
        file_name = getattr(getattr(message, "file", None), "name", target_path.name) or target_path.name

        # 5. Worker Routine
        async def worker_loop() -> None:
            nonlocal downloaded_bytes, active_location, total_retries, last_meta_save, last_progress_update

            while not chunk_queue.empty():
                try:
                    chunk_idx = chunk_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                offset = chunk_idx * chunk_size
                # Telegram MTProto GetFileRequest limit must be divisible by 1024/4096.
                # Passing the full chunk_size allows Telegram to return remaining file bytes cleanly.
                limit = chunk_size
                chunk_retries = 0

                while True:
                    try:
                        req = functions.upload.GetFileRequest(
                            location=active_location,
                            offset=offset,
                            limit=limit,
                        )
                        result = await client._call(sender, req)
                        data = getattr(result, "bytes", None)
                        if not data:
                            raise RuntimeError("Telegram returned empty chunk payload.")

                        # Ensure we do not write beyond file_size if Telegram returned padding
                        if file_size and (offset + len(data) > file_size):
                            data = data[: max(0, file_size - offset)]

                        # Write to exact offset in file without blocking event loop
                        await asyncio.to_thread(os.pwrite, fd, data, offset)

                        # Update accounting
                        downloaded_bytes += len(data)
                        meta.mark_chunk_completed(chunk_idx)
                        speed_tracker.add_progress(downloaded_bytes)

                        # Save metadata periodically (every ~3 seconds)
                        now = time.monotonic()
                        if now - last_meta_save >= 3.0:
                            last_meta_save = now
                            meta.save()

                        # Progress callback update
                        if (
                            progress_callback
                            and now - last_progress_update
                            >= settings.DOWNLOAD_PROGRESS_INTERVAL
                        ):
                            last_progress_update = now
                            pct = (
                                (downloaded_bytes / file_size * 100.0)
                                if file_size
                                else 0.0
                            )
                            prog_info = DownloadProgressInfo(
                                file_name=file_name,
                                file_size=file_size,
                                downloaded_bytes=downloaded_bytes,
                                percent=pct,
                                current_speed=speed_tracker.current_speed,
                                average_speed=speed_tracker.average_speed,
                                peak_speed=speed_tracker.peak_speed,
                                eta_seconds=speed_tracker.eta_seconds,
                                workers=worker_count,
                                is_resumed=is_resumed,
                            )
                            try:
                                asyncio.create_task(progress_callback(prog_info))
                            except Exception as pe:
                                logger.debug("Progress callback failed: %s", pe)

                        chunk_queue.task_done()
                        break

                    except FloodWaitError as fe:
                        total_retries += 1
                        logger.warning(
                            "FloodWait downloading chunk %s (%ss sleep)",
                            chunk_idx,
                            fe.seconds,
                        )
                        await asyncio.sleep(fe.seconds + 1)

                    except (
                        FilerefUpgradeNeededError,
                        FileReferenceExpiredError,
                    ):
                        total_retries += 1
                        async with file_ref_lock:
                            logger.info(
                                "File reference expired mid-download for msg %s. Re-fetching message...",
                                message.id,
                            )
                            refreshed = await client.get_messages(
                                message.chat_id, ids=message.id
                            )
                            if (
                                refreshed
                                and refreshed.media
                                and hasattr(refreshed.media, "document")
                                and refreshed.media.document
                            ):
                                active_location.file_reference = (
                                    refreshed.media.document.file_reference
                                )
                        await asyncio.sleep(0.5)

                    except (
                        TimedOutError,
                        ServerError,
                        ConnectionError,
                        RpcCallFailError,
                        Exception,
                    ) as err:
                        chunk_retries += 1
                        total_retries += 1
                        if chunk_retries > settings.MAX_DOWNLOAD_RETRIES:
                            logger.error(
                                "Chunk %s exceeded max retries (%s): %s",
                                chunk_idx,
                                settings.MAX_DOWNLOAD_RETRIES,
                                err,
                            )
                            chunk_queue.task_done()
                            raise NonRetryableTransferError(
                                f"Failed downloading chunk {chunk_idx}: {err}"
                            ) from err

                        backoff = min(8.0, 1.5 ** chunk_retries)
                        logger.debug(
                            "Chunk %s transient error: %s. Retrying in %.1fs (%d/%d)",
                            chunk_idx,
                            err,
                            backoff,
                            chunk_retries,
                            settings.MAX_DOWNLOAD_RETRIES,
                        )
                        await asyncio.sleep(backoff)

        # 6. Run Worker Pool Concurrently
        t_start = time.perf_counter()
        actual_workers = min(worker_count, max(1, len(missing_chunks)))
        try:
            worker_tasks = [
                asyncio.create_task(worker_loop()) for _ in range(actual_workers)
            ]
            await asyncio.gather(*worker_tasks)
        finally:
            try:
                os.close(fd)
            except Exception:
                pass

            if is_borrowed:
                try:
                    await client._return_exported_sender(sender)
                except Exception as ex:
                    logger.debug("Failed to return exported sender: %s", ex)

        t_end = time.perf_counter()
        total_time = max(0.001, t_end - t_start)
        avg_speed_mb = (file_size / total_time) / (1024 * 1024)
        peak_speed_mb = speed_tracker.peak_speed / (1024 * 1024)

        # 7. Atomic Finalization
        if part_path.exists():
            os.replace(part_path, target_path)
        meta.delete()

        # Send final 100% progress callback
        if progress_callback:
            final_info = DownloadProgressInfo(
                file_name=file_name,
                file_size=file_size,
                downloaded_bytes=file_size,
                percent=100.0,
                current_speed=avg_speed_mb * 1024 * 1024,
                average_speed=avg_speed_mb * 1024 * 1024,
                peak_speed=speed_tracker.peak_speed,
                eta_seconds=0.0,
                workers=worker_count,
                is_resumed=is_resumed,
            )
            try:
                await progress_callback(final_info)
            except Exception:
                pass

        # 8. Performance Logging (Section 21 format)
        size_fmt = (
            f"{file_size / (1024 * 1024 * 1024):.2f}GB"
            if file_size >= 1024 * 1024 * 1024
            else f"{file_size / (1024 * 1024):.1f}MB"
        )
        logger.info(
            "DOWNLOAD COMPLETE job=%s message=%s size=%s workers=%s request_size=%sKB "
            "time=%.2fs average_speed=%.2fMB/s peak_speed=%.2fMB/s retries=%s resume=%s",
            job_id if job_id is not None else "-",
            message.id,
            size_fmt,
            worker_count,
            chunk_size // 1024,
            total_time,
            avg_speed_mb,
            peak_speed_mb,
            total_retries,
            str(is_resumed).lower(),
        )

        return str(target_path)

    async def benchmark_download(
        self,
        client: TelegramClient,
        message: Message,
        sample_mb: float = 20.0,
        worker_options: Optional[List[int]] = None,
    ) -> BenchmarkResult:
        """Run an internal download benchmark comparing 1, 2, 4, and 8 workers."""
        if worker_options is None:
            worker_options = [1, 2, 4, 8]

        chunk_size = settings.DOWNLOAD_REQUEST_SIZE
        target_bytes = int(sample_mb * 1024 * 1024)

        worker_speeds: Dict[int, float] = {}
        worker_times: Dict[int, float] = {}

        t_process_start = time.process_time()

        for w in worker_options:
            logger.info("Running download benchmark with workers=%d...", w)
            temp_out = Path(
                f"/tmp/bench_{message.id}_{w}_{int(time.time())}.tmp"
            )
            try:
                t0 = time.perf_counter()
                await self.download_media(
                    client=client,
                    message=message,
                    target_path=temp_out,
                    workers=w,
                    request_size=chunk_size,
                )
                t1 = time.perf_counter()
                elapsed = max(0.001, t1 - t0)
                sz = os.path.getsize(temp_out) if temp_out.exists() else target_bytes
                spd = (sz / elapsed) / (1024 * 1024)
                worker_speeds[w] = spd
                worker_times[w] = elapsed
                logger.info("Benchmark workers=%d speed=%.2f MB/s", w, spd)
            finally:
                if temp_out.exists():
                    try:
                        temp_out.unlink()
                    except Exception:
                        pass
                part = temp_out.with_name(temp_out.name + ".part")
                if part.exists():
                    try:
                        part.unlink()
                    except Exception:
                        pass

        cpu_time = time.process_time() - t_process_start
        # Select best worker count that provided >= 10% gain over previous
        best_w = 1
        best_speed = worker_speeds.get(1, 0.0)
        for w in [2, 4, 8]:
            if w in worker_speeds:
                cur_speed = worker_speeds[w]
                if cur_speed > best_speed * 1.08:
                    best_w = w
                    best_speed = cur_speed

        return BenchmarkResult(
            file_size=target_bytes,
            chunk_size=chunk_size,
            worker_speeds=worker_speeds,
            worker_times=worker_times,
            cpu_percent=round(cpu_time * 100, 1),
            recommended_workers=best_w,
        )


fast_media_downloader = FastMediaDownloader()
