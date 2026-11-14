from __future__ import annotations

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

    directory = platform_utils.ensure_private_directory(tmp_path / "private")
    file_path = directory / "database.db"
    file_path.touch()
    platform_utils.ensure_private_file(file_path)

    assert directory.is_dir()
    assert chmod_calls == []


def test_posix_permission_helpers_keep_private_modes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(platform_utils, "_is_windows", lambda: False)
    directory = platform_utils.ensure_private_directory(tmp_path / "private")
    file_path = directory / "database.db"
    file_path.touch()
    platform_utils.ensure_private_file(file_path)

    assert platform_utils.inspect_path_permissions(directory)["mode"] == "0o700"
    assert platform_utils.inspect_path_permissions(file_path)["mode"] == "0o600"
