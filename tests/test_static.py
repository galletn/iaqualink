"""Static-analysis tests (story M15).

These tests are pure-source-text greps over the ``custom_components/iaqualink_robots``
package. They lock in invariants about the *shape* of the public API that
unit tests are not well placed to catch (you'd have to instantiate the world
to notice a stray ``client._serial`` in production code).

Story M15 makes ``device_type`` and ``serial`` public ``@property`` on
``AqualinkClient`` (matching the existing ``username`` / ``robot_id``
pattern), exposes ``title`` as a public attribute on the coordinator, and
swaps every external reach-through to use those names. The greps below fail
on regressions of either half of that contract.

Brittleness note
----------------
Like ``test_button_commands_parity.py``, this test parses source text with
regexes. Two coarse filters help: ``_scan`` skips lines whose stripped form
starts with ``#``, so a deprecation comment like
``# Do not use coordinator._title -- use coordinator.title instead`` does not
trip CI. Docstring lines are NOT skipped (we'd need a tokenizer); keep
sample-code references inside docstrings to canonical-OK names
(``coordinator.title``, ``client.serial``) and the regexes will stay quiet.
If a future refactor changes how attributes are accessed
(``operator.attrgetter("_serial")``, ``vars(client)["_serial"]``,
``client.__dict__["_serial"]``, etc.), extend the patterns rather than
relaxing them.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = (
    Path(__file__).parent.parent / "custom_components" / "iaqualink_robots"
)

_ALL_PYTHON_FILES = sorted(PACKAGE.glob("*.py"))

# Match ``client._serial`` / ``client._device_type`` / ``self.client._serial`` /
# ``self._client._serial`` etc. The optional ``_?`` before ``client`` catches
# the ``_client`` binding pattern that ``button.py`` uses internally for its
# AqualinkClient reference (``self._client``); without that, a reintroduced
# ``self._client._serial`` would slip past CI. Internal ``self._serial`` reads
# from within ``AqualinkClient`` are still ignored: those have no ``client``
# token in them at all.
_CLIENT_PRIVATE_READ = re.compile(
    r"""(?<!\w)                         # left boundary: not part of a longer word
        (?:[A-Za-z_]\w*\.)*             # optional dotted prefix (e.g. ``self.``)
        _?client                        # literal ``client`` or ``_client``
        \._                             # dot + underscore (private attr)
        (serial|device_type|model)\b    # captured attr name (model added in P9)
    """,
    re.VERBOSE,
)

# Catch ``getattr(self.client, '_serial', ...)`` / ``getattr(client, "_device_type", ...)``.
# This complements ``_CLIENT_PRIVATE_READ`` because the dotted access there
# misses the ``getattr`` form (the attr name is a string literal, not a
# real ``.attr`` access). Both forms are reach-through. Whitespace around the
# comma is tolerated.
_CLIENT_GETATTR_PRIVATE = re.compile(
    r"""getattr\(\s*
        (?:[A-Za-z_]\w*\.)*_?client\s*,\s*
        ["']_(serial|device_type|model)["']
    """,
    re.VERBOSE,
)

# Match ``coordinator._title`` (reads or writes). Same shape as above.
_COORDINATOR_PRIVATE_TITLE = re.compile(
    r"""(?<!\w)
        (?:[A-Za-z_]\w*\.)*
        _?coordinator
        \._title\b
    """,
    re.VERBOSE,
)

# Catch ``getattr(self.coordinator, '_title', ...)`` mirror — same reasoning
# as ``_CLIENT_GETATTR_PRIVATE`` (the dotted regex misses string-keyed access).
_COORDINATOR_GETATTR_TITLE = re.compile(
    r"""getattr\(\s*
        (?:[A-Za-z_]\w*\.)*_?coordinator\s*,\s*
        ["']_title["']
    """,
    re.VERBOSE,
)


def _scan(pattern: re.Pattern[str], files: list[Path]) -> list[tuple[Path, int, str]]:
    """Return (path, line_no, line) for every match across ``files``.

    Skips lines whose stripped form starts with ``#`` (Python single-line
    comments) so deprecation notes and TODOs mentioning the underscored
    names don't trip CI. Docstring lines are NOT skipped — keep example
    code inside docstrings to the canonical-OK names if you want to stay
    out of these patterns.
    """
    hits: list[tuple[Path, int, str]] = []
    for path in files:
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                hits.append((path, line_no, line.rstrip()))
    return hits


def test_no_external_reach_through_to_client_private_serial_or_device_type() -> None:
    """No external read of ``client._serial`` or ``client._device_type``.

    These are exposed as ``@property serial`` and ``@property device_type``
    on ``AqualinkClient`` (story M15). Every caller -- ``button.py``,
    ``sensor.py``, ``__init__.py``, and the coordinator class itself --
    must use the public names. Internal ``self._serial`` /
    ``self._device_type`` reads from within ``AqualinkClient`` are
    excluded by construction: the regex anchors on a ``client.`` prefix.
    """
    hits = _scan(_CLIENT_PRIVATE_READ, _ALL_PYTHON_FILES) + _scan(
        _CLIENT_GETATTR_PRIVATE, _ALL_PYTHON_FILES
    )
    assert not hits, (
        "Private reach-through detected. Replace ``client._serial`` / "
        "``client._device_type`` reads with the public ``client.serial`` / "
        "``client.device_type`` properties (story M15); replace ``client._model`` "
        "reads with ``coordinator.data.get('model')`` (story P9, where "
        "``device.build_device_info`` centralises the live-model lookup):\n"
        + "\n".join(f"  {p.name}:{n}: {line}" for p, n, line in hits)
    )


def test_no_external_reach_through_to_coordinator_private_title() -> None:
    """No external read/write of ``coordinator._title``.

    The coordinator exposes ``title`` as a public attribute (story M15).
    External callers (``__init__.py`` setting the entry title,
    ``sensor.py`` reading the display name) must use the public name.
    """
    hits = _scan(_COORDINATOR_PRIVATE_TITLE, _ALL_PYTHON_FILES) + _scan(
        _COORDINATOR_GETATTR_TITLE, _ALL_PYTHON_FILES
    )
    assert not hits, (
        "Private reach-through detected. Replace ``coordinator._title`` "
        "with the public ``coordinator.title`` attribute (story M15):\n"
        + "\n".join(f"  {p.name}:{n}: {line}" for p, n, line in hits)
    )


# ---------------------------------------------------------------------------
# Story L18 — dead-state guard
# ---------------------------------------------------------------------------

# Names removed in story L18. The package must never re-introduce these
# attribute names — they accumulated as dead state across the lifetime of
# the integration and confused future readers about whether they had real
# behavioural meaning.
#
#   * ``_cached_stepper_value`` / ``_cached_stepper_time`` — written by the
#     C4-ws push path under a comment claiming ``button.py`` depended on
#     them, but ``button.py`` never read either. Pure dead writes.
#   * ``_should_stop_listener`` — set to ``True`` on stop and ``False`` on
#     start, but the websocket-listener loop never consulted it. Real
#     cancellation goes through ``asyncio.Task.cancel()`` (hardened by P10).
#   * ``_get_close_code_info`` — a websocket-close-code lookup helper with
#     zero callers anywhere in the package.
_L18_DEAD_NAMES = (
    "_cached_stepper_value",
    "_cached_stepper_time",
    "_should_stop_listener",
    "_get_close_code_info",
)


def test_no_dead_state() -> None:
    """L18: dead attributes and methods must not reappear in the package.

    Each name in ``_L18_DEAD_NAMES`` was confirmed by audit to have zero
    readers / callers before deletion. If a future change reintroduces one,
    it's almost certainly cargo-culted from old code — CI fails and the
    author has to either repair the reader path or pick a different name.
    """
    offenders: list[tuple[str, int, str, str]] = []  # (filename, line_no, name, line)
    for path in _ALL_PYTHON_FILES:
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            for name in _L18_DEAD_NAMES:
                if name in line:
                    offenders.append((path.name, line_no, name, line.rstrip()))

    assert not offenders, (
        "Story L18 dead-state names reintroduced. Each name below was "
        "removed for having zero readers/callers — if you need behaviour "
        "the old name implied, build a real path that's actually exercised:\n"
        + "\n".join(
            f"  {fname}:{lno}: {name!r} -> {line}"
            for fname, lno, name, line in offenders
        )
    )


def test_aqualink_client_exposes_device_type_and_serial_as_properties() -> None:
    """``AqualinkClient.device_type`` and ``AqualinkClient.serial`` are ``@property`` descriptors.

    Asserted at the class level (``getattr(type(...), name)``) so we don't
    need an instance. This locks the M15 contract: a future refactor that
    drops the property and exposes a plain attribute named ``device_type``
    would still pass an instance-level ``hasattr`` check but break the
    "no-reach-through" guarantee, because callers could no longer rely on
    a stable descriptor shape. Failure here means the property was removed
    or renamed -- restore it before landing.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    device_type_attr = getattr(AqualinkClient, "device_type", None)
    serial_attr = getattr(AqualinkClient, "serial", None)

    assert isinstance(device_type_attr, property), (
        "AqualinkClient.device_type must be a @property (story M15). "
        f"Got: {type(device_type_attr).__name__}"
    )
    assert isinstance(serial_attr, property), (
        "AqualinkClient.serial must be a @property (story M15). "
        f"Got: {type(serial_attr).__name__}"
    )
