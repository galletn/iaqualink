"""C1b — services.yaml schema parity tests.

Pre-C1b every service in `services.yaml` carried both a `target:` block
AND a `fields.entity_id: required: true` field. HA's service dispatcher
routes by `target:`, so the `entity_id` field was redundant — the
dev-tools service picker rendered both, requiring the user to fill in
the entity twice.

C1b drops the redundant `fields:` block from every service. These tests
lock the new shape in so a future contributor can't re-introduce the
duplication (e.g. by copying an old pre-C1b service def from another
integration repo).
"""

from __future__ import annotations

from pathlib import Path

import yaml


SERVICES_YAML = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "iaqualink_robots"
    / "services.yaml"
)

# The 7 services C1a registered. C1b expects each to have a `target.entity`
# block (HA 2024+ target-based dispatch) and NO `fields:` block.
EXPECTED_SERVICES = (
    "remote_forward",
    "remote_backward",
    "remote_rotate_left",
    "remote_rotate_right",
    "remote_stop",
    "add_fifteen_minutes",
    "reduce_fifteen_minutes",
)


def _load_services_yaml() -> dict:
    """Parse services.yaml with PyYAML's safe_load."""
    return yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))


def test_services_yaml_contains_all_seven_c1a_services() -> None:
    """services.yaml advertises exactly the 7 C1a-registered services."""
    services = _load_services_yaml()
    actual = set(services.keys())
    expected = set(EXPECTED_SERVICES)
    extra = actual - expected
    missing = expected - actual
    assert not missing, f"C1b: services.yaml missing services {missing}"
    assert not extra, (
        f"C1b: services.yaml advertises extra services {extra} not registered "
        "by C1a. Either remove from services.yaml or register them in "
        "vacuum.py::async_setup_entry."
    )


def test_every_service_has_target_entity_block() -> None:
    """AC #2: each service uses `target.entity` for HA 2024+ dispatch."""
    services = _load_services_yaml()
    for svc_name in EXPECTED_SERVICES:
        svc = services[svc_name]
        assert "target" in svc, (
            f"C1b: service {svc_name!r} missing `target:` block — HA's "
            "service dispatcher needs it to route by entity."
        )
        target = svc["target"]
        assert "entity" in target, (
            f"C1b: service {svc_name!r} target lacks `entity:` constraint."
        )
        assert target["entity"].get("domain") == "vacuum", (
            f"C1b: service {svc_name!r} target.entity.domain must be 'vacuum' "
            f"(got {target['entity'].get('domain')!r})."
        )
        # `integration:` filter narrows the picker to ONLY our vacuum
        # entities — guards against accidental cross-integration calls.
        assert target["entity"].get("integration") == "iaqualink_robots", (
            f"C1b: service {svc_name!r} target.entity.integration must be "
            f"'iaqualink_robots' (got {target['entity'].get('integration')!r}). "
            "Without this filter the dev-tools picker lists every vacuum on "
            "the HA instance, not just iAqualink robots."
        )


def test_no_service_carries_a_legacy_entity_id_field() -> None:
    """AC #2 + AC #1: each service has NO `fields.entity_id` (legacy shape).

    Pre-C1b every service had `fields.entity_id: required: true` alongside
    its target block — redundant duplication. The dev-tools picker
    rendered both, forcing the user to fill in entity_id twice.
    """
    services = _load_services_yaml()
    offenders: list[str] = []
    for svc_name in EXPECTED_SERVICES:
        svc = services[svc_name]
        fields = svc.get("fields") or {}
        if "entity_id" in fields:
            offenders.append(svc_name)
    assert not offenders, (
        f"C1b: services still carry a legacy `fields.entity_id` block: "
        f"{offenders}. HA 2024+ dispatch is via `target:`; the duplicate "
        "field renders as a redundant text input in the dev-tools picker. "
        "Remove the `fields:` block entirely."
    )


def test_strings_json_has_service_translations() -> None:
    """strings.json's `services:` section covers every service in services.yaml.

    AC #3: service names + descriptions exist under the `services` section
    using the modern HA format. The dev-tools picker reads these to render
    human-friendly labels.
    """
    import json

    strings_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "strings.json"
    )
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    svc_section = strings.get("services", {})
    for svc_name in EXPECTED_SERVICES:
        assert svc_name in svc_section, (
            f"C1b: strings.json missing services.{svc_name} translation block. "
            "HA's UI service picker won't have a human-friendly label."
        )
        block = svc_section[svc_name]
        for required_key in ("name", "description"):
            assert required_key in block, (
                f"C1b: strings.json::services.{svc_name} missing "
                f"`{required_key}` (got keys: {list(block.keys())})."
            )
            assert block[required_key].strip(), (
                f"C1b: strings.json::services.{svc_name}.{required_key} "
                "is empty / whitespace-only — won't render in the UI."
            )


def test_no_locale_carries_dead_fields_entity_id_translations() -> None:
    """C1b cleanup: no locale's `services.<name>.fields.entity_id` survives.

    Pre-C1b every locale carried `fields.entity_id.name` and
    `fields.entity_id.description` mirroring the now-dropped service
    field. These are dead translation strings (HA won't render them
    since the field no longer exists in services.yaml). Drift hazard;
    confirm they're stripped from every shipped locale.
    """
    import json

    trans_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "translations"
    )
    offenders: list[str] = []
    for locale_file in sorted(trans_dir.glob("*.json")):
        data = json.loads(locale_file.read_text(encoding="utf-8"))
        svc_section = data.get("services", {})
        for svc_name in EXPECTED_SERVICES:
            block = svc_section.get(svc_name, {})
            fields = block.get("fields") or {}
            if "entity_id" in fields:
                offenders.append(f"{locale_file.name}::services.{svc_name}.fields.entity_id")
    assert not offenders, (
        "C1b: dead translation entries detected:\n  " + "\n  ".join(offenders)
    )
