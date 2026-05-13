"""Shared fixtures for the iAqualink Robots test suite.

Conventions:
    - Tests never hit the real iAqualink cloud. All HTTP/WS is mocked.
    - `enable_custom_integrations` is autouse so pytest-homeassistant-custom-component
      can discover this integration from `custom_components/`.
    - `bypass_setup_fixture` short-circuits `async_setup_entry` for tests that
      only care about config-flow behavior — opt in by listing it as a fixture
      arg. Tests that exercise setup explicitly should NOT use it.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.const import MOCK_DEVICE_SECOND, MOCK_DEVICE_SINGLE


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom_components discovery for every test."""
    yield


@pytest.fixture
def mock_discover_single_device() -> Generator[MagicMock, None, None]:
    """Patch AqualinkClient.discover_devices to return a single mock device.

    Used by config-flow tests for the "one device" happy path which skips the
    select_device step.
    """
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(return_value=[MOCK_DEVICE_SINGLE]),
    ) as mock:
        yield mock


@pytest.fixture
def mock_discover_two_devices() -> Generator[MagicMock, None, None]:
    """Patch AqualinkClient.discover_devices to return two devices.

    Forces the select_device step path (different code branch from single-device).
    """
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(return_value=[MOCK_DEVICE_SINGLE, MOCK_DEVICE_SECOND]),
    ) as mock:
        yield mock


@pytest.fixture
def mock_discover_empty_serial() -> Generator[MagicMock, None, None]:
    """Patch AqualinkClient.discover_devices to return a device with an empty serial."""
    bad_device = {**MOCK_DEVICE_SINGLE, "serial_number": ""}
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(return_value=[bad_device]),
    ) as mock:
        yield mock


@pytest.fixture
def mock_discover_good_and_empty_serial() -> Generator[MagicMock, None, None]:
    """Patch discover_devices to return one good device + one with an empty serial.

    Forces the select_device step so the no_serial guard inside that branch is
    exercised when the user picks the bad device.
    """
    bad_device = {**MOCK_DEVICE_SECOND, "serial_number": ""}
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(return_value=[MOCK_DEVICE_SINGLE, bad_device]),
    ) as mock:
        yield mock


@pytest.fixture
def mock_discover_no_devices() -> Generator[MagicMock, None, None]:
    """Patch AqualinkClient.discover_devices to return an empty list."""
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(return_value=[]),
    ) as mock:
        yield mock


@pytest.fixture
def mock_discover_raises() -> Generator[MagicMock, None, None]:
    """Patch AqualinkClient.discover_devices to raise (simulates auth/network failure)."""
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(side_effect=Exception("simulated network error")),
    ) as mock:
        yield mock


@pytest.fixture
def bypass_setup_fixture() -> Generator[None, None, None]:
    """Short-circuit async_setup_entry so config-flow tests don't start a real coordinator."""
    with patch(
        "custom_components.iaqualink_robots.async_setup_entry",
        return_value=True,
    ):
        yield
