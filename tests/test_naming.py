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

# Note: IAquaLinkRobotVacuum is intentionally NOT imported — the P11 review
# follow-up moved the 3 vacuum tests to AST-based inspection because HA's
# Entity base class wraps `_attr_*` as property descriptors at class-init
# time, making class-level access return the descriptor not the value.


# ---------------------------------------------------------------------------
# has_entity_name parity — every entity class must opt in.
# ---------------------------------------------------------------------------


# Distinct sentinel for "attribute not declared in the class body". Using
# `None` instead would conflict with the legitimate `_attr_name = None`
# class-body assignment that some entities use. Module-level so both
# AST-inspection helpers below share the same identity.
_NOT_FOUND = object()


def _class_body_attr_value(source_path: Path, class_name: str, attr_name: str):
    """Walk the class body in ``source_path`` and return the assigned constant
    for ``ClassName.attr_name = <value>`` declarations.

    Returns the literal value (bool / None / str / int), ``_NOT_FOUND``
    if the attribute is not assigned in the class body, or a string
    diagnostic if the value is a non-constant expression. Skips
    assignments inside method bodies (those are instance-level).

    Handles BOTH ``ast.Assign`` (plain `_attr_x = True`) and
    ``ast.AnnAssign`` (annotated `_attr_x: bool = True`) — the latter is
    common with `mypy --strict` and would silently slip through a
    plain-Assign-only scan, producing a misleading "attribute not declared"
    failure message even when the contract IS met (review follow-up).

    HA's recent ``Entity`` base class wraps several ``_attr_*`` attributes
    as property descriptors at class-init time. A direct
    ``Class._attr_has_entity_name`` access returns the parent's descriptor,
    not the subclass's class-body value — which makes ``assert ... is True``
    fail even though the assignment IS in the subclass body and works at
    runtime. AST parsing inspects the source directly, bypassing the
    descriptor.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for body_node in node.body:
                # Plain assignment: `_attr_x = True`
                if isinstance(body_node, ast.Assign):
                    for target in body_node.targets:
                        if isinstance(target, ast.Name) and target.id == attr_name:
                            if isinstance(body_node.value, ast.Constant):
                                return body_node.value.value
                            return f"<non-constant: {ast.dump(body_node.value)}>"
                # Annotated assignment: `_attr_x: bool = True`
                elif isinstance(body_node, ast.AnnAssign):
                    target = body_node.target
                    if isinstance(target, ast.Name) and target.id == attr_name:
                        if body_node.value is None:
                            # `_attr_x: bool` with no value — type hint only,
                            # not an assignment. Skip.
                            continue
                        if isinstance(body_node.value, ast.Constant):
                            return body_node.value.value
                        return f"<non-constant: {ast.dump(body_node.value)}>"
    return _NOT_FOUND


def test_button_class_sets_has_entity_name_true() -> None:
    """AqualinkRemoteButton declares `_attr_has_entity_name = True` (class-body assignment).

    HA wraps the inherited ``_attr_has_entity_name`` as a property
    descriptor at class-init time, so a class-level access via
    ``AqualinkRemoteButton._attr_has_entity_name`` returns the parent's
    descriptor (not our subclass value). Inspecting the AST is the
    descriptor-resistant way to assert the contract.
    """
    val = _class_body_attr_value(_COMP / "button.py", "AqualinkRemoteButton", "_attr_has_entity_name")
    assert val is True, (
        "P11: AqualinkRemoteButton class body must declare "
        f"`_attr_has_entity_name = True`; got {val!r}"
    )


def test_vacuum_class_sets_has_entity_name_true() -> None:
    """IAquaLinkRobotVacuum declares `_attr_has_entity_name = True` (class-body assignment)."""
    val = _class_body_attr_value(_COMP / "vacuum.py", "IAquaLinkRobotVacuum", "_attr_has_entity_name")
    assert val is True, (
        "P11: IAquaLinkRobotVacuum class body must declare "
        f"`_attr_has_entity_name = True`; got {val!r}"
    )


def test_sensor_instance_sets_has_entity_name_true() -> None:
    """AqualinkSensor sets `_attr_has_entity_name = True` in __init__."""
    # Sensor's value is set per-instance (not class attr), so check the source.
    src = inspect.getsource(AqualinkSensor.__init__)
    assert "self._attr_has_entity_name = True" in src


# ---------------------------------------------------------------------------
# Vacuum-specific: _attr_name = None so the friendly name is just the device.
# ---------------------------------------------------------------------------


def test_vacuum_attr_name_is_none() -> None:
    """The vacuum is the primary entity for the device — name = device name only.

    AST check rather than direct class attribute lookup because HA's Entity
    base class wraps ``_attr_name`` as a property descriptor at class-init
    time (recent versions). The class-body source IS the contract.
    """
    val = _class_body_attr_value(_COMP / "vacuum.py", "IAquaLinkRobotVacuum", "_attr_name")
    # `_NOT_FOUND` is a sentinel; `None` is the explicit class-body value
    # we DO want. Distinguish "attribute not declared" from "declared as None".
    assert val is not _NOT_FOUND, (
        "P11: IAquaLinkRobotVacuum class body must declare `_attr_name = None` "
        "so HA renders the friendly name as just the device-registry name."
    )
    assert val is None, (
        f"P11: IAquaLinkRobotVacuum._attr_name must be None (got {val!r}). "
        "Non-None values would append a translation-key suffix to the device name."
    )


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
