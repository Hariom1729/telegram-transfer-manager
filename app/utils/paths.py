"""Cross-platform path resolution and safe filesystem utilities.

Provides operating-system-aware directories for user downloads and temporary files,
along with filename sanitization and collision prevention.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

# Invalid filename characters on common filesystems (Windows, POSIX)
_INVALID_CHARS_REGEX = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def get_download_directory() -> Path:
    """Resolve the operating system's standard user Downloads folder.

    Cross-platform behavior:
    - Custom env: Respects DOWNLOAD_DIRECTORY or DOWNLOAD_DIR if configured
    - macOS: ~/Downloads (e.g. /Users/<username>/Downloads)
    - Windows: Resolves actual shell Downloads folder via registry / USERPROFILE / home
    - Linux: Respects XDG_DOWNLOAD_DIR config or falls back to ~/Downloads
    """
    custom_dir = os.environ.get("DOWNLOAD_DIRECTORY") or os.environ.get("DOWNLOAD_DIR")
    if custom_dir and custom_dir.strip():
        p = Path(custom_dir.strip())
        p.mkdir(parents=True, exist_ok=True)
        return p

    home = Path.home()

    if sys.platform == "win32":
        # 1. Try Windows Registry for Explorer Shell Folders (GUID: {374DE290-123F-4565-9164-39C4925E467B})
        try:
            import winreg

            sub_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                val, _ = winreg.QueryValueEx(
                    key, "{374DE290-123F-4565-9164-39C4925E467B}"
                )
                if val and os.path.isdir(val):
                    return Path(val)
        except Exception:
            pass

        # 2. Try USERPROFILE environment variable
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            p = Path(user_profile) / "Downloads"
            if p.exists() or p.parent.exists():
                return p

        # 3. Fallback
        return home / "Downloads"

    if sys.platform.startswith("linux"):
        # 1. Try XDG_DOWNLOAD_DIR from environment
        xdg_env = os.environ.get("XDG_DOWNLOAD_DIR")
        if xdg_env and os.path.isabs(xdg_env):
            return Path(xdg_env)

        # 2. Try ~/.config/user-dirs.dirs
        user_dirs = home / ".config" / "user-dirs.dirs"
        if user_dirs.is_file():
            try:
                with open(user_dirs, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("XDG_DOWNLOAD_DIR="):
                            val = line.split("=", 1)[1].strip().strip('"').strip("'")
                            val = val.replace("$HOME", str(home))
                            if os.path.isabs(val):
                                return Path(val)
            except Exception:
                pass

        return home / "Downloads"

    # macOS (darwin) and other Unix-like systems
    return home / "Downloads"


def get_temp_download_directory() -> Path:
    """Return an isolated directory in the OS temporary storage for streaming/transfers."""
    temp_dir = Path(tempfile.gettempdir()) / "telegram_transfer_manager"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def sanitize_folder_name(name: str) -> str:
    """Sanitize a chat or channel title for safe directory creation."""
    if not name:
        return "unnamed_chat"

    # Remove invalid characters
    cleaned = _INVALID_CHARS_REGEX.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned.rstrip(". ")

    if not cleaned:
        return "unnamed_chat"

    # Truncate length
    return cleaned[:100]


def sanitize_filename(filename: str, max_length: int = 200) -> str:
    """Sanitize invalid filesystem characters from Telegram file names.

    Preserves extension and replaces invalid characters with underscores.
    """
    if not filename:
        return "unnamed_file"

    # Normalize backslashes and slashes
    base = os.path.basename(filename.replace("\\", "/"))
    cleaned = _INVALID_CHARS_REGEX.sub("_", base)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned.rstrip(". ")

    if not cleaned:
        return "unnamed_file"

    p = Path(cleaned)
    stem = p.stem.strip(". ")
    suffix = p.suffix.strip()

    if not stem:
        stem = "file"

    # Truncate if filename is too long while preserving extension
    max_stem_len = max(10, max_length - len(suffix))
    if len(stem) > max_stem_len:
        stem = stem[:max_stem_len]

    return f"{stem}{suffix}"


def get_unique_filepath(directory: Path, filename: str) -> Path:
    """Ensure a filename in directory does not collide with an existing file.

    If video.mp4 exists, returns video (1).mp4, video (2).mp4, etc.
    """
    directory.mkdir(parents=True, exist_ok=True)
    clean_name = sanitize_filename(filename)
    target = directory / clean_name

    if not target.exists():
        return target

    p = Path(clean_name)
    stem = p.stem
    suffix = p.suffix

    counter = 1
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def ensure_download_directory(subfolder: Optional[str] = None) -> Path:
    """Ensure the user's Downloads directory (and optional subfolder) exists and is writable."""
    base = get_download_directory()
    if subfolder:
        safe_sub = sanitize_folder_name(subfolder)
        target = base / safe_sub
    else:
        target = base

    target.mkdir(parents=True, exist_ok=True)
    return target
