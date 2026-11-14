"""Small platform-aware filesystem helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _is_windows() -> bool:
    return os.name == "nt"


def ensure_private_directory(path: str | Path) -> Path:
    """Create a directory and restrict POSIX permissions where they apply."""
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _is_windows():
        directory.chmod(0o700)
    return directory


def ensure_private_file(path: str | Path) -> Path:
    """Restrict a file to its owner on POSIX; leave Windows ACLs untouched."""
    file_path = Path(path).expanduser()
    if not _is_windows():
        file_path.chmod(0o600)
    return file_path


def inspect_path_permissions(path: str | Path) -> dict[str, Any]:
    """Report POSIX private-mode checks without making ACL claims on Windows."""
    target = Path(path).expanduser()
    if _is_windows():
        return {
            "ok": None,
            "status": "not_checked",
            "mode": None,
            "message": "NTFS ACL permissions were not inspected.",
        }

    mode = target.stat().st_mode & 0o777
    private = mode & 0o077 == 0
    return {
        "ok": private,
        "status": "private" if private else "insecure",
        "mode": oct(mode),
        "message": None if private else "Group or other users have permission bits set.",
    }
