"""The messages between a browser pod's agent and the operator (ferry-control), version 1.

The agent dials the operator over one WebSocket and the two exchange these JSON messages
(docs/multi-tenant/design-reference.md §5.3). The vocabulary is closed on purpose: nothing here can
carry a URL, a selector, a script, a cookie or a screenshot, so the operator can't steer the browser
or read what's in it. All it can do is configure the trip, ask for a check, release or dismiss a cart,
drain the pod and take VNC away. Every field is typed and bounded, and unknown fields are rejected.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

import msgspec

from .bcferries.errors import (
    BookingError,
    CartMismatch,
    LoginFailed,
    MarkupChanged,
    NotBookable,
    QueueTimeout,
    RouteRejected,
    SessionExpired,
)

VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
MAX_UNACKED = 200  # agent messages buffered until acked, and replayed on reconnect

# Limits the agent enforces whatever the operator says (§5.4); the schema refuses anything beyond them.
MIN_INTERVAL_S = 30
MAX_UNCLAIMED_S = 45 * 60
MAX_SESSION_S = 45 * 60
MAX_VIEWER_GONE_S = 15 * 60
MAX_HARD_S = 90 * 60

# ---- field types

HHMM = Annotated[str, msgspec.Meta(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
IsoDate = Annotated[str, msgspec.Meta(pattern=r"^\d{4}-\d{2}-\d{2}$")]
TripId = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]{1,32}$")]
CartId = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]{1,32}$")]
Boot = Annotated[str, msgspec.Meta(pattern=r"^[a-f0-9]{8,32}$")]
TerminalCode = Annotated[str, msgspec.Meta(pattern=r"^[A-Z]{3}$")]
PodName = Annotated[str, msgspec.Meta(pattern=r"^fw-[a-z0-9]{1,32}-g\d{1,6}$")]
ImageDigest = Annotated[str, msgspec.Meta(pattern=r"^sha256:[a-f0-9]{64}$")]
Version = Annotated[str, msgspec.Meta(max_length=64, pattern=r"^[\w.+-]+$")]
Price = Annotated[str, msgspec.Meta(max_length=24, pattern=r"^[\w$.,\- ]*$")]
Label = Annotated[str, msgspec.Meta(max_length=64)]
FareName = Annotated[str, msgspec.Meta(max_length=32)]
ErrorText = Annotated[str, msgspec.Meta(max_length=300)]
Seq = Annotated[int, msgspec.Meta(ge=0)]
Count = Annotated[int, msgspec.Meta(ge=0, le=10_000_000)]
Passengers = Annotated[int, msgspec.Meta(ge=0, le=9)]
Epoch = Annotated[float, msgspec.Meta(ge=0)]
Departures = Annotated[list[HHMM], msgspec.Meta(max_length=100)]

ErrorClass = Literal[
    "markup_changed",
    "session_expired",
    "queue_timeout",
    "route_rejected",
    "login_failed",
    "not_bookable",
    "cart_mismatch",
    "booking",
    "timeout",
    "unexpected",
]
SailingStatus = Literal["available", "sold_out", "unavailable"]
CartStatus = Literal["adding", "ready", "failed"]
Phase = Literal["starting", "watching", "holding", "draining"]
CheckoutMode = Literal["guest"]  # reserved so an account mode is an addition, not a change of meaning
Vehicle = Literal["standard", "tall", "none"]
FarePreference = Literal["cheapest", "saver", "prepaid", "reservation"]

_ERROR_CLASSES: tuple[tuple[type[BaseException], ErrorClass], ...] = (
    (MarkupChanged, "markup_changed"),
    (SessionExpired, "session_expired"),
    (QueueTimeout, "queue_timeout"),
    (RouteRejected, "route_rejected"),
    (LoginFailed, "login_failed"),
    (NotBookable, "not_bookable"),
    (CartMismatch, "cart_mismatch"),
    (BookingError, "booking"),
    (TimeoutError, "timeout"),
)


def error_class(error: BaseException) -> ErrorClass:
    """What kind of failure this was, as it crosses to the operator (never the exception itself)."""
    for kind, name in _ERROR_CLASSES:
        if isinstance(error, kind):
            return name
    return "unexpected"


# ---- shared parts


class _Part(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    pass


class FareInfo(_Part, frozen=True, kw_only=True):
    name: FareName
    price: Price | None = None
    selectable: bool


class SailingInfo(_Part, frozen=True, kw_only=True):
    depart: HHMM
    arrive: HHMM
    vessel: Label
    status: SailingStatus
    fares: Annotated[list[FareInfo], msgspec.Meta(max_length=6)] = []


class ErrorInfo(_Part, frozen=True, kw_only=True):
    kind: ErrorClass = msgspec.field(name="class")
    text: ErrorText  # already masked by describe() in the agent


class CartInfo(_Part, frozen=True, kw_only=True):
    id: CartId
    status: CartStatus
    depart: HHMM
    fare: FareName | None = None
    price: Price | None = None
    ready_at: Epoch | None = None
    unclaimed_deadline: Epoch | None = None
    session_deadline: Epoch | None = None
    hard_deadline: Epoch | None = None
    payment_in_progress: bool = False


class ViewerInfo(_Part, frozen=True, kw_only=True):
    connected: bool = False
    first_at: Epoch | None = None
    seconds: Count = 0


class Navigations(_Part, frozen=True, kw_only=True):
    """Page loads since the last state message, by purpose (feeds the fleet's request budget)."""

    refresh: Count = 0
    search: Count = 0
    cart: Count = 0
    empty: Count = 0
    route_info: Count = 0


class TripSpec(_Part, frozen=True, kw_only=True):
    """A trip as Trip.to_json writes it. The agent re-validates it with Trip.from_json."""

    id: TripId
    enabled: bool
    origin: TerminalCode = msgspec.field(name="from")
    destination: TerminalCode = msgspec.field(name="to")
    date: IsoDate
    adults: Passengers
    children: Passengers
    infants: Passengers
    seniors: Passengers
    vehicle: Vehicle
    height_ft: Annotated[float, msgspec.Meta(ge=0, le=20)]
    earliest: HHMM
    latest: HHMM
    auto_cart: bool
    fare: FarePreference


class HoldLimits(_Part, frozen=True, kw_only=True):
    unclaimed_s: Annotated[int, msgspec.Meta(gt=0, le=MAX_UNCLAIMED_S)]
    session_s: Annotated[int, msgspec.Meta(gt=0, le=MAX_SESSION_S)]
    viewer_gone_s: Annotated[int, msgspec.Meta(gt=0, le=MAX_VIEWER_GONE_S)]
    hard_s: Annotated[int, msgspec.Meta(gt=0, le=MAX_HARD_S)]


class Brake(_Part, frozen=True, kw_only=True):
    multiplier: Annotated[float, msgspec.Meta(ge=1, le=8)] = 1.0
    paused: bool = False


# ---- envelope


class _Message(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True, tag_field="type"):
    v: Literal[1] = 1
    seq: Seq  # monotonic per sender; the agent's restart at each boot
    trip: TripId
    boot: Boot  # the agent's boot id (the operator echoes it back)
    rev: Seq  # the trip revision this message was computed under; stale ones are dropped


# ---- agent -> operator


class State(_Message, tag="state", frozen=True, kw_only=True):
    """Everything the dashboard shows about the trip; sent on change and at least every 15 s."""

    phase: Phase
    checking: bool = False
    checked_at: Epoch | None = None
    next_due: Epoch | None = None
    retry_at: Epoch | None = None
    effective_interval: Annotated[int, msgspec.Meta(ge=MIN_INTERVAL_S)] | None = None
    sailings: Annotated[list[SailingInfo], msgspec.Meta(max_length=60)] = []
    error: ErrorInfo | None = None
    consecutive_errors: Count = 0
    cart: CartInfo | None = None
    viewer: ViewerInfo = msgspec.field(default_factory=ViewerInfo)
    alerted: Departures = []  # departures already alerted
    offered: Departures = []  # departures already put in a cart (never re-carted)
    navs: Navigations = msgspec.field(default_factory=Navigations)
    queue_detours: Count = 0
    working_set_mb: Count = 0
    lockdown_ok: bool = False
    bucket: Annotated[float, msgspec.Meta(ge=0, le=100)] = 0.0


EventCode = Literal[
    "session_opened",
    "sold_out_again",
    "check_failed",
    "cart_added",
    "cart_released",
    "viewer_connected",
    "viewer_disconnected",
    "booking_confirmed",
    "checkout_host_blocked",
    "checkout_login_seen",
    "cart_expired_by_site",
    "opening_seen",
    "opening_gone",
]


class EventParams(_Part, frozen=True, kw_only=True):
    depart: HHMM | None = None
    fare: FareName | None = None
    minutes: Count | None = None
    kind: ErrorClass | None = msgspec.field(default=None, name="class")
    seconds: Count | None = None
    forced: bool | None = None  # only on checkout_login_seen


class Event(_Message, tag="event", frozen=True, kw_only=True):
    """A typed activity entry. No free text and no hostnames."""

    code: EventCode
    params: EventParams = msgspec.field(default_factory=EventParams)


AlertKind = Literal[
    "bookable",
    "cart_held",
    "cart_failed",
    "cart_lost",
    "cart_expired",
    "failing",
    "hold_reminder",
    "hold_ending",
    "release_unverified",
    "checkout_blocked",
]


class AlertParams(_Part, frozen=True, kw_only=True):
    departures: Annotated[list[HHMM], msgspec.Meta(max_length=10)] = []
    fare: FareName | None = None
    price: Price | None = None
    minutes: Count | None = None
    error_class: ErrorClass | None = None
    by_site: bool | None = None


class Alert(_Message, tag="alert", frozen=True, kw_only=True):
    """Something the user should hear about. The operator writes the words and the links."""

    kind: AlertKind
    params: AlertParams = msgspec.field(default_factory=AlertParams)


ByeReason = Literal[
    "hold_ended", "drained", "deadline", "recycle", "startup_failed", "lockdown_failed", "fatal", "smoke_done"
]
Outcome = Literal["booked", "released", "expired", "expired_by_site", "payment_left_to_site", "none"]
ReleaseCheck = Literal["cart_empty", "landing_url", "none"]


class Bye(_Message, tag="bye", frozen=True, kw_only=True):
    """The agent's last word before it exits."""

    reason: ByeReason
    outcome: Outcome = "none"
    cart_emptied: bool = False
    release_check: ReleaseCheck = "none"
    viewer_was_connected: bool = False


class Hello(_Message, tag="hello", frozen=True, kw_only=True):
    """The first message on every connection: who this is, and what hasn't been acknowledged yet."""

    proto: Annotated[list[Annotated[int, msgspec.Meta(ge=1)]], msgspec.Meta(min_length=1, max_length=4)]
    agent_version: Version
    image_digest: ImageDigest
    pod_name: PodName
    gen: Seq
    applied_rev: Seq
    snapshot: State
    unacked: Annotated[list[Event | Alert | Bye], msgspec.Meta(max_length=MAX_UNACKED)] = []


AgentMessage = Hello | State | Event | Alert | Bye

# ---- operator -> agent


class HelloAck(_Message, tag="hello_ack", frozen=True, kw_only=True):
    server_time: Epoch
    accepted_proto: Annotated[int, msgspec.Meta(ge=1)]
    resend_from: Seq


class Configure(_Message, tag="configure", frozen=True, kw_only=True):
    """The trip and the limits to run it under. Idempotent; the latest revision wins."""

    trip_spec: TripSpec
    active: bool
    interval_s: Annotated[int, msgspec.Meta(ge=MIN_INTERVAL_S, le=24 * 3600)]
    auto_cart: bool
    checkout_mode: CheckoutMode = "guest"
    hold: HoldLimits
    alerted: Departures = []
    offered: Departures = []
    vnc_allowed: bool = False
    brake: Brake = msgspec.field(default_factory=Brake)
    breaker: Literal["closed", "open"] = "closed"


CommandOp = Literal["check_now", "release", "dismiss", "drain", "vnc_revoke"]
CommandReason = Literal["deleted", "past", "superseded", "suspended", "maintenance", "recycle", "hold_ended", "limit"]


class Command(_Message, tag="command", frozen=True, kw_only=True):
    id: Seq
    op: CommandOp
    cart_id: CartId | None = None  # release and dismiss name the cart they mean
    reason: CommandReason | None = None  # drain and vnc_revoke say why

    def __post_init__(self) -> None:
        needs_cart = self.op in ("release", "dismiss")
        if needs_cart != (self.cart_id is not None):
            raise ValueError(f"{self.op} {'needs' if needs_cart else 'takes no'} cart_id")
        needs_reason = self.op in ("drain", "vnc_revoke")
        if needs_reason != (self.reason is not None):
            raise ValueError(f"{self.op} {'needs' if needs_reason else 'takes no'} reason")


class Ack(_Message, tag="ack", frozen=True, kw_only=True):
    acked: Seq  # the agent message being acknowledged


ControlMessage = HelloAck | Configure | Command | Ack

# ---- wire

_agent_decoder = msgspec.json.Decoder(AgentMessage)
_control_decoder = msgspec.json.Decoder(ControlMessage)
_encoder = msgspec.json.Encoder()


class ProtocolError(ValueError):
    """A frame that isn't a valid protocol message. The connection is closed."""


def encode(message: AgentMessage | ControlMessage) -> bytes:
    return _encoder.encode(message)


def _check_size(frame: bytes | str) -> bytes:
    data = frame.encode() if isinstance(frame, str) else frame
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError(f"Frame of {len(data)} bytes is over the {MAX_FRAME_BYTES}-byte limit")
    return data


def decode_agent_message(frame: bytes | str) -> AgentMessage:
    """What the operator reads from an agent."""
    try:
        return cast(AgentMessage, _agent_decoder.decode(_check_size(frame)))
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ProtocolError(str(e)) from None


def decode_control_message(frame: bytes | str) -> ControlMessage:
    """What an agent reads from the operator."""
    try:
        return cast(ControlMessage, _control_decoder.decode(_check_size(frame)))
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ProtocolError(str(e)) from None
