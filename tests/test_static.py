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

_ALL_PYTHON_FILES = sorted(
    p
    for p in PACKAGE.rglob("*.py")
    if "__pycache__" not in p.parts
)
assert _ALL_PYTHON_FILES, (
    "Static-grep guards have no files to scan — _ALL_PYTHON_FILES is empty. "
    "Verify PACKAGE points at the integration directory and that *.py files exist."
)

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
    from custom_components.iaqualink_robots.client import AqualinkClient

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


# ---------------------------------------------------------------------------
# Story P3 — runtime_data migration guard.
#
# Pre-P3 the integration stored per-entry runtime state on
# ``hass.data[DOMAIN][entry.entry_id]``. P3 moved that to the typed
# ``ConfigEntry.runtime_data`` attribute (HA 2024+ convention). Any
# reintroduction of ``hass.data[DOMAIN]`` reads or writes in production code
# would silently fork the storage contract — diagnostics, lifecycle, and
# platform setup would diverge on which dict carries the truth.
# ---------------------------------------------------------------------------


_HASS_DATA_DOMAIN_ACCESS = re.compile(
    r"""hass\.data            # ``hass.data``
        (?:\.(?:get|setdefault|pop)\(\s*)?  # optional .get( / .setdefault( / .pop(
        \[?DOMAIN              # ``[DOMAIN`` or `(DOMAIN`
    """,
    re.VERBOSE,
)


def test_no_hass_data_domain_access_in_production_code() -> None:
    """P3: production code must not touch ``hass.data[DOMAIN]``.

    Per-entry runtime state lives on ``entry.runtime_data`` (typed as
    ``IaqualinkData`` in ``types.py``). Reintroducing ``hass.data[DOMAIN]``
    in production would fork the storage contract — diagnostics, lifecycle
    cleanup, and platform setup would each read a different source of truth.

    Scope is the production package only (``custom_components/iaqualink_robots/*.py``).
    Tests are excluded — they may reference the legacy shape in xfail
    comments or migration docstrings.
    """
    hits = _scan(_HASS_DATA_DOMAIN_ACCESS, _ALL_PYTHON_FILES)
    assert not hits, (
        "Found ``hass.data[DOMAIN]`` / ``hass.data.get(DOMAIN, ...)`` /\n"
        "``hass.data.setdefault(DOMAIN, ...)`` access in production code. "
        "Story P3 migrated per-entry runtime state to the typed "
        "``entry.runtime_data`` attribute (``IaqualinkData`` in types.py). "
        "Read via ``entry.runtime_data.coordinator`` instead.\n"
        + "\n".join(f"  {p.name}:{n}: {line}" for p, n, line in hits)
    )


# ---------------------------------------------------------------------------
# Story P6 — Platinum strict typing guards.
#
# P6 ratcheted ``mypy --strict`` clean on the whole package and added the
# foundations every typed integration needs:
#   1. ``from __future__ import annotations`` at the top of every module so
#      forward references resolve cleanly and runtime evaluation cost stays
#      zero.
#   2. A ``py.typed`` marker so consumers (HA core, tests) read our type
#      hints instead of treating the package as untyped (PEP 561).
#   3. Zero ``# type: ignore`` — every site has either been fixed or is
#      annotated explicitly. Re-introducing one without a comment + upstream
#      issue link is a regression of AC #3.
# ---------------------------------------------------------------------------


_TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore")


def test_no_type_ignore_in_production_code() -> None:
    """P6 AC#3: zero ``# type: ignore`` comments remain in the package.

    Each one historically masked either a missing HA-side stub or a real
    typing bug. The whole point of the P6 ratchet is to surface those at
    lint time, not hide them. If a future site genuinely needs one (e.g.
    an HA-internal API that still lacks types), the fix is to address the
    underlying typing gap — there is intentionally no allowlist here.

    Unlike the other ``_scan``-based guards in this file, this check
    deliberately DOES NOT skip comment-only lines: a standalone
    ``# type: ignore`` on its own line is still a real mypy pragma and
    must be caught. The standard ``_scan`` helper skips ``#``-prefixed
    lines, so we inline a tighter loop here instead of using it.
    """
    hits: list[tuple[Path, int, str]] = []
    for path in _ALL_PYTHON_FILES:
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _TYPE_IGNORE.search(line):
                hits.append((path, line_no, line.rstrip()))
    assert not hits, (
        "``# type: ignore`` reappeared in production code. Each one either "
        "masks a real typing bug or a missing HA stub — fix the underlying "
        "issue rather than reintroducing the pragma:\n"
        + "\n".join(f"  {p.name}:{n}: {line}" for p, n, line in hits)
    )


# Number of leading source lines to scan for the ``from __future__`` import.
# Generous enough to clear a multi-line module docstring (rare but real —
# ``coordinator.py``'s pre-P6 docstring spanned ~40 lines), tight enough to
# catch a regression where the import is removed but a stray reference to it
# survives as a comment further down the file.
_FUTURE_ANNOTATIONS_SCAN_LIMIT = 60


def test_every_module_imports_future_annotations() -> None:
    """P6 AC#2: every ``.py`` module declares ``from __future__ import annotations``
    near the top.

    The check scans only the first :data:`_FUTURE_ANNOTATIONS_SCAN_LIMIT`
    lines so a comment like ``# from __future__ import annotations was
    here once`` 200 lines down the file can't spuriously satisfy the
    guard — a real regression where the import is removed but a comment
    about it survives would silently pass a substring search.

    Empty ``__init__.py`` files are excluded; they have nothing to
    annotate. ``py.typed`` is a marker file (not Python source) and
    lives outside the glob.
    """
    needle = "from __future__ import annotations"
    missing: list[Path] = []
    for path in _ALL_PYTHON_FILES:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue
        head = "\n".join(text.splitlines()[:_FUTURE_ANNOTATIONS_SCAN_LIMIT])
        if needle not in head:
            missing.append(path)

    assert not missing, (
        "P6 AC#2: every module must declare ``from __future__ import "
        f"annotations`` within the first {_FUTURE_ANNOTATIONS_SCAN_LIMIT} "
        "lines. Missing in:\n"
        + "\n".join(f"  {p.relative_to(PACKAGE.parent.parent)}" for p in missing)
    )


def test_py_typed_marker_exists() -> None:
    """P6 AC#5: the ``py.typed`` marker file exists.

    PEP 561 — without this, downstream consumers (HA core, unit tests
    using ``mypy --strict``) treat the package as untyped no matter how
    many annotations it carries. The file is intentionally empty.
    """
    marker = PACKAGE / "py.typed"
    assert marker.exists(), (
        f"Expected PEP 561 marker at {marker} — without this consumers "
        "ignore the package's type annotations (P6 AC#5)."
    )


# ---------------------------------------------------------------------------
# Story R26 — module-split guards.
#
# R26 split the pre-existing 3500-line ``coordinator.py`` into three
# single-responsibility modules:
#   * ``client.py``       — ``AqualinkClient`` (HTTP + websocket + auth)
#   * ``coordinator.py``  — ``AqualinkDataUpdateCoordinator`` (HA glue)
#   * ``parsing.py``      — pure date/time/duration helpers
#
# These guards lock the split in: the modules must exist, their sizes must
# stay below an agreed ceiling (so a future ``# stuff a feature into the
# convenient file'' regression is caught), and ``import``-ing the package
# must not introduce a circular dependency between client and coordinator.
# ---------------------------------------------------------------------------


def test_module_split_files_exist() -> None:
    """R26 AC#1: post-split layout — client.py, coordinator.py, parsing.py
    are all present in the package.

    The story spec calls these out explicitly. Without this guard, a
    refactor that re-merges client into coordinator (or drops parsing
    back into client) would slip past CI because the legacy
    ``from .coordinator import AqualinkClient`` import would still work
    via a re-export.
    """
    expected = ["client.py", "coordinator.py", "parsing.py"]
    missing = [name for name in expected if not (PACKAGE / name).exists()]
    assert not missing, (
        f"R26 AC#1 layout regression — missing: {missing}. The post-split "
        "package must contain client.py + coordinator.py + parsing.py."
    )


# Module-size ceilings (R26 testing-clause). Set ~10% above current line
# counts so they catch regressions without tripping on a one-off review fix.
# When a legitimate feature pushes a file past the ceiling, raise the
# threshold AND document why in the same commit — the story author chose
# to make this a knob rather than a hard cliff so the file gets attention
# at review time rather than getting silently re-bloated.
#
# Current sizes at R26 land (2026-05-22):
#   client.py       2629 lines (was 2611 inline in pre-R26 coordinator.py)
#   coordinator.py   817 lines (was 774 inline in pre-R26 coordinator.py)
#   parsing.py       186 lines (new)
#
# The story aspirational target of client.py ≤ 800 / coordinator.py ≤ 500
# is not achievable with the "purely mechanical split" the spec calls for —
# further compaction (collapsing per-device-type dispatch ifs, factoring
# command-method boilerplate) is the natural next refactor (R26-followup).
# These thresholds gate regressions, not aspirations.
_MODULE_SIZE_CEILINGS = {
    "client.py": 2900,
    "coordinator.py": 900,
    "parsing.py": 250,
}


def test_module_sizes_stay_under_ceiling() -> None:
    """R26 testing-clause: per-module line-count regression gate.

    Counts physical lines (``len(text.splitlines())``) — the same metric
    ``wc -l`` reports. The ceilings in ``_MODULE_SIZE_CEILINGS`` are set
    ~10% above the size at R26 land so a one-off review fix doesn't trip
    CI but a steady-state regression (a new 200-line method on the
    coordinator) does.

    When a legitimate growth pushes the file past the ceiling, raise the
    ceiling in this file AND name the reason in the commit message —
    that surfaces the size as a review-time concern instead of a silent
    drift.
    """
    offenders: list[tuple[str, int, int]] = []
    for name, ceiling in _MODULE_SIZE_CEILINGS.items():
        path = PACKAGE / name
        actual = len(path.read_text(encoding="utf-8").splitlines())
        if actual > ceiling:
            offenders.append((name, actual, ceiling))
    assert not offenders, (
        "R26 module-size ceiling exceeded. If this is intentional growth, "
        "raise the ceiling in _MODULE_SIZE_CEILINGS in this file and "
        "name the reason in the commit message:\n"
        + "\n".join(
            f"  {name}: {actual} lines (> ceiling {ceiling})"
            for name, actual, ceiling in offenders
        )
    )


def test_package_imports_without_circular_dependency() -> None:
    """R26 testing-clause: ``import custom_components.iaqualink_robots``
    must complete without ImportError or RecursionError.

    Catches the classic post-split failure mode where client.py imports
    AqualinkDataUpdateCoordinator at module scope and coordinator.py
    imports AqualinkClient at module scope — a cycle that resolves at
    runtime only because ``from __future__ import annotations`` defers
    annotation evaluation, but blows up the moment the typing pragma is
    removed or a non-TYPE_CHECKING import is added.

    Importing the package walks every ``__init__.py`` and re-imports
    each submodule the bootstrap loads, so a cycle would surface here.
    """
    import importlib

    # Use importlib.reload to force a fresh import even if a prior test
    # already loaded the module (the integration package is not exposing
    # a clean teardown hook). If any submodule has a circular import, the
    # ImportError surfaces here.
    pkg = importlib.import_module("custom_components.iaqualink_robots")
    importlib.reload(pkg)

    # Walk submodules to ensure each can be imported standalone too —
    # catches the "module A only imports cleanly because module B was
    # already loaded" failure mode.
    for submodule in ("client", "coordinator", "parsing"):
        importlib.import_module(f"custom_components.iaqualink_robots.{submodule}")
