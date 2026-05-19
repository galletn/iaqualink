"""Stepper add/reduce regression tests for story R24.

R24 collapses ``add_fifteen_minutes`` / ``reduce_fifteen_minutes`` (two
near-identical ~95-line methods on ``AqualinkClient``) plus their coordinator
wrappers (~80-line near-clones) into one shared helper per layer:

* ``AqualinkClient._adjust_stepper(delta_minutes, action_label)`` — owns the
  cloud-side work: device-type guard, websocket-suspend guard, debounce,
  ``fetch_status`` → compute → ``_build_state_request`` set+clear pair →
  ``_safe_post_command_refresh``.
* ``AqualinkDataUpdateCoordinator._async_adjust_stepper(...)`` — owns the
  HA-side notifications + final ``async_request_refresh``.

The thin public surface ``add_fifteen_minutes`` / ``reduce_fifteen_minutes``
(client) and ``async_add_fifteen_minutes`` / ``async_reduce_fifteen_minutes``
(coordinator) become 1-2 line wrappers that route through the shared helper
with the appropriate sign and labels.

These tests lock in:

* AC #3 envelope-byte-equality with pre-R24 — verified by dict-content
  equality against the canonical R23 envelope shape (the existing
  ``tests/test_envelope.py`` already pins ``stepper_set__vr_add`` etc, this
  file just confirms the new helper still produces those envelopes through
  the same code path).
* AC behavior — ``test_reduce_at_zero_clamps`` enforces the new
  ``max(0, current + delta)`` semantic. Pre-R24 a reduce from
  ``current_stepper=0`` would have sent ``{"stepper": -15}`` to the cloud,
  which is undefined behavior. R24's clamp is a behavior change documented
  in the CHANGELOG.
* AC #1 + #2 structural — both directions route through the shared helper,
  not via parallel copy-paste paths. Caught at the AST level so a future
  refactor that re-inlines either direction fails CI rather than silently
  forking the contract.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared fixture — minimal AqualinkClient with the surface area the stepper
# methods exercise. Mirrors the `_build_client_for_command` pattern from
# tests/test_coordinator.py.
# ---------------------------------------------------------------------------


def _build_stepper_client(
    *,
    device_type: str = "vr",
    current_stepper: int = 30,
) -> object:
    """Build an ``AqualinkClient`` with the minimum fields the stepper
    helper consults: device type / serial / auth strings for envelope
    construction, plus stubs for the cloud roundtrip and refresh callback.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient.__new__(AqualinkClient)
    client._device_type = device_type
    client._serial = "TEST-SERIAL"
    client._id = "user-id"
    client._auth_token = "tok"
    client._app_client_id = "acid"
    client._debug_mode = False
    # Stubs.
    client.set_cleaner_state = AsyncMock(return_value={"ok": True})
    client._should_use_websocket = MagicMock(return_value=True)
    client.fetch_status = AsyncMock(return_value={"stepper": current_stepper})
    client._coordinator_callback = AsyncMock()
    client._coordinator_ref = MagicMock()
    return client


def _captured_envelopes(client) -> list[dict]:
    """Extract the envelope dicts the helper sent to ``set_cleaner_state``."""
    return [call.args[0] for call in client.set_cleaner_state.await_args_list]


# ---------------------------------------------------------------------------
# AC #3 — envelope-byte equality for the happy paths (set + clear pair).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_fifteen_increments_by_15() -> None:
    """``add_fifteen_minutes`` sends ``setCleaningMode`` with
    ``stepper = current + 15`` first, then a clear (``stepper: None``).
    """
    client = _build_stepper_client(current_stepper=30)

    result = await client.add_fifteen_minutes()

    envelopes = _captured_envelopes(client)
    assert len(envelopes) == 2, "expected one set + one clear envelope"

    set_env, clear_env = envelopes
    assert set_env["action"] == "setCleaningMode"
    assert set_env["payload"]["state"]["desired"]["equipment"]["robot"] == {
        "stepper": 45
    }
    assert clear_env["payload"]["state"]["desired"]["equipment"]["robot"] == {
        "stepper": None
    }
    assert result["success"] is True
    assert result["previous_stepper"] == 30
    assert result["new_stepper"] == 45


@pytest.mark.asyncio
async def test_reduce_fifteen_decrements_by_15() -> None:
    """``reduce_fifteen_minutes`` sends ``setCleaningMode`` with
    ``stepper = current - 15`` first, then a clear.
    """
    client = _build_stepper_client(current_stepper=30)

    result = await client.reduce_fifteen_minutes()

    envelopes = _captured_envelopes(client)
    assert len(envelopes) == 2

    set_env, clear_env = envelopes
    assert set_env["action"] == "setCleaningMode"
    assert set_env["payload"]["state"]["desired"]["equipment"]["robot"] == {
        "stepper": 15
    }
    assert clear_env["payload"]["state"]["desired"]["equipment"]["robot"] == {
        "stepper": None
    }
    assert result["success"] is True
    assert result["previous_stepper"] == 30
    assert result["new_stepper"] == 15


@pytest.mark.asyncio
async def test_reduce_at_zero_clamps() -> None:
    """R24 behavior change: reducing when ``current_stepper == 0`` must
    clamp ``new_stepper`` to ``0``, not send ``{"stepper": -15}`` to the
    cloud.

    Pre-R24 the cloud received a negative stepper from this edge case.
    The cloud's documented behavior on a negative value is undefined.
    R24's ``max(0, current + delta)`` semantic makes the contract safe.
    """
    client = _build_stepper_client(current_stepper=0)

    result = await client.reduce_fifteen_minutes()

    set_env = _captured_envelopes(client)[0]
    assert (
        set_env["payload"]["state"]["desired"]["equipment"]["robot"]["stepper"]
        == 0
    ), "reduce at stepper=0 must clamp to 0, not send -15"
    assert result["new_stepper"] == 0
    assert result["previous_stepper"] == 0


@pytest.mark.asyncio
async def test_add_at_high_value_does_not_clamp() -> None:
    """Clamp only fires on the lower bound. Adding from any non-negative
    current value must not silently cap at some upper limit — the integration
    has no knowledge of a per-device max stepper.
    """
    client = _build_stepper_client(current_stepper=300)

    result = await client.add_fifteen_minutes()

    assert result["new_stepper"] == 315


# ---------------------------------------------------------------------------
# AC #1 + AC #2 — both directions route through the shared helper.
# Structural guard: AST inspection of ``coordinator.py`` confirms
# ``add_fifteen_minutes`` and ``reduce_fifteen_minutes`` bodies delegate
# to a shared ``_adjust_stepper`` method rather than duplicating the
# fetch-status / build-request / refresh logic.
# ---------------------------------------------------------------------------


def _method_node(class_name: str, method_name: str) -> ast.AsyncFunctionDef:
    """Find the AST node for a method on a class in coordinator.py."""
    coordinator_path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    )
    tree = ast.parse(coordinator_path.read_text(encoding="utf-8"))
    for class_node in ast.walk(tree):
        if (
            isinstance(class_node, ast.ClassDef)
            and class_node.name == class_name
        ):
            for method in class_node.body:
                if (
                    isinstance(method, ast.AsyncFunctionDef)
                    and method.name == method_name
                ):
                    return method
    raise AssertionError(f"{class_name}.{method_name} not found in coordinator.py")


def _calls_self_method(method_node: ast.AsyncFunctionDef, target: str) -> bool:
    """True if ``method_node`` contains an ``await self.<target>(...)`` call."""
    for node in ast.walk(method_node):
        if (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == target
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "self"
        ):
            return True
    return False


def test_client_add_fifteen_routes_through_shared_helper() -> None:
    """AC #1 (client layer): ``add_fifteen_minutes`` delegates to the
    shared ``_adjust_stepper`` helper, not a copy-pasted body.
    """
    node = _method_node("AqualinkClient", "add_fifteen_minutes")
    assert _calls_self_method(node, "_adjust_stepper"), (
        "AqualinkClient.add_fifteen_minutes must delegate to "
        "self._adjust_stepper(...) — R24 collapsed the two near-clones."
    )


def test_client_reduce_fifteen_routes_through_shared_helper() -> None:
    """AC #1 (client layer): ``reduce_fifteen_minutes`` delegates to the
    same shared helper as ``add_fifteen_minutes``.
    """
    node = _method_node("AqualinkClient", "reduce_fifteen_minutes")
    assert _calls_self_method(node, "_adjust_stepper"), (
        "AqualinkClient.reduce_fifteen_minutes must delegate to "
        "self._adjust_stepper(...) — R24 collapsed the two near-clones."
    )


def test_coordinator_async_add_routes_through_shared_async_helper() -> None:
    """AC #2 (coordinator layer): ``async_add_fifteen_minutes`` delegates
    to ``_async_adjust_stepper`` instead of repeating the rate-limit /
    notification / refresh logic.
    """
    node = _method_node(
        "AqualinkDataUpdateCoordinator", "async_add_fifteen_minutes"
    )
    assert _calls_self_method(node, "_async_adjust_stepper"), (
        "AqualinkDataUpdateCoordinator.async_add_fifteen_minutes must "
        "delegate to self._async_adjust_stepper(...) — R24 collapsed the "
        "two near-clones."
    )


def test_coordinator_async_reduce_routes_through_shared_async_helper() -> None:
    """AC #2 (coordinator layer): ``async_reduce_fifteen_minutes``
    delegates to the same shared helper.
    """
    node = _method_node(
        "AqualinkDataUpdateCoordinator", "async_reduce_fifteen_minutes"
    )
    assert _calls_self_method(node, "_async_adjust_stepper"), (
        "AqualinkDataUpdateCoordinator.async_reduce_fifteen_minutes must "
        "delegate to self._async_adjust_stepper(...) — R24 collapsed the "
        "two near-clones."
    )


def test_client_wrappers_are_short_after_r24() -> None:
    """AC #4 (LOC reduction): after R24, ``add_fifteen_minutes`` and
    ``reduce_fifteen_minutes`` should be near-empty wrappers.

    A budget of <= 8 statements per method (counting docstring as a
    statement) lets the body be the device-type guard + a delegating
    ``return await self._adjust_stepper(...)`` plus a couple of lines of
    breathing room — but fails CI if a future PR re-inlines the full
    ~95-line copy.
    """
    for name in ("add_fifteen_minutes", "reduce_fifteen_minutes"):
        node = _method_node("AqualinkClient", name)
        stmt_count = len(node.body)
        assert stmt_count <= 8, (
            f"AqualinkClient.{name} has {stmt_count} top-level statements "
            f"post-R24 — expected <= 8 (delegate + minimal scaffolding). "
            "Did a regression re-inline the copy-pasted body?"
        )


# ---------------------------------------------------------------------------
# Guard paths — preserve pre-R24 behavior for the early-return branches.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("device_type", ["cyclobat", "cyclonext", "i2d_robot"])
async def test_unsupported_device_types_no_op(device_type: str) -> None:
    """Stepper add/reduce is vr/vortrax-only. Other device families must
    emit a warning and return None without touching the cloud.
    """
    client = _build_stepper_client(device_type=device_type)

    result_add = await client.add_fifteen_minutes()
    result_reduce = await client.reduce_fifteen_minutes()

    assert result_add is None
    assert result_reduce is None
    client.set_cleaner_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_unavailable_returns_error() -> None:
    """When ``_should_use_websocket()`` returns False (e.g. cloud is in
    long-outage state), the helper must short-circuit with a structured
    error dict and never touch ``set_cleaner_state``.
    """
    client = _build_stepper_client(current_stepper=30)
    client._should_use_websocket = MagicMock(return_value=False)

    result = await client.add_fifteen_minutes()

    assert result == {
        "success": False,
        "error": "websocket_unavailable",
        "message": "Connection issues detected. Please try again in a moment.",
    }
    client.set_cleaner_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limit_returns_cooldown() -> None:
    """The 3-second per-client debounce on ``_last_timing_command_time``
    must apply to the shared helper too. A back-to-back call within the
    cooldown window returns a structured ``rate_limited`` error and
    does NOT issue a second cloud roundtrip.
    """
    import time

    client = _build_stepper_client(current_stepper=30)

    await client.add_fifteen_minutes()
    # Manually set a very recent timing-command timestamp so the second
    # call lands inside the 3 s window without relying on real wall-clock
    # scheduling (which would be flaky on slow CI).
    client._last_timing_command_time = time.time()

    result = await client.reduce_fifteen_minutes()

    assert result["success"] is False
    assert result["error"] == "rate_limited"
    assert "cooldown_remaining" in result
    # First call: 2 envelopes (set + clear). Second call: 0. Total: 2.
    assert len(_captured_envelopes(client)) == 2


# ---------------------------------------------------------------------------
# Coordinator helper — behavioral test that the async wrappers dispatch to
# the correct client method based on delta sign.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_helper_dispatches_add_to_client_add() -> None:
    """``async_add_fifteen_minutes`` must end up calling
    ``client.add_fifteen_minutes`` (not ``reduce_fifteen_minutes``).
    Defends against a sign-flip typo if a future refactor swaps the
    dispatch table.
    """
    from custom_components.iaqualink_robots.coordinator import (
        AqualinkDataUpdateCoordinator,
    )

    coord = AqualinkDataUpdateCoordinator.__new__(AqualinkDataUpdateCoordinator)
    coord.client = MagicMock()
    coord.client.device_type = "vr"
    coord.client.robot_id = "rid"
    coord.client.serial = "ser"
    coord.client.add_fifteen_minutes = AsyncMock(
        return_value={"success": True, "message": "ok"}
    )
    coord.client.reduce_fifteen_minutes = AsyncMock(
        return_value={"success": True, "message": "ok"}
    )
    coord._last_timing_command = 0.0
    coord.hass = MagicMock()
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    coord.async_request_refresh = AsyncMock()

    await coord.async_add_fifteen_minutes()

    coord.client.add_fifteen_minutes.assert_awaited_once()
    coord.client.reduce_fifteen_minutes.assert_not_awaited()


@pytest.mark.asyncio
async def test_coordinator_helper_dispatches_reduce_to_client_reduce() -> None:
    """Symmetric to the add case: ``async_reduce_fifteen_minutes`` must
    call ``client.reduce_fifteen_minutes`` (not ``add_fifteen_minutes``).
    """
    from custom_components.iaqualink_robots.coordinator import (
        AqualinkDataUpdateCoordinator,
    )

    coord = AqualinkDataUpdateCoordinator.__new__(AqualinkDataUpdateCoordinator)
    coord.client = MagicMock()
    coord.client.device_type = "vr"
    coord.client.robot_id = "rid"
    coord.client.serial = "ser"
    coord.client.add_fifteen_minutes = AsyncMock(
        return_value={"success": True, "message": "ok"}
    )
    coord.client.reduce_fifteen_minutes = AsyncMock(
        return_value={"success": True, "message": "ok"}
    )
    coord._last_timing_command = 0.0
    coord.hass = MagicMock()
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    coord.async_request_refresh = AsyncMock()

    await coord.async_reduce_fifteen_minutes()

    coord.client.reduce_fifteen_minutes.assert_awaited_once()
    coord.client.add_fifteen_minutes.assert_not_awaited()
