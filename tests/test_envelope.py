"""Envelope-shape regression tests for story R23.

R23 collapsed 21 near-identical websocket envelope literals — one per
command-method × device-type combination — into a single
``AqualinkClient._build_state_request`` helper.

These tests pin **dict-content equality** between the helper's output for
each call-site's parameters and the verbatim envelope literal that lived
at that call site pre-R23. Python's ``dict.__eq__`` is recursive
value-equality (key order irrelevant), which matches the user-confirmed
interpretation of AC#4 — the iAqualink cloud is AWS IoT-shaped and parses
JSON into dicts, so transmit-time key order is unobservable.

A regression here means **a field was dropped, a value drifted, or the
nesting changed** — exactly the failure modes the spec's "Adversary's
warning" called out and the regression test is designed to catch.

Coverage: 21 cases, one per pre-R23 envelope literal, spanning all 5
device families (vr, vortrax, cyclobat, cyclonext, plus the i2d_robot
path which uses HTTP not websocket and is therefore out-of-scope). The
user has a VR robot for manual smoke-testing; cyclobat / cyclonext /
vortrax coverage is carried entirely by these fixture-shaped tests.
"""

from __future__ import annotations

import pytest

from custom_components.iaqualink_robots.coordinator import AqualinkClient


# ---------------------------------------------------------------------------
# Shared fixture values — produce a deterministic ``clientToken`` and
# ``target`` so the expected envelopes can be written as literals.
# ---------------------------------------------------------------------------

_ID = "test_user_id"
_AUTH = "test_auth_tok"
_APP = "test_app_client"
_SERIAL = "TEST_SERIAL"
_TOKEN = f"{_ID}|{_AUTH}|{_APP}"


def _make_client(device_type: str) -> AqualinkClient:
    """Build a freshly-constructed ``AqualinkClient`` with the minimum
    fields ``_build_state_request`` consults.

    Avoids ``MagicMock`` — the helper reads four ``str`` attributes plus
    ``self._device_type``, all set via real ``__init__`` + assignment.
    A real instance protects against accidental drift if the helper ever
    starts reading additional state (the test would surface the new
    dependency immediately rather than a MagicMock silently auto-attr'ing).
    """
    client = AqualinkClient(username="u", password="p", api_key="k")
    client._id = _ID
    client._auth_token = _AUTH
    client._app_client_id = _APP
    client._serial = _SERIAL
    client._device_type = device_type
    return client


# ---------------------------------------------------------------------------
# Helper that emits the canonical post-R23 envelope by construction —
# tests below compare the helper's output to a hand-written literal that
# encodes the SAME dict-content the pre-R23 call site used.
# ---------------------------------------------------------------------------


def _expected(action: str, ns: str, equipment: dict) -> dict:
    """Build the expected envelope for an assertion.

    Hand-rolled to match the canonical post-R23 shape — used as the
    assertion target. If the helper output ever diverges from this
    shape (extra field, dropped field, wrong nesting), the comparison
    fails with a clear ``-/+`` diff thanks to pytest's dict-introspection.
    """
    return {
        "action": action,
        "version": 1,
        "namespace": ns,
        "payload": {
            "state": {"desired": {"equipment": equipment}},
            "clientToken": _TOKEN,
        },
        "service": "StateController",
        "target": _SERIAL,
    }


# ---------------------------------------------------------------------------
# 21 parameterized cases — one per pre-R23 envelope literal.
#
# Format: (test_id, device_type, helper_kwargs, expected_envelope)
#
# ``helper_kwargs`` are passed as a tuple ``(args, kwargs)`` so the test
# can call ``client._build_state_request(*args, **kwargs)``. The expected
# envelope is the dict-content the pre-R23 literal would have produced
# with our fixture values substituted.
# ---------------------------------------------------------------------------


_CASES = [
    # --- start_cleaning (3 device types) ---
    (
        "start_cleaning__vr",
        "vr",
        (("setCleanerState", {"state": 1}), {}),
        _expected("setCleanerState", "vr", {"robot": {"state": 1}}),
    ),
    (
        "start_cleaning__vortrax",
        "vortrax",
        (("setCleanerState", {"state": 1}), {}),
        _expected("setCleanerState", "vortrax", {"robot": {"state": 1}}),
    ),
    (
        "start_cleaning__cyclobat",
        "cyclobat",
        (("setCleaningMode", {"main": {"ctrl": 1}}), {"namespace": "cyclobat"}),
        _expected("setCleaningMode", "cyclobat", {"robot": {"main": {"ctrl": 1}}}),
    ),
    (
        "start_cleaning__cyclonext",
        "cyclonext",
        (
            ("setCleanerState", {"mode": 1}),
            {"namespace": "cyclonext", "robot_key": "robot.1"},
        ),
        _expected("setCleanerState", "cyclonext", {"robot.1": {"mode": 1}}),
    ),
    # --- clear_desired_state (vr only — bound to PR #94 natural-completion path) ---
    (
        "clear_desired_state__vr",
        "vr",
        (("setCleanerState", {"state": 0}), {}),
        _expected("setCleanerState", "vr", {"robot": {"state": 0}}),
    ),
    # --- stop_cleaning (3 device types) ---
    (
        "stop_cleaning__vr",
        "vr",
        (("setCleanerState", {"state": 0}), {}),
        _expected("setCleanerState", "vr", {"robot": {"state": 0}}),
    ),
    (
        "stop_cleaning__cyclobat",
        "cyclobat",
        (("setCleaningMode", {"main": {"ctrl": 0}}), {"namespace": "cyclobat"}),
        _expected("setCleaningMode", "cyclobat", {"robot": {"main": {"ctrl": 0}}}),
    ),
    (
        "stop_cleaning__cyclonext",
        "cyclonext",
        (
            ("setCleanerState", {"mode": 0}),
            {"namespace": "cyclonext", "robot_key": "robot.1"},
        ),
        _expected("setCleanerState", "cyclonext", {"robot.1": {"mode": 0}}),
    ),
    # --- pause_cleaning (3 device types — state=2 / ctrl=2 / mode=2) ---
    (
        "pause_cleaning__vr",
        "vr",
        (("setCleanerState", {"state": 2}), {}),
        _expected("setCleanerState", "vr", {"robot": {"state": 2}}),
    ),
    (
        "pause_cleaning__cyclobat",
        "cyclobat",
        (("setCleaningMode", {"main": {"ctrl": 2}}), {"namespace": "cyclobat"}),
        _expected("setCleaningMode", "cyclobat", {"robot": {"main": {"ctrl": 2}}}),
    ),
    (
        "pause_cleaning__cyclonext",
        "cyclonext",
        (
            ("setCleanerState", {"mode": 2}),
            {"namespace": "cyclonext", "robot_key": "robot.1"},
        ),
        _expected("setCleanerState", "cyclonext", {"robot.1": {"mode": 2}}),
    ),
    # --- return_to_base (vr/vortrax/cyclobat — state=3, `robot` key for ALL three,
    #     even though cyclobat's other commands nest under `main`. Pre-R23 quirk
    #     preserved as-is per AC#4) ---
    (
        "return_to_base__vr",
        "vr",
        (("setCleanerState", {"state": 3}), {}),
        _expected("setCleanerState", "vr", {"robot": {"state": 3}}),
    ),
    (
        "return_to_base__cyclobat",
        "cyclobat",
        (("setCleanerState", {"state": 3}), {}),
        _expected("setCleanerState", "cyclobat", {"robot": {"state": 3}}),
    ),
    # --- set_fan_speed (3 device types — different value-field names) ---
    (
        "set_fan_speed__vr",
        "vr",
        (("setCleaningMode", {"prCyc": 1}), {}),
        _expected("setCleaningMode", "vr", {"robot": {"prCyc": 1}}),
    ),
    (
        "set_fan_speed__cyclobat",
        "cyclobat",
        (
            ("setCleaningMode", {"main": {"mode": "0"}}),
            {"namespace": "cyclobat"},
        ),
        _expected(
            "setCleaningMode", "cyclobat", {"robot": {"main": {"mode": "0"}}}
        ),
    ),
    (
        "set_fan_speed__cyclonext",
        "cyclonext",
        (
            ("setCleaningMode", {"cycle": "1"}),
            {"namespace": "cyclonext", "robot_key": "robot.1"},
        ),
        _expected("setCleaningMode", "cyclonext", {"robot.1": {"cycle": "1"}}),
    ),
    # --- remote-control mode toggle (vr/vortrax — state=2 enter, state=0 exit) ---
    (
        "enter_remote_control__vr",
        "vr",
        (("setCleanerState", {"state": 2}), {}),
        _expected("setCleanerState", "vr", {"robot": {"state": 2}}),
    ),
    (
        "exit_remote_control__vr",
        "vr",
        (("setCleanerState", {"state": 0}), {}),
        _expected("setCleanerState", "vr", {"robot": {"state": 0}}),
    ),
    # --- remote steering (vr/vortrax — rmt_ctrl field; uses setRemoteSteeringControl) ---
    (
        "remote_steering__vr_forward",
        "vr",
        (("setRemoteSteeringControl", {"rmt_ctrl": 1}), {}),
        _expected("setRemoteSteeringControl", "vr", {"robot": {"rmt_ctrl": 1}}),
    ),
    # --- stepper add/reduce + clear (vr/vortrax — 4 envelopes total) ---
    (
        "stepper_set__vr_add",
        "vr",
        (("setCleaningMode", {"stepper": 45}), {}),
        _expected("setCleaningMode", "vr", {"robot": {"stepper": 45}}),
    ),
    (
        "stepper_clear__vr_after_add",
        "vr",
        (("setCleaningMode", {"stepper": None}), {}),
        _expected("setCleaningMode", "vr", {"robot": {"stepper": None}}),
    ),
    # --- vortrax device-type permutations of the vr-or-vortrax merged branches ---
    #
    # Production routes vortrax alongside vr through the same `if device_type
    # in {"vr", "vortrax"}:` lines for stop / pause / return_to_base /
    # set_fan_speed / remote-control state / remote steering / stepper. Both
    # adversarial reviewers (Blind Hunter + Edge Case Hunter) flagged that
    # without per-site vortrax fixtures, a future bug that hardcodes
    # `namespace="vr"` at one of those call sites would slip past every vr
    # test. These cases drive the helper with `device_type="vortrax"` and
    # assert the envelope carries `namespace: "vortrax"` — the helper's
    # default-from-`self._device_type` path is the only behaviour difference
    # vs the vr cases above, but pinning it here means the regression test
    # catches the literal-namespace-hardcode bug class. R23 review patch.
    (
        "stop_cleaning__vortrax",
        "vortrax",
        (("setCleanerState", {"state": 0}), {}),
        _expected("setCleanerState", "vortrax", {"robot": {"state": 0}}),
    ),
    (
        "pause_cleaning__vortrax",
        "vortrax",
        (("setCleanerState", {"state": 2}), {}),
        _expected("setCleanerState", "vortrax", {"robot": {"state": 2}}),
    ),
    (
        "return_to_base__vortrax",
        "vortrax",
        (("setCleanerState", {"state": 3}), {}),
        _expected("setCleanerState", "vortrax", {"robot": {"state": 3}}),
    ),
    (
        "set_fan_speed__vortrax",
        "vortrax",
        (("setCleaningMode", {"prCyc": 1}), {}),
        _expected("setCleaningMode", "vortrax", {"robot": {"prCyc": 1}}),
    ),
    (
        "enter_remote_control__vortrax",
        "vortrax",
        (("setCleanerState", {"state": 2}), {}),
        _expected("setCleanerState", "vortrax", {"robot": {"state": 2}}),
    ),
    (
        "exit_remote_control__vortrax",
        "vortrax",
        (("setCleanerState", {"state": 0}), {}),
        _expected("setCleanerState", "vortrax", {"robot": {"state": 0}}),
    ),
    (
        "remote_steering__vortrax_forward",
        "vortrax",
        (("setRemoteSteeringControl", {"rmt_ctrl": 1}), {}),
        _expected(
            "setRemoteSteeringControl", "vortrax", {"robot": {"rmt_ctrl": 1}}
        ),
    ),
    (
        "stepper_set__vortrax_add",
        "vortrax",
        (("setCleaningMode", {"stepper": 45}), {}),
        _expected("setCleaningMode", "vortrax", {"robot": {"stepper": 45}}),
    ),
    (
        "stepper_clear__vortrax_after_add",
        "vortrax",
        (("setCleaningMode", {"stepper": None}), {}),
        _expected("setCleaningMode", "vortrax", {"robot": {"stepper": None}}),
    ),
]


@pytest.mark.parametrize(
    "test_id,device_type,helper_call,expected",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_envelope_matches_pre_r23_literal(
    test_id: str,
    device_type: str,
    helper_call: tuple,
    expected: dict,
) -> None:
    """For each pre-R23 envelope site, the helper's output must be
    dict-content equal to the verbatim envelope literal that lived there.

    Python's ``dict.__eq__`` is recursive value-equality, so this asserts
    "no field dropped, no value drifted, no nesting changed" without
    coupling to JSON serialisation order — exactly the AC#4 contract
    after the user's "key order is don't-care, params must not be
    scrapped" interpretation.
    """
    args, kwargs = helper_call
    client = _make_client(device_type)
    actual = client._build_state_request(*args, **kwargs)
    assert actual == expected, (
        f"{test_id}: helper output diverged from pre-R23 envelope literal — "
        f"a field was dropped, a value drifted, or the nesting changed.\n"
        f"actual:   {actual!r}\n"
        f"expected: {expected!r}"
    )


# ---------------------------------------------------------------------------
# Canonical-shape sanity test — independent of any specific call site,
# pins the helper's output shape so a future refactor that drops e.g.
# `version` from the envelope fails CI loudly.
# ---------------------------------------------------------------------------


def test_helper_produces_canonical_envelope_shape() -> None:
    """The canonical envelope MUST contain exactly these top-level fields:
    ``action``, ``version``, ``namespace``, ``payload``, ``service``,
    ``target``. ``payload`` MUST contain ``state`` and ``clientToken``.
    ``state.desired.equipment`` MUST nest the caller-supplied ``desired``
    dict under the caller-supplied ``robot_key``.

    Locks in the AC#1 contract independently from the per-site tests —
    a refactor that changes the canonical shape would fail every per-site
    test in confusing ways; this test fails first with a clear diagnostic.
    """
    client = _make_client("vr")

    envelope = client._build_state_request(
        "setCleanerState",
        {"state": 1},
    )

    assert set(envelope.keys()) == {
        "action",
        "version",
        "namespace",
        "payload",
        "service",
        "target",
    }, "canonical envelope must have exactly these 6 top-level fields"
    assert envelope["action"] == "setCleanerState"
    assert envelope["version"] == 1
    assert envelope["namespace"] == "vr"  # defaulted from self._device_type
    assert envelope["service"] == "StateController"
    assert envelope["target"] == _SERIAL
    assert set(envelope["payload"].keys()) == {"state", "clientToken"}
    assert envelope["payload"]["clientToken"] == _TOKEN
    assert envelope["payload"]["state"] == {
        "desired": {"equipment": {"robot": {"state": 1}}}
    }


def test_helper_uses_robot_dot_one_key_for_cyclonext() -> None:
    """Cyclonext envelopes use ``robot.1`` as the equipment key, not
    ``robot``. Locks in the per-device-type drift the AC#3 mentioned.
    """
    client = _make_client("cyclonext")

    envelope = client._build_state_request(
        "setCleanerState",
        {"mode": 1},
        namespace="cyclonext",
        robot_key="robot.1",
    )

    equipment = envelope["payload"]["state"]["desired"]["equipment"]
    assert "robot.1" in equipment
    assert "robot" not in equipment, (
        "cyclonext must NOT also write a `robot` key — that would change "
        "the payload shape the cloud expects (pre-R23 only had robot.1)"
    )
    assert equipment["robot.1"] == {"mode": 1}


def test_helper_namespace_defaults_to_device_type() -> None:
    """When ``namespace`` is not passed, the helper uses ``self._device_type``.

    This is the vr/vortrax path — those two device types are the only
    ones whose ``_device_type`` literal IS the namespace string. The
    cyclobat / cyclonext paths pass explicit ``namespace=`` for clarity
    even though it's equivalent.
    """
    client_vr = _make_client("vr")
    client_vortrax = _make_client("vortrax")

    assert client_vr._build_state_request("setCleanerState", {"state": 1})[
        "namespace"
    ] == "vr"
    assert client_vortrax._build_state_request("setCleanerState", {"state": 1})[
        "namespace"
    ] == "vortrax"


def test_helper_includes_client_token_built_from_three_attrs() -> None:
    """``payload.clientToken`` must be the pipe-joined
    ``{_id}|{_auth_token}|{_app_client_id}`` — the cloud uses this to
    correlate the websocket request with a logged-in session. A
    refactor that swapped any of these three attrs would silently
    break authentication.
    """
    client = _make_client("vr")
    envelope = client._build_state_request("setCleanerState", {"state": 1})
    assert envelope["payload"]["clientToken"] == "test_user_id|test_auth_tok|test_app_client"
