## What

<!-- One paragraph: what changes and why. -->

## Platform

- [ ] core
- [ ] bilibili
- [ ] x
- [ ] server / docs

## Checklist

- [ ] `ruff check src tests && ruff format --check src tests`
- [ ] `pytest -q`
- [ ] `python scripts/dump_openapi.py` if the HTTP API changed
- [ ] CHANGELOG.md updated
- [ ] No credentials, cookies, or exported personal data in the diff
