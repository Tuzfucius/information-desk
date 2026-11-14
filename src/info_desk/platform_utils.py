"""Small platform-aware filesystem helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _is_windows() -> bool:
    return os.name == "nt"


def ensure_app_private_directory(path: str | Path) -> Path:
    """Create application-owned directories privately; never repair existing paths."""
    directory = Path(path).expanduser()
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for candidate in reversed(missing):
        created = False
        try:
            candidate.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        if created and not _is_windows():
            candidate.chmod(0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise PermissionError(f"Application data path must be a real directory: {directory}")
    if not _is_windows():
        mode = directory.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(
                f"Application data directory is not private: {directory}; "
                f"set its permissions to 0700 manually."
            )
    return directory


def ensure_parent_directory(path: str | Path) -> Path:
    """Create a normal parent directory without changing existing permissions."""
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    return directory


def ensure_private_file(path: str | Path) -> Path:
    """Restrict a file to its owner on POSIX; leave Windows ACLs untouched."""
    file_path = Path(path).expanduser()
    if not _is_windows():
        if file_path.is_symlink():
            raise PermissionError(f"Refusing to change permissions through a symlink: {file_path}")
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
