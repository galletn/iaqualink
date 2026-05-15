# Claude orientation

This file is for Claude Code sessions (and any other AI agents) picking up work in this repo. It captures project-specific context that isn't obvious from a single-file read but matters from turn 1.

## What this is

Home Assistant custom integration for **iAqualink robotic pool cleaners** (Zodiac, Polaris, Aqua Products and rebrands). Domain: `iaqualinkRobots`. The main code lives in [`custom_components/iaqualink_robots/`](custom_components/iaqualinkrobots/).

The user-facing entry point is the config flow in [`config_flow.py`](custom_components/iaqualinkrobots/config_flow.py); the heavy lifting (cloud auth, websocket, per-device parsing) is in [`coordinator.py`](custom_components/iaqualinkrobots/coordinator.py).

## Device types — the most important gotcha

The integration handles **5 distinct device families** with **different cloud encodings**. Mismatching them silently breaks fan-speed / cycle commands. The split lives in `coordinator.py::AqualinkClient._set_other_fan_speed` and the per-type branches in [`vacuum.py::IAquaLinkRobotVacuum`](custom_components/iaqualinkrobots/vacuum.py):

| `device_type` | Modes (`_fan_speed_list`) | Example models |
|---|---|---|
| `vr` | `wall_only` / `floor_only` / `smart_floor_and_walls` / `floor_and_walls` | Polaris VRX iQ+, RA 65xx iQ, RA 6900 iQ |
| `vortrax` | `floor_only` / `floor_and_walls` | VortraX TRX 8500 iQ |
| `cyclobat` | `wall_only` / `floor_only` / `smart_floor_and_walls` / `floor_and_walls` | RF 5600 iQ, P965 iQ, OA 6400 iQ |
| `cyclonext` | `floor_only` / `floor_and_walls` | CNX 30/40/50/4090 iQ, RE 44xx/46xx iQ, EvoluX EX5000 iQ |
| `i2d_robot` | `floor_only` / `walls_only` / `floor_and_walls` | OV 5490 iQ, EX 4000 iQ |

**`wall_only` (singular) and `walls_only` (plural) are NOT the same key.** They are distinct cloud-side modes for different families. `wall_only` is the vr/cyclobat dedicated wall-scrubbing cycle; `walls_only` is i2d's waterline-only mode (0x04). The parity test `tests/test_vacuum.py::test_wall_only_and_walls_only_are_distinct_keys` locks this in — do not "fix the typo" by collapsing them.

The `vacuum.py` per-type branching and the `_set_other_fan_speed` per-type `cycle_speed_map`s **must stay in sync**. Both must use snake_case keys (the translation key, not the display name). Issue #76 was both halves of this contract drifting.

## Tests — Windows constraint

`pytest` **cannot run on Windows** in this repo. Home Assistant's test runner imports `fcntl`, which is Unix-only. The CI GitHub Actions runner (`ubuntu-latest`) is the verifier.

For local-on-Windows validation, use:
```powershell
python -m py_compile <file.py>      # Python syntax
# JSON: Get-Content file.json -Raw | ConvertFrom-Json
```

CI (`.github/workflows/ci.yaml`) runs: hassfest (informational), flake8, mypy (currently `ignore_errors=True` per-module — story P6 ratchets that), pytest with coverage.

## Polling cadence — do not slow it globally

[`const.py`](custom_components/iaqualinkrobots/const.py) sets `SCAN_INTERVAL = 3s`. This is deliberately fast so the **vacuum entity's state reflects remote-control button presses (forward/backward/rotate/stop) within a few seconds**. Slowing it globally is a UX regression.

When users complain about activity-log spam from frequently-changing sensors, the right fix is **per-sensor**, not global. See the `include_seconds_remaining` option (commit `30eb650`) as the pattern: an OptionsFlow-toggleable knob that gates whether `time_remaining_human` includes seconds, defaulted to historical behavior, reloads on toggle via `entry.add_update_listener`.

## BMad story system (planning artifacts are gitignored)

Planning lives under `_bmad-output/` (gitignored, local-only). Key files:

- [`_bmad-output/planning-artifacts/stories/EPICS.md`](_bmad-output/planning-artifacts/stories/EPICS.md) — backlog index, ~43 stories across 11 epics
- `_bmad-output/planning-artifacts/stories/story-<KEY>-<slug>.md` — one file per story, with `Status: backlog | in-progress | done`
- [`_bmad-output/implementation-artifacts/deferred-work.md`](_bmad-output/implementation-artifacts/deferred-work.md) — items absorbed/moved/dropped from code reviews, annotated with owning story keys

Story key prefixes: **P** (Platinum / project foundation), **C** (Config flow), **M** (Medium hygiene), **H** (High coordinator/auth), **L** (Low cleanup), **R** (Refactor). Numbering is sequential within prefix.

Code reviews are run via `bmad-code-review` skill — they produce findings triaged into decision-needed / patch / defer / dismiss buckets, with deferred items added to `deferred-work.md`.

## Commit conventions

- **Per-story commits** when scope is clear: `<KEY>: <one-line summary>` body explains the change. Multi-AC stories (e.g. M19) get one commit per AC: `<KEY> AC#N: <scope>`.
- **Review follow-ups**: `<KEY> review follow-up: <theme>`.
- **Bug fixes**: `fix(<area>): <summary> (#<issue>)`.
- **Features**: `feat: <summary>` or `<KEY>: <summary>` if tied to a story.
- **Docs**: `docs: <summary>`.

Every commit ends with the Claude co-author footer:
```
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Always add a corresponding [`CHANGELOG.md`](CHANGELOG.md) entry under `## Unreleased > ### Added | Changed | Fixed | Migration / manual cleanup notes` as appropriate. The Definition of Done (from EPICS.md) requires this.

Push directly to `main` — this project does not gate routine work behind PRs, though `gh pr create` is fine for larger collaborations.

## Translations

10 locale files (`strings.json` + `translations/{cs,de,en,es,fr,it,nl,pt,sk}.json`). When adding a user-facing string:

1. Add to `strings.json` (canonical) and `translations/en.json` with the English text.
2. Add the same key to all 8 non-English files with the **English text as fallback** (matches the pattern established for `no_serial` in C2). Native translations are welcomed via separate PRs.

Story **P7** (translations completeness) is the dedicated place to roll up native translations; until it lands, English-fallback is the convention.

## Current state (as of `ab2d37b`)

**6 stories done**: C2 (unique_id), M12 (button unique_ids), M17 (api_key drop), H9a (JWT exp decode), M18 (parity test), M19 (defensive hardening bundle). Plus P1/P2 (test/CI scaffolding) shipped earlier and bookkeeping-flipped to done.

**Open issues** worth glancing at:

- `#76` — vacuum fan_speed bugs — **fixed** in `6bdefc5` + `ab2d37b`, awaiting user confirmation
- `#88` — EX6000 reporting as "unknown" model — README updated, underlying detection fix still open
- `#76` (related) — `#79`, `#81` — entity/translation polish, belongs to P7 / P11
- Discussion `#91` — reporting interval — partially addressed by `include_seconds_remaining` toggle

**Obvious next work**:

- **H9b** — 401-on-stale-token wrapper + `ConfigEntryAuthFailed` + `asyncio.Lock` around `_authenticate` + WARNING rate-limit. Picks up 3 absorbed items from the H9a review.
- **M15** — public `serial` / `device_type` / `title` properties. Unblocks the `client._serial` TODO in `button.py` and the `coordinator._title` reach-through in tests. Picks up 3 absorbed items from the M12 review.
- **C6** — auth-before-discovery (dedupe before expensive cloud calls). Depends on H9b's design.

See `deferred-work.md` for the full triage table mapping review findings to owning stories.

## Tone for working in this repo

The user (Galletn) prefers:

- **Decisive action**: when scope is clear and risk is low, just do it. Don't ask 4 questions when 1 is enough.
- **Defensible work over dropping it**: see `~/.claude/projects/.../memory/feedback_drop_calls.md`. Drop calls require strong justification, not "low likelihood" hand-waving.
- **Parallel agents in worktrees** for parallelizable work. Worktree isolation prevents merge collisions; merges back to `main` are fast-forward.
- **Per-story commits** for clarity when there are multiple distinct ACs.
- **CHANGELOG-as-truth**: users read it on upgrade. Every functional change gets an entry.

When you find yourself about to drop something as "not worth doing," ask: is the cost-to-fix genuinely larger than the risk? If you can't say yes, default to fixing.
