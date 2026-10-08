"""Lightweight asynchronous HTTP health check server for Hugging Face Spaces and cloud monitoring.

Hugging Face Spaces expects an HTTP service listening on port 7860 (or $PORT).
This module serves /health and / endpoints to keep the Space awake and report live status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_START_TIME = time.time()
_server: Optional[asyncio.Server] = None


from pathlib import Path
from sqlalchemy import select, desc
from app.database import get_session
from app.models.transfer_job import TransferJob, JobStatus
from app.models.telegram_account import TelegramAccount
from app.transfer.manager import transfer_manager
from app.transfer.worker import transfer_worker

HTML_FILE = Path(__file__).parent / "web" / "index.html"


async def _handle_http_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Handle incoming HTTP requests for web UI, health checks, and REST APIs."""
    try:
        data = await reader.read(4096)
        if not data:
            writer.close()
            await writer.wait_closed()
            return

        raw_text = data.decode("utf-8", errors="ignore")
        headers_part, *body_parts = raw_text.split("\r\n\r\n", 1)
        req_lines = headers_part.split("\r\n")
        request_line = req_lines[0] if req_lines else ""
        parts = request_line.split(" ")
        method = parts[0] if len(parts) > 0 else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        clean_path = path.split("?")[0]
        elapsed = int(time.time() - _START_TIME)

        content_type = "application/json; charset=utf-8"
        status_line = "HTTP/1.1 200 OK\r\n"

        # 1. Health check endpoints (for Hugging Face Spaces & Render)
        if clean_path in ("/health", "/ping"):
            payload = {
                "status": "ok",
                "service": "telegram-transfer-manager",
                "healthy": True,
                "uptime_seconds": elapsed,
            }
            body = json.dumps(payload, indent=2) + "\n"

        # 2. Modern Web Dashboard / Telegram Mini App UI
        elif clean_path in ("/", "/app", "/index.html"):
            if HTML_FILE.exists():
                body = HTML_FILE.read_text(encoding="utf-8")
            else:
                body = "<h1>Telegram Transfer Manager Web App</h1><p>Running.</p>"
            content_type = "text/html; charset=utf-8"

        # 3. Active Transfers API
        elif clean_path == "/api/status":
            active_jobs = await transfer_manager.get_active_jobs()
            jobs_data = []
            for j in active_jobs:
                tracker = transfer_worker.get_tracker(j.id)
                speed_str = f"{tracker.speed:.1f} msg/s" if tracker and tracker.speed > 0 else None
                jobs_data.append({
                    "id": j.id,
                    "source_title": j.source_chat_title or str(j.source_chat_id),
                    "destination_title": j.destination_chat_title or str(j.destination_chat_id),
                    "topic_name": j.topic_name,
                    "status": j.status,
                    "total": j.total_messages,
                    "processed": j.processed_messages,
                    "success": j.successful_messages,
                    "skipped": j.skipped_messages,
                    "failed": j.failed_messages,
                    "speed": speed_str,
                })
            body = json.dumps({"status": "ok", "jobs": jobs_data}) + "\n"

        # 4. History API
        elif clean_path == "/api/history":
            async with get_session() as session:
                stmt = select(TransferJob).order_by(desc(TransferJob.id)).limit(20)
                res = await session.execute(stmt)
                history_jobs = list(res.scalars().all())

            history_data = []
            for j in history_jobs:
                history_data.append({
                    "id": j.id,
                    "source_title": j.source_chat_title or str(j.source_chat_id),
                    "destination_title": j.destination_chat_title or str(j.destination_chat_id),
                    "status": j.status,
                    "total": j.total_messages,
                    "processed": j.processed_messages,
                    "success": j.successful_messages,
                    "skipped": j.skipped_messages,
                    "failed": j.failed_messages,
                })
            body = json.dumps({"status": "ok", "history": history_data}) + "\n"

        # 5. Connected Accounts API
        elif clean_path == "/api/accounts":
            async with get_session() as session:
                stmt = select(TelegramAccount).where(TelegramAccount.is_active == True)
                res = await session.execute(stmt)
                accounts = list(res.scalars().all())

            acc_data = []
            for acc in accounts:
                name = acc.first_name or acc.username or f"Account #{acc.id}"
                acc_data.append({"id": acc.id, "name": name, "is_active": acc.is_active})
            body = json.dumps({"status": "ok", "accounts": acc_data}) + "\n"

        # 6. Job Control Actions (POST)
        elif clean_path.startswith("/api/jobs/") and method == "POST":
            action = clean_path.replace("/api/jobs/", "").strip()
            req_body = body_parts[0] if body_parts else "{}"
            try:
                body_json = json.loads(req_body) if req_body else {}
            except Exception:
                body_json = {}
            job_id = int(body_json.get("job_id", 0))

            if action == "retry-failed":
                retry_job = await transfer_manager.retry_failed_messages(job_id)
                if retry_job:
                    await transfer_manager.start_job(retry_job.id)
                    body = json.dumps({"status": "ok", "retry_job_id": retry_job.id}) + "\n"
                else:
                    body = json.dumps({"status": "error", "message": "No failed messages"}) + "\n"
            elif action == "pause":
                await transfer_manager.pause_job(job_id)
                body = json.dumps({"status": "ok"}) + "\n"
            elif action == "resume":
                await transfer_manager.resume_job(job_id)
                body = json.dumps({"status": "ok"}) + "\n"
            elif action == "cancel":
                await transfer_manager.cancel_job(job_id)
                body = json.dumps({"status": "ok"}) + "\n"
            else:
                body = json.dumps({"status": "error", "message": "Unknown action"}) + "\n"

        else:
            payload = {"error": "Not Found", "healthy": True}
            body = json.dumps(payload) + "\n"
            status_line = "HTTP/1.1 404 Not Found\r\n"

        encoded_body = body.encode("utf-8")
        headers = (
            f"{status_line}"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(encoded_body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        writer.write(headers.encode("utf-8"))
        if method != "HEAD":
            writer.write(encoded_body)
        await writer.drain()
    except Exception as e:
        logger.debug("HTTP server error: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_health_server(host: str = "0.0.0.0", port: Optional[int] = None) -> Optional[asyncio.Server]:
    """Start the background HTTP server for Hugging Face Spaces."""
    global _server
    if port is None:
        port = int(os.getenv("PORT", os.getenv("HEALTH_PORT", "7860")))

    try:
        _server = await asyncio.start_server(_handle_http_request, host, port)
        logger.info("HTTP Health Check server started on http://%s:%d/health (Hugging Face ready)", host, port)
        return _server
    except Exception as e:
        logger.warning("Could not start HTTP health check server on port %s: %s", port, e)
        return None


async def stop_health_server() -> None:
    """Stop the background HTTP health server."""
    global _server
    if _server:
        logger.info("Stopping HTTP health check server...")
        _server.close()
        await _server.wait_closed()
        _server = None
