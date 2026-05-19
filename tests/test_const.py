"""Tests for ``const.py`` (story L22 — no sync I/O at module import).

L22 deleted the ``manifest.json`` file read that ran at module-import time.
HA's blocking-call detector in dev mode flagged that read because
``async_setup_entry``'s first import of any ``custom_components.*`` module
runs on the event loop thread on first load. Post-L22 there is zero file
I/O at import; ``DOMAIN`` is hardcoded (verified to match
``manifest.json`` by a unit test here AND by hassfest in CI).
"""

from __future__ import annotations

import builtins
import importlib
import json
import sys
from pathlib import Path

import pytest


def test_no_io_on_import() -> None:
    """AC#1: re-importing ``const`` must not perform any file I/O.

    Patches ``builtins.open`` to raise loudly, then forces a fresh
    ``importlib.reload`` of the const module. If any code path inside
    const.py touches the filesystem (open / Path.open / json.load on a
    file), the patched open raises and ``reload`` fails. A clean reload
    proves the AC.

    Also guards against an accidental future re-introduction of the
    manifest read pattern (or any other module-import I/O) — anyone
    re-adding ``open(manifest)`` at the top level will trip this test.
    """
    from custom_components.iaqualink_robots import const as const_mod

    original_open = builtins.open

    def _exploding_open(*args, **kwargs):
        raise AssertionError(
            "const.py must not perform file I/O at module-import time (L22 AC#1) — "
            f"open() was called with args={args!r} kwargs={kwargs!r}"
        )

    builtins.open = _exploding_open
    try:
        # Forcing a full reload re-executes the module body, which is the
        # codepath HA's blocking-call detector observes on first import.
        importlib.reload(const_mod)
    finally:
        builtins.open = original_open

    # Sanity: after reload, the public surface must still be present.
    assert const_mod.DOMAIN == "iaqualink_robots", (
        "const.DOMAIN must survive reload — sanity check on the AC#1 "
        "no-I/O reload path"
    )


def test_domain_matches_manifest() -> None:
    """AC#2 + hassfest alignment: hardcoded ``DOMAIN`` MUST equal the
    ``domain`` field in ``manifest.json``.

    Hassfest is the authoritative CI check (it errors when the directory
    name, ``domain`` in manifest, and the integration's ``DOMAIN`` constant
    disagree), but it runs as a separate workflow step. This unit test
    gives a faster, more localised diagnostic: a contributor who edits
    ``manifest.json`` without updating ``const.py`` (or vice versa) sees
    the failure here under the pytest job, with a clear "DOMAIN mismatch"
    message, rather than having to read a hassfest log.
    """
    from custom_components.iaqualink_robots.const import DOMAIN

    manifest_path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert DOMAIN == manifest["domain"], (
        f"const.DOMAIN ({DOMAIN!r}) must match manifest.json's `domain` "
        f"field ({manifest['domain']!r}). Hassfest enforces this — fix one or "
        f"the other (typically: update const.DOMAIN to match the manifest)."
    )


def test_orphan_startup_constants_are_gone() -> None:
    """AC#3: the orphan ``STARTUP`` banner block and its dependencies are
    deleted.

    Pre-L22, ``const.py`` exported ``NAME``, ``ISSUEURL``, ``VERSION``,
    ``STARTUP``, and the internal ``_MANIFEST_DATA`` / ``_load_manifest_data``
    helpers, all in service of a startup banner that was never printed
    anywhere in the package. Verified zero callers via grep before
    deletion. This test locks the contract — re-adding any of these names
    requires explicit intent + a test update, not an accidental import
    cargo-culted from another HA integration.
    """
    from custom_components.iaqualink_robots import const as const_mod

    for name in (
        "NAME",
        "ISSUEURL",
        "VERSION",
        "STARTUP",
        "_MANIFEST_DATA",
        "_load_manifest_data",
    ):
        assert not hasattr(const_mod, name), (
            f"const.{name} was deleted by L22 (orphan banner code, zero "
            f"callers anywhere in the package). Reintroduction would risk "
            f"bringing back the sync file I/O at module import that HA's "
            f"blocking-call detector flags. If you genuinely need this name, "
            f"add it via a different mechanism (e.g. async_get_integration "
            f"inside async_setup_entry) and update this test."
        )


def test_const_module_imports_no_io_modules() -> None:
    """Defence-in-depth: the const module body must not import ``json``
    or ``pathlib`` — those are the only stdlib modules the pre-L22
    manifest read needed. Their presence in const.py is a strong smell
    that someone is preparing to do I/O at import time again.

    A future const-py change that genuinely needs ``json`` (e.g. for a
    JSON-shaped constant literal) can extend this allowlist with a
    comment explaining why.
    """
    const_src = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "const.py"
    ).read_text(encoding="utf-8")

    forbidden_imports = ["import json", "from pathlib", "import pathlib"]
    found = [pat for pat in forbidden_imports if pat in const_src]
    assert not found, (
        f"const.py imports {found} — these are the imports the pre-L22 "
        f"manifest read pulled in. If you re-need them at import time, "
        f"verify there's no synchronous I/O on the loaded path (HA "
        f"blocking-call detector) and update this test's allowlist."
    )


def test_const_reload_is_idempotent() -> None:
    """Sanity: reloading const must produce the same DOMAIN value.

    If a future refactor moves ``DOMAIN`` resolution to module-level
    state that depends on something mutable (env var, sys.modules
    introspection, ...), this test catches it. Hardcoded literals are
    naturally idempotent — this is the regression guard for accidental
    dynamic resolution.
    """
    from custom_components.iaqualink_robots import const as const_mod

    before = const_mod.DOMAIN
    importlib.reload(const_mod)
    after = const_mod.DOMAIN
    assert before == after == "iaqualink_robots"


# Ensure reload during this test module doesn't leave a half-loaded
# const_mod in sys.modules for other tests — restore on session teardown.
@pytest.fixture(autouse=True, scope="module")
def _reload_const_on_teardown():
    yield
    # Force a clean reload at module teardown so subsequent test modules
    # see the canonical const namespace, not whatever state our reload
    # tests left in place.
    if "custom_components.iaqualink_robots.const" in sys.modules:
        importlib.reload(sys.modules["custom_components.iaqualink_robots.const"])
