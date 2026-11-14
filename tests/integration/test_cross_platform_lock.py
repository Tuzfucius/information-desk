from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from info_desk.contracts import FetchRequest
from info_desk.exceptions import InfoDeskError
from info_desk.resolver import IdentityResolver
from info_desk.sources.base import PageResult
from info_desk.sync_engine import SyncEngine
from info_desk.watch_refresh import WatchRefresher

_LOCKED_PROCESS = r"""
import sys
from info_desk.locking import acquire_file_lock
with acquire_file_lock(sys.argv[1]):
    print("LOCKED", flush=True)
    if sys.argv[2] == "wait":
        sys.stdin.readline()
    elif sys.argv[2] == "raise":
        raise RuntimeError("intentional lock holder failure")
"""

_TRY_LOCK = r"""
import sys
from info_desk.locking import LockConflictError, acquire_file_lock
try:
    with acquire_file_lock(sys.argv[1]):
        print("ACQUIRED", flush=True)
except LockConflictError:
    print("BUSY", flush=True)
"""

_TRY_ORDERED_FEED_LOCKS = r"""
import sys
from contextlib import ExitStack
from pathlib import Path
from info_desk.locking import LockConflictError, acquire_file_lock
root = Path(sys.argv[1])
try:
    with ExitStack() as stack:
        for feed in sorted(("arxiv", "github")):
            stack.enter_context(acquire_file_lock(root / f"{feed}.lock"))
        print("ACQUIRED", flush=True)
except LockConflictError:
    print("BUSY", flush=True)
"""

_PROCESS_TIMEOUT = 8


def _start_process(code: str, *arguments: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _read_line(process: subprocess.Popen[str], timeout: float = _PROCESS_TIMEOUT) -> str:
    assert process.stdout is not None
    result: queue.Queue[str] = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: result.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty:
        _terminate(process)
        reader.join(timeout=_PROCESS_TIMEOUT)
        stdout, stderr = process.communicate(timeout=_PROCESS_TIMEOUT)
        pytest.fail(f"child did not signal lock result; stdout={stdout!r}, stderr={stderr!r}")
    reader.join(timeout=_PROCESS_TIMEOUT)
    if not line:
        _stdout, stderr = process.communicate(timeout=_PROCESS_TIMEOUT)
        pytest.fail(f"child exited before reporting a lock result; stderr={stderr!r}")
    return line.strip()


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=_PROCESS_TIMEOUT)


def _stop_holder(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        assert process.stdin is not None
        try:
            process.stdin.write("release\n")
            process.stdin.flush()
            process.wait(timeout=_PROCESS_TIMEOUT)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            _terminate(process)
    process.communicate(timeout=_PROCESS_TIMEOUT)


def _hold_lock(path: Path, mode: str = "wait") -> subprocess.Popen[str]:
    process = _start_process(_LOCKED_PROCESS, str(path), mode)
    try:
        assert _read_line(process) == "LOCKED"
    except BaseException:
        _terminate(process)
        process.communicate(timeout=_PROCESS_TIMEOUT)
        raise
    return process


def _try_lock(path: Path) -> str:
    process = _start_process(_TRY_LOCK, str(path))
    try:
        result = _read_line(process)
        process.wait(timeout=_PROCESS_TIMEOUT)
        _stdout, stderr = process.communicate(timeout=_PROCESS_TIMEOUT)
        assert process.returncode == 0, stderr
        return result
    finally:
        if process.poll() is None:
            _terminate(process)
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()


def _special_lock_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "中文 folder with spaces"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
    if os.name == "nt":
        assert Path(directory).drive, "native Windows test path should include a drive"
    return directory


def test_same_lock_conflicts_quickly_then_can_be_reacquired(tmp_path: Path) -> None:
    lock_path = _special_lock_dir(tmp_path) / "arxiv.lock"
    holder = _hold_lock(lock_path)
    try:
        assert _try_lock(lock_path) == "BUSY"
    finally:
        _stop_holder(holder)

    assert _try_lock(lock_path) == "ACQUIRED"
    assert lock_path.exists(), "released lock files remain as stable lock inodes"


def test_different_feed_locks_are_independent(tmp_path: Path) -> None:
    root = _special_lock_dir(tmp_path)
    holder = _hold_lock(root / "arxiv.lock")
    try:
        assert _try_lock(root / "github.lock") == "ACQUIRED"
    finally:
        _stop_holder(holder)


def test_exception_in_lock_holder_releases_for_another_process(tmp_path: Path) -> None:
    lock_path = _special_lock_dir(tmp_path) / "arxiv.lock"
    process = _hold_lock(lock_path, "raise")
    try:
        assert process.wait(timeout=_PROCESS_TIMEOUT) != 0
    finally:
        process.communicate(timeout=_PROCESS_TIMEOUT)
    assert _try_lock(lock_path) == "ACQUIRED"


def test_terminated_lock_holder_releases_after_process_exit(tmp_path: Path) -> None:
    lock_path = _special_lock_dir(tmp_path) / "arxiv.lock"
    process = _hold_lock(lock_path)
    _terminate(process)
    process.communicate(timeout=_PROCESS_TIMEOUT)
    assert process.poll() is not None
    assert _try_lock(lock_path) == "ACQUIRED"


def test_partial_ordered_lock_acquisition_releases_first_lock(tmp_path: Path) -> None:
    root = _special_lock_dir(tmp_path)
    holder = _hold_lock(root / "github.lock")
    process = _start_process(_TRY_ORDERED_FEED_LOCKS, str(root))
    try:
        assert _read_line(process) == "BUSY"
        process.wait(timeout=_PROCESS_TIMEOUT)
        _stop_holder(holder)
        assert _try_lock(root / "arxiv.lock") == "ACQUIRED"
    finally:
        if process.poll() is None:
            _terminate(process)
        process.communicate(timeout=_PROCESS_TIMEOUT)
        if holder.poll() is None:
            _stop_holder(holder)


class _EmptyAdapter:
    async def discover(self, query, window, page_token=None):
        return PageResult(records=[], page_complete=True)


@pytest.mark.asyncio
async def test_sync_engine_maps_real_child_process_contention(settings, repository) -> None:
    adapters = {"arxiv": _EmptyAdapter()}
    resolver = IdentityResolver(repository)
    watch = WatchRefresher(settings, repository, adapters, resolver)
    sync = SyncEngine(settings, repository, adapters, resolver, watch, lambda _response: None)
    holder = _hold_lock(settings.home / "run" / "arxiv.lock")
    try:
        with pytest.raises(InfoDeskError) as raised:
            await sync.run(FetchRequest(feeds=["arxiv"], topics=["VLA"]))
        assert raised.value.code == "SYNC_ALREADY_RUNNING"
    finally:
        _stop_holder(holder)
