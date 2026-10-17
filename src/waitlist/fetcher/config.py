"""waitlist-fetcher's configuration: one JSON file rendered by the deployment (`waitlist-fetcher-config`).

It holds no secret and no credential (I7): the listen port, the upstream timeout, the body cap, and each
provider's own hard cap in requests a minute. A provider missing from `providers` is refused at request time.
Rendered as `{"listenPort": 8767, "upstreamTimeoutSeconds": 10, "maxBodyBytes": 262144,
"providers": {"buntzen": {"maxPerMinute": 30}}}` (design.md §4.4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import msgspec

from waitlist.providers import REGISTRY

from .registry import FETCHERS


class ConfigError(ValueError):
    pass


class _Config(msgspec.Struct, forbid_unknown_fields=True, kw_only=True, rename="camel"):
    pass


class ProviderLimit(_Config, kw_only=True):
    max_per_minute: Annotated[int, msgspec.Meta(ge=1, le=60)]


class FetcherConfig(_Config, kw_only=True):
    listen_port: int = 8767
    upstream_timeout_seconds: Annotated[int, msgspec.Meta(ge=1, le=30)] = 10
    max_body_bytes: Annotated[int, msgspec.Meta(ge=1024, le=1_048_576)] = 262_144
    providers: dict[str, ProviderLimit] = {}

    def check(self) -> FetcherConfig:
        """Refuse what would only fail later: a provider this build can't fetch, a cap above its ceiling."""
        if not 1 <= self.listen_port <= 65535:
            raise ConfigError(f"listenPort must be from 1 to 65535, not {self.listen_port}")
        for provider_id, limit in self.providers.items():
            if provider_id not in FETCHERS:
                raise ConfigError(f"providers.{provider_id}: not a provider waitlist-fetcher can check")
            ceiling = REGISTRY.http[provider_id].budget.max_per_minute
            if limit.max_per_minute > ceiling:
                raise ConfigError(
                    f"providers.{provider_id}.maxPerMinute is {limit.max_per_minute}; its ceiling is {ceiling}"
                )
        return self


def load(path: Path) -> FetcherConfig:
    try:
        return msgspec.json.decode(path.read_bytes(), type=FetcherConfig).check()
    except (OSError, msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
