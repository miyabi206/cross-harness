"""Runner-independent isolation from a cross-harness environment."""

import os
import unittest


for name in list(os.environ):
    if name.startswith("CROSS_HARNESS_"):
        del os.environ[name]


class CrossHarnessEnvironmentIsolationTests(unittest.TestCase):
    def test_cross_harness_variables_are_absent(self) -> None:
        self.assertFalse(
            [name for name in os.environ if name.startswith("CROSS_HARNESS_")]
        )
