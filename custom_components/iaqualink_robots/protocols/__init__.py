"""Per-device-type protocol strategies (story R25).

A ``RobotProtocol`` encapsulates everything that varies between the
five device families (vr / vortrax / cyclobat / cyclonext / i2d).
See ``base.py`` for the contract; one module per family.

The ``_DEVICE_PROTOCOLS`` table is the dispatch source of truth.
``get_protocol(device_type)`` returns the matching instance, or
``None`` for families that haven't been extracted yet — the
coordinator's inline if/elif branches carry those until their
matching R25 story lands.
"""

from __future__ import annotations

from .base import RobotProtocol
from .vr import VRProtocol

#: Dispatch table — ``device_type`` → protocol instance. Extended as each
#: R25 story lands (R25-vortrax / cyclobat / cyclonext / i2d are pending).
_DEVICE_PROTOCOLS: dict[str, RobotProtocol] = {
    "vr": VRProtocol(),
}


def get_protocol(device_type: str) -> RobotProtocol | None:
    """Return the protocol for ``device_type``, or ``None`` if it hasn't
    been extracted yet. Callers must fall through to the legacy inline
    branch when ``None`` is returned (until all five families ship).
    """
    return _DEVICE_PROTOCOLS.get(device_type)


__all__ = ["RobotProtocol", "VRProtocol", "get_protocol"]
