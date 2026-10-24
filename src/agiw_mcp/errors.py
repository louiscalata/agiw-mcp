# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""A single refusal type for tool-level failures.

Every message is written by this package. Callers never copy exception text,
subprocess output or response bodies into a ToolError, so a refusal cannot leak
a key, a path or provider output by accident.
"""
from __future__ import annotations


class ToolError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(code)
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}
