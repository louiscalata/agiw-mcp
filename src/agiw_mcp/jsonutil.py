# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Shared callbacks for strict JSON decoding."""

from __future__ import annotations

from typing import NoReturn


def unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object while refusing its first duplicate member name."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_nonfinite(_value: str) -> NoReturn:
    """Refuse a JSON decoder extension for NaN or infinity."""
    raise ValueError("non-finite number")
