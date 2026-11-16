"""The decisions that strand a host when they are wrong.

Every one fails silently: a runner whose labels did not come back does not go
red, it simply stops being scheduled. The clock is passed in at every seam, so
nothing here reads the wall clock.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest import mock

from runner_pause_lease import pause
from runner_pause_lease.labels import Fleet, Runner
from runner_pause_lease.lease import LEASE_LABEL, Lease, lease_from_body

# A UTC evening that is the NEXT day in +13:00 and the PREVIOUS day in -11:00,
# so accidental local-date arithmetic shows up as an off-by-one day.
NOW = datetime(2026, 8, 25, 23, 30, tzinfo=UTC)

FLEET = Fleet(
    repo="owner/name", host_label_prefix="host:", keep_label_prefixes=("vm:",)
)


def a_lease(**fields: Any) -> Lease:
    base: dict[str, Any] = {
        "host": "h",
        "opened_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "runners": (),
        "strip_confirmed": False,
    }
    base.update(fields)
    return Lease(**base)


class TestWindowExpiry(unittest.TestCase):
    """How long a pause buys, and why re-pausing cannot buy forever."""

    def test_a_fresh_pause_gets_the_whole_window(self) -> None:
        self.assertEqual(pause.window_expiry(NOW, NOW), NOW + pause.WINDOW)

    def test_the_cap_is_measured_from_when_the_window_opened(self) -> None:
        # Measured from `now`, a host re-paused every few hours could be kept
        # out of the pool indefinitely, with every single pause looking
        # reasonable.
        opened = NOW - timedelta(hours=10)
        self.assertEqual(pause.window_expiry(NOW, opened), opened + pause.CAP)

    def test_an_extension_inside_the_cap_gets_the_full_window(self) -> None:
        opened = NOW - timedelta(hours=1)
        self.assertEqual(pause.window_expiry(NOW, opened), NOW + pause.WINDOW)

    def test_past_the_cap_returns_no_later_than_now(self) -> None:
        # The pause reads this as "resume instead of extending"; a future
        # instant here would re-arm a window the cap already ended.
        opened = NOW - (pause.CAP + timedelta(hours=3))
        self.assertLessEqual(pause.window_expiry(NOW, opened), NOW)


class TestWhenToRestore(unittest.TestCase):
    def test_the_instant_of_expiry_restores(self) -> None:
        # A reconcile landing exactly on the boundary must not wait another
        # whole interval.
        self.assertTrue(pause.needs_restore(a_lease(expires_at=NOW), NOW))

    def test_a_live_window_is_left_alone(self) -> None:
        self.assertFalse(pause.needs_restore(a_lease(), NOW))

    def test_a_closed_lease_restores_before_its_expiry(self) -> None:
        # Closing the issue is the resume that needs no tooling, so it has to
        # reach the same restore the expiry does.
        self.assertTrue(pause.needs_restore(a_lease(closed=True), NOW))

    def test_the_answer_does_not_depend_on_the_callers_zone(self) -> None:
        local = NOW.astimezone(timezone(timedelta(hours=13)))
        self.assertNotEqual(NOW.date(), local.date())
        self.assertTrue(pause.needs_restore(a_lease(expires_at=NOW), local))
        self.assertFalse(pause.needs_restore(a_lease(), local))


class TestLiveWindow(unittest.TestCase):
    def test_an_open_unexpired_window_is_live(self) -> None:
        lease = a_lease(host="build-1")
        self.assertIs(pause.live_window([lease], "build-1", NOW), lease)

    def test_another_hosts_window_is_not_this_ones(self) -> None:
        self.assertIsNone(pause.live_window([a_lease(host="build-1")], "x", NOW))

    def test_closed_and_expired_windows_are_not_live(self) -> None:
        leases = [a_lease(closed=True), a_lease(expires_at=NOW)]
        self.assertIsNone(pause.live_window(leases, "h", NOW))


class TestNeedsStripReconcile(unittest.TestCase):
    """A clean observation retires active enforcement without blocking restore."""

    def test_an_unconfirmed_active_lease_is_reconciled(self) -> None:
        self.assertTrue(pause.needs_strip_reconcile(a_lease(), NOW))

    def test_a_confirmed_active_lease_stops_reconciling(self) -> None:
        lease = a_lease(strip_confirmed=True)
        self.assertFalse(pause.needs_strip_reconcile(lease, NOW))

    def test_an_ended_unconfirmed_lease_restores_instead(self) -> None:
        self.assertFalse(pause.needs_strip_reconcile(a_lease(expires_at=NOW), NOW))
        self.assertFalse(pause.needs_strip_reconcile(a_lease(closed=True), NOW))


class TestResumeTargets(unittest.TestCase):
    """No lease for a named host may fall through into active enforcement."""

    def test_named_hosts_stay_out_of_the_reconcile_that_follows(self) -> None:
        # Reconciling after a partial resume must not re-strip a named host
        # whose restore failed.
        first, other = a_lease(host="first"), a_lease(host="first-other")
        second, second_again = a_lease(host="second"), a_lease(host="second")
        targets, remaining = pause.partition_resume_targets(
            [first, other, second, second_again], ["first", "second", "missing"]
        )
        self.assertEqual(targets, [first, second, second_again])
        self.assertEqual(remaining, [other])


class TestMergeRestoreCapture(unittest.TestCase):
    """A retry must not remove routing state the eventual restore forgets."""

    def test_a_stripped_snapshot_does_not_erase_the_capture(self) -> None:
        captured = (Runner(1, "r", ("pool-a", "host:h")),)
        observed = (Runner(1, "r", ("host:h",)),)
        self.assertEqual(pause.merge_restore_capture(captured, observed), captured)

    def test_a_new_label_is_captured_before_the_retry_removes_it(self) -> None:
        captured = (Runner(1, "r", ("pool-a", "host:h")),)
        observed = (Runner(1, "r", ("pool-b", "host:h")),)
        merged = pause.merge_restore_capture(captured, observed)
        self.assertEqual(merged[0].custom_labels, ("pool-a", "host:h", "pool-b"))
        self.assertEqual(pause.merge_restore_capture(merged, observed), merged)

    def test_a_re_registered_name_with_a_new_id_is_a_new_debt(self) -> None:
        # Keyed by name, a replacement would merge into the dead runner and be
        # left stripped at restore.
        captured = (Runner(1, "r", ("pool-a", "host:h")),)
        observed = (Runner(2, "r", ("pool-b", "host:h")),)
        self.assertEqual(
            pause.merge_restore_capture(captured, observed), captured + observed
        )


class TestPartitionByRegistry(unittest.TestCase):
    """A capture that outlives its runners must not hold the lease open forever."""

    def test_a_runner_the_registry_no_longer_has_is_gone(self) -> None:
        captured = (
            Runner(1, "kept", ("pool-a",)),
            Runner(2, "torn-down", ("pool-a",)),
            Runner(3, "also-kept", ("pool-b",)),
        )
        present, gone = pause.partition_by_registry(captured, {1, 3, 99})
        self.assertEqual(present, (captured[0], captured[2]))
        self.assertEqual(gone, (captured[1],))

    def test_an_empty_registry_read_is_not_evidence_anything_is_gone(self) -> None:
        # Otherwise a read that came back empty would close the lease having
        # restored nothing, taking the only record of what to put back with it.
        captured = (Runner(1, "kept", ("pool-a",)),)
        self.assertEqual(pause.partition_by_registry(captured, set()), (captured, ()))


class FakeGitHub:
    """The runner and issue endpoints the commands call, held in memory.

    `events` records each mutation in order, which is what the ordering
    guarantees below are about.
    """

    def __init__(self, runners: dict[int, tuple[str, list[str]]]) -> None:
        self.runners = runners
        self.issues: dict[int, dict[str, Any]] = {}
        self.events: list[str] = []
        # Mutations to refuse, named as `events` names them: "lease", "update",
        # "strip <id> <label>", "restore <id>".
        self.failing: set[str] = set()

    def refuse(self, event: str) -> None:
        if event in self.failing:
            raise RuntimeError(f"gh api: refused {event}")

    def gh_api(self, arguments: list[str], token: str | None = None) -> str:
        if arguments[0].startswith("repos/owner/name/actions/runners?"):
            page = [
                {
                    "id": rid,
                    "name": name,
                    "labels": [{"name": n, "type": "custom"} for n in labels],
                }
                for rid, (name, labels) in self.runners.items()
            ]
            return json.dumps([{"total_count": len(page), "runners": page}])
        method, path = arguments[1], arguments[2]
        rid = int(path.split("/")[5])
        if method == "DELETE":
            label = path.split("/")[-1].replace("%3A", ":")
            self.refuse(f"strip {rid} {label}")
            self.runners[rid][1].remove(label)
            self.events.append(f"strip {rid} {label}")
            return ""
        self.refuse(f"restore {rid}")
        added = [a.split("=", 1)[1] for a in arguments[3:] if a != "-f"]
        for label in added:
            if label not in self.runners[rid][1]:
                self.runners[rid][1].append(label)
        self.events.append(f"restore {rid}")
        return "{}"

    def issue_api(
        self, arguments: list[str], method: str | None = None, absent_ok: bool = False
    ) -> str:
        path = arguments[0]
        fields = dict(a.split("=", 1) for a in arguments[1:] if "=" in a)
        if method == "GET":
            return json.dumps(
                [i for i in self.issues.values() if LEASE_LABEL in i["labels"]]
            )
        if method == "POST" and path.endswith("/issues"):
            self.refuse("lease")
            number = len(self.issues) + 1
            self.issues[number] = {
                "number": number,
                "body": fields["body"],
                "state": "open",
                "labels": [LEASE_LABEL],
            }
            self.events.append(f"lease {number}")
            return json.dumps({"number": number})
        number = int(path.split("/")[4])
        issue = self.issues[number]
        if method is None:
            return json.dumps(issue)
        if method == "PATCH":
            self.refuse("update")
            issue.update(fields)
            self.events.append(f"update {number} {sorted(fields)}")
        elif method == "DELETE":
            issue["labels"].remove(LEASE_LABEL)
            self.events.append(f"unlabel {number}")
        return "{}"

    def lease(self, number: int) -> Lease:
        parsed = lease_from_body(self.issues[number]["body"], issue=number)
        assert parsed is not None
        return parsed


class TestCommands(unittest.TestCase):
    """The orderings the README promises, through the commands themselves."""

    def setUp(self) -> None:
        self.github = FakeGitHub(
            {
                1: ("laptop-a", ["pool-a", "self-hosted", "host:laptop"]),
                2: ("laptop-b", ["pool-b", "host:laptop", "vm:laptop-1"]),
                3: ("other", ["pool-a", "host:other"]),
            }
        )
        for target, fake in (
            ("runner_pause_lease.labels.gh_api", self.github.gh_api),
            ("runner_pause_lease.pause.issue_api", self.github.issue_api),
        ):
            patcher = mock.patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        printer = mock.patch("builtins.print")
        printer.start()
        self.addCleanup(printer.stop)

    def labels(self, rid: int) -> list[str]:
        return self.github.runners[rid][1]

    def test_the_lease_is_written_before_any_label_comes_off(self) -> None:
        self.assertEqual(pause.command_pause(FLEET, ["laptop"], NOW), 0)
        first_strip = next(
            i for i, e in enumerate(self.github.events) if e.startswith("strip")
        )
        self.assertEqual(self.github.events[0], "lease 1")
        self.assertLess(0, first_strip)
        self.assertEqual(self.labels(1), ["host:laptop"])
        self.assertEqual(self.labels(2), ["host:laptop", "vm:laptop-1"])
        self.assertEqual(self.labels(3), ["pool-a", "host:other"])
        self.assertTrue(self.github.lease(1).strip_confirmed)

    def test_a_second_pause_extends_and_never_recaptures(self) -> None:
        # A capture taken now would record the stripped set and the restore
        # would put nothing back.
        pause.command_pause(FLEET, ["laptop"], NOW)
        captured = self.github.lease(1).runners
        later = NOW + timedelta(hours=1)
        self.assertEqual(pause.command_pause(FLEET, ["laptop"], later), 0)
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(self.github.lease(1).runners, captured)
        self.assertEqual(self.github.lease(1).expires_at, later + pause.WINDOW)

    def test_expiry_restores_and_releases_the_lease(self) -> None:
        pause.command_pause(FLEET, ["laptop"], NOW)
        self.assertEqual(pause.command_reconcile(FLEET, NOW + pause.WINDOW), 0)
        self.assertEqual(self.labels(1), ["host:laptop", "pool-a", "self-hosted"])
        self.assertEqual(self.labels(2), ["host:laptop", "vm:laptop-1", "pool-b"])
        self.assertEqual(self.github.issues[1]["labels"], [])
        self.assertEqual(self.github.issues[1]["state"], "closed")

    def test_closing_the_issue_by_hand_is_a_resume(self) -> None:
        pause.command_pause(FLEET, ["laptop"], NOW)
        self.github.issues[1]["state"] = "closed"
        self.assertEqual(pause.command_reconcile(FLEET, NOW), 0)
        self.assertIn("pool-a", self.labels(1))
        self.assertEqual(self.github.issues[1]["labels"], [])

    def test_a_partial_strip_converges_and_captures_what_appeared(self) -> None:
        pause.command_pause(FLEET, ["laptop"], NOW)
        # A label added to the host while it is paused, before confirmation.
        self.github.issues[1]["body"] = self.github.issues[1]["body"].replace(
            '"strip_confirmed":true', '"strip_confirmed":false'
        )
        self.labels(1).append("pool-new")
        self.assertEqual(pause.command_reconcile(FLEET, NOW), 0)
        self.assertEqual(self.labels(1), ["host:laptop"])
        self.assertIn("pool-new", self.github.lease(1).runners[0].custom_labels)
        pause.command_resume(FLEET, ["laptop"], NOW)
        self.assertIn("pool-new", self.labels(1))

    def test_a_lease_that_cannot_be_written_strips_nothing(self) -> None:
        self.github.failing.add("lease")
        with self.assertRaises(RuntimeError):
            pause.command_pause(FLEET, ["laptop"], NOW)
        self.assertEqual(self.labels(1), ["pool-a", "self-hosted", "host:laptop"])
        self.assertFalse(any(e.startswith("strip") for e in self.github.events))

    def test_a_capture_that_cannot_grow_strips_nothing_new(self) -> None:
        # The label a retry found is removed only once the lease records it,
        # or the restore would never put it back.
        pause.command_pause(FLEET, ["laptop"], NOW)
        self.github.issues[1]["body"] = self.github.issues[1]["body"].replace(
            '"strip_confirmed":true', '"strip_confirmed":false'
        )
        self.labels(1).append("pool-new")
        self.github.failing.add("update")
        with self.assertRaises(RuntimeError):
            pause.command_reconcile(FLEET, NOW)
        self.assertIn("pool-new", self.labels(1))

    def test_a_failed_removal_does_not_stop_the_others(self) -> None:
        self.github.failing.add("strip 1 pool-a")
        self.assertEqual(pause.command_pause(FLEET, ["laptop"], NOW), 1)
        self.assertEqual(self.labels(1), ["pool-a", "host:laptop"])
        self.assertEqual(self.labels(2), ["host:laptop", "vm:laptop-1"])
        # The next reconcile finishes the strip; only a pass that starts out
        # clean confirms it.
        self.github.failing.clear()
        self.assertEqual(pause.command_reconcile(FLEET, NOW), 0)
        self.assertEqual(self.labels(1), ["host:laptop"])
        self.assertFalse(self.github.lease(1).strip_confirmed)
        self.assertEqual(pause.command_reconcile(FLEET, NOW), 0)
        self.assertTrue(self.github.lease(1).strip_confirmed)

    def test_a_failed_restore_keeps_the_lease_and_restores_the_rest(self) -> None:
        pause.command_pause(FLEET, ["laptop"], NOW)
        self.github.failing.add("restore 1")
        self.assertEqual(pause.command_reconcile(FLEET, NOW + pause.WINDOW), 1)
        self.assertEqual(self.labels(1), ["host:laptop"])
        self.assertIn("pool-b", self.labels(2))
        self.assertEqual(self.github.issues[1]["labels"], [LEASE_LABEL])
        # A later reconcile retries the whole set and then releases the lease.
        self.github.failing.clear()
        self.assertEqual(pause.command_reconcile(FLEET, NOW + pause.WINDOW), 0)
        self.assertIn("pool-a", self.labels(1))
        self.assertEqual(self.github.issues[1]["labels"], [])

    def test_the_lease_names_a_resume_that_runs_as_written(self) -> None:
        # Pasting it must reach the same fleet, keep prefixes included: a
        # resume also reconciles other hosts' leases.
        pause.command_pause(FLEET, ["laptop"], NOW)
        self.assertIn(
            "`runner-pause-lease --repo owner/name --host-label-prefix host: "
            "--keep-label-prefix vm: resume --host laptop`",
            self.github.issues[1]["body"],
        )

    def test_a_host_no_runner_names_is_an_error_and_writes_nothing(self) -> None:
        self.assertEqual(pause.command_pause(FLEET, ["nobody"], NOW), 1)
        self.assertEqual(self.github.issues, {})

    def test_status_reports_a_live_window_with_its_own_exit_code(self) -> None:
        self.assertEqual(pause.command_status(FLEET, "laptop", NOW), 0)
        pause.command_pause(FLEET, ["laptop"], NOW)
        self.assertEqual(pause.command_status(FLEET, "laptop", NOW), pause.QUIET_STATUS)
        self.assertEqual(pause.command_status(FLEET, "other", NOW), 0)

    def test_an_unreadable_lease_fails_the_reconcile(self) -> None:
        # It may name a host stripped with nothing able to restore it.
        pause.command_pause(FLEET, ["laptop"], NOW)
        self.github.issues[1]["body"] = "<!-- runner-quiet:{broken -->"
        self.assertEqual(pause.command_reconcile(FLEET, NOW), 1)
        self.assertEqual(pause.command_status(FLEET, "other", NOW), 1)


if __name__ == "__main__":
    unittest.main()
