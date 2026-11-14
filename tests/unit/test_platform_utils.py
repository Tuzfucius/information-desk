from __future__ import annotations

import os

import pytest

from info_desk import platform_utils


def test_windows_permission_inspection_is_explicitly_not_checked(tmp_path, monkeypatch) -> None:
    target = tmp_path / "database.db"
    target.touch()
    monkeypatch.setattr(platform_utils, "_is_windows", lambda: True)

    result = platform_utils.inspect_path_permissions(target)

    assert result["status"] == "not_checked"
    assert result["ok"] is None
    assert result["mode"] is None
    assert "ACL" in result["message"]


def test_windows_private_helpers_do_not_apply_posix_chmod(tmp_path, monkeypatch) -> None:
    chmod_calls = []
    monkeypatch.setattr(platform_utils, "_is_windows", lambda: True)
    monkeypatch.setattr(platform_utils.os, "chmod", lambda *args: chmod_calls.append(args))

    directory = platform_utils.ensure_app_private_directory(tmp_path / "private")
    file_path = directory / "database.db"
    file_path.touch()
    platform_utils.ensure_private_file(file_path)

    assert directory.is_dir()
    assert chmod_calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented by NTFS ACLs")
def test_posix_permission_helpers_keep_private_modes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(platform_utils, "_is_windows", lambda: False)
    directory = platform_utils.ensure_app_private_directory(tmp_path / "private")
    file_path = directory / "database.db"
    file_path.touch()
    platform_utils.ensure_private_file(file_path)

    assert platform_utils.inspect_path_permissions(directory)["mode"] == "0o700"
    assert platform_utils.inspect_path_permissions(file_path)["mode"] == "0o600"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented by NTFS ACLs")
def test_app_private_directory_does_not_change_existing_ancestors(tmp_path) -> None:
    parent = tmp_path / "ordinary-parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    home = platform_utils.ensure_app_private_directory(parent / "app-home")
    run = platform_utils.ensure_app_private_directory(home / "run")
    assert platform_utils.inspect_path_permissions(parent)["mode"] == "0o755"
    assert platform_utils.inspect_path_permissions(home)["mode"] == "0o700"
    assert platform_utils.inspect_path_permissions(run)["mode"] == "0o700"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented by NTFS ACLs")
def test_existing_insecure_app_directory_is_reported_without_chmod(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "insecure"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(PermissionError, match="set its permissions to 0700 manually"):
        platform_utils.ensure_app_private_directory(directory)
    assert directory.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented by NTFS ACLs")
def test_existing_symlink_app_directory_is_rejected(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PermissionError, match="real directory"):
        platform_utils.ensure_app_private_directory(link)
