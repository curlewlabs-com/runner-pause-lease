"""Unit tests for the narrowing helpers decoded JSON passes through.

Every reader of a lease marker or an API answer relies on these to tell a shape
it can read from one it must refuse, so a helper that answered for a shape it
was not given would let a malformed record through as a valid one.
"""

from __future__ import annotations

import unittest

from runner_pause_lease.decoded import as_dict, as_list, as_str_list


class TestDecoded(unittest.TestCase):
    def test_each_helper_answers_only_for_its_own_shape(self) -> None:
        for value in (None, 3, "text", b"bytes", ("a",), {"a"}):
            with self.subTest(value=value):
                self.assertIsNone(as_dict(value))
                self.assertIsNone(as_list(value))
                self.assertIsNone(as_str_list(value))

    def test_a_dict_is_only_a_dict(self) -> None:
        self.assertEqual(as_dict({"a": 1}), {"a": 1})
        self.assertIsNone(as_dict(["a"]))

    def test_a_list_is_a_list_and_strings_only_when_every_item_is_one(self) -> None:
        self.assertEqual(as_list([1, "a"]), [1, "a"])
        self.assertEqual(as_list([]), [])
        self.assertEqual(as_str_list(["a", "b"]), ["a", "b"])
        self.assertEqual(as_str_list([]), [])
        self.assertIsNone(as_str_list(["a", 1]))
        self.assertIsNone(as_list({"a": 1}))


if __name__ == "__main__":
    unittest.main()
