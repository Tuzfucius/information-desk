from __future__ import annotations

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


def test_missing_config_uses_default_and_invalid_settings_raise_value_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INFO_DESK_HOME", str(tmp_path / "home"))
    defaults = load_settings(tmp_path / "missing.toml")
    assert defaults.enabled_feeds == ["arxiv", "hf_papers", "hf_models", "github"]
    invalid = tmp_path / "invalid.toml"
    invalid.write_text('[http]\nconnect_timeout_sec = "fast"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid configuration"):
        load_settings(invalid)
    assert Settings(home=tmp_path).database_path() == tmp_path / "intel.db"
