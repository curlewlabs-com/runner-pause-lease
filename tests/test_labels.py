"""The label reads whose failure is silent: which runners a command acts on,
and which labels it promises to put back. Selecting one runner too many pauses
a host nobody asked for; dropping a label from the capture restores a runner
that then never takes work again. Neither raises at the time."""

from __future__ import annotations

import unittest

from runner_pause_lease.labels import (
    Fleet,
    Runner,
    carries_label,
    custom_labels,
    registered_ids,
    routing_labels,
    runner_payloads_from_pages,
    select_runners_from,
)

FLEET = Fleet(repo="owner/name", host_label_prefix="host:")


class TestCustomLabels(unittest.TestCase):
    def test_keeps_only_labels_that_can_be_put_back(self) -> None:
        # POSTing a read-only label back is rejected, so carrying one into the
        # restore fails the whole call for a runner registered the default way.
        payload = {
            "labels": [
                {"name": "self-hosted", "type": "read-only"},
                {"name": "Linux", "type": "read-only"},
                {"name": "pool-a", "type": "custom"},
                {"name": "host:build-1", "type": "custom"},
            ]
        }
        self.assertEqual(custom_labels(payload), ("pool-a", "host:build-1"))

    def test_a_no_default_labels_runner_keeps_its_platform_labels(self) -> None:
        # The same names are CUSTOM on a runner registered with
        # --no-default-labels, so a pause strips them and the restore owes
        # them back. One host's runners can differ this way.
        payload = {
            "labels": [
                {"name": "self-hosted", "type": "custom"},
                {"name": "pool-a", "type": "custom"},
            ]
        }
        self.assertEqual(custom_labels(payload), ("self-hosted", "pool-a"))


class TestMalformedLabels(unittest.TestCase):
    def test_a_labels_field_that_is_not_a_list_of_names_raises(self) -> None:
        # Read as no labels, it would capture nothing for the runner and let
        # the strip check count it clean with its labels still on.
        for payload in (
            {"labels": None},
            {"labels": "pool-a"},
            {"labels": [{"type": "custom"}]},
            {"labels": ["pool-a"]},
            "not a runner",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(TypeError):
                    custom_labels(payload)
                with self.assertRaises(TypeError):
                    carries_label(payload, "host:x")


class TestRunnerPayloadsFromPages(unittest.TestCase):
    """The registry read must survive the fleet outgrowing one page."""

    def test_pages_flatten_in_order(self) -> None:
        pages = [
            {"total_count": 3, "runners": [{"id": 1}, {"id": 2}]},
            {"total_count": 3, "runners": [{"id": 3}]},
        ]
        self.assertEqual(
            runner_payloads_from_pages(pages), [{"id": 1}, {"id": 2}, {"id": 3}]
        )

    def test_a_page_without_runners_raises_rather_than_reading_empty(self) -> None:
        # An empty read is what would let a lease close without restoring.
        with self.assertRaises(TypeError):
            runner_payloads_from_pages([{"total_count": 1}])
        with self.assertRaises(TypeError):
            runner_payloads_from_pages({"runners": []})


class TestRegisteredIds(unittest.TestCase):
    def test_a_garbled_entry_raises_rather_than_hiding_a_runner(self) -> None:
        # Skipping it would make the runner it stood for look unregistered,
        # and that reading lets a lease close without restoring it.
        with self.assertRaises(TypeError):
            registered_ids([{"id": 7, "name": "a"}, {"name": "no-id"}])
        self.assertEqual(registered_ids([{"id": 7}, {"id": 9}]), {7, 9})


class TestHostSelection(unittest.TestCase):
    def test_matches_the_whole_label(self) -> None:
        payload = {"labels": [{"name": "host:build-1", "type": "custom"}]}
        self.assertTrue(carries_label(payload, FLEET.host_label("build-1")))

    def test_does_not_match_a_longer_host_name(self) -> None:
        # Host names are not prefix-free once they are numbered, and a prefix
        # match would pause runners on a host nobody named.
        payload = {"labels": [{"name": "host:build-10", "type": "custom"}]}
        self.assertFalse(carries_label(payload, FLEET.host_label("build-1")))

    def test_ignores_a_pool_label_spelled_like_the_host(self) -> None:
        payload = {"labels": [{"name": "build-1", "type": "custom"}]}
        self.assertFalse(carries_label(payload, FLEET.host_label("build-1")))

    def test_selects_every_runner_on_the_host_with_its_capture(self) -> None:
        payloads = [
            {
                "id": 1,
                "name": "build-1-a",
                "labels": [
                    {"name": "self-hosted", "type": "read-only"},
                    {"name": "pool-a", "type": "custom"},
                    {"name": "host:build-1", "type": "custom"},
                ],
            },
            {
                "id": 2,
                "name": "build-2-a",
                "labels": [{"name": "host:build-2", "type": "custom"}],
            },
        ]
        self.assertEqual(
            select_runners_from(FLEET, payloads, "build-1"),
            [Runner(1, "build-1-a", ("pool-a", "host:build-1"))],
        )


class TestRoutingLabels(unittest.TestCase):
    """Which labels come off, which identities stay, and why."""

    def test_the_host_label_is_kept(self) -> None:
        # Leaving it on is what keeps a paused host findable by the next
        # command while every routing label is gone.
        runner = Runner(88, "laptop", ("pool-a", "pool-b", "host:laptop"))
        self.assertEqual(routing_labels(FLEET, runner), ("pool-a", "pool-b"))

    def test_a_named_identity_prefix_is_kept(self) -> None:
        fleet = Fleet(
            repo="owner/name", host_label_prefix="host:", keep_label_prefixes=("vm:",)
        )
        runner = Runner(89, "laptop-vm-1", ("pool-a", "host:laptop", "vm:laptop-1"))
        self.assertEqual(routing_labels(fleet, runner), ("pool-a",))

    def test_an_identity_prefix_nobody_named_comes_off(self) -> None:
        # The tool cannot see which labels a `runs-on` selects on, so anything
        # not named as identity is treated as routing.
        runner = Runner(89, "laptop-vm-1", ("pool-a", "host:laptop", "vm:laptop-1"))
        self.assertEqual(routing_labels(FLEET, runner), ("pool-a", "vm:laptop-1"))

    def test_platform_labels_come_off_when_they_are_custom(self) -> None:
        # Custom on a runner registered with --no-default-labels, so a
        # `runs-on: self-hosted` could still reach the host if they stayed.
        runner = Runner(
            95, "laptop-4", ("self-hosted", "macOS", "ARM64", "pool-a", "host:laptop")
        )
        self.assertEqual(
            routing_labels(FLEET, runner),
            ("self-hosted", "macOS", "ARM64", "pool-a"),
        )


if __name__ == "__main__":
    unittest.main()
