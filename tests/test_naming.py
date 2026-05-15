"""Story P11 — entity-naming parity tests.

Locks in the HA 2024+ entity-naming contract for every entity class:

- `_attr_has_entity_name = True` (so HA composes the friendly name from
  the device name + a translation_key-driven suffix).
- No legacy `name` property defined on the class (would override the
  translation system).
- Button entities set `_attr_translation_key` (so the localized button
  name actually fires); the previous code commented this out and a
  hardcoded English name property took precedence — issue #79.
- Vacuum entity sets `_attr_name = None` (so the friendly name is just
  the device name, no English suffix appended).

These tests are pure-Python — no HA fixtures required. They inspect the
class objects directly.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from custom_components.iaqualink_robots.button import AqualinkRemoteButton
from custom_components.iaqualink_robots.sensor import AqualinkSensor
from custom_components.iaqualink_robots.vacuum import IAquaLinkRobotVacuum


# ---------------------------------------------------------------------------
# has_entity_name parity — every entity class must opt in.
# ---------------------------------------------------------------------------


def test_button_class_sets_has_entity_name_true() -> None:
    """AqualinkRemoteButton declares `_attr_has_entity_name = True` (class attr)."""
    assert AqualinkRemoteButton._attr_has_entity_name is True


def test_vacuum_class_sets_has_entity_name_true() -> None:
    """IAquaLinkRobotVacuum declares `_attr_has_entity_name = True` (class attr)."""
    assert IAquaLinkRobotVacuum._attr_has_entity_name is True


def test_sensor_instance_sets_has_entity_name_true() -> None:
    """AqualinkSensor sets `_attr_has_entity_name = True` in __init__."""
    # Sensor's value is set per-instance (not class attr), so check the source.
    src = inspect.getsource(AqualinkSensor.__init__)
    assert "self._attr_has_entity_name = True" in src


# ---------------------------------------------------------------------------
# Vacuum-specific: _attr_name = None so the friendly name is just the device.
# ---------------------------------------------------------------------------


def test_vacuum_attr_name_is_none() -> None:
    """The vacuum is the primary entity for the device — name = device name only."""
    assert IAquaLinkRobotVacuum._attr_name is None


# ---------------------------------------------------------------------------
# Button-specific: translation_key wired up (not commented out anymore).
# ---------------------------------------------------------------------------


def test_button_init_sets_translation_key() -> None:
    """Button __init__ assigns _attr_translation_key from the constructor arg.

    Pre-P11 this assignment was commented out and a hardcoded English `name`
    property bypassed the translations. The button localized name was dead
    in all 9 locales as a result.
    """
    src = inspect.getsource(AqualinkRemoteButton.__init__)
    assert "self._attr_translation_key = translation_key" in src
    # And the line is not commented out — the leading content of the
    # statement must be the assignment, not `#`.
    for line in src.splitlines():
        stripped = line.strip()
        if "_attr_translation_key" in stripped and "=" in stripped:
            assert not stripped.startswith("#"), (
                f"P11: _attr_translation_key assignment is commented out: {stripped!r}"
            )


# ---------------------------------------------------------------------------
# No-legacy-name-property guard — covers all three entity files.
# ---------------------------------------------------------------------------


def _has_name_property(source_path: Path, class_name: str) -> bool:
    """Return True if `class_name` defines a `name` property in `source_path`."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for body_node in node.body:
                if not isinstance(body_node, ast.FunctionDef):
                    continue
                if body_node.name != "name":
                    continue
                # Must have @property decorator for it to be the legacy override.
                for dec in body_node.decorator_list:
                    if isinstance(dec, ast.Name) and dec.id == "property":
                        return True
    return False


_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMP = _REPO_ROOT / "custom_components" / "iaqualink_robots"


def test_button_has_no_legacy_name_property() -> None:
    """AqualinkRemoteButton must not define a legacy `name` @property override."""
    assert not _has_name_property(_COMP / "button.py", "AqualinkRemoteButton"), (
        "P11: legacy `name` @property on AqualinkRemoteButton would override "
        "the translation_key system — remove it and let HA compose the name."
    )


def test_vacuum_has_no_legacy_name_property() -> None:
    """IAquaLinkRobotVacuum must not define a legacy `name` @property override."""
    assert not _has_name_property(_COMP / "vacuum.py", "IAquaLinkRobotVacuum"), (
        "P11: legacy `name` @property on IAquaLinkRobotVacuum would override "
        "the `_attr_name = None` + `_attr_has_entity_name = True` pair."
    )


def test_sensor_has_no_legacy_name_property() -> None:
    """AqualinkSensor must not define a legacy `name` @property override."""
    assert not _has_name_property(_COMP / "sensor.py", "AqualinkSensor"), (
        "P11: legacy `name` @property on AqualinkSensor would override "
        "the translation_key system."
    )


# ---------------------------------------------------------------------------
# Localized-button-name regression guard — pin that every locale carries a
# non-empty translation for at least one canonical button key. If a locale
# regresses to English-fallback-or-missing the test fails noisily.
# ---------------------------------------------------------------------------


_BUTTON_TRANSLATION_KEYS = (
    "remote_forward",
    "remote_backward",
    "remote_rotate_left",
    "remote_rotate_right",
    "remote_stop",
    "add_fifteen_minutes",
    "reduce_fifteen_minutes",
)


def test_every_locale_has_button_translations() -> None:
    """Every locale has a non-empty `entity.button.<key>.name` for all 7 buttons.

    P11 wired the button translation_key through HA's lookup chain; this
    parity test guards against any future P7 / translation-cleanup work that
    might drop a per-locale key. Originally only `remote_forward` was
    checked — the P11 review widened the guard to all 7 buttons so a
    silent regression on (e.g.) `remote_stop` in Dutch can't slip through.
    """
    import json

    trans_dir = _COMP / "translations"
    locale_files = sorted(trans_dir.glob("[a-z][a-z].json"))
    assert locale_files, "no locale files found — translation dir layout regression"

    for f in locale_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        button_section = data.get("entity", {}).get("button", {})
        for key in _BUTTON_TRANSLATION_KEYS:
            name = button_section.get(key, {}).get("name")
            assert isinstance(name, str) and name.strip(), (
                f"{f.name}: missing or empty entity.button.{key}.name "
                f"— translation regression"
            )
