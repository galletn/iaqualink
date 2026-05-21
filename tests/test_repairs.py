"""Repairs-flow tests for story P8.

Story P8 raises a one-shot HA Repairs notification ("legacy service
behaviour change") when an upgrading user has automations or scripts
calling any of the seven previously-broken services
(``remote_forward`` / ``remote_backward`` / ``remote_rotate_left`` /
``remote_rotate_right`` / ``remote_stop`` / ``add_fifteen_minutes`` /
``reduce_fifteen_minutes``). Story C1a fixed the registration so they
now actually move the robot — the issue tells the user to verify their
automations still do what they intended.

Acceptance criteria coverage:

* AC #1 — issue is raised when at least one automation or script
  references a legacy service. ``test_issue_raised_when_automation_references_legacy_service``,
  ``test_issue_raised_when_script_references_legacy_service``,
  ``test_issue_raised_when_action_keyword_used_instead_of_service``.
* AC #2 — the fix flow marks the per-entry flag and lets HA delete the
  issue. ``test_fix_flow_marks_notified_flag_and_finishes``,
  ``test_issue_not_raised_after_flag_set``.
* AC #3 — no issue when no relevant automation references exist.
  ``test_no_issue_when_no_automations``,
  ``test_no_issue_when_unrelated_service_referenced``.
* AC #4 — forward-cleanup contract is documented at
  ``async_migrate_entry`` and the flag + issue-id constants are stable
  for a future-version migration to delete. ``test_constants_are_stable``.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iaqualink_robots import (
    _contains_legacy_service_ref,
    _async_check_legacy_service_repair,
    _scan_for_legacy_service_calls,
)
from custom_components.iaqualink_robots.const import DOMAIN
from custom_components.iaqualink_robots.repairs import (
    LEGACY_SERVICE_REPAIR_FLAG,
    LEGACY_SERVICE_REPAIR_ISSUE_ID,
    LegacyServiceBehaviorChangeFixFlow,
    async_create_fix_flow,
)

from tests.const import MOCK_ENTRY_DATA, MOCK_NAME


# ---------------------------------------------------------------------------
# Helpers: a minimal stand-in for HA's EntityComponent + AutomationEntity /
# ScriptEntity that exposes only the surface our scanner reads
# (`.entities` iterable, each entry has a `.raw_config` dict).
# ---------------------------------------------------------------------------


class _FakeAutomationEntity:
    """Minimal entity stub exposing the ``raw_config`` attribute the scan reads."""

    def __init__(self, raw_config: dict[str, Any] | None) -> None:
        self.raw_config = raw_config


class _FakeComponent:
    """Stand-in for ``hass.data['automation']`` / ``hass.data['script']``."""

    def __init__(self, entities: list[_FakeAutomationEntity]) -> None:
        self.entities = entities


def _install_fake_automations(
    hass: HomeAssistant, raw_configs: list[dict[str, Any] | None]
) -> None:
    """Place a fake automation component on ``hass.data`` so the scanner
    walks the supplied raw_configs. Removes any leftover script component
    so the test only exercises the automation path unless explicitly set
    by ``_install_fake_scripts``.
    """
    hass.data["automation"] = _FakeComponent(
        [_FakeAutomationEntity(rc) for rc in raw_configs]
    )
    hass.data.pop("script", None)


def _install_fake_scripts(
    hass: HomeAssistant, raw_configs: list[dict[str, Any] | None]
) -> None:
    """Place a fake script component on ``hass.data`` (without an
    automation component) so the test exercises the script branch
    independently from automations.
    """
    hass.data["script"] = _FakeComponent(
        [_FakeAutomationEntity(rc) for rc in raw_configs]
    )
    hass.data.pop("automation", None)


def _legacy_automation_config() -> dict[str, Any]:
    """Realistic-shaped automation config calling ``remote_forward``."""
    return {
        "alias": "Nudge robot at 8 AM",
        "trigger": {"platform": "time", "at": "08:00:00"},
        "action": [
            {
                "service": f"{DOMAIN}.remote_forward",
                "target": {"entity_id": "vacuum.pool"},
            }
        ],
    }


def _legacy_automation_config_action_keyword() -> dict[str, Any]:
    """HA 2024+ accepts ``action: domain.service`` as a synonym for
    ``service: domain.service`` inside the action block. The scanner has
    to recognise both.
    """
    return {
        "alias": "Add 15m when motion ends",
        "trigger": {"platform": "state", "entity_id": "binary_sensor.motion"},
        "action": [{"action": f"{DOMAIN}.add_fifteen_minutes"}],
    }


# ---------------------------------------------------------------------------
# Detection helper tests — pure-function checks, no hass needed.
# ---------------------------------------------------------------------------


def test_contains_legacy_service_ref_finds_classic_service_form() -> None:
    """The walker matches ``service: <domain>.<name>`` in the action block."""
    assert _contains_legacy_service_ref(_legacy_automation_config()) is True


def test_contains_legacy_service_ref_finds_action_keyword_form() -> None:
    """HA's newer ``action:`` keyword is treated equivalently to ``service:``."""
    assert _contains_legacy_service_ref(
        _legacy_automation_config_action_keyword()
    ) is True


def test_contains_legacy_service_ref_walks_nested_choose_then_blocks() -> None:
    """``choose``/``then``/``parallel`` blocks nest service calls arbitrarily
    deep — the scanner must recurse into lists and nested dicts to find
    references buried under a ``choose`` branch.
    """
    config = {
        "alias": "Conditional remote_stop",
        "trigger": {"platform": "event", "event_type": "x"},
        "action": [
            {
                "choose": [
                    {
                        "conditions": [{"condition": "state", "entity_id": "x", "state": "on"}],
                        "sequence": [
                            {"service": "light.turn_off", "target": {}},
                            {
                                "parallel": [
                                    {"service": f"{DOMAIN}.remote_stop"}
                                ]
                            },
                        ],
                    }
                ],
                "default": [{"service": "notify.persistent_notification"}],
            }
        ],
    }
    assert _contains_legacy_service_ref(config) is True


def test_contains_legacy_service_ref_ignores_unrelated_services() -> None:
    """A config that only calls non-iaqualink services must not match."""
    config = {
        "action": [
            {"service": "light.turn_on"},
            {"service": "vacuum.start", "target": {"entity_id": "vacuum.x"}},
            {"action": "media_player.play_media"},
        ]
    }
    assert _contains_legacy_service_ref(config) is False


def test_contains_legacy_service_ref_handles_empty_and_non_mapping_inputs() -> None:
    """The walker must tolerate degenerate inputs (None, str, int, empty
    dict / list) without raising — automation entities sometimes have
    ``raw_config = None`` until first load completes.
    """
    for sample in (None, "", 0, [], {}, "iaqualink_robots.remote_forward"):
        assert _contains_legacy_service_ref(sample) is False


@pytest.mark.parametrize(
    "service_name",
    [
        "remote_forward",
        "remote_backward",
        "remote_rotate_left",
        "remote_rotate_right",
        "remote_stop",
        "add_fifteen_minutes",
        "reduce_fifteen_minutes",
    ],
)
def test_contains_legacy_service_ref_matches_every_legacy_service(
    service_name: str,
) -> None:
    """Every one of the seven legacy services must be detected — a future
    change that adds an eighth service should also append it to the
    detection set in ``__init__._P8_LEGACY_SERVICES``.
    """
    config = {"action": [{"service": f"{DOMAIN}.{service_name}"}]}
    assert _contains_legacy_service_ref(config) is True


# ---------------------------------------------------------------------------
# Scan helper tests — exercise ``hass.data`` lookup paths.
# ---------------------------------------------------------------------------


async def test_scan_returns_false_when_no_automation_or_script_components(
    hass: HomeAssistant,
) -> None:
    """If neither component has loaded, the scan must be a clean False —
    no exception, no false positive. Mirrors the "user dismissed the
    automation integration entirely" case.
    """
    hass.data.pop("automation", None)
    hass.data.pop("script", None)
    assert _scan_for_legacy_service_calls(hass) is False


async def test_scan_returns_true_when_automation_references_legacy_service(
    hass: HomeAssistant,
) -> None:
    _install_fake_automations(hass, [_legacy_automation_config()])
    assert _scan_for_legacy_service_calls(hass) is True


async def test_scan_returns_true_when_script_references_legacy_service(
    hass: HomeAssistant,
) -> None:
    _install_fake_scripts(
        hass,
        [
            {
                "alias": "Nudge robot",
                "sequence": [{"service": f"{DOMAIN}.remote_forward"}],
            }
        ],
    )
    assert _scan_for_legacy_service_calls(hass) is True


async def test_scan_returns_false_when_only_unrelated_services_referenced(
    hass: HomeAssistant,
) -> None:
    """AC #3: a user who has automations but none of them call our legacy
    services must not see the issue.
    """
    _install_fake_automations(
        hass,
        [
            {"action": [{"service": "light.turn_on"}]},
            {"action": [{"service": "vacuum.start"}]},
        ],
    )
    assert _scan_for_legacy_service_calls(hass) is False


async def test_scan_tolerates_entity_iteration_failure(hass: HomeAssistant) -> None:
    """A misbehaving automation entity that raises during iteration must
    be swallowed and treated as "no match" rather than aborting setup.
    The scan is best-effort by design.
    """

    class _BadEntities:
        def __iter__(self) -> Any:
            raise RuntimeError("simulated internal error")

    class _BadComponent:
        entities = _BadEntities()

    hass.data["automation"] = _BadComponent()
    hass.data.pop("script", None)

    assert _scan_for_legacy_service_calls(hass) is False


# ---------------------------------------------------------------------------
# Issue-raising tests — exercise the full async flow including the
# entry.data gate and the issue-registry effect.
# ---------------------------------------------------------------------------


def _make_entry(hass: HomeAssistant, *, notified: bool = False) -> MockConfigEntry:
    data = dict(MOCK_ENTRY_DATA)
    if notified:
        data[LEGACY_SERVICE_REPAIR_FLAG] = True
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        title=MOCK_NAME,
        unique_id=data["serial_number"],
    )
    entry.add_to_hass(hass)
    return entry


async def test_issue_raised_when_automation_references_legacy_service(
    hass: HomeAssistant,
) -> None:
    """AC #1: legacy service reference + unset flag => issue is created."""
    entry = _make_entry(hass)
    _install_fake_automations(hass, [_legacy_automation_config()])

    await _async_check_legacy_service_repair(hass, entry)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_SERVICE_REPAIR_ISSUE_ID)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_key == LEGACY_SERVICE_REPAIR_ISSUE_ID


async def test_issue_raised_when_script_references_legacy_service(
    hass: HomeAssistant,
) -> None:
    """AC #1: a script (not just an automation) calling a legacy service
    also triggers the issue.
    """
    entry = _make_entry(hass)
    _install_fake_scripts(
        hass, [{"sequence": [{"service": f"{DOMAIN}.add_fifteen_minutes"}]}]
    )

    await _async_check_legacy_service_repair(hass, entry)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_SERVICE_REPAIR_ISSUE_ID)
        is not None
    )


async def test_issue_raised_when_action_keyword_used_instead_of_service(
    hass: HomeAssistant,
) -> None:
    """AC #1: HA 2024+ ``action: domain.name`` form is equivalent."""
    entry = _make_entry(hass)
    _install_fake_automations(
        hass, [_legacy_automation_config_action_keyword()]
    )

    await _async_check_legacy_service_repair(hass, entry)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_SERVICE_REPAIR_ISSUE_ID)
        is not None
    )


async def test_no_issue_when_no_automations(hass: HomeAssistant) -> None:
    """AC #3: no automation / script component loaded => no issue."""
    entry = _make_entry(hass)
    hass.data.pop("automation", None)
    hass.data.pop("script", None)

    await _async_check_legacy_service_repair(hass, entry)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_SERVICE_REPAIR_ISSUE_ID)
        is None
    )


async def test_no_issue_when_unrelated_service_referenced(
    hass: HomeAssistant,
) -> None:
    """AC #3: automations exist but none reference our services => no issue."""
    entry = _make_entry(hass)
    _install_fake_automations(
        hass, [{"action": [{"service": "vacuum.start"}]}]
    )

    await _async_check_legacy_service_repair(hass, entry)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_SERVICE_REPAIR_ISSUE_ID)
        is None
    )


async def test_issue_not_raised_after_flag_set(hass: HomeAssistant) -> None:
    """AC #2 (gate): a previously-notified entry never re-fires the issue
    even when the user re-adds an automation calling a legacy service —
    the flag is sticky until the next-major-version migration clears it.
    """
    entry = _make_entry(hass, notified=True)
    _install_fake_automations(hass, [_legacy_automation_config()])

    await _async_check_legacy_service_repair(hass, entry)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_SERVICE_REPAIR_ISSUE_ID)
        is None
    )


# ---------------------------------------------------------------------------
# Repairs flow tests — exercise the LegacyServiceBehaviorChangeFixFlow.
# ---------------------------------------------------------------------------


async def test_async_create_fix_flow_returns_correct_flow(hass: HomeAssistant) -> None:
    flow = await async_create_fix_flow(
        hass, LEGACY_SERVICE_REPAIR_ISSUE_ID, data=None
    )
    assert isinstance(flow, LegacyServiceBehaviorChangeFixFlow)


async def test_async_create_fix_flow_raises_for_unknown_issue(
    hass: HomeAssistant,
) -> None:
    """Defensive guard: an unknown issue_id surfaces a clear error rather
    than constructing the wrong flow. Future repairs flows must extend
    the dispatcher explicitly.
    """
    with pytest.raises(NotImplementedError):
        await async_create_fix_flow(hass, "some-other-issue", data=None)


async def test_fix_flow_marks_notified_flag_and_finishes(hass: HomeAssistant) -> None:
    """AC #2: completing the fix flow flips the entry-data flag (so the
    issue does not re-fire on next restart) and returns a create-entry
    result (which lets HA auto-delete the issue from the dashboard).
    """
    entry = _make_entry(hass)
    assert entry.data.get(LEGACY_SERVICE_REPAIR_FLAG) is not True

    flow = LegacyServiceBehaviorChangeFixFlow()
    flow.hass = hass

    # First call — show the confirm form (user hasn't clicked Submit yet).
    result = await flow.async_step_init()
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "confirm"

    # Second call with user_input — record dismissal.
    result = await flow.async_step_confirm(user_input={})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    # The entry flag is now set; future checks will short-circuit.
    assert entry.data.get(LEGACY_SERVICE_REPAIR_FLAG) is True


async def test_fix_flow_marks_every_entry_when_user_has_multiple_robots(
    hass: HomeAssistant,
) -> None:
    """Multi-entry users see ONE shared issue (HA's registry is keyed
    on domain + issue_id). The flow flips the flag on every entry so a
    future setup of any of them does not re-fire.
    """
    e1 = _make_entry(hass)
    e2 = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_ENTRY_DATA, "serial_number": "R23X87654321"},
        title="Second Robot",
        unique_id="R23X87654321",
    )
    e2.add_to_hass(hass)

    flow = LegacyServiceBehaviorChangeFixFlow()
    flow.hass = hass
    await flow.async_step_confirm(user_input={})

    assert e1.data.get(LEGACY_SERVICE_REPAIR_FLAG) is True
    assert e2.data.get(LEGACY_SERVICE_REPAIR_FLAG) is True


# ---------------------------------------------------------------------------
# Constants — AC #4 hook for the next-major-version migration.
# ---------------------------------------------------------------------------


def test_constants_are_stable() -> None:
    """AC #4: the next-major-version migration block in
    ``async_migrate_entry`` will import these constants to delete the
    issue and strip the flag. Renaming them silently is a behaviour
    change for any user who never dismissed the issue — pin both names.
    """
    assert LEGACY_SERVICE_REPAIR_ISSUE_ID == "legacy_service_behavior_change"
    assert LEGACY_SERVICE_REPAIR_FLAG == "p8_legacy_service_notified"
