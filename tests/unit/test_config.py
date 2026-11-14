from __future__ import annotations

import json
from pathlib import Path

import pytest

from info_desk.config import Settings, load_settings


def test_config_load_reads_toml_and_environment_secrets(tmp_path, monkeypatch) -> None:
    config = tmp_path / "desk.toml"
    config.write_text(
        "[data]\nhome = '/config/home'\ndatabase = 'store.db'\n"
        "[http]\nconnect_timeout_sec = 2\nmax_response_bytes = 9000\n"
        "[sync]\nfeeds = ['arxiv', 'github']\ntopics = ['VLA']\nmax_pages_per_window = 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "secret-gh")
    monkeypatch.setenv("HF_TOKEN", "secret-hf")
    monkeypatch.delenv("INFO_DESK_HOME", raising=False)
    settings = load_settings(config)
    assert settings.home == Path("/config/home")
    assert settings.database_path().name == "store.db"
    assert settings.enabled_feeds == ["arxiv", "github"]
    assert settings.max_pages_per_window == 4
    assert settings.github_token == "secret-gh"
    assert settings.hf_token == "secret-hf"


def test_environment_home_overrides_toml_and_relative_database(tmp_path, monkeypatch) -> None:
    config = tmp_path / "desk.toml"
    config.write_text("[data]\nhome = '/ignored'\ndatabase = 'relative.db'\n", encoding="utf-8")
    monkeypatch.setenv("INFO_DESK_HOME", str(tmp_path / "env-home"))
    settings = load_settings(config)
    assert settings.database_path() == tmp_path / "env-home" / "relative.db"


def test_config_paths_support_unicode_and_spaces(tmp_path, monkeypatch) -> None:
    home = tmp_path / "配置目录 with spaces"
    config = tmp_path / "配置 文件.toml"
    config.write_text(
        f'[data]\nhome = {json.dumps(str(home), ensure_ascii=False)}\ndatabase = "资料 #1.db"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("INFO_DESK_HOME", raising=False)

    settings = load_settings(config)

    assert settings.home == home
    assert settings.database_path() == home / "资料 #1.db"


def test_missing_config_uses_default_and_invalid_settings_raise_value_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INFO_DESK_HOME", str(tmp_path / "home"))
    defaults = load_settings(tmp_path / "missing.toml")
    assert defaults.enabled_feeds == ["arxiv", "hf_papers", "hf_models", "github"]
    invalid = tmp_path / "invalid.toml"
    invalid.write_text('[http]\nconnect_timeout_sec = "fast"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid configuration"):
        load_settings(invalid)
    assert Settings(home=tmp_path).database_path() == tmp_path / "intel.db"


def test_aihot_is_enabled_by_default_and_can_only_use_the_official_endpoint(tmp_path) -> None:
    default = load_settings(tmp_path / "missing.toml")
    assert default.aihot_enabled is True
    assert default.aihot_endpoint == "https://aihot.news/api/mcp"
    assert default.aihot_timeout_sec == 25

    config = tmp_path / "aihot.toml"
    config.write_text(
        '[aihot]\nenabled = true\nendpoint = "https://aihot.news/api/mcp"\ntimeout_sec = 30\n',
        encoding="utf-8",
    )
    enabled = load_settings(config)
    assert enabled.aihot_enabled is True
    assert enabled.aihot_timeout_sec == 30

    disabled_config = tmp_path / "disabled.toml"
    disabled_config.write_text('[aihot]\nenabled = false\n', encoding="utf-8")
    disabled = load_settings(disabled_config)
    assert disabled.aihot_enabled is False

    unsafe = tmp_path / "unsafe.toml"
    unsafe.write_text('[aihot]\nendpoint = "http://127.0.0.1:8080/mcp"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid configuration"):
        load_settings(unsafe)
