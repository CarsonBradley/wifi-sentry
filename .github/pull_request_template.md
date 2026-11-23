## What this changes

<!-- One or two sentences, and why. Link an issue if there is one. -->

## Evidence

<!-- How do you know it works? Be specific about what ran where:
     - tests added / updated
     - measured on real hardware (which Mac, macOS version)?
     - simulated only (fake sampler, injected motion, fake_esp32)?
     - for detector changes: replay results on engine/tests/fixtures/ before and after -->

## Checklist

- [ ] `WIFI_SENTRY_NO_HARDWARE=1 scripts/test.sh` passes
- [ ] No machine-specific values (addresses, `/Users/...` paths, tokens, `config.local.toml`, `data/`)
- [ ] Docs updated if behaviour, commands or config keys changed (`docs/configuration.md`, `docs/api.md`)
- [ ] A line under `## Unreleased` at the top of `CHANGELOG.md`
