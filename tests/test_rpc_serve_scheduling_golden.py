# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Saved worker, cancellation, progress, pending-work, and teardown transcripts."""

from __future__ import annotations

import unittest

from test_support.rpc_serve_golden.support import install_cases


class RpcServeSchedulingGoldenTests(unittest.TestCase):
    pass


install_cases(globals(), RpcServeSchedulingGoldenTests, "scheduling")


if __name__ == "__main__":
    unittest.main()
