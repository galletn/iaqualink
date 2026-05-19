"""Abstract base for per-device-type protocol strategies (story R25).

A ``RobotProtocol`` instance encapsulates everything that varies between
device families: the websocket envelope shape for each command (``start`` /
``stop`` / ``pause`` / ``return_to_base`` / ``clear_desired``), the
fan-speed cloud encoding, and the per-payload status parser.

Each cloud-side family has its own protocol subclass — see ``vr.py``,
``vortrax.py``, ``cyclobat.py``, ``cyclonext.py``, ``i2d.py``. The
coordinator dispatches via ``_DEVICE_PROTOCOLS`` (see ``__init__.py``)
which maps a ``device_type`` string (the cloud-returned identifier) to
the appropriate instance.

The pattern is extracted incrementally — R25-vr ships the VR family first;
the other four land as separate stories. While a device-type is still
inline-encoded in ``coordinator.py``, ``get_protocol(device_type)``
returns ``None`` for it and the coordinator's existing if/elif branches
carry the load. Once all five are extracted, the chains collapse into a
single protocol dispatch (story R26 — module split).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..coordinator import AqualinkClient


class RobotProtocol(ABC):
    """Strategy interface for per-device-type cloud behaviour.

    Methods are split into three groups:

    * **Envelope builders** (``start_payload`` / ``stop_payload`` /
      ``pause_payload`` / ``return_to_base_payload`` /
      ``clear_desired_payload`` / ``set_fan_speed_payload``) — each
      returns the dict the coordinator passes to ``set_cleaner_state``.
      They take ``client`` so they can call the R23
      ``client._build_state_request(...)`` helper that owns the
      canonical envelope shape (action + version + namespace + payload
      + clientToken + service + target).
    * **Cloud-encoding helpers** (``fan_speed_codes`` /
      ``extract_fan_speed_from_response``) — read-only mappings between
      the integration's translation keys and the cloud's numeric /
      string state values.
    * **Status parser** (``parse_status``) — lift-and-shift of the
      existing per-family ``_update_<type>_robot_data`` method. Mutates
      the ``result`` dict in place to match pre-R25 behaviour exactly.
    """

    #: Cloud-side namespace identifier (matches ``_device_type``).
    namespace: str

    # -- Envelope builders --------------------------------------------------

    @abstractmethod
    def start_payload(self, client: AqualinkClient) -> dict[str, Any]:
        """Return the websocket envelope that starts a cleaning cycle."""

    @abstractmethod
    def stop_payload(self, client: AqualinkClient) -> dict[str, Any]:
        """Return the websocket envelope that stops a cleaning cycle."""

    def pause_payload(self, client: AqualinkClient) -> dict[str, Any] | None:
        """Return the pause envelope, or ``None`` if the family has no pause.

        Default is ``None`` (no-pause). Override for families that map
        pause to a distinct cloud state (vr/vortrax use ``state=2``).
        """
        return None

    def return_to_base_payload(self, client: AqualinkClient) -> dict[str, Any] | None:
        """Return the return-to-dock envelope, or ``None`` if unsupported."""
        return None

    def clear_desired_payload(self, client: AqualinkClient) -> dict[str, Any] | None:
        """Return an envelope that clears the cloud's ``desired.state``.

        Used by the PR #94 natural-completion path to prevent the cloud
        from auto-restarting after the robot reports idle. Only the VR
        family has documented behaviour here; default is ``None``.
        """
        return None

    @abstractmethod
    def fan_speed_codes(self) -> dict[str, Any]:
        """Return the translation_key → cloud-side encoding map.

        Values are the cloud's representation — int for VR's ``prCyc``,
        str for cyclobat's ``main.mode``, etc. The protocol's
        ``set_fan_speed_payload`` consumes this map.
        """

    def set_fan_speed_payload(
        self, client: AqualinkClient, fan_speed: str
    ) -> dict[str, Any] | None:
        """Return the envelope that sets the cloud's fan-speed value.

        Default implementation uses ``fan_speed_codes()`` to look up the
        cloud encoding and assumes the field is ``prCyc`` under ``robot``.
        Override for families with a different field name (cyclobat,
        cyclonext).
        """
        codes = self.fan_speed_codes()
        if fan_speed not in codes:
            return None
        return client._build_state_request(
            "setCleaningMode", {"prCyc": codes[fan_speed]}
        )

    # -- Cloud-encoding helpers --------------------------------------------

    def extract_fan_speed_from_response(
        self,
        payload: dict[str, Any],
        requested_fan_speed: str,
    ) -> str | None:
        """Inverse of ``set_fan_speed_payload`` — derive the translation
        key from a cloud-shaped response envelope, or return ``None`` if
        the response can't be decoded.

        Used by ``coordinator._extract_fan_speed_from_response`` so
        post-command updates report the cloud's actual mode (which may
        differ from the requested mode if the cloud overrode the call).
        """
        return None

    # -- Status parser -----------------------------------------------------

    @abstractmethod
    def parse_status(
        self,
        client: AqualinkClient,
        data: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Parse a per-family status payload into ``result`` in place.

        Lifts the body of pre-R25 ``_update_<type>_robot_data``. Takes
        ``client`` so it can reach the shared helpers
        (``_resolve_cycle_duration``, ``_calculate_times``,
        ``_format_time_human``) without duplicating them. Same in-place
        mutation semantics as the pre-R25 method — adds keys to ``result``
        rather than returning a new dict.
        """
