"""waitlist-control <-> waitlist-fetcher, version 1 (design.md §4.3). Written verbatim from the design contract.

Control POSTs one FetchRequest to the fetcher's /v1/fetch and reads one FetchReply. The vocabulary is closed, as
the agent protocol's is: a request names a provider and that provider's typed query (never a URL, a header or a
host); a reply carries that provider's typed inventory or an error kind (never text from the site). Both sides
convert `query` and `inventory` with the provider's own Struct types, which refuse unknown fields.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

import msgspec

from waitlist.providers.base import FetchErrorKind, ProviderId

VERSION = 1
PATH = "/v1/fetch"
MAX_REQUEST_BYTES = 4 * 1024
MAX_REPLY_BYTES = 64 * 1024

RequestId = Annotated[str, msgspec.Meta(pattern=r"^[a-f0-9]{16}$")]
HttpStatus = Annotated[int, msgspec.Meta(ge=100, le=599)]
RetryAfter = Annotated[int, msgspec.Meta(ge=0, le=3600)]
Millis = Annotated[int, msgspec.Meta(ge=0, le=600_000)]


class _Wire(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    pass


class FetchRequest(_Wire, frozen=True, kw_only=True):
    v: Literal[1] = 1
    id: RequestId  # for matching log lines on both sides; carries nothing about who is watching
    provider: ProviderId
    query: dict[str, Any]  # msgspec.to_builtins(the provider's query_type)


class FetchOk(_Wire, tag="ok", tag_field="result", frozen=True, kw_only=True):
    v: Literal[1] = 1
    id: RequestId
    inventory: dict[str, Any]  # msgspec.to_builtins(the provider's inventory_type)
    elapsed_ms: Millis


class FetchFailed(_Wire, tag="failed", tag_field="result", frozen=True, kw_only=True):
    v: Literal[1] = 1
    id: RequestId
    error: FetchErrorKind
    http_status: HttpStatus | None = None
    retry_after_s: RetryAfter | None = None
    elapsed_ms: Millis = 0


FetchReply = FetchOk | FetchFailed

_request_decoder = msgspec.json.Decoder(FetchRequest)
_reply_decoder = msgspec.json.Decoder(FetchReply)
_encoder = msgspec.json.Encoder()


class WireError(ValueError):
    """A body that isn't a valid message of this protocol."""


def encode(message: FetchRequest | FetchOk | FetchFailed) -> bytes:
    return _encoder.encode(message)


def decode_request(body: bytes) -> FetchRequest:
    if len(body) > MAX_REQUEST_BYTES:
        raise WireError("request too large")
    try:
        return _request_decoder.decode(body)
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        raise WireError(str(e)) from None


def decode_reply(body: bytes) -> FetchReply:
    if len(body) > MAX_REPLY_BYTES:
        raise WireError("reply too large")
    try:
        return cast(FetchReply, _reply_decoder.decode(body))
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        raise WireError(str(e)) from None
