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
from .cyclobat import CycloBatProtocol
from .cyclonext import CycloNextProtocol
from .i2d import I2DProtocol
from .vortrax import VortraxProtocol
from .vr import VRProtocol

#: Dispatch table — ``device_type`` → protocol instance. All 5 R25
#: families now extracted; ``get_protocol()`` returns a protocol for
#: every device type the integration supports.
_DEVICE_PROTOCOLS: dict[str, RobotProtocol] = {
    "vr": VRProtocol(),
    "vortrax": VortraxProtocol(),
    "cyclobat": CycloBatProtocol(),
    "cyclonext": CycloNextProtocol(),
    "i2d_robot": I2DProtocol(),
}


def get_protocol(device_type: str) -> RobotProtocol | None:
    """Return the protocol for ``device_type``, or ``None`` for an
    unknown family. Post-R25-i2d every supported family has an entry;
    the ``None`` return path exists for defence-in-depth against a
    cloud bug surfacing an unrecognised ``device_type`` string.
    """
    return _DEVICE_PROTOCOLS.get(device_type)


__all__ = [
    "CycloBatProtocol",
    "CycloNextProtocol",
    "I2DProtocol",
    "RobotProtocol",
    "VortraxProtocol",
    "VRProtocol",
    "get_protocol",
]
