"""The issue marker must keep the routing state an eventual restore owes."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from runner_pause_lease import lease as leases
from runner_pause_lease.labels import Runner

NOW = datetime(2026, 8, 25, 23, 30, tzinfo=UTC)


def a_lease(host: str = "host-a", strip_confirmed: bool = False) -> leases.Lease:
    return leases.Lease(
        host=host,
        opened_at=NOW,
        expires_at=NOW + timedelta(hours=4),
        strip_confirmed=strip_confirmed,
        runners=(
            Runner(88, "host-a", ("pool-a", "pool-b")),
            # Registered with --no-default-labels, so its platform labels are
            # CUSTOM and a restore owes them back. Losing these in the round
            # trip is unrecoverable.
            Runner(95, "host-a-4", ("self-hosted", "macOS", "ARM64")),
        ),
    )


class TestLeaseRoundTrip(unittest.TestCase):
    """The marker is the only record of what a stripped host carried."""

    def test_survives_the_body(self) -> None:
        original = a_lease()
        body = f"{leases.lease_marker(original)}\n\nprose a human reads\n"
        parsed = leases.lease_from_body(body, issue=42)
        assert parsed is not None
        self.assertEqual(parsed, replace(original, issue=42))

    def test_a_clean_observation_survives_the_body(self) -> None:
        parsed = leases.lease_from_body(
            leases.lease_marker(a_lease(strip_confirmed=True))
        )
        assert parsed is not None
        self.assertTrue(parsed.strip_confirmed)

    def test_a_marker_without_confirmation_still_owes_a_clean_observation(
        self,
    ) -> None:
        # Markers written before strip confirmation existed lack the field;
        # reading them as confirmed would stop enforcing a strip that may be
        # partial.
        marker = (
            f'{leases.MARKER_PREFIX}{{"host":"h","opened_at":"2026-08-25T00:00:00Z",'
            f'"expires_at":"2026-08-25T04:00:00Z","runners":[]}}'
            f"{leases.MARKER_SUFFIX}"
        )
        parsed = leases.lease_from_body(marker)
        assert parsed is not None
        self.assertFalse(parsed.strip_confirmed)

    def test_the_stored_format_is_read_as_written(self) -> None:
        # A literal marker, not one this code produced: leases outlive a
        # release, so an upgrade must read what the previous version wrote.
        marker = (
            '<!-- runner-quiet:{"host":"host-a","opened_at":"2026-08-25T23:30:00Z",'
            '"expires_at":"2026-08-26T03:30:00Z","strip_confirmed":true,'
            '"runners":[{"id":7,"name":"host-a-1","custom":["pool-a","self-hosted"]}]}'
            " -->"
        )
        parsed = leases.lease_from_body(marker, issue=3)
        self.assertEqual(
            parsed,
            leases.Lease(
                host="host-a",
                opened_at=NOW,
                expires_at=NOW + timedelta(hours=4),
                runners=(Runner(7, "host-a-1", ("pool-a", "self-hosted")),),
                strip_confirmed=True,
                issue=3,
            ),
        )

    def test_a_non_boolean_confirmation_makes_the_lease_unreadable(self) -> None:
        marker = leases.lease_marker(a_lease()).replace(
            '"strip_confirmed":false', '"strip_confirmed":"false"'
        )
        self.assertIsNone(leases.lease_from_body(marker))

    def test_the_marker_stays_one_line_and_closes_once(self) -> None:
        marker = leases.lease_marker(a_lease())
        self.assertNotIn("\n", marker)
        self.assertTrue(leases.marker_is_safe(marker))

    def test_a_label_that_would_close_the_comment_is_caught(self) -> None:
        # `-->` inside the marker would end the HTML comment early and leave
        # the rest of the capture as visible, unparsed text.
        lease = leases.Lease(
            host="h",
            opened_at=NOW,
            expires_at=NOW,
            runners=(Runner(1, "r", ("a-->b",)),),
            strip_confirmed=False,
        )
        self.assertFalse(leases.marker_is_safe(leases.lease_marker(lease)))

    def test_an_indented_quote_is_not_state(self) -> None:
        # A body may quote a marker while explaining one; reading the quote as
        # state would let prose about a lease become the lease.
        real = leases.lease_marker(a_lease())
        other = leases.lease_marker(a_lease(host="other"))
        parsed = leases.lease_from_body(f"{real}\n\nthe set reads\n\n    {other}\n")
        assert parsed is not None
        self.assertEqual(parsed.host, "host-a")

    def test_the_first_marker_wins(self) -> None:
        # So an appended quote cannot replace a live lease.
        real = leases.lease_marker(a_lease())
        later = leases.lease_marker(a_lease(host="other"))
        parsed = leases.lease_from_body(f"{real}\n{later}\n")
        assert parsed is not None
        self.assertEqual(parsed.host, "host-a")

    def test_a_body_with_no_marker_records_no_lease(self) -> None:
        self.assertIsNone(leases.lease_from_body("just prose\n"))

    def test_a_corrupt_marker_reads_as_no_lease_rather_than_a_wrong_one(self) -> None:
        # Fail closed: a half-parsed lease would restore a partial label set,
        # which is worse than reporting none and leaving the issue open.
        self.assertIsNone(
            leases.lease_from_body(
                f'{leases.MARKER_PREFIX}{{"host":}}{leases.MARKER_SUFFIX}'
            )
        )

    def test_a_lease_missing_its_runners_reads_as_no_lease(self) -> None:
        self.assertIsNone(
            leases.lease_from_body(
                f'{leases.MARKER_PREFIX}{{"host":"h","opened_at":"2026-08-25T00:00:00Z",'
                f'"expires_at":"2026-08-25T04:00:00Z"}}{leases.MARKER_SUFFIX}'
            )
        )

    def test_a_runner_entry_that_cannot_be_restored_reads_as_no_lease(self) -> None:
        # A partial capture would restore a partial label set, so one entry
        # that is not exactly an id, a name and a list of label names voids
        # the whole lease.
        good = {"id": 1, "name": "a", "custom": ["x"]}
        bad_entries: list[object] = [
            "not an object",
            {"id": "1", "name": "a", "custom": []},
            {"id": 1, "name": 2, "custom": []},
            {"id": 1, "name": "a", "custom": "x"},
            {"id": 1, "name": "a", "custom": ["x", 3]},
            {"id": 1, "name": "a"},
        ]
        for bad in bad_entries:
            with self.subTest(entry=bad):
                marker = json.dumps(
                    {
                        "host": "h",
                        "opened_at": "2026-08-25T00:00:00Z",
                        "expires_at": "2026-08-25T04:00:00Z",
                        "runners": [good, bad],
                    },
                    separators=(",", ":"),
                )
                self.assertIsNone(
                    leases.lease_from_body(
                        f"{leases.MARKER_PREFIX}{marker}{leases.MARKER_SUFFIX}"
                    )
                )

    def test_an_instant_written_in_another_zone_normalises(self) -> None:
        # +13:00 puts this instant on the next calendar day, which is where a
        # date-based shortcut would show.
        far_east = NOW.astimezone(timezone(timedelta(hours=13)))
        self.assertNotEqual(far_east.date(), NOW.date())
        lease = leases.Lease(
            host="h",
            opened_at=far_east,
            expires_at=far_east,
            runners=(),
            strip_confirmed=False,
        )
        parsed = leases.lease_from_body(leases.lease_marker(lease))
        assert parsed is not None
        self.assertEqual(parsed.opened_at, NOW)
        self.assertEqual(parsed.opened_at.tzinfo, UTC)


if __name__ == "__main__":
    unittest.main()
