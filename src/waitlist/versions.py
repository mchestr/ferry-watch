"""Which Waitlist release an agent is, where that decides what may be sent to it.

waitlist-web and waitlist-control read a browser pod's agent version (its hello, kept in `browser_pods.agent_version`)
before offering it something an older agent can't take: the agent-operator protocol refuses unknown fields, and an
older agent's gate has no streamed viewer.
"""

from __future__ import annotations

import re

Release = tuple[int, int, int]

STREAM_VIEWER: Release = (0, 7, 0)  # the first agent with the streamed checkout viewer (`wl.cast1`, `cast_allowed`)

# A normalized PEP 440 version, as the package metadata has it: a release, then optionally a pre-release, a
# post-release, a dev release and a local label ("0.7.0", "0.7.1rc1", "0.7.0.post1", "0.8.0.dev3+g1a2b3c4").
_VERSION = re.compile(
    r"(?P<release>\d{1,6}(?:\.\d{1,6}){0,3})(?P<pre>(?:a|b|rc)\d{1,6})?(?P<post>\.post\d{1,6})?(?P<dev>\.dev\d{1,6})?"
    r"(?:\+[a-z0-9]{1,32}(?:\.[a-z0-9]{1,32}){0,7})?"
)


def at_least(version: str | None, release: Release) -> bool:
    """Whether `version` is `release` or later. A pre-release or dev build of `release` itself comes before it (as
    in PEP 440). None and anything that doesn't parse count as older, so the caller offers only what every version
    takes."""
    match = _VERSION.fullmatch(version) if version is not None else None
    if match is None:
        return False
    numbers = tuple(int(part) for part in match["release"].split("."))
    numbers += (0,) * (3 - len(numbers))
    target = release + (0,) * (len(numbers) - 3)
    if numbers != target:
        return numbers > target
    # Exactly the release: its pre-releases and dev builds come before it; a post-release (even its dev build) after.
    return match["pre"] is None and (match["dev"] is None or match["post"] is not None)
