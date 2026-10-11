"""Runtime settings, all from environment variables."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .bcferries.site import Credentials


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    config_path: Path = Path("config.json")
    static_dir: Path | None = None
    checkout_url: str | None = None  # where the cart browser can be used (noVNC), linked from alerts
    headless: bool = False
    window_size: tuple[int, int] = (1280, 1000)
    debug_dir: Path | None = None  # failure screenshots; never inside the backed-up config volume
    log_level: str = "INFO"
    credentials: Credentials | None = None
    cart_hold_minutes: int = 60  # a cart nobody checks out is handed back after this; 0 holds it until released
    pushover_token: str | None = field(default=None, repr=False)
    pushover_user_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def opt(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        user, password = opt("BCF_USER"), opt("BCF_PASS")
        if bool(user) != bool(password):
            raise SettingsError("Set both BCF_USER and BCF_PASS, or neither")
        token, user_key = opt("PUSHOVER_TOKEN"), opt("PUSHOVER_USER_KEY")
        if bool(token) != bool(user_key):
            raise SettingsError("Set both PUSHOVER_TOKEN and PUSHOVER_USER_KEY, or neither")

        try:
            port = int(env.get("BCF_PORT", "8765"))
            width, height = (int(v) for v in env.get("BCF_WINDOW_SIZE", "1280,1000").split(","))
            cart_hold_minutes = int(env.get("BCF_CART_HOLD_MINUTES", "60"))
        except ValueError as e:
            raise SettingsError(f"Invalid BCF_PORT, BCF_WINDOW_SIZE or BCF_CART_HOLD_MINUTES: {e}") from e
        if cart_hold_minutes < 0:
            raise SettingsError("BCF_CART_HOLD_MINUTES can't be negative (0 holds carts until released)")

        debug_dir = env.get("BCF_DEBUG_DIR", str(Path(tempfile.gettempdir()) / "ferry-watch"))
        static_dir = opt("BCF_STATIC")
        return cls(
            host=env.get("BCF_HOST", "127.0.0.1"),
            port=port,
            config_path=Path(env.get("BCF_CONFIG", "config.json")),
            static_dir=Path(static_dir) if static_dir else None,
            checkout_url=opt("BCF_VNC_URL"),
            headless=env.get("BCF_HEADLESS", "") == "1",
            window_size=(width, height),
            debug_dir=Path(debug_dir) if debug_dir else None,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            credentials=Credentials(user, password) if user and password else None,
            cart_hold_minutes=cart_hold_minutes,
            pushover_token=token,
            pushover_user_key=user_key,
        )
