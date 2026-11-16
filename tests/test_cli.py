"""The command line: which repository and hosts a command acts on."""

from __future__ import annotations

import contextlib
import io
import unittest
from datetime import datetime
from typing import Any
from unittest import mock

from runner_pause_lease import cli
from runner_pause_lease.labels import Fleet

PREFIX = ["--repo", "owner/name", "--host-label-prefix", "host:"]


class TestArguments(unittest.TestCase):
    def test_pause_and_resume_take_one_host_or_several(self) -> None:
        for command in ("pause", "resume"):
            for hosts, expected in (
                (["--host", "a"], ["a"]),
                (["--host", "a", "b"], ["a", "b"]),
                (["--host", "a", "b", "--host", "c"], ["a", "b", "c"]),
            ):
                with self.subTest(command=command, hosts=hosts):
                    parsed = cli.build_parser().parse_args([*PREFIX, command, *hosts])
                    self.assertEqual(parsed.host, expected)

    def test_the_fleet_carries_the_names_and_the_admin_token(self) -> None:
        seen: list[Fleet] = []

        def record(fleet: Fleet, now: datetime) -> int:
            seen.append(fleet)
            return 0

        env = {"RUNNER_ADMIN_TOKEN": "t", "GITHUB_REPOSITORY": "ignored/repo"}
        with (
            mock.patch.dict("os.environ", env),
            mock.patch.object(cli, "command_reconcile", side_effect=record),
        ):
            argv = [*PREFIX, "--keep-label-prefix", "vm:", "--keep-label-prefix"]
            self.assertEqual(cli.main([*argv, "id:", "reconcile"]), 0)
        self.assertEqual(
            seen,
            [Fleet("owner/name", "host:", ("vm:", "id:"), admin_token="t")],
        )

    def test_the_repository_defaults_to_the_actions_one(self) -> None:
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "o/r"}):
            parsed = cli.build_parser().parse_args(
                ["--host-label-prefix", "host:", "reconcile"]
            )
        self.assertEqual(parsed.repo, "o/r")

    def test_no_repository_is_a_usage_error_not_a_guess(self) -> None:
        self.assert_usage_error(["--host-label-prefix", "host:", "reconcile"])

    def test_an_empty_host_prefix_is_a_usage_error(self) -> None:
        # With no prefix every runner's labels would count as identity, and a
        # host would be any runner carrying a label equal to its name.
        self.assert_usage_error(
            ["--repo", "o/r", "--host-label-prefix", "", "reconcile"]
        )

    def assert_usage_error(self, argv: list[str]) -> None:
        env: dict[str, Any] = {}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(argv)
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
