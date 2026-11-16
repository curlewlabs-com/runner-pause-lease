"""Pause a host's runners for a bounded window, and put them back by themselves.

A pause removes the host's routing labels. That stops the NEXT job from being
assigned without touching the ones already running, so the host goes quiet as
its current work drains, not by killing it.

WHY A LEASE RATHER THAN A TIMER. The dangerous half is the window between the
strip and the restore: a runner with no labels does not fail, it is simply
never scheduled again, and nothing alarms on that. A one-shot timer is a single
point of failure for the restore, and it fails silently. So a pause records a
LEASE with an expiry, and `reconcile`, run on a schedule, restores every lease
that has ended. A missed run is retried by the next one.

THE CAPTURE IS WRITTEN BEFORE ITS LABELS COME OFF, for the first strip and for
any runner or label a retry finds later. If the write fails, the labels stay
on. A strip with no record of what to put back is the one outcome this tool
must not be able to produce.

A SECOND PAUSE EXTENDS; IT NEVER RE-CAPTURES. On a host whose labels are
already off, a capture would record the stripped set, an empty one, and
overwrite the good one, so the eventual restore would put nothing back.

Each command reads the clock once and is handed that instant, so a command
cannot decide a window was live when it opened it and expired when it wrote
it, and tests need no clock.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import quote

from .labels import (
    Fleet,
    Runner,
    fetch_runner_payloads,
    registered_ids,
    restore_labels,
    routing_labels,
    select_runners_from,
    strip_routing,
)
from .lease import (
    LEASE_LABEL,
    LEASE_QUERY,
    MARKER_PREFIX,
    Lease,
    format_time,
    lease_from_body,
    lease_marker,
    lease_title,
    marker_is_safe,
)

# How much quiet one pause buys, and the bound no amount of re-pausing passes.
# Forgetting to resume costs one window of one host's capacity; the cap stops a
# lease nobody revisits from running overnight.
WINDOW = timedelta(hours=4)
CAP = timedelta(hours=12)

# `status` exits with this while the host is inside a live window, so a caller
# can tell "paused" from "could not tell" (1).
QUIET_STATUS = 3


def window_expiry(
    now: datetime,
    opened_at: datetime,
    window: timedelta = WINDOW,
    cap: timedelta = CAP,
) -> datetime:
    """When a pause requested at `now` should end.

    The cap is measured from when the window first opened, so re-pausing
    cannot walk a host past it one extension at a time. A result at or before
    `now` means the cap is spent, and the caller resumes instead of writing an
    already-expired lease.
    """
    return min(now + window, opened_at + cap)


def is_expired(lease: Lease, now: datetime) -> bool:
    return now >= lease.expires_at


def needs_restore(lease: Lease, now: datetime) -> bool:
    """Whether this host's labels should go back on now: the window ran out, or
    someone closed the issue to end it early. Every restore is gated here."""
    return lease.closed or is_expired(lease, now)


def needs_strip_reconcile(lease: Lease, now: datetime) -> bool:
    """Whether an active lease still owes one clean observation."""
    return not lease.strip_confirmed and not needs_restore(lease, now)


def live_window(leases: Sequence[Lease], host: str, now: datetime) -> Lease | None:
    """The host's pause that is open and unexpired, if any."""
    for lease in leases:
        if lease.host == host and not lease.closed and not is_expired(lease, now):
            return lease
    return None


def partition_resume_targets(
    leases: Sequence[Lease], hosts: Collection[str]
) -> tuple[list[Lease], list[Lease]]:
    """Separate the leases a resume names from the ones it reconciles after."""
    targets: list[Lease] = []
    remaining: list[Lease] = []
    for lease in leases:
        (targets if lease.host in hosts else remaining).append(lease)
    return targets, remaining


def merge_restore_capture(
    captured: tuple[Runner, ...], observed: tuple[Runner, ...]
) -> tuple[Runner, ...]:
    """Add newly observed runners and labels without forgetting the capture.

    Keyed by runner id, not name: a runner re-registered under the same name
    is a different runner with its own restore debt.
    """
    merged = list(captured)
    indices = {runner.identifier: index for index, runner in enumerate(merged)}
    for runner in observed:
        index = indices.get(runner.identifier)
        if index is None:
            indices[runner.identifier] = len(merged)
            merged.append(runner)
            continue
        original = merged[index]
        added = tuple(
            label
            for label in runner.custom_labels
            if label not in original.custom_labels
        )
        merged[index] = replace(original, custom_labels=original.custom_labels + added)
    return tuple(merged)


def partition_by_registry(
    captured: Sequence[Runner], registered: Collection[int]
) -> tuple[tuple[Runner, ...], tuple[Runner, ...]]:
    """Split a capture into the runners the API still has and the ones it lacks.

    A runner unregistered during its window has no labels endpoint left, so
    acting on it can only fail, and a restore that keeps failing holds the
    lease open with every reconcile red. It owes nothing: whatever replaced it
    registered with its own labels.

    An EMPTY registry is not evidence that anything is gone. It cannot be told
    apart from a read that returned nothing, and treating every runner as gone
    would close the lease having restored nothing. So an empty read keeps every
    runner, and the restore fails loudly on each instead.
    """
    if not registered:
        return tuple(captured), ()
    present = tuple(r for r in captured if r.identifier in registered)
    gone = tuple(r for r in captured if r.identifier not in registered)
    return present, gone


def issue_api(
    arguments: list[str], method: str | None = None, absent_ok: bool = False
) -> str:
    """One `gh api` call for the lease issue, on the ambient credential.

    `absent_ok` makes a 404 a success for a DELETE: the issue listing lags a
    label removal by seconds, so a lease restored moments ago can still be
    listed with its label, and removing the label again must read as done.
    """
    command = ["gh", "api", *(["-X", method] if method else []), *arguments]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        if absent_ok and method == "DELETE" and "HTTP 404" in completed.stderr:
            return ""
        raise RuntimeError(f"gh api {' '.join(arguments)}: {completed.stderr.strip()}")
    return completed.stdout


def tracked_leases(fleet: Fleet) -> tuple[list[Lease], int]:
    """Every lease the label still marks, and how many could not be read.

    Read from the issues rather than the runners, because stripping removes
    the API's record of what the host carried. An issue whose marker will not
    parse is counted, not skipped: it may name a host stripped with no
    readable record, which must never pass quietly.
    """
    document = json.loads(
        issue_api([f"repos/{fleet.repo}/issues?{LEASE_QUERY}"], method="GET")
    )
    leases: list[Lease] = []
    unreadable = 0
    for entry in document if isinstance(document, list) else []:
        if not isinstance(entry, dict):
            continue
        number, body = entry.get("number"), entry.get("body")
        if not isinstance(number, int) or not isinstance(body, str):
            unreadable += 1
            continue
        lease = lease_from_body(body, issue=number)
        if lease is None:
            print(
                f"issue #{number} carries {LEASE_LABEL} but its lease will not "
                "parse; a host may be stripped with no readable record",
                file=sys.stderr,
            )
            unreadable += 1
            continue
        leases.append(replace(lease, closed=entry.get("state") == "closed"))
    return leases, unreadable


def lease_for(fleet: Fleet, host: str) -> Lease | None:
    """This host's open lease, if it has one.

    Callers reconcile first, so a closed lease still awaiting its restore has
    been restored by the time this runs.
    """
    for lease in tracked_leases(fleet)[0]:
        if lease.host == host and not lease.closed:
            return lease
    return None


def write_lease(fleet: Fleet, lease: Lease) -> int:
    """Open the lease issue and return its number. Called BEFORE any strip.

    The body offers closing the issue as the way to end a window early, and no
    command: a reconcile acts under whatever lock the caller holds around this
    tool, while a command pasted from an issue would not.
    """
    marker = lease_marker(lease)
    if not marker_is_safe(marker):
        raise RuntimeError(f"lease marker is not round-trippable: {marker!r}")
    body = (
        f"{marker}\n\n"
        f"CI job routing to `{lease.host}` is paused until "
        f"{format_time(lease.expires_at)}.\n\n"
        "Jobs already running when the window opened were left alone and drain "
        "normally. At expiry the next reconcile restores the labels and closes "
        "this issue. To end it early, close this issue; the next reconcile "
        "restores the host.\n\n"
        "The marker above is the only record of what this host's runners "
        "carried. The reconciler retries any incomplete strip, then uses it to "
        "restore routing when the window ends."
    )
    created = json.loads(
        issue_api(
            [
                f"repos/{fleet.repo}/issues",
                "-f",
                f"title={lease_title(lease.host)}",
                "-f",
                f"body={body}",
                "-f",
                f"labels[]={LEASE_LABEL}",
            ],
            method="POST",
        )
    )
    number = created.get("number") if isinstance(created, dict) else None
    if not isinstance(number, int):
        raise TypeError("issue create returned no number; nothing was stripped")
    return number


def write_lease_marker(fleet: Fleet, lease: Lease) -> None:
    """Replace the marker line in the lease issue, leaving the prose alone."""
    if lease.issue is None:
        raise ValueError(f"lease for {lease.host} has no issue to update")
    marker = lease_marker(lease)
    if not marker_is_safe(marker):
        raise RuntimeError(f"lease marker is not round-trippable: {marker!r}")
    current = json.loads(
        issue_api([f"repos/{fleet.repo}/issues/{lease.issue}", "--jq", "."])
    )
    body = current.get("body") if isinstance(current, dict) else None
    if not isinstance(body, str):
        raise TypeError(f"issue #{lease.issue} has no body to update")
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(MARKER_PREFIX):
            lines[index] = marker
            break
    else:
        raise RuntimeError(f"issue #{lease.issue} lost its marker; not updating")
    issue_api(
        [
            f"repos/{fleet.repo}/issues/{lease.issue}",
            "-f",
            f"body={chr(10).join(lines)}",
        ],
        method="PATCH",
    )


def close_lease(fleet: Fleet, lease: Lease, note: str) -> None:
    """Record the outcome, drop the label, and close the issue if still open.

    The label comes off last and only here, because it is what says the lease
    still owns the host's routing; dropping it before the restore succeeded
    would hide that debt from the reconciler.
    """
    issue_api(
        [f"repos/{fleet.repo}/issues/{lease.issue}/comments", "-f", f"body={note}"],
        method="POST",
    )
    label = quote(LEASE_LABEL, safe="")
    issue_api(
        [f"repos/{fleet.repo}/issues/{lease.issue}/labels/{label}"],
        method="DELETE",
        absent_ok=True,
    )
    if not lease.closed:
        issue_api(
            [f"repos/{fleet.repo}/issues/{lease.issue}", "-f", "state=closed"],
            method="PATCH",
        )


def reconcile_active_strip(fleet: Fleet, lease: Lease) -> int:
    """Enforce an unfinished strip until one pass observes the host clean.

    A runner or label this pass finds that the capture lacks is added to the
    capture BEFORE it is stripped, so convergence cannot remove something the
    eventual restore forgets.
    """
    payloads = fetch_runner_payloads(fleet)
    current = tuple(select_runners_from(fleet, payloads, lease.host))
    current_ids = {runner.identifier for runner in current}
    still_registered, gone = partition_by_registry(
        lease.runners, registered_ids(payloads)
    )
    for runner in gone:
        print(f"{lease.host}: {runner.name} was unregistered; nothing to strip")
    missing = [
        runner for runner in still_registered if runner.identifier not in current_ids
    ]
    if missing:
        print(
            f"{lease.host}: cannot confirm quiet while captured runner(s) are "
            "registered without the host label: "
            + ",".join(runner.name for runner in missing),
            file=sys.stderr,
        )

    routable = tuple(runner for runner in current if routing_labels(fleet, runner))
    if routable:
        expanded = merge_restore_capture(lease.runners, routable)
        if expanded != lease.runners:
            write_lease_marker(
                fleet, replace(lease, runners=expanded, strip_confirmed=False)
            )
        return len(missing) + strip_routing(fleet, routable)
    if missing:
        return len(missing)

    write_lease_marker(fleet, replace(lease, strip_confirmed=True))
    print(f"{lease.host}: quiet confirmed")
    return 0


def restore_and_close(fleet: Fleet, lease: Lease, note: str) -> int:
    """Restore the capture and close the lease, or leave it open on failure."""
    if lease.issue is None:
        raise ValueError(f"lease for {lease.host} has no issue to close")
    present, gone = partition_by_registry(
        lease.runners, registered_ids(fetch_runner_payloads(fleet))
    )
    for runner in gone:
        print(f"{lease.host}: {runner.name} was unregistered; nothing to restore")
    failed = restore_labels(fleet, present)
    if failed:
        print(
            f"{failed} runner(s) on {lease.host} could not be restored; "
            f"leaving issue #{lease.issue} open",
            file=sys.stderr,
        )
        return failed
    if gone:
        names = ", ".join(f"`{runner.name}`" for runner in gone)
        note += (
            f" {len(gone)} captured runner(s) had been unregistered by then and "
            f"had nothing to restore: {names}."
        )
    close_lease(fleet, lease, note)
    print(f"{lease.host}: routing restored, issue #{lease.issue} closed")
    return 0


def reconcile_leases(fleet: Fleet, leases: Sequence[Lease], now: datetime) -> int:
    failures = 0
    for lease in leases:
        if needs_strip_reconcile(lease, now):
            failures += reconcile_active_strip(fleet, lease)
            continue
        if not needs_restore(lease, now):
            continue
        if lease.closed:
            print(f"{lease.host}: lease closed early")
            note = "Quiet window ended early; CI routing restored."
        else:
            print(f"{lease.host}: window expired {format_time(lease.expires_at)}")
            note = "Quiet window expired; CI routing restored automatically."
        failures += restore_and_close(fleet, lease, note)
    return failures


def command_reconcile(fleet: Fleet, now: datetime) -> int:
    """Enforce unfinished strips and restore ended windows. Safe at any time.

    A confirmed active lease costs no runner API calls. An unreadable lease is
    a failure in its own right: it may name a host stripped with nothing able
    to restore it, and the only useful response is a non-zero exit.
    """
    leases, unreadable = tracked_leases(fleet)
    return unreadable + reconcile_leases(fleet, leases, now)


def command_pause(fleet: Fleet, hosts: Sequence[str], now: datetime) -> int:
    failures = command_reconcile(fleet, now)
    for host in dict.fromkeys(hosts):
        failures += pause_host(fleet, host, now)
    return failures


def pause_host(fleet: Fleet, host: str, now: datetime) -> int:
    existing = lease_for(fleet, host)
    if existing is not None:
        expires_at = window_expiry(now, existing.opened_at)
        if expires_at <= now:
            print(
                f"{host}: the {int(CAP.total_seconds() // 3600)}h cap since "
                f"{format_time(existing.opened_at)} is spent; "
                "resuming instead of extending",
                file=sys.stderr,
            )
            return restore_and_close(
                fleet, existing, "Quiet window hit its cap; CI routing restored."
            )
        write_lease_marker(fleet, replace(existing, expires_at=expires_at))
        print(f"{host}: quiet extended to {format_time(expires_at)}")
        return 0

    runners = select_runners_from(fleet, fetch_runner_payloads(fleet), host)
    if not runners:
        print(f"error: no runners carry {fleet.host_label(host)}", file=sys.stderr)
        return 1

    lease = Lease(
        host=host,
        opened_at=now,
        expires_at=window_expiry(now, now),
        runners=tuple(runners),
        strip_confirmed=False,
    )
    # The lease first, always: a strip with no record of what to put back is
    # the one state this tool must not be able to reach.
    number = write_lease(fleet, lease)
    print(f"lease #{number} opened for {host}")
    failures = strip_routing(fleet, lease.runners)
    if failures == 0:
        failures += reconcile_active_strip(fleet, replace(lease, issue=number))
    print(f"{host}: quiet until {format_time(lease.expires_at)}")
    return failures


def command_resume(fleet: Fleet, hosts: Sequence[str], now: datetime) -> int:
    """Restore the named hosts, then reconcile every other lease.

    The named hosts' leases are kept out of the reconcile that follows, so a
    host whose restore just failed cannot fall through into being stripped
    again by active enforcement.
    """
    leases, unreadable = tracked_leases(fleet)
    targets, remaining = partition_resume_targets(leases, hosts)
    failures = unreadable
    for host in dict.fromkeys(hosts):
        if not any(lease.host == host for lease in targets):
            print(f"{host}: no open quiet window; nothing to resume")
    for lease in targets:
        failures += restore_and_close(
            fleet, lease, "Quiet window ended; CI routing restored."
        )
    return failures + reconcile_leases(fleet, remaining, now)


def command_status(fleet: Fleet, host: str, now: datetime) -> int:
    leases, unreadable = tracked_leases(fleet)
    lease = live_window(leases, host, now)
    if lease is not None:
        print(
            f"{host}: quiet until {format_time(lease.expires_at)} (issue #{lease.issue})"
        )
        return QUIET_STATUS
    if unreadable:
        print(
            f"{host}: {unreadable} lease issue(s) will not parse, so whether {host} "
            "is quiet cannot be told",
            file=sys.stderr,
        )
        return 1
    print(f"{host}: no live quiet window")
    return 0
