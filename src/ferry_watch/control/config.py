"""ferry-control's configuration: one JSON file rendered by the chart (`ferry-control-config`).

The same Helm values render the admission policy's parameters, so the pod shape ferry-control builds and
the shape the cluster admits can't drift (a CI check compares them). Database credentials come from the
usual libpq variables (PGHOST, PGUSER, PGPASSWORD, ...), never from this file.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import time, timedelta
from pathlib import Path
from typing import Annotated

import msgspec

from ferry_watch.polling import PollPolicy, Tier
from ferry_watch.protocol import MAX_HARD_S, MAX_SESSION_S, MAX_UNCLAIMED_S, MAX_VIEWER_GONE_S, HoldLimits

from .configure import HubSettings
from .db import Roles
from .limits import CEILINGS, DEFAULTS, LimitKey, Limits
from .pods import NAMESPACE, PodParams

_DURATION = re.compile(r"(\d+)(s|m|h|d)")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ConfigError(ValueError):
    pass


def duration(text: str) -> timedelta:
    """'30s', '5m', '6h', '7d'."""
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        raise ConfigError(f"Not a duration: {text!r}")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])


class _Config(msgspec.Struct, forbid_unknown_fields=True, kw_only=True, rename="camel"):
    pass


class PodConfig(_Config, kw_only=True):
    image: str
    hub_url: str
    vnc_pubkeys: str
    vnc_origin: str
    sandbox: bool = False
    cpu_request: str = "200m"
    pod_memory: str = "1536Mi"
    require_user_namespace: bool = False
    namespace: str = NAMESPACE


class FleetConfig(_Config, kw_only=True):
    max_browsers_total: Annotated[int, msgspec.Meta(ge=1, le=100)] = 7
    short_check_slots: Annotated[int, msgspec.Meta(ge=0)] = 1


class HoldConfig(_Config, kw_only=True):
    unclaimed: str = "30m"
    session: str = "30m"
    viewer_gone: str = "10m"
    hard: str = "60m"


class TierConfig(_Config, kw_only=True):
    within: str
    interval: str


class PollingConfig(_Config, kw_only=True):
    tiers: list[TierConfig] | None = None
    short_check_interval: str = "6h"
    horizon_days: Annotated[int, msgspec.Meta(ge=7, le=120)] = 30
    quiet_start: str = "23:00"
    quiet_end: str = "06:00"


class LimitsConfig(_Config, kw_only=True):
    defaults: dict[str, int] = {}
    ceilings: dict[str, int] = {}


class RolesConfig(_Config, kw_only=True):
    web: str = "ferrywatch_web"
    control: str = "ferrywatch_control"


class ControlConfig(_Config, kw_only=True):
    pod: PodConfig
    fleet: FleetConfig = msgspec.field(default_factory=FleetConfig)
    hold: HoldConfig = msgspec.field(default_factory=HoldConfig)
    polling: PollingConfig = msgspec.field(default_factory=PollingConfig)
    limits: LimitsConfig = msgspec.field(default_factory=LimitsConfig)
    roles: RolesConfig = msgspec.field(default_factory=RolesConfig)
    checks_per_minute: Annotated[int, msgspec.Meta(ge=1, le=40)] = 20
    auto_cart: bool = True
    alert_only_fares: list[str] = []
    alert_only_vehicles: list[str] = []
    listen_port: int = 8766
    probe_port: int = 9090

    # ---- what the rest of ferry-control is built from

    def pod_params(self) -> PodParams:
        p = self.pod
        return PodParams(
            image=p.image,
            hub_url=p.hub_url,
            vnc_pubkeys=p.vnc_pubkeys,
            vnc_origin=p.vnc_origin,
            sandbox=p.sandbox,
            cpu_request=p.cpu_request,
            pod_memory=p.pod_memory,
            require_user_namespace=p.require_user_namespace,
            namespace=p.namespace,
        )

    def policy(self) -> PollPolicy:
        return poll_policy(self.polling)

    def hold_limits(self) -> HoldLimits:
        h = self.hold
        values = {
            "unclaimed_s": (duration(h.unclaimed), MAX_UNCLAIMED_S),
            "session_s": (duration(h.session), MAX_SESSION_S),
            "viewer_gone_s": (duration(h.viewer_gone), MAX_VIEWER_GONE_S),
            "hard_s": (duration(h.hard), MAX_HARD_S),
        }
        for name, (value, maximum) in values.items():
            if not 0 < value.total_seconds() <= maximum:
                raise ConfigError(f"hold.{name} must be between 1 s and the compiled {maximum} s")
        return HoldLimits(**{name: int(value.total_seconds()) for name, (value, _) in values.items()})

    def hub_settings(self) -> HubSettings:
        return HubSettings(
            policy=self.policy(),
            hold=self.hold_limits(),
            checks_per_minute=self.checks_per_minute,
            auto_cart=self.auto_cart,
            alert_only_fares=frozenset(self.alert_only_fares),
            alert_only_vehicles=frozenset(self.alert_only_vehicles),
        )

    def user_limits(self) -> Limits:
        return user_limits(self.limits)

    def db_roles(self) -> Roles:
        return Roles(web=self.roles.web, control=self.roles.control)

    def check(self) -> ControlConfig:
        """Build everything once, so a bad value stops startup rather than a later pass."""
        try:
            self.pod_params()
            self.hub_settings()
            self.user_limits()
            self.db_roles()
        except ValueError as e:
            raise ConfigError(str(e)) from None
        return self


def poll_policy(p: PollingConfig) -> PollPolicy:
    policy = PollPolicy(
        short_check_interval=duration(p.short_check_interval),
        horizon=timedelta(days=p.horizon_days),
        quiet_start=time.fromisoformat(p.quiet_start),
        quiet_end=time.fromisoformat(p.quiet_end),
    )
    if p.tiers:
        policy = replace(policy, tiers=tuple(Tier(duration(t.within), duration(t.interval)) for t in p.tiers))
    return policy


def user_limits(c: LimitsConfig) -> Limits:
    unknown = (set(c.defaults) | set(c.ceilings)) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"Unknown limits: {sorted(unknown)}")
    defaults: dict[LimitKey, int] = {**DEFAULTS, **c.defaults}  # type: ignore[dict-item]
    ceilings: dict[LimitKey, int] = {**CEILINGS, **c.ceilings}  # type: ignore[dict-item]
    return Limits(defaults=defaults, ceilings=ceilings)


def load(path: Path) -> ControlConfig:
    try:
        return msgspec.json.decode(path.read_bytes(), type=ControlConfig).check()
    except (OSError, msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
