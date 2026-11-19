"""Label surgery on a repository's self-hosted runners, through `gh api`.

Removing a runner's labels takes it out of the scheduler without touching the
job it is already running. Each property below is invisible when it is lost: a
runner with no labels does not fail, it is simply never scheduled again, and
nothing alarms on that.

- ONLY CUSTOM LABELS ROUND-TRIP. Read-only labels (the ones the runner
  application adds by default) cannot be removed, and POSTing one back is
  rejected. Which names are custom differs BETWEEN RUNNERS ON ONE HOST: a
  runner registered with `--no-default-labels` carries `self-hosted`, `macOS`
  and `ARM64` as custom labels, and one registered the default way does not.
  So a restore is built from what each runner reported, never from a
  remembered pool label.
- A HOST IS SELECTED BY ITS IDENTITY LABEL, matched whole. A name prefix match
  is wrong as soon as hosts are numbered: `build-1` is a prefix of `build-10`.
- A RESTORE CONTINUES PAST A FAILING RUNNER. The runners after a failing one
  are exactly as unreachable as it is, and stopping would leave them that way
  with nothing scheduled to retry.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .decoded import as_dict, as_list


@dataclass(frozen=True)
class Fleet:
    """Where the runners and the leases live, and how hosts are named.

    `host_label_prefix` names a host: a host is every runner carrying the label
    `<prefix><host>`. `keep_label_prefixes` names other labels that identify a
    runner rather than route work to it. A pause strips every custom label
    except these, because it cannot see which labels a workflow's `runs-on`
    selects on.

    `admin_token` is the credential for the runner endpoints, which need repo
    Administration; None uses the ambient `gh` login. The lease issue always
    uses the ambient login.
    """

    repo: str
    host_label_prefix: str
    keep_label_prefixes: tuple[str, ...] = ()
    admin_token: str | None = None

    def host_label(self, host: str) -> str:
        return f"{self.host_label_prefix}{host}"

    def identity_prefixes(self) -> tuple[str, ...]:
        return (self.host_label_prefix, *self.keep_label_prefixes)


@dataclass(frozen=True)
class Runner:
    """One registered runner, with the labels a restore has to reproduce."""

    identifier: int
    name: str
    custom_labels: tuple[str, ...]


def gh_api(arguments: Sequence[str], token: str | None = None) -> str:
    """One `gh api` call, failing loudly rather than returning a partial answer.

    `token`, when given, replaces the ambient `gh` credential for this call
    only, so the Administration credential never reaches the issue calls.
    """
    completed = subprocess.run(
        ["gh", "api", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GH_TOKEN": token} if token else None,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(arguments)}: {completed.stderr.strip()}")
    return completed.stdout


def runner_payloads_from_pages(document: object) -> list[Any]:
    """Flatten a slurped registry read: one page object per element.

    A top level that is not a page list, or a page without its `runners`
    array, raises rather than reading as an empty fleet: an empty read is what
    would let a lease close without restoring anything.
    """
    pages = as_list(document)
    if pages is None:
        raise TypeError("unexpected shape from the runners endpoint: not a page list")
    runners: list[Any] = []
    for page in pages:
        page_fields = as_dict(page)
        page_runners = (
            as_list(page_fields.get("runners")) if page_fields is not None else None
        )
        if page_runners is None:
            raise TypeError(
                "unexpected shape from the runners endpoint: page lacks runners"
            )
        runners.extend(page_runners)
    return runners


def fetch_runner_payloads(fleet: Fleet) -> list[Any]:
    """Every registered runner, as the API reports it, in one paginated read.

    `--slurp`, because without it a second page is a second JSON document on
    the same stream, and the read stops parsing the moment the fleet outgrows
    one page.
    """
    document = json.loads(
        gh_api(
            [
                f"repos/{fleet.repo}/actions/runners?per_page=100",
                "--paginate",
                "--slurp",
            ],
            fleet.admin_token,
        )
    )
    return runner_payloads_from_pages(document)


def label_entries(payload: object) -> list[dict[str, Any]]:
    """A runner's label objects, or a TypeError when they are not that.

    Read as no labels, a malformed field would capture nothing for the runner
    and let the strip check count it clean with its labels still on.
    """
    fields = as_dict(payload)
    labels = as_list(fields.get("labels")) if fields is not None else None
    if labels is None:
        raise TypeError(f"runner without a labels list: {payload!r}")
    entries: list[dict[str, Any]] = []
    for label in labels:
        entry = as_dict(label)
        if entry is None or not isinstance(entry.get("name"), str):
            raise TypeError(f"runner label without a name: {label!r}")
        entries.append(entry)
    return entries


def custom_labels(payload: object) -> tuple[str, ...]:
    """The labels that can be removed and put back, in the order given."""
    return tuple(
        label["name"]
        for label in label_entries(payload)
        if label.get("type") == "custom"
    )


def carries_label(payload: object, wanted: str) -> bool:
    """Whether a runner payload carries exactly this label."""
    return any(label["name"] == wanted for label in label_entries(payload))


def registered_ids(payloads: Sequence[Any]) -> set[int]:
    """The id of every runner the API reported, whatever host it is on.

    This decides whether a captured label set still has anywhere to go back to.
    An entry without a usable id raises rather than being skipped: skipping
    would make the runner it stood for look unregistered, and "gone" is the
    one reading that must never be reached by accident.
    """
    ids: set[int] = set()
    for payload in payloads:
        fields = as_dict(payload)
        identifier = fields.get("id") if fields is not None else None
        if not isinstance(identifier, int):
            raise TypeError(f"runner without a usable id: {payload!r}")
        ids.add(identifier)
    return ids


def select_runners_from(
    fleet: Fleet, payloads: Sequence[Any], host: str
) -> list[Runner]:
    """Every registered runner on one host, with its labels captured."""
    wanted = fleet.host_label(host)
    selected: list[Runner] = []
    for payload in payloads:
        if not carries_label(payload, wanted):
            continue
        identifier = payload.get("id")
        name = payload.get("name")
        if not isinstance(identifier, int) or not isinstance(name, str):
            raise TypeError(f"runner without a usable id/name: {payload!r}")
        selected.append(
            Runner(
                identifier=identifier,
                name=name,
                custom_labels=custom_labels(payload),
            )
        )
    return selected


def routing_labels(fleet: Fleet, runner: Runner) -> tuple[str, ...]:
    """Every custom label except the identities the fleet names."""
    keep = fleet.identity_prefixes()
    return tuple(label for label in runner.custom_labels if not label.startswith(keep))


def strip_routing(fleet: Fleet, runners: Sequence[Runner]) -> int:
    """Remove every routing label, one call per label, keeping identities.

    Per-label DELETE, because the call that removes all of a runner's labels
    also takes its identity, and a host that cannot be identified while it is
    paused can be reconciled from nothing but the lease. Returns how many
    runners had a label it could not remove.
    """
    failed = 0
    for runner in runners:
        removed: list[str] = []
        runner_failed = False
        base = f"repos/{fleet.repo}/actions/runners/{runner.identifier}/labels"
        for label in routing_labels(fleet, runner):
            try:
                gh_api(
                    ["-X", "DELETE", f"{base}/{quote(label, safe='')}"],
                    fleet.admin_token,
                )
            except RuntimeError as error:
                runner_failed = True
                print(
                    f"STRIP FAILED {runner.name} label {label}: {error}",
                    file=sys.stderr,
                )
            else:
                removed.append(label)
        failed += int(runner_failed)
        if removed:
            print(f"stripped {runner.name}: {','.join(removed)}")
    return failed


def restore_labels(fleet: Fleet, runners: Sequence[Runner]) -> int:
    """Put every captured label back, and keep going when one runner fails.

    Returns how many runners could not be restored. Adding a label a runner
    already carries is a no-op, so a repeated restore is safe.
    """
    failed = 0
    for runner in runners:
        if not runner.custom_labels:
            continue
        arguments = [
            "-X",
            "POST",
            f"repos/{fleet.repo}/actions/runners/{runner.identifier}/labels",
        ]
        for label in runner.custom_labels:
            arguments += ["-f", f"labels[]={label}"]
        try:
            gh_api(arguments, fleet.admin_token)
        except RuntimeError as error:
            failed += 1
            print(f"RESTORE FAILED {runner.name}: {error}", file=sys.stderr)
        else:
            print(f"restored {runner.name}: {','.join(runner.custom_labels)}")
    return failed
