# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Byte-for-byte transport goldens recorded before any stdio-loop extraction."""

from __future__ import annotations

import unittest

from test_support.rpc_serve_golden.support import install_cases


class RpcServeGoldenTests(unittest.TestCase):
    pass


install_cases(globals(), RpcServeGoldenTests, "streams")


if __name__ == "__main__":
    unittest.main()
