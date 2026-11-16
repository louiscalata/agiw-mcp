# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Malformed replies, early refusals and transport failure observations."""

from __future__ import annotations

import unittest

from test_jev_analyse_golden import install_cases


class JevAnalyseFailureGoldenTests(unittest.TestCase):
    """Preserve error payloads and the order of side effects on failures."""


def load_tests(loader, standard_tests, pattern):
    install_cases(JevAnalyseFailureGoldenTests, "failures")
    return loader.loadTestsFromTestCase(JevAnalyseFailureGoldenTests)


if __name__ == "__main__":
    unittest.main()
