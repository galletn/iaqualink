"""Manifest sanity tests.

These are tier-Bronze hygiene checks. They run alongside hassfest in CI but
catch project-level concerns hassfest doesn't (e.g. the M16 aiohttp pin)
before those story PRs land.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MANIFEST = Path(__file__).parent.parent / "custom_components" / "iaqualink_robots" / "manifest.json"


@pytest.fixture(scope="module")
def manifest_data() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_has_required_fields(manifest_data: dict) -> None:
    """Every custom integration manifest must have these keys."""
    required = {"domain", "name", "version", "documentation", "codeowners", "iot_class"}
    missing = required - manifest_data.keys()
    assert not missing, f"manifest.json is missing required keys: {missing}"


def test_manifest_version_is_semver(manifest_data: dict) -> None:
    """Version string must parse as `X.Y.Z`."""
    parts = manifest_data["version"].split(".")
    assert len(parts) == 3, f"version must be X.Y.Z, got {manifest_data['version']!r}"
    for part in parts:
        assert part.isdigit(), f"version part {part!r} is not numeric"


@pytest.mark.xfail(
    reason="Story M16: aiohttp pin should be removed; HA core bundles aiohttp.",
    strict=True,
)
def test_manifest_does_not_pin_aiohttp(manifest_data: dict) -> None:
    """Custom integrations must not pin aiohttp — HA core owns it."""
    reqs = manifest_data.get("requirements", [])
    aiohttp_pins = [r for r in reqs if r.startswith("aiohttp")]
    assert not aiohttp_pins, f"manifest pins aiohttp: {aiohttp_pins}"
