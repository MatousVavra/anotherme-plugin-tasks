"""Standalone load test — the plugin must load against FakePluginContext
with zero host dependencies (the Protocol v1 contract)."""
from unittest.mock import MagicMock

from conftest import load_plugin_module
from fake_plugin_context import FakePluginContext


def test_plugin_loads_and_registers():
    ctx = FakePluginContext(db_module=MagicMock())
    mod = load_plugin_module()
    mod.Plugin().on_load(ctx)
    assert ctx.registry.routers, "plugin registered no router"
    assert ctx.registry.apis.get("tasks") is not None
