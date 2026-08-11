"""Fan-speed lists narrowed by the cloud's `/features` capability list.

Issue #76 removed a non-functional "Walls only" option from cyclonext by
hardcoding that family's list. The same failure remained for `vr`: the vr
list is a family-wide upper bound, but capability varies *within* the
family. Of two live vr robots captured for this work, only one advertised
`preset_waterline` — the other was still being offered "Wall only", which
the cloud silently ignores (exactly vinyamar's report on #76: "I can still
change the fan_speed attribute on HA... but the device ignores it").

The feature names here come from four live captures:

    RA 6570 iQ  (vr)        quick_clean, preset_deep_ultra, smart_cycle
    other vr    (vr)        + preset_waterline, custom_mode, spot_clean,
                              water_temp
    RE 4600 iQ  (cyclonext) quick_clean, preset_deep_ultra
    CNX 50 iQ   (cyclonext) quick_clean, preset_deep_ultra

Note `durations` is NOT usable for this: both cyclonext units report a
`waterTim` duration while advertising no waterline preset. Only `features`
describes support.
"""

from __future__ import annotations

import pytest

from custom_components.iaqualink_robots.vacuum import (
    _FAN_SPEED_REQUIRED_FEATURE,
    _narrow_fan_speeds_to_features,
)

VR_FULL = ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"]
CYCLONEXT_FULL = ["floor_only", "floor_and_walls"]

# Verbatim from the live captures.
FEATURES_RA_6570 = [
    "full_canister", "lift_system", "mode_info", "next_schedulebanner",
    "preset_deep_ultra", "quick_clean", "remote_control", "remote_motion",
    "schedule_mode", "schedule_time", "smart_cycle", "start_stop", "status",
    "time_adjust", "timer",
]
FEATURES_VR_WITH_WATERLINE = FEATURES_RA_6570 + [
    "preset_waterline", "custom_mode", "spot_clean", "water_temp",
]
FEATURES_CYCLONEXT = [
    "lift_system", "mode_info", "preset_deep_ultra", "quick_clean",
    "remote_control", "start_stop", "status", "time_adjust", "timer",
]


def test_vr_without_waterline_drops_wall_only() -> None:
    """The bug this fixes: an RA 6570 iQ advertises no `preset_waterline`,
    so "Wall only" must not be offered.
    """
    got = _narrow_fan_speeds_to_features(VR_FULL, FEATURES_RA_6570)
    assert got == ["floor_only", "smart_floor_and_walls", "floor_and_walls"]
    assert "wall_only" not in got


def test_vr_with_waterline_keeps_all_four() -> None:
    """The other captured vr does advertise it, and keeps the full list —
    proving the narrowing is per-device, not a blanket removal.
    """
    assert _narrow_fan_speeds_to_features(
        VR_FULL, FEATURES_VR_WITH_WATERLINE
    ) == VR_FULL


def test_cyclonext_features_reproduce_the_known_correct_list() -> None:
    """Cross-check on the #76 fix.

    Deriving cyclonext's list purely from its advertised features arrives at
    the same 2-entry list that was hardcoded to fix #76 — independent
    confirmation that both the fix and this mapping are right.
    """
    assert _narrow_fan_speeds_to_features(
        CYCLONEXT_FULL, FEATURES_CYCLONEXT
    ) == CYCLONEXT_FULL
    assert _narrow_fan_speeds_to_features(
        VR_FULL, FEATURES_CYCLONEXT
    ) == CYCLONEXT_FULL


@pytest.mark.parametrize("features", [None, [], "not-a-list", {}, 0])
def test_fails_open_when_features_unusable(features: object) -> None:
    """Absent/garbage features must leave the list untouched.

    The features call may not have succeeded yet. Narrowing on no evidence
    would remove working functionality; keeping the family default merely
    reproduces today's behaviour.
    """
    assert _narrow_fan_speeds_to_features(VR_FULL, features) == VR_FULL


def test_i2d_walls_only_is_never_narrowed_away() -> None:
    """`walls_only` is i2d's waterline mode and has no gate entry.

    i2d speaks the REST hex protocol and isn't described by this endpoint,
    so its modes must survive narrowing even against an unrelated feature
    list.
    """
    i2d = ["floor_only", "walls_only", "floor_and_walls"]
    assert "walls_only" not in _FAN_SPEED_REQUIRED_FEATURE
    got = _narrow_fan_speeds_to_features(i2d, FEATURES_CYCLONEXT)
    assert "walls_only" in got


def test_never_returns_an_empty_list() -> None:
    """An empty `fan_speed_list` would strip fan-speed support from the
    entity altogether, so narrowing to nothing falls back to the default.
    """
    assert _narrow_fan_speeds_to_features(
        ["wall_only"], ["status", "timer"]
    ) == ["wall_only"]


def test_unknown_future_speed_is_kept() -> None:
    """A speed with no gate entry is always kept, so adding a new mode can't
    accidentally have it filtered out before its gate is defined.
    """
    got = _narrow_fan_speeds_to_features(
        ["floor_only", "some_future_mode"], FEATURES_CYCLONEXT
    )
    assert "some_future_mode" in got
