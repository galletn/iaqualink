"""Button-command parity test (story M18).

Locks in the invariant that every command exposed by ``button.py`` as a
``AqualinkRemoteButton`` is also listed in ``__init__._M12_BUTTON_COMMANDS``.

Why this matters
----------------
The v1->v2 migration in ``async_migrate_entry`` walks the registry and rewrites
each legacy button ``unique_id`` from a title-derived form to a serial-based
form, keying off the command suffix. The suffix list is hard-coded as the
tuple ``_M12_BUTTON_COMMANDS`` (kept self-contained on purpose; see the comment
above that tuple in ``__init__.py``).

If a future developer adds a new ``AqualinkRemoteButton`` row in ``button.py``
but forgets to extend the migration tuple, *only* the legacy unique_ids of
that new command silently fail to migrate. The user then ends up with both
the legacy and the v2 entity in their registry after the upgrade -- a quiet,
hard-to-diagnose duplication bug. This test catches the drift in CI.

Direction of the assertion (intentional)
----------------------------------------
We require ``button.py`` commands to be a **subset** of ``_M12_BUTTON_COMMANDS``.
We deliberately do **not** require equality. The tuple is allowed to be a
strict superset: historical commands that were removed from ``button.py`` must
still be migrated for users who upgrade from very old versions, so old entries
linger in the tuple by design.

Brittleness note
----------------
This test parses ``button.py`` with a regex over the source text. That is
intentional (per the M18 spec, Option A) to keep ``button.py`` itself
untouched. The trade-off is that a sufficiently aggressive refactor of
``button.py`` (e.g. moving the command literals into a list comprehension,
a dict, or a separate module) can make the regex stop matching. The
``test_button_commands_parity_regex_matches_something`` guard below fires in
that case with a clear pointer at what to do: either re-tune the regex
**or** refactor to expose a module-level ``BUTTON_COMMANDS`` constant in
``button.py`` and switch this test to import it (Option B from the M18 spec).
"""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.iaqualink_robots import _M12_BUTTON_COMMANDS

BUTTON_PY = (
    Path(__file__).parent.parent
    / "custom_components"
    / "iaqualink_robots"
    / "button.py"
)

# Match the third positional argument to AqualinkRemoteButton(...), which is
# the command string. Example line we want to match:
#     AqualinkRemoteButton(coordinator, client, "forward", "remote_forward", ...
# We anchor on the class name and the two preceding args so we don't match
# random quoted strings elsewhere in the file.
_COMMAND_PATTERN = re.compile(
    r"""AqualinkRemoteButton\(\s*       # class call open
        coordinator\s*,\s*               # arg 1
        client\s*,\s*                    # arg 2
        ["']([^"']+)["']                 # arg 3: the command literal (captured)
    """,
    re.VERBOSE,
)


def _parse_button_commands_from_source() -> list[str]:
    """Extract command literals from the AqualinkRemoteButton(...) calls in button.py."""
    source = BUTTON_PY.read_text(encoding="utf-8")
    return _COMMAND_PATTERN.findall(source)


def test_button_commands_parity_regex_matches_something() -> None:
    """Defensive: a future refactor of button.py must not silently zero out this test.

    If this fails, the regex in ``_COMMAND_PATTERN`` no longer recognises how
    button.py instantiates buttons. Either retune the pattern, or -- preferred
    -- refactor button.py to expose a module-level ``BUTTON_COMMANDS`` tuple
    and switch this test to import it directly (Option B from story M18).
    """
    matched = _parse_button_commands_from_source()
    assert matched, (
        "M18 parity test parsed zero commands from button.py. The regex no longer "
        "matches the AqualinkRemoteButton(...) call shape. Either re-tune "
        "tests/test_button_commands_parity.py::_COMMAND_PATTERN, or refactor "
        "button.py to expose a BUTTON_COMMANDS module constant (M18 Option B) "
        "and have this test import it."
    )


def test_button_commands_are_subset_of_migration_tuple() -> None:
    """Every command in button.py must appear in __init__._M12_BUTTON_COMMANDS.

    Superset is allowed (legacy commands removed from button.py must still be
    migrated for old user installs). What's forbidden is a command in button.py
    that's missing from the migration tuple -- that's the drift that produces
    duplicate registry entries on upgrade.
    """
    button_commands = set(_parse_button_commands_from_source())
    migration_commands = set(_M12_BUTTON_COMMANDS)

    missing = button_commands - migration_commands
    assert not missing, (
        "Drift detected: button.py exposes command(s) that the v1->v2 migration "
        f"in __init__.py does not know how to rewrite: {sorted(missing)}. "
        "Add the missing command(s) to _M12_BUTTON_COMMANDS in "
        "custom_components/iaqualink_robots/__init__.py, otherwise users "
        "upgrading from v1 will end up with duplicate button entities in their "
        "entity registry (legacy + new) for these commands."
    )
