"""The agent's settings: all from its pod's environment, every value pinned by the admission policy."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ferry_watch.tickets import TicketError, public_keys

DEFAULT_TOKEN_PATH = "/var/run/secrets/ferry-watch/token"  # noqa: S105 - a path, not a token; projected ServiceAccount token, audience ferry-control

_TRIP_ID = re.compile(r"[a-z0-9]{1,32}")
_POD_NAME = re.compile(r"fw-([a-z0-9]{1,32})-g(\d{1,6})")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_OWNER = re.compile(r"[a-z0-9-]{1,64}")


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgentSettings:
    trip_id: str  # FW_TRIP_ID
    pod_name: str  # FW_POD_NAME: fw-<trip>-g<gen>
    gen: int
    owner: str  # FW_OWNER: the user id checkout tickets must name
    image_digest: str  # FW_IMAGE_DIGEST
    deadline_at: float  # FW_DEADLINE_AT: when Kubernetes ends the pod (activeDeadlineSeconds), epoch seconds
    control_url: str  # FW_CONTROL_URL: ws://ferry-control...:8766/agent/v1
    vnc_origin: str  # FW_VNC_ORIGIN: https://ferry-checkout.chestr.dev
    vnc_keys: Mapping[str, Ed25519PublicKey]  # FW_VNC_PUBKEYS
    token_path: Path = Path(DEFAULT_TOKEN_PATH)  # FW_TOKEN_PATH
    sandbox: bool = False  # FW_SANDBOX=1: Chromium's own sandbox (needs the seccomp profile)
    screen: tuple[int, int] = (1280, 1000)
    display: str = ":99"
    tmp: Path = Path("/tmp")  # noqa: S108 - the pod's memory-backed emptyDir
    termination_log: Path = Path("/dev/termination-log")
    proc_net: Path = Path("/proc/net")
    gate_port: int = 6080
    probe_port: int = 8080

    @property
    def rfb_socket(self) -> Path:
        return self.tmp / "kasm" / "rfb.sock"

    @property
    def home(self) -> Path:
        return self.tmp / "home"

    @property
    def profile_dir(self) -> Path:
        return self.tmp / "profile"

    @property
    def debug_dir(self) -> Path:
        return self.tmp / "debug"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AgentSettings:
        def need(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise SettingsError(f"{name} is not set")
            return value

        trip_id = need("FW_TRIP_ID")
        if not _TRIP_ID.fullmatch(trip_id):
            raise SettingsError("FW_TRIP_ID is not a trip id")
        pod_name = need("FW_POD_NAME")
        match = _POD_NAME.fullmatch(pod_name)
        if match is None or match.group(1) != trip_id:
            raise SettingsError("FW_POD_NAME is not this trip's pod name")
        owner = need("FW_OWNER")
        if not _OWNER.fullmatch(owner):
            raise SettingsError("FW_OWNER is not a user id")
        digest = need("FW_IMAGE_DIGEST")
        if not _DIGEST.fullmatch(digest):
            raise SettingsError("FW_IMAGE_DIGEST is not a sha256 digest")
        try:
            deadline_at = float(need("FW_DEADLINE_AT"))
        except ValueError:
            raise SettingsError("FW_DEADLINE_AT is not a time") from None
        control_url = need("FW_CONTROL_URL")
        if urllib.parse.urlsplit(control_url).scheme not in ("ws", "wss"):
            raise SettingsError("FW_CONTROL_URL is not a WebSocket URL")
        origin = need("FW_VNC_ORIGIN")
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme != "https" or not parts.hostname or parts.path or parts.query:
            raise SettingsError("FW_VNC_ORIGIN is not an https origin")
        try:
            keys = public_keys(need("FW_VNC_PUBKEYS"))
        except TicketError as e:
            raise SettingsError(f"FW_VNC_PUBKEYS: {e}") from None
        return cls(
            trip_id=trip_id,
            pod_name=pod_name,
            gen=int(match.group(2)),
            owner=owner,
            image_digest=digest,
            deadline_at=deadline_at,
            control_url=control_url,
            vnc_origin=origin,
            vnc_keys=keys,
            token_path=Path(env.get("FW_TOKEN_PATH", DEFAULT_TOKEN_PATH)),
            sandbox=env.get("FW_SANDBOX") == "1",
        )
