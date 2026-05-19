"""Translation completeness + drift tests for story P7.

P7 closes two gaps that pre-P7 had no enforcement:

1. ``strings.json`` was missing the entire ``entity.*`` block — all 9
   locale files carried it independently, so the canonical schema
   never matched. A maintainer adding a new sensor / button / state
   could ship to one locale and silently skip others.
2. Three sensor-state substructures (``activity.state.*``,
   ``fan_speed.state.*``, ``status.state.*``) lived only in
   ``nl.json``. Every other locale rendered raw keys for those values
   in HA's UI.

Post-P7, ``strings.json`` is the single source of truth for every
key any locale uses, and ``test_no_locale_drift`` fails CI if any of
the 9 locale files drifts from that schema. The convention for new
keys is English fallback in the locale until a native translation
PR lands (matches how ``no_serial`` was introduced for C2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PACKAGE_DIR = (
    Path(__file__).parent.parent
    / "custom_components"
    / "iaqualink_robots"
)
STRINGS_JSON = PACKAGE_DIR / "strings.json"
TRANSLATIONS_DIR = PACKAGE_DIR / "translations"


def _flatten(obj, prefix: str = "") -> set[str]:
    """Return the set of dotted key paths reachable from ``obj``.

    Both intermediate dict keys and leaf keys are emitted, so the
    drift check fails on a missing intermediate (``entity.sensor``)
    not just on a missing leaf (``entity.sensor.activity.name``) —
    a structural difference that a leaf-only flatten would miss.
    """
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = k if not prefix else f"{prefix}.{k}"
            keys.add(path)
            keys.update(_flatten(v, path))
    return keys


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _all_locale_files() -> list[Path]:
    return sorted(TRANSLATIONS_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# AC #1 — strings.json contains the required top-level sections.
# ---------------------------------------------------------------------------


def test_strings_json_has_required_top_level_sections() -> None:
    """``strings.json`` is the canonical schema. AC #1 requires it to
    contain ``config``, ``entity``, and ``services`` at minimum (the
    ``options`` block is also present today but was already lockable
    before P7). ``exceptions`` and ``issues`` are listed in the spec
    but are deferred until custom exceptions / Repair issues are
    actually registered — see the P7 story for the scope decision.
    """
    strings = _load(STRINGS_JSON)
    required = {"config", "entity", "services"}
    missing = required - set(strings.keys())
    assert not missing, (
        f"strings.json is missing required top-level sections: "
        f"{sorted(missing)}. P7 added `entity` as the gap-closer; "
        f"`config` and `services` predate P7."
    )


def test_strings_json_entity_block_has_sensor_button_vacuum() -> None:
    """The ``entity`` block must enumerate the three platforms the
    integration ships entities for. ``vacuum`` is intentionally empty
    today (the vacuum entity uses ``_attr_name = None`` so HA renders
    just the device-registry name — see P11) but the key must exist
    so a future addition has a slot to land in.
    """
    entity = _load(STRINGS_JSON)["entity"]
    for platform in ("sensor", "button", "vacuum"):
        assert platform in entity, (
            f"strings.json.entity must contain `{platform}` (got "
            f"{sorted(entity.keys())})"
        )


def test_strings_json_has_sensor_state_translations() -> None:
    """P7 added value-translations for the three sensors that emit a
    finite enum of states — ``activity`` (cleaning/idle/...),
    ``fan_speed`` (floor_only/wall_only/walls_only/...), ``status``
    (connected/disconnected/online/offline). Pre-P7 only ``nl.json``
    had these; the other 8 locales rendered raw keys in HA's UI.
    """
    sensor = _load(STRINGS_JSON)["entity"]["sensor"]
    for key in ("activity", "fan_speed", "status"):
        assert "state" in sensor[key], (
            f"strings.json.entity.sensor.{key} must have a `state` block "
            f"so HA's frontend can localize the enum values"
        )
        assert isinstance(sensor[key]["state"], dict)
        assert sensor[key]["state"], (
            f"strings.json.entity.sensor.{key}.state must enumerate the "
            f"possible state values"
        )


# Note on "wall_only" vs "walls_only" — they are two DISTINCT cloud-side
# fan-speed modes (see CLAUDE.md). The state block must list both, not
# collapse them as a typo. The drift test below would catch a future
# "fix" that removes one.
def test_fan_speed_states_include_both_wall_and_walls_variants() -> None:
    """Regression guard: ``wall_only`` (vr/cyclobat dedicated wall
    scrubbing) and ``walls_only`` (i2d waterline-only mode) are two
    distinct cloud-side modes. They MUST both appear in the canonical
    state translations so a maintainer "fixing the typo" by removing
    one would break the affected device family's UI.

    Mirrors the parity guard in ``tests/test_vacuum.py::
    test_wall_only_and_walls_only_are_distinct_keys`` at the
    translation layer.
    """
    states = _load(STRINGS_JSON)["entity"]["sensor"]["fan_speed"]["state"]
    assert "wall_only" in states, (
        "Missing fan_speed.state.wall_only (vr/cyclobat dedicated wall-scrub)"
    )
    assert "walls_only" in states, (
        "Missing fan_speed.state.walls_only (i2d waterline-only mode)"
    )


def test_strings_json_has_already_in_progress_abort() -> None:
    """C2 review absorbed item: the HA-core ``already_in_progress``
    abort (raised when a concurrent flow already claimed the same
    ``unique_id``) had no localized translation pre-P7 — users saw the
    raw key. P7 adds an English entry to ``strings.json`` and English
    fallback to every locale.
    """
    abort = _load(STRINGS_JSON)["config"]["abort"]
    assert "already_in_progress" in abort, (
        "config.abort.already_in_progress must be defined so HA renders "
        "human-readable text instead of the raw key when a concurrent "
        "flow lands on the same unique_id"
    )


# ---------------------------------------------------------------------------
# AC #2 + AC #3 — no key drift between strings.json and any locale.
# This is the regression gate: the moment someone adds a key to
# strings.json without updating every locale (or vice versa), CI fails.
# ---------------------------------------------------------------------------


def test_at_least_nine_locale_files_present() -> None:
    """Sanity check on the fixture — if a locale file disappears we
    want a clear failure here rather than an empty parameterized
    matrix in the drift test below.
    """
    locales = _all_locale_files()
    expected_min = 9
    assert len(locales) >= expected_min, (
        f"Expected at least {expected_min} locale files under "
        f"{TRANSLATIONS_DIR}, found {len(locales)}: "
        f"{[p.name for p in locales]}"
    )


@pytest.mark.parametrize(
    "locale_path",
    _all_locale_files(),
    ids=lambda p: p.stem,
)
def test_no_locale_drift(locale_path: Path) -> None:
    """Drift gate: every locale file must have **the exact same key
    shape** as ``strings.json``. Both directions are checked — a key
    in ``strings.json`` missing from the locale fails (the user sees
    raw key text), and a key in the locale but not in ``strings.json``
    also fails (it's dead text that won't render anywhere).

    The flatten includes intermediate dict keys, so a structural
    mismatch (e.g. ``entity.sensor.activity`` exists but
    ``entity.sensor.activity.state`` is missing) surfaces clearly.
    """
    canonical = _flatten(_load(STRINGS_JSON))
    locale = _flatten(_load(locale_path))

    missing_in_locale = canonical - locale
    extra_in_locale = locale - canonical

    msg_parts: list[str] = []
    if missing_in_locale:
        msg_parts.append(
            f"keys present in strings.json but missing from "
            f"{locale_path.name} ({len(missing_in_locale)}):\n  "
            + "\n  ".join(f"- {k}" for k in sorted(missing_in_locale))
        )
    if extra_in_locale:
        msg_parts.append(
            f"keys present in {locale_path.name} but NOT in "
            f"strings.json ({len(extra_in_locale)}):\n  "
            + "\n  ".join(f"+ {k}" for k in sorted(extra_in_locale))
        )

    assert not msg_parts, (
        f"Translation drift in {locale_path.name}. Fix by either "
        f"adding the missing keys to {locale_path.name} (English "
        f"fallback is the convention for new keys) or removing the "
        f"extra keys.\n\n" + "\n\n".join(msg_parts)
    )


@pytest.mark.parametrize(
    "locale_path",
    _all_locale_files(),
    ids=lambda p: p.stem,
)
def test_locale_file_is_valid_json(locale_path: Path) -> None:
    """Cheap structural test — fast failure if a locale file is
    syntactically broken (trailing comma, unbalanced quote, etc.).
    Runs before the drift test so the error message points at the
    actual JSON parse failure rather than a confusing "key set
    mismatch" on an empty dict.
    """
    try:
        json.loads(locale_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        pytest.fail(
            f"{locale_path.name} is not valid JSON: {e}"
        )


# ---------------------------------------------------------------------------
# AC #4 — English fallback for new keys is acceptable. We only assert
# that the FALLBACK text exists, not that it's been translated, since
# native translations land via separate PRs. The drift gate above is
# what protects key-shape; this test protects key-presence at runtime.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locale_path",
    _all_locale_files(),
    ids=lambda p: p.stem,
)
def test_every_locale_has_already_in_progress_text(locale_path: Path) -> None:
    """Every locale must define ``config.abort.already_in_progress``
    as non-empty text. English fallback is acceptable per AC #4 — the
    contract is that HA renders SOMETHING human-readable, not raw
    key text, regardless of locale.
    """
    locale = _load(locale_path)
    text = locale.get("config", {}).get("abort", {}).get("already_in_progress")
    assert text, (
        f"{locale_path.name}.config.abort.already_in_progress is missing "
        f"or empty — HA will render the raw key in the UI on a concurrent-"
        f"flow abort"
    )
    assert isinstance(text, str)


@pytest.mark.parametrize(
    "locale_path",
    _all_locale_files(),
    ids=lambda p: p.stem,
)
def test_every_locale_has_sensor_state_text(locale_path: Path) -> None:
    """Every locale must define non-empty text for every state value
    in the three enum-valued sensors. English fallback is acceptable.
    """
    locale = _load(locale_path)
    sensor = locale.get("entity", {}).get("sensor", {})
    for sensor_key in ("activity", "fan_speed", "status"):
        state = sensor.get(sensor_key, {}).get("state", {})
        assert state, (
            f"{locale_path.name}.entity.sensor.{sensor_key}.state is "
            f"missing or empty"
        )
        for state_key, text in state.items():
            assert text, (
                f"{locale_path.name}.entity.sensor.{sensor_key}.state."
                f"{state_key} is empty"
            )
            assert isinstance(text, str)
