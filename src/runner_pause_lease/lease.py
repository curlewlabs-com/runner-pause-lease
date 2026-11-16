"""A pause's lease, and its representation in a GitHub issue body.

A pause IS a GitHub issue. Its body carries a hidden one-line marker holding
the host, the window and the labels captured before the strip, and the
`LEASE_LABEL` on the issue says the lease still owns that host's routing
state. The label is the state, not the issue's open/closed flag: a closed issue
still carrying the label is a resume request that has not been carried out yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .labels import Runner

# Only a successful restore removes it, so it marks an active pause, a strip
# still converging, and a closed issue the reconciler has yet to restore.
LEASE_LABEL = "ci-quiet"


def lease_title(host: str) -> str:
    """The issue title, for a human scanning the issue list and nothing else.

    The host a lease covers is read from the body marker, so a retitled issue
    costs legibility and no correctness.
    """
    return f"CI quiet: {host}"


# `state=all` is required: a reader that asks only for open issues concludes
# that a host whose issue was closed by hand is routing again, before anything
# restored it. One page: the bound is how many hosts are paused at once.
LEASE_QUERY = f"labels={quote(LEASE_LABEL, safe='')}&state=all&per_page=100"

MARKER_PREFIX = "<!-- runner-quiet:"
MARKER_SUFFIX = " -->"


@dataclass(frozen=True)
class Lease:
    """One host's pause, and the labels its restore owes.

    `runners` is the capture taken before the first strip. Once the labels are
    off, it is the only record of what to put back.

    `strip_confirmed` becomes true only after a fresh read of the runners finds
    none of them still routable. Until then the reconciler retries the strip;
    afterward it stops reading runner state for this lease.

    A lease has no owner: a second pause of a paused host extends it, and any
    resume ends it.
    """

    host: str
    opened_at: datetime
    expires_at: datetime
    runners: tuple[Runner, ...]
    strip_confirmed: bool
    issue: int | None = None
    # Closed while the label is still on: a resume not yet carried out.
    closed: bool = False


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def lease_marker(lease: Lease) -> str:
    """The machine-read half of a lease issue body, on one line.

    Compact JSON has no newline, so a body scan can bound the marker by line.
    `-->` would end the HTML comment early; `marker_is_safe` checks for it
    rather than assuming no label or host name contains it.
    """
    payload = {
        "host": lease.host,
        "opened_at": format_time(lease.opened_at),
        "expires_at": format_time(lease.expires_at),
        "strip_confirmed": lease.strip_confirmed,
        "runners": [
            {"id": r.identifier, "name": r.name, "custom": list(r.custom_labels)}
            for r in lease.runners
        ],
    }
    return MARKER_PREFIX + json.dumps(payload, separators=(",", ":")) + MARKER_SUFFIX


def marker_is_safe(marker: str) -> bool:
    """Whether the marker survives a round trip through an HTML comment."""
    return marker.count("-->") == 1 and marker.endswith(MARKER_SUFFIX)


def lease_from_body(body: str, issue: int | None = None) -> Lease | None:
    """The lease a body records, or None when it records none.

    Only a marker at the start of its own line counts, so a marker quoted in
    prose or indented in a code block is not state. The first one wins, so an
    appended quote cannot replace it either. A marker that is malformed in any
    way reads as no lease rather than a partial one: a partial capture would
    restore a partial label set.
    """
    for line in body.splitlines():
        if not line.startswith(MARKER_PREFIX):
            continue
        if not line.endswith(MARKER_SUFFIX):
            return None
        raw = line[len(MARKER_PREFIX) : -len(MARKER_SUFFIX)]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return _lease_from_payload(payload, issue)
    return None


def _lease_from_payload(payload: dict[str, Any], issue: int | None) -> Lease | None:
    host = payload.get("host")
    opened = payload.get("opened_at")
    expires = payload.get("expires_at")
    # Absent in a marker written before strip confirmation existed; such a
    # lease still owes one clean observation.
    strip_confirmed = payload.get("strip_confirmed", False)
    entries = payload.get("runners")
    if not isinstance(host, str) or not isinstance(entries, list):
        return None
    if not isinstance(opened, str) or not isinstance(expires, str):
        return None
    if not isinstance(strip_confirmed, bool):
        return None
    runners: list[Runner] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        identifier, name, custom = (
            entry.get("id"),
            entry.get("name"),
            entry.get("custom"),
        )
        if not isinstance(identifier, int) or not isinstance(name, str):
            return None
        if not isinstance(custom, list) or not all(isinstance(c, str) for c in custom):
            return None
        runners.append(
            Runner(identifier=identifier, name=name, custom_labels=tuple(custom))
        )
    try:
        return Lease(
            host=host,
            opened_at=parse_time(opened),
            expires_at=parse_time(expires),
            runners=tuple(runners),
            strip_confirmed=strip_confirmed,
            issue=issue,
        )
    except ValueError:
        return None
