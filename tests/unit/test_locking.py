from __future__ import annotations

import portalocker
import pytest

from info_desk.locking import (
    LockConflictError,
    LockOperationError,
    acquire_file_lock,
)


def test_lock_uses_exclusive_nonblocking_portalocker_and_keeps_lock_file(
    tmp_path, monkeypatch
) -> None:
    calls = []

    class FakeLock:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def acquire(self):
            calls.append("acquire")

        def release(self):
            calls.append("release")

    monkeypatch.setattr("info_desk.locking.portalocker.Lock", FakeLock)
    path = tmp_path / "目录 with spaces" / "arxiv.lock"

    with acquire_file_lock(path):
        assert path.exists()

    args, kwargs = calls[0]
    assert args == (str(path),)
    assert kwargs["mode"] == "a+"
    assert kwargs["timeout"] == 0
    assert kwargs["flags"] == (
        portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING
    )
    assert kwargs["raise_on_release_error"] is True
    assert calls[1:] == ["acquire", "release"]
    assert path.exists()


@pytest.mark.parametrize(
    ("backend_error", "expected_error"),
    [
        (portalocker.exceptions.AlreadyLocked("busy"), LockConflictError),
        (portalocker.exceptions.LockException("backend"), LockOperationError),
        (
            portalocker.exceptions.LockException(PermissionError("acl denied")),
            PermissionError,
        ),
        (portalocker.exceptions.LockException(OSError("filesystem")), OSError),
        (PermissionError("denied"), PermissionError),
        (OSError("disk"), OSError),
    ],
)
def test_lock_classifies_backend_errors(tmp_path, monkeypatch, backend_error, expected_error):
    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            raise backend_error

    monkeypatch.setattr("info_desk.locking.portalocker.Lock", FakeLock)
    with pytest.raises(expected_error), acquire_file_lock(tmp_path / "arxiv.lock"):
        pytest.fail("lock backend should have failed before entering the body")


def test_lock_is_released_when_body_raises(tmp_path, monkeypatch) -> None:
    calls = []

    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            calls.append("acquire")

        def release(self):
            calls.append("release")

    monkeypatch.setattr("info_desk.locking.portalocker.Lock", FakeLock)
    with pytest.raises(RuntimeError, match="sync failed"), acquire_file_lock(
        tmp_path / "arxiv.lock"
    ):
        raise RuntimeError("sync failed")
    assert calls == ["acquire", "release"]


@pytest.mark.parametrize(
    ("release_error", "expected_error"),
    [
        (portalocker.exceptions.LockException("backend"), LockOperationError),
        (portalocker.exceptions.LockException(PermissionError("acl denied")), PermissionError),
        (OSError("disk"), OSError),
        (PermissionError("acl denied"), PermissionError),
    ],
)
def test_lock_classifies_release_errors(tmp_path, monkeypatch, release_error, expected_error):
    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            return None

        def release(self):
            raise release_error

    monkeypatch.setattr("info_desk.locking.portalocker.Lock", FakeLock)
    with pytest.raises(expected_error), acquire_file_lock(tmp_path / "arxiv.lock"):
        pass
