from __future__ import annotations

from pathlib import Path

import pytest

from ferry_watch.settings import Settings, SettingsError


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert (settings.host, settings.port) == ("127.0.0.1", 8765)
    assert settings.config_path == Path("config.json")
    assert settings.credentials is None
    assert settings.pushover_token is None
    assert settings.headless is False
    assert settings.window_size == (1280, 1000)


def test_from_env() -> None:
    settings = Settings.from_env(
        {
            "BCF_HOST": "0.0.0.0",  # noqa: S104 (test value)
            "BCF_PORT": "9000",
            "BCF_CONFIG": "/config/config.json",
            "BCF_STATIC": "/app/static",
            "BCF_VNC_URL": "https://ferry.example/vnc",
            "BCF_HEADLESS": "1",
            "BCF_WINDOW_SIZE": "800,600",
            "BCF_DEBUG_DIR": "",
            "LOG_LEVEL": "debug",
            "BCF_USER": "me@example.com",
            "BCF_PASS": "pw",
            "PUSHOVER_TOKEN": "tok",
            "PUSHOVER_USER_KEY": "user",
        }
    )
    assert settings.port == 9000
    assert settings.static_dir == Path("/app/static")
    assert settings.checkout_url == "https://ferry.example/vnc"
    assert settings.headless is True
    assert settings.window_size == (800, 600)
    assert settings.debug_dir is None
    assert settings.log_level == "DEBUG"
    assert settings.credentials is not None
    assert settings.credentials.username == "me@example.com"
    assert "pw" not in repr(settings)
    assert "tok" not in repr(settings)


@pytest.mark.parametrize(
    "env",
    [
        {"BCF_USER": "me@example.com"},
        {"PUSHOVER_TOKEN": "tok"},
        {"BCF_PORT": "http"},
        {"BCF_WINDOW_SIZE": "big"},
    ],
)
def test_invalid(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        Settings.from_env(env)


def test_cart_hold_time() -> None:
    assert Settings.from_env({}).cart_hold_minutes == 60
    assert Settings.from_env({"BCF_CART_HOLD_MINUTES": "0"}).cart_hold_minutes == 0  # hold until released
    with pytest.raises(SettingsError):
        Settings.from_env({"BCF_CART_HOLD_MINUTES": "-5"})
    with pytest.raises(SettingsError):
        Settings.from_env({"BCF_CART_HOLD_MINUTES": "soon"})
