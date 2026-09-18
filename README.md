# anotherme-plugin-tasks

Tasks — inbox, due dates, AI extraction from diary for
[AnotherMe](https://github.com/MatousVavra/AnotherMe).

## Development

Unit tests run standalone against `FakePluginContext`:

    pip install fastapi pydantic httpx pyyaml pytest pytest-asyncio openai
    pytest tests/ --ignore=tests/integration

Integration tests run inside the released app image (see
`.github/workflows/test.yml`). To develop against a live app, point
`COMMUNITY_PLUGINS_DIR` at this checkout's parent directory.

## Releases

Tag `vX.Y.Z` (must match `plugin/plugin.yaml` `version`), then bump the tag
in the [community index](https://github.com/MatousVavra/anotherme-plugins).
