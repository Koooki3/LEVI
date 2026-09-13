"""Canonical LeRobot dataset version helpers shared by Python services."""

from __future__ import annotations

import re


SUPPORTED_DATASET_VERSIONS = ("v3.1", "v3.0", "v2.1", "v2.0")


def normalize_dataset_version(value: object) -> str | None:
    """Return the supported canonical major/minor version for *value*."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.\d+)?", value.strip().lower())
    if not match:
        return None
    candidate = f"v{int(match.group(1))}.{int(match.group(2))}"
    return candidate if candidate in SUPPORTED_DATASET_VERSIONS else None


def is_dataset_v2(value: object) -> bool:
    """Return whether *value* is a supported LeRobot v2 dataset version."""
    return normalize_dataset_version(value) in {"v2.0", "v2.1"}


def is_dataset_v3(value: object) -> bool:
    """Return whether *value* is a supported LeRobot v3 dataset version."""
    return normalize_dataset_version(value) in {"v3.0", "v3.1"}
