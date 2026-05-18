"""C7 + P8: legacy-domain migration tests.

The legacy stub at `custom_components/iaqualinkRobots/` migrates pre-C7
config entries (domain ``iaqualinkRobots``, camelCase) over to the new
snake_case domain ``iaqualink_robots``. The migration touches three pieces
of HA state — entity_registry ``platform`` field, device_registry
``identifiers`` tuples, and the ``ConfigEntry.domain`` attribute itself —
all of which lie outside HA's public mutation API. The tests below pin the
contract for each piece plus the surrounding control flow (Repair-issue
emission on success and on failure; idempotency; no-op when the entry
isn't legacy).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

LEGACY_DOMAIN = "iaqualinkRobots"
NEW_DOMAIN = "iaqualink_robots"


def _build_entity_registry_entry(
    *,
    entity_id: str,
    platform: str,
    config_entry_id: str,
) -> MagicMock:
    """A MagicMock that mimics an HA `RegistryEntry`.

    `RegistryEntry` is a frozen attrs/dataclass in HA; the production code
    bypasses the freeze with ``object.__setattr__`` to rewrite `platform`.
    A bare MagicMock accepts ``object.__setattr__`` natively, so the same
    call path exercises here.
    """
    ent = MagicMock()
    ent.entity_id = entity_id
    ent.platform = platform
    ent.config_entry_id = config_entry_id
    return ent


def _build_device_registry_entry(*, device_id: str, identifiers: set) -> MagicMock:
    dev = MagicMock()
    dev.id = device_id
    dev.identifiers = identifiers
    return dev


def _build_config_entry(*, entry_id: str, domain: str, title: str = "Test Robot") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.domain = domain
    entry.title = title
    return entry


def _build_hass(entity_registry: MagicMock, device_registry: MagicMock) -> MagicMock:
    """Build a MagicMock hass with `er.async_get` and `dr.async_get` patched.

    The actual patching happens in each test via ``patch.object`` so the
    fixture can return the registry mocks the test can inspect.
    """
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries._async_schedule_save = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# `_migrate_entry` — exercises each of the three migration sub-steps.
# ---------------------------------------------------------------------------


async def test_migrate_entry_rewrites_entity_platform_on_matching_entries() -> None:
    """An entity tied to the entry whose `platform` is `LEGACY_DOMAIN`
    must have its `platform` field rewritten to `NEW_DOMAIN`. The
    surrounding `ent_registry.async_schedule_save` must be called.
    """
    from custom_components.iaqualinkRobots import _migrate_entry

    target = _build_entity_registry_entry(
        entity_id="vacuum.test_robot",
        platform=LEGACY_DOMAIN,
        config_entry_id="entry-1",
    )
    unrelated = _build_entity_registry_entry(
        entity_id="sensor.other",
        platform="other_domain",
        config_entry_id="entry-1",
    )
    wrong_entry = _build_entity_registry_entry(
        entity_id="vacuum.different_robot",
        platform=LEGACY_DOMAIN,
        config_entry_id="entry-2",
    )

    ent_registry = MagicMock()
    ent_registry.entities = {
        e.entity_id: e for e in (target, unrelated, wrong_entry)
    }
    ent_registry.async_schedule_save = MagicMock()

    dev_registry = MagicMock()
    dev_registry.devices = {}

    hass = _build_hass(ent_registry, dev_registry)
    entry = _build_config_entry(entry_id="entry-1", domain=LEGACY_DOMAIN)

    with patch("custom_components.iaqualinkRobots.er.async_get", return_value=ent_registry), \
         patch("custom_components.iaqualinkRobots.dr.async_get", return_value=dev_registry):
        await _migrate_entry(hass, entry)

    # Target was rewritten.
    assert target.platform == NEW_DOMAIN
    # Other platform on same entry untouched (not legacy).
    assert unrelated.platform == "other_domain"
    # Same-platform entity on a different entry untouched.
    assert wrong_entry.platform == LEGACY_DOMAIN
    # Save was triggered because at least one mutation happened.
    ent_registry.async_schedule_save.assert_called_once()


async def test_migrate_entry_skips_entity_save_when_no_legacy_entities() -> None:
    """If no entities for this entry carry `platform == LEGACY_DOMAIN`,
    `async_schedule_save` is NOT called — defensive against unnecessary
    registry writes.
    """
    from custom_components.iaqualinkRobots import _migrate_entry

    ent = _build_entity_registry_entry(
        entity_id="sensor.modern",
        platform=NEW_DOMAIN,  # already on the new domain (shouldn't happen but defensive)
        config_entry_id="entry-1",
    )

    ent_registry = MagicMock()
    ent_registry.entities = {ent.entity_id: ent}
    ent_registry.async_schedule_save = MagicMock()

    dev_registry = MagicMock()
    dev_registry.devices = {}

    hass = _build_hass(ent_registry, dev_registry)
    entry = _build_config_entry(entry_id="entry-1", domain=LEGACY_DOMAIN)

    with patch("custom_components.iaqualinkRobots.er.async_get", return_value=ent_registry), \
         patch("custom_components.iaqualinkRobots.dr.async_get", return_value=dev_registry):
        await _migrate_entry(hass, entry)

    # Untouched — already on the new domain.
    assert ent.platform == NEW_DOMAIN
    ent_registry.async_schedule_save.assert_not_called()


async def test_migrate_entry_rewrites_device_identifiers() -> None:
    """A device whose identifiers tuple uses `LEGACY_DOMAIN` must be
    rewritten to use `NEW_DOMAIN`. The public `async_update_device` API
    is invoked (unlike entity_registry, this one HA supports cleanly).
    """
    from custom_components.iaqualinkRobots import _migrate_entry

    legacy_dev = _build_device_registry_entry(
        device_id="dev-1",
        identifiers={(LEGACY_DOMAIN, "R23X12345678")},
    )
    modern_dev = _build_device_registry_entry(
        device_id="dev-2",
        identifiers={("some_other_integration", "X")},
    )
    mixed_dev = _build_device_registry_entry(
        device_id="dev-3",
        identifiers={(LEGACY_DOMAIN, "ABC"), ("some_other_integration", "XYZ")},
    )

    dev_registry = MagicMock()
    dev_registry.devices = {d.id: d for d in (legacy_dev, modern_dev, mixed_dev)}
    dev_registry.async_update_device = MagicMock()

    ent_registry = MagicMock()
    ent_registry.entities = {}
    ent_registry.async_schedule_save = MagicMock()

    hass = _build_hass(ent_registry, dev_registry)
    entry = _build_config_entry(entry_id="entry-1", domain=LEGACY_DOMAIN)

    with patch("custom_components.iaqualinkRobots.er.async_get", return_value=ent_registry), \
         patch("custom_components.iaqualinkRobots.dr.async_get", return_value=dev_registry):
        await _migrate_entry(hass, entry)

    # legacy_dev: full rewrite.
    dev_registry.async_update_device.assert_any_call(
        "dev-1", new_identifiers={(NEW_DOMAIN, "R23X12345678")},
    )
    # mixed_dev: just the legacy half rewritten; the other one preserved.
    dev_registry.async_update_device.assert_any_call(
        "dev-3", new_identifiers={(NEW_DOMAIN, "ABC"), ("some_other_integration", "XYZ")},
    )
    # modern_dev: NOT called (no legacy identifier).
    assert dev_registry.async_update_device.call_count == 2


async def test_migrate_entry_mutates_config_entry_domain_and_schedules_save() -> None:
    """The third and final step: the entry's own `domain` field is
    rewritten and `hass.config_entries._async_schedule_save()` is called
    so the change survives a HA restart.
    """
    from custom_components.iaqualinkRobots import _migrate_entry

    ent_registry = MagicMock()
    ent_registry.entities = {}
    ent_registry.async_schedule_save = MagicMock()

    dev_registry = MagicMock()
    dev_registry.devices = {}

    hass = _build_hass(ent_registry, dev_registry)
    entry = _build_config_entry(entry_id="entry-1", domain=LEGACY_DOMAIN, title="Pool")

    with patch("custom_components.iaqualinkRobots.er.async_get", return_value=ent_registry), \
         patch("custom_components.iaqualinkRobots.dr.async_get", return_value=dev_registry):
        await _migrate_entry(hass, entry)

    assert entry.domain == NEW_DOMAIN
    hass.config_entries._async_schedule_save.assert_called_once()


# ---------------------------------------------------------------------------
# `async_setup_entry` — control flow + Repair-issue emission.
# ---------------------------------------------------------------------------


async def test_setup_entry_emits_warning_repair_on_successful_migration() -> None:
    """Happy path: migration succeeds; a WARNING-severity Repair issue
    informing the user to restart HA is emitted.
    """
    from custom_components.iaqualinkRobots import async_setup_entry

    ent_registry = MagicMock()
    ent_registry.entities = {}
    ent_registry.async_schedule_save = MagicMock()
    dev_registry = MagicMock()
    dev_registry.devices = {}

    hass = _build_hass(ent_registry, dev_registry)
    entry = _build_config_entry(entry_id="e1", domain=LEGACY_DOMAIN)

    with patch("custom_components.iaqualinkRobots.er.async_get", return_value=ent_registry), \
         patch("custom_components.iaqualinkRobots.dr.async_get", return_value=dev_registry), \
         patch("custom_components.iaqualinkRobots.ir.async_create_issue") as mock_create:
        result = await async_setup_entry(hass, entry)

    assert result is True  # entry must NOT be marked FAILED_SETUP
    mock_create.assert_called_once()
    args, kwargs = mock_create.call_args
    # Signature: ir.async_create_issue(hass, domain, issue_id, **kwargs)
    assert args[0] is hass
    assert args[1] == NEW_DOMAIN  # repair issues are namespaced under the NEW domain
    assert args[2] == "legacy_domain_migrated_restart_required"
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_key"] == "legacy_domain_migrated_restart_required"


async def test_setup_entry_emits_error_repair_when_migration_raises() -> None:
    """Migration failure path: `_migrate_entry` raising must NOT propagate.
    An ERROR-severity Repair issue is created instead, with manual-recovery
    instructions in its description. The entry still returns True so HA
    doesn't shove it into FAILED_SETUP retry-spam.
    """
    from custom_components.iaqualinkRobots import async_setup_entry

    hass = _build_hass(MagicMock(), MagicMock())
    entry = _build_config_entry(
        entry_id="e1", domain=LEGACY_DOMAIN, title="Pool Robot Alpha",
    )

    with patch(
        "custom_components.iaqualinkRobots._migrate_entry",
        new=AsyncMock(side_effect=RuntimeError("simulated registry corruption")),
    ), patch("custom_components.iaqualinkRobots.ir.async_create_issue") as mock_create:
        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_create.assert_called_once()
    args, kwargs = mock_create.call_args
    assert args[2] == "legacy_domain_migration_failed"
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_placeholders"]["entry_title"] == "Pool Robot Alpha"


async def test_setup_entry_is_noop_when_entry_is_not_legacy_domain() -> None:
    """Defensive: should never happen (HA only invokes us for our domain),
    but if some misconfiguration sends a non-legacy entry through here, we
    must NOT mutate it or emit a Repair issue.
    """
    from custom_components.iaqualinkRobots import async_setup_entry

    hass = _build_hass(MagicMock(), MagicMock())
    entry = _build_config_entry(entry_id="e1", domain="some_other_domain")

    with patch("custom_components.iaqualinkRobots._migrate_entry") as mock_migrate, \
         patch("custom_components.iaqualinkRobots.ir.async_create_issue") as mock_create:
        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_migrate.assert_not_called()
    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Manifest + module sanity (cheap static checks).
# ---------------------------------------------------------------------------


def test_legacy_stub_manifest_declares_legacy_domain() -> None:
    """The legacy stub's manifest must declare `iaqualinkRobots` so HA
    matches existing config entries to it.
    """
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).parent.parent / "custom_components" / "iaqualinkRobots"
         / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["domain"] == "iaqualinkRobots"
    assert manifest.get("config_flow") is False, (
        "Legacy stub must not advertise config_flow; new entries should "
        "only go under the new domain."
    )


def test_new_domain_manifest_declares_snake_case_domain() -> None:
    """The new-domain manifest must declare `iaqualink_robots` (snake_case)
    — the whole point of C7.
    """
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).parent.parent / "custom_components" / "iaqualink_robots"
         / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["domain"] == "iaqualink_robots"


def test_strings_json_contains_both_legacy_domain_issue_keys() -> None:
    """The two Repair-issue translation keys are present in `strings.json`
    so HA can render them. Locks the C7 + P8 contract: any future commit
    that drops a key without dropping the corresponding `ir.async_create_issue`
    call fails CI here.
    """
    import json
    from pathlib import Path

    strings = json.loads(
        (Path(__file__).parent.parent / "custom_components" / "iaqualink_robots"
         / "strings.json").read_text(encoding="utf-8")
    )
    issues = strings.get("issues", {})
    assert "legacy_domain_migrated_restart_required" in issues
    assert "legacy_domain_migration_failed" in issues
    assert "title" in issues["legacy_domain_migrated_restart_required"]
    assert "description" in issues["legacy_domain_migrated_restart_required"]
    assert "title" in issues["legacy_domain_migration_failed"]
    assert "description" in issues["legacy_domain_migration_failed"]


def test_all_locale_translations_carry_legacy_domain_issue_keys() -> None:
    """Every shipped locale (cs / de / en / es / fr / it / nl / pt / sk)
    has the two issue translation keys. English fallback is the convention
    for non-natively-translated strings (matches the C2 / `no_serial`
    precedent); native translations welcome via PR.
    """
    import json
    from pathlib import Path

    translations_dir = (
        Path(__file__).parent.parent / "custom_components" / "iaqualink_robots"
        / "translations"
    )
    missing: list[str] = []
    for path in sorted(translations_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        issues = data.get("issues", {})
        for key in (
            "legacy_domain_migrated_restart_required",
            "legacy_domain_migration_failed",
        ):
            if key not in issues:
                missing.append(f"{path.name}::{key}")
    assert not missing, (
        f"Missing Repair-issue translation key(s) in locale file(s): {missing}"
    )


# pytest signature warnings on async_def without asyncio mark — `asyncio_mode`
# is set to `"auto"` in pyproject.toml so these run without the marker.
_ = pytest  # silence "imported but unused" if pytest is otherwise unused
