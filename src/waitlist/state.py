"""A browser pod's cart: what the agent holds for its trip (agent/watch.py), before it crosses the wire."""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from datetime import time as clock_time

from .models import Fare


class CartStatus(enum.StrEnum):
    ADDING = "adding"  # selecting the fare
    READY = "ready"  # in the cart; the trip is paused until it's released
    FAILED = "failed"  # couldn't add it; shown until dismissed


@dataclass(slots=True)
class Cart:
    status: CartStatus
    depart: clock_time
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    since: float = field(default_factory=time.time)
    fare: Fare | None = None
    error: str | None = None
