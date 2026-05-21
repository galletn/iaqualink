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
#:
#: R25-followup D1: ``cyclonext_vr`` registered defensively — the
#: R25-vr story spec explicitly named it as a VR-family variant
#: (line 21: "vr + cyclonext_vr variants — they share most of the
#: protocol") and required the dispatch entry. The R25-vr
#: implementation silently dropped it. The codebase has no evidence
#: of this device_type string today, but if any user's cloud account
#: emits it the integration would crash with ``AttributeError`` on
#: ``get_protocol("cyclonext_vr").start_payload(self)``. Registering
#: defensively to VRProtocol matches the spec's stated "share the
#: protocol" intent; if a future audit shows the protocols actually
#: diverge, this entry can be swapped to a dedicated CycloNextVR
#: subclass.
_VR_PROTOCOL_SINGLETON = VRProtocol()

_DEVICE_PROTOCOLS: dict[str, RobotProtocol] = {
    "vr": _VR_PROTOCOL_SINGLETON,
    # cyclonext_vr resolves to the SAME singleton — see comment above.
    "cyclonext_vr": _VR_PROTOCOL_SINGLETON,
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
