"""Manifest sanity tests.

These are tier-Bronze hygiene checks. They run alongside hassfest in CI but
catch project-level concerns hassfest doesn't (e.g. the M16 aiohttp pin)
before those story PRs land.
"""

from __future__ import annotations

import json
import re
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


# SemVer 2.0 grammar excluding build metadata (the ``+<build>`` suffix). HACS
# reads the pre-release flag from the GitHub release, but the manifest itself
# follows SemVer so HA's device-card "version" display surfaces a sensible
# ``X.Y.Z`` or ``X.Y.Z-beta.N`` string. Build metadata is omitted from the
# grammar because no current release uses it — extend the regex if that
# changes.
_SEMVER_RE = re.compile(
    r"^"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>"
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"$"
)


def test_manifest_version_is_semver(manifest_data: dict) -> None:
    """Version string must parse as SemVer 2.0: ``X.Y.Z[-pre[.id…]]``.

    Pre-release suffixes (e.g. ``3.0.0-beta.1``) are accepted: the project
    cuts pre-release builds for beta testing (HACS's "Show beta versions"
    toggle gates them off the default channel). Plain ``X.Y.Z`` continues
    to pass; the test catches malformed strings like ``"3.0"``, ``"3.0.0.1"``,
    or trailing dashes from copy-paste accidents.
    """
    version = manifest_data["version"]
    assert _SEMVER_RE.match(version), (
        f"version must be SemVer 2.0 (X.Y.Z[-pre]), got {version!r}"
    )


@pytest.mark.xfail(
    reason="Story M16: aiohttp pin should be removed; HA core bundles aiohttp.",
    strict=True,
)
def test_manifest_does_not_pin_aiohttp(manifest_data: dict) -> None:
    """Custom integrations must not pin aiohttp — HA core owns it."""
    reqs = manifest_data.get("requirements", [])
    aiohttp_pins = [r for r in reqs if r.startswith("aiohttp")]
    assert not aiohttp_pins, f"manifest pins aiohttp: {aiohttp_pins}"
