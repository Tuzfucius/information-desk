"""Cross-platform, non-blocking process locks used by synchronization."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import portalocker
from portalocker.exceptions import AlreadyLocked, LockException

from info_desk.platform_utils import ensure_private_directory, ensure_private_file


class LockConflictError(Exception):
    """The requested lock is currently owned by another process."""


class LockOperationError(Exception):
    """The lock backend failed for a reason other than lock contention."""


def _raise_lock_operation_error(error: LockException, operation: str, name: str) -> NoReturn:
    underlying = error.__cause__
    if underlying is None and error.args and isinstance(error.args[0], OSError):
        underlying = error.args[0]
    if isinstance(underlying, PermissionError):
        raise underlying from error
    if isinstance(underlying, OSError):
        raise underlying from error
    raise LockOperationError(
        f"Lock {operation} failed for {name}: {type(error).__name__}"
    ) from error


@contextmanager
def acquire_file_lock(path: str | Path) -> Iterator[None]:
    """Acquire an exclusive non-blocking OS lock and release it on every exit."""
    lock_path = Path(path).expanduser()
    ensure_private_directory(lock_path.parent)

    # Create the persistent lock inode with restrictive POSIX mode. The file's
    # existence never indicates ownership; portalocker supplies the OS lock.
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    ensure_private_file(lock_path)

    lock = portalocker.Lock(
        str(lock_path),
        mode="a+",
        timeout=0,
        flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
        raise_on_release_error=True,
    )
    try:
        lock.acquire()
    except AlreadyLocked as error:
        raise LockConflictError(f"Lock is already held: {lock_path.name}") from error
    except PermissionError:
        raise
    except OSError:
        raise
    except LockException as error:
        _raise_lock_operation_error(error, "acquisition", lock_path.name)

    try:
        yield
    finally:
        try:
            lock.release()
        except PermissionError:
            raise
        except OSError:
            raise
        except LockException as error:
            _raise_lock_operation_error(error, "release", lock_path.name)
