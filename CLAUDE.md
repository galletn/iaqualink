# Claude orientation

This file is for Claude Code sessions (and any other AI agents) picking up work in this repo. It captures project-specific context that isn't obvious from a single-file read but matters from turn 1.

## What this is

Home Assistant custom integration for **iAqualink robotic pool cleaners** (Zodiac, Polaris, Aqua Products and rebrands). Domain: `iaqualink_robots` (snake_case post-story-C7, matching the directory name and HA's snake_case convention). Pre-C7 the manifest declared the camelCase `iaqualinkRobots` which still exists in some legacy config-entry storage on user installs that haven't been deleted + re-added (the upgrade-time migration story is documented in the C7 CHANGELOG entry). The main code lives in [`custom_components/iaqualink_robots/`](custom_components/iaqualink_robots/).

The user-facing entry point is the config flow in [`config_flow.py`](custom_components/iaqualink_robots/config_flow.py). After story R26 the heavy lifting is split across three modules: [`client.py`](custom_components/iaqualink_robots/client.py) (cloud auth + HTTP + WebSocket + per-device dispatch), [`coordinator.py`](custom_components/iaqualink_robots/coordinator.py) (HA `DataUpdateCoordinator` glue + outage-aware availability + realtime-envelope apply), [`parsing.py`](custom_components/iaqualink_robots/parsing.py) (pure date/time/duration helpers as free functions), and the [`protocols/`](custom_components/iaqualink_robots/protocols/) subpackage (per-device parsing — one module per device family).

## Device types — the most important gotcha

The integration handles **5 distinct device families** with **different cloud encodings**. Mismatching them silently breaks fan-speed / cycle commands. The split lives in `client.py::AqualinkClient._set_other_fan_speed` (the cloud-encoding side) and the per-type branches in [`vacuum.py::IAquaLinkRobotVacuum`](custom_components/iaqualink_robots/vacuum.py) (the HA-display side); per-family parsing now lives in the [`protocols/`](custom_components/iaqualink_robots/protocols/) subpackage:

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

CI (`.github/workflows/ci.yaml`) runs: hassfest (informational), flake8, **`mypy --strict`** (P6 ratcheted `ignore_errors = False` for every module — `# type: ignore` is now banned by `tests/test_static.py::test_no_type_ignore_in_production_code`), pytest with coverage.

## Polling cadence — do not slow it globally

[`const.py`](custom_components/iaqualink_robots/const.py) sets `SCAN_INTERVAL = 3s`. This is deliberately fast so the **vacuum entity's state reflects remote-control button presses (forward/backward/rotate/stop) within a few seconds**. Slowing it globally is a UX regression.

When users complain about activity-log spam from frequently-changing sensors, the right fix is **per-sensor**, not global. See the `include_seconds_remaining` option (commit `30eb650`) as the pattern: an OptionsFlow-toggleable knob that gates whether `time_remaining_human` includes seconds, defaulted to historical behavior, reloads on toggle via `entry.add_update_listener`.

## BMad story system (planning artifacts are gitignored)

Planning lives under `_bmad-output/` (gitignored, local-only). Key files:

- [`_bmad-output/planning-artifacts/stories/EPICS.md`](_bmad-output/planning-artifacts/stories/EPICS.md) — backlog index, **47 stories across 11 epics** (46 done as of 2026-05-22; R30 is the only `backlog` story, gated on Epic-4 soak)
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

## Current state (as of `f9ae8b4`, 2026-05-22)

**46 of 47 stories done.** The full Epic 1-10 sweep has landed: CI + test scaffolding (P1, P2), config-flow hardening (C2/C7 + M12/M17/M18/M19 + H9a/H9b/P4 + C6), service-layer repair (C1a/C1b/P8), refresh-storm reduction + lifecycle (C4-cmd/C4-ws/H6/H7/H10/P10), micro-cleanups (C3/L18/L19/L22/M14/M15/M16), datetime hygiene + display strings (H8/M11/C5), Platinum HA patterns (P3/P5/P6/P7/P9/P11), helper extractions (R23/R24), per-device strategy refactor (R25 × 5 families), module split + typed WS payloads (R26/P12). The `coordinator.py` god-file went from 3566 lines → 704 + `client.py` (2306) + `parsing.py` (152) + `protocols/{base,common,vr,vortrax,cyclobat,cyclonext,i2d}.py`.

**R30** (drop `_status_history` stabilizer) is the only `backlog` story and is **gated on 4 weeks of Epic-4 production soak with zero flapping bug reports** — soak window begins when Epic 4 hits a released version.

**Open GitHub issues** (live state, not aspirational):

- `#97` — Rename integration ID `iaqualink_robots` → `iaqualinkrobots` for HACS compatibility. **Conflicts with C7's deliberate snake_case choice.** Worth reading before acting; HA's snake_case convention and our hassfest-compliant manifest are the defenders.
- `#93` — Help implement upstream HA pool-cleaner entity domain. Collaboration ask, not a bug.
- `#92` — How to select different cleaning modes / add time. User question, not a bug.
- `#88` — EX6000 support. README is updated; underlying device-detection fix is open.
- `#81` — Hide irrelevant entities for certain vacuum models. Pre-P11; may be addressable now that translation_key + has_entity_name are wired up.
- `#76` — "Walls only" mode showing on Zodiac CNX 30 iQ post-2.4.2. Patches in `6bdefc5`/`ab2d37b` claim a fix but the issue is still open — likely user confirmation outstanding.

**Obvious next work** (no story file exists yet for any of these — `bmad-create-story` first):

- **R26-followup** — compact `coordinator.py` toward ≤ 500 lines and `client.py` toward ≤ 800 (currently 704 / 2306) by collapsing per-device-type dispatch ifs and factoring command-method boilerplate. Named in CHANGELOG + deferred-work but never written up.
- **Bug-fix triage of `#76` / `#88` / `#97`** — pick one of the live user-reported issues and turn it into a story.
- **5 deferred items from R26 review** (in `deferred-work.md`): H8 broad-except in `parsing.calculate_times`, cyclonext strict-int `mode == 1` (cyclobat sibling coerces), `_state_signature` hardcoded to `equipment.robot` (cyclonext's `robot.1` shadow missing), 5 coordinator → `client._foo` private reach-throughs, one-sided size-ceiling guard.

See `deferred-work.md` for the full triage table mapping review findings to owning stories.

## Tone for working in this repo

The user (Galletn) prefers:

- **Decisive action**: when scope is clear and risk is low, just do it. Don't ask 4 questions when 1 is enough.
- **Defensible work over dropping it**: see `~/.claude/projects/.../memory/feedback_drop_calls.md`. Drop calls require strong justification, not "low likelihood" hand-waving.
- **Parallel agents in worktrees** for parallelizable work. Worktree isolation prevents merge collisions; merges back to `main` are fast-forward.
- **Per-story commits** for clarity when there are multiple distinct ACs.
- **CHANGELOG-as-truth**: users read it on upgrade. Every functional change gets an entry.

When you find yourself about to drop something as "not worth doing," ask: is the cost-to-fix genuinely larger than the risk? If you can't say yes, default to fixing.
