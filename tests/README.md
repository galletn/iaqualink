# Tests

Seed test suite for the iAqualink Robots integration. Created as part of stories
[P1](../_bmad-output/planning-artifacts/stories/story-P1-pytest-skeleton.md) and
[P2](../_bmad-output/planning-artifacts/stories/story-P2-ci-scaffolding.md).

## Running

### CI (always works)

`.github/workflows/ci.yaml` runs the full suite on `ubuntu-latest`. PRs against
`main` must pass `hassfest`, `flake8`, `mypy`, and `pytest` before merging.

### Local — Linux / macOS

```bash
python -m pip install -e ".[test]"
pytest
```

### Local — Windows

**Home Assistant Core does not run on native Windows** (it imports `fcntl`,
which is a Unix-only stdlib module). Two options:

1. **WSL2** (recommended): install Ubuntu via WSL, then run the Linux commands
   above inside WSL. VS Code's "Remote - WSL" extension makes this seamless.
2. **CI only**: push your branch and let GitHub Actions run the suite. You can
   still run `flake8`, `mypy`, and `python -m py_compile tests/*.py` locally on
   Windows since those don't import HA.

## Suite layout

| File | Covers |
|------|--------|
| `conftest.py` | Shared HA fixtures, mocked `AqualinkClient.discover_devices` |
| `const.py` | Mock device/user constants |
| `test_config_flow.py` | User flow: happy path, no_devices, cannot_connect. XFAIL placeholders for [C2](../_bmad-output/planning-artifacts/stories/story-C2-config-entry-unique-id.md) duplicate-abort and [P4](../_bmad-output/planning-artifacts/stories/story-P4-async-step-reauth.md) reauth |
| `test_init.py` | `async_setup` no-op. XFAIL placeholders for full `async_setup_entry` / `async_unload_entry` (depend on [P3](../_bmad-output/planning-artifacts/stories/story-P3-runtime-data-migration.md) `runtime_data`) |
| `test_manifest.py` | Manifest sanity: required keys, semver version. XFAIL for [M16](../_bmad-output/planning-artifacts/stories/story-M16-manifest-aiohttp-pin.md) aiohttp pin |

## Conventions

- **No real cloud calls.** `conftest.py` patches `AqualinkClient.discover_devices`
  at the import path used by the config flow. Coordinator-level tests (forthcoming)
  will patch `AqualinkClient.__init__` + cloud methods.
- **`asyncio_mode = "auto"`** is set in `pyproject.toml` — every `async def test_*`
  is automatically wrapped, no `@pytest.mark.asyncio` decorator needed.
- **`enable_custom_integrations`** is autouse-d via `auto_enable_custom_integrations`
  in `conftest.py` — every test has the integration discoverable from
  `custom_components/`.
- **XFAIL placeholders** mark behavior expected from forthcoming stories. When
  the story PR opens, the dev removes the `@pytest.mark.xfail` and implements
  the test. `strict=True` means a passing XFAIL becomes an XPASS failure —
  forcing the dev to also update the marker.

## Coverage ratchet

`pyproject.toml` `[tool.coverage.report].fail_under` starts at **5%** as a
placeholder. Each story that lands tests must raise this number incrementally:

| Story bundle | Target fail_under |
|--------------|-------------------|
| P1 (this) | 5% |
| Epic 3 (config-flow + auth) | 30% |
| Epic 4 (refresh storm + lifecycle) | 50% |
| Epic 7 (Platinum patterns) | 80% |
| Epic 9 (per-device strategy) | 90% |
| Pre-Platinum claim | 95% |

The CI workflow surfaces the `coverage.xml` artifact on every run so the trend
is visible.
