"""FakePluginContext — the plugin contract surface, for unit-testing plugins
without the host.

This file is the source of truth for the plugin-facing context API (Protocol
v1). It is copied verbatim into anotherme-plugin-template so external plugin
repos can test against the same surface. tests/test_plugin_contract.py asserts
that the real PluginContext implements everything the shim exposes.
"""
import os
from typing import Any, Callable


class _FakeRegistry:
    def __init__(self):
        self.tools: dict[str, dict] = {}
        self.routers: list = []
        self.apis: dict[str, Any] = {}
        self.prompt_fragments: dict[str, list[str]] = {}

    def register_tool(self, name, schema, handler):
        self.tools[name] = {"schema": schema, "handler": handler}

    def register_router(self, plugin_name, router):
        self.routers.append((plugin_name, router))

    def register_api(self, name, api_object):
        self.apis[name] = api_object

    def add_prompt_fragment(self, context_type, text):
        self.prompt_fragments.setdefault(context_type, []).append(text)


class _FakeEventBus:
    def __init__(self):
        self.handlers: dict[str, list[Callable]] = {}
        self.history: list[dict] = []

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    async def emit(self, event, payload):
        self.history.append({"event": event, "payload": payload})
        for handler in self.handlers.get(event, []):
            result = handler(payload)
            if hasattr(result, "__await__"):
                await result


class _FakeStore:
    def __init__(self):
        self._data: dict[str, Any] = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class FakePluginContext:
    """Mirrors src.plugins.context.PluginContext — constructor differs on
    purpose (fakes need no wiring); the plugin-facing surface must match."""

    def __init__(self, config: dict | None = None, env: dict[str, str] | None = None,
                 vault_manager=None, db_module=None, llm_client=None,
                 context_builder=None, tool_executor=None):
        self.plugin_name = "fake"
        self.registry = _FakeRegistry()
        self.event_bus = _FakeEventBus()
        self.store = _FakeStore()
        self.vault_manager = vault_manager
        self.db_module = db_module
        self.llm_client = llm_client
        self.context_builder = context_builder
        self.tool_executor = tool_executor
        self.package_manager = None
        self.config = dict(config or {})
        self._env = dict(env or {})

    @property
    def vault_name(self) -> str:
        return "main"

    def register_tool(self, name: str, schema: dict, handler: Callable):
        self.registry.register_tool(f"{self.plugin_name}.{name}", schema, handler)

    def register_router(self, router):
        self.registry.register_router(self.plugin_name, router)

    def on_event(self, event: str, handler: Callable):
        self.event_bus.on(event, handler)

    def add_prompt_fragment(self, context_type: str, text: str):
        self.registry.add_prompt_fragment(context_type, text)

    def register_migration(self, version: int, sql: str):
        if self.db_module is None:
            raise RuntimeError("FakePluginContext has no db_module wired")

    def register_api(self, name: str, api_object: Any):
        self.registry.register_api(name, api_object)

    def get_plugin_api(self, name: str) -> Any | None:
        return self.registry.apis.get(name)

    def register_context_provider(self, kind: str, handler: Callable):
        if self.context_builder and hasattr(self.context_builder, "register_provider"):
            self.context_builder.register_provider(kind, handler)

    def get_config(self) -> dict:
        merged = dict(self.config)
        stored = self.store.get("settings")
        if stored and isinstance(stored, dict):
            merged.update(stored)
        return merged

    def get_env(self, key: str, default: str | None = None) -> str | None:
        """Host secrets (anything ending in _SECRET/_TOKEN/_PASSWORD) are
        never readable by plugins; the default is returned instead. Provider
        API keys (*_API_KEY) are designed for plugin consumption and stay
        readable."""
        if key.upper().endswith(("_SECRET", "_TOKEN", "_PASSWORD")):
            return default
        return self._env.get(key, os.environ.get(key, default))
