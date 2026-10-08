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


async def _handle_http_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Handle incoming HTTP requests for health check endpoints."""
    try:
        data = await reader.read(2048)
        if not data:
            writer.close()
            await writer.wait_closed()
            return

        request_line = data.decode("utf-8", errors="ignore").split("\r\n")[0]
        parts = request_line.split(" ")
        method = parts[0] if len(parts) > 0 else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        clean_path = path.split("?")[0]
        elapsed = int(time.time() - _START_TIME)

        if clean_path in ("/", "/health", "/ping"):
            payload = {
                "status": "ok",
                "service": "telegram-transfer-manager",
                "healthy": True,
                "uptime_seconds": elapsed,
            }
            body = json.dumps(payload, indent=2) + "\n"
            status_line = "HTTP/1.1 200 OK\r\n"
        else:
            payload = {"error": "Not Found", "healthy": True}
            body = json.dumps(payload) + "\n"
            status_line = "HTTP/1.1 404 Not Found\r\n"

        encoded_body = body.encode("utf-8")
        headers = (
            f"{status_line}"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(encoded_body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        writer.write(headers.encode("utf-8"))
        if method != "HEAD":
            writer.write(encoded_body)
        await writer.drain()
    except Exception as e:
        logger.debug("Health check HTTP error: %s", e)
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
